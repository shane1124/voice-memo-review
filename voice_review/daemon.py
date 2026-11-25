"""统一单守护核心服务（vmr serve）：单实例文件锁、四插槽执行器、热启停监听与 127.0.0.1 REST/SSE API。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import fcntl
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import queue
import re
import secrets as py_secrets
import select
import shutil
import signal
import tempfile
import threading
import time
from typing import Any, Callable
import urllib.parse
import urllib.request
import uuid

from voice_review import __version__
from voice_review.analyzers import (
    AnalysisOutcome,
    AnalysisRequest,
    get_analyzer,
    test_analyzer_connection,
)
from voice_review.autostart import AutostartManager, consume_restart_token, spawn_restart_helper
from voice_review.config import (
    VoiceReviewConfig,
    config_to_dict,
    load_config,
    save_config_dict,
    validate_provider_combo,
)
from voice_review.diarizer import FluidAudioDiarizer, NoSpeechDetectedError, VoiceprintStore
from voice_review.rules import RuleStudio
from voice_review.secrets import SecretStore
from voice_review.sources import AudioCandidate, MultiSourceScanner
from voice_review.state import RecordingState, StateStore
from voice_review.transcribers import (
    get_transcriber,
    prepare_audio_job,
    test_transcriber_connection,
)
from voice_review.voice_memos import SHANGHAI_TZ, probe_duration_sec, sanitize_filename_stem

logger = logging.getLogger(__name__)
WEB_UI_DIR = Path(__file__).resolve().parent / "web_ui"
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


class DaemonAlreadyRunningError(RuntimeError):
    """同 VMR_HOME 下已有单实例守护进程在运行。"""


@dataclass(frozen=True)
class DaemonInfo:
    pid: int
    host: str
    port: int
    token: str

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ui_url(self) -> str:
        return f"{self.base_url}/?token={self.token}"


def discover_running_daemon(vmr_home: Path) -> DaemonInfo | None:
    """读取 daemon.json 并向 /api/v1/health 探活，确认守护是否在线。"""
    info_path = Path(vmr_home).expanduser().resolve() / "daemon.json"
    if not info_path.is_file():
        return None
    try:
        raw = json.loads(info_path.read_text(encoding="utf-8"))
        info = DaemonInfo(
            pid=int(raw["pid"]),
            host=str(raw.get("host") or "127.0.0.1"),
            port=int(raw["port"]),
            token=str(raw["token"]),
        )
    except Exception:
        return None
    try:
        with urllib.request.urlopen(f"{info.base_url}/api/v1/health", timeout=2.0) as resp:
            if resp.status == 200:
                return info
    except Exception:
        return None
    return None


class EventBroadcaster:
    """线程安全的 SSE 事件广播器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: set[queue.Queue[str]] = set()

    def subscribe(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=128)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[str]) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self._lock:
            dead: list[queue.Queue[str]] = []
            for q in self._subs:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subs.discard(q)

    def close_all(self) -> None:
        with self._lock:
            for q in list(self._subs):
                try:
                    q.put_nowait("")
                except queue.Full:
                    pass
            self._subs.clear()


class PipelineExecutor:
    """四插槽统一执行器：预处理 → 转写 → 按策略分析，供守护、CLI 与 MCP 共享。"""

    def __init__(
        self,
        cfg: VoiceReviewConfig,
        *,
        store: StateStore | None = None,
        secrets: SecretStore | None = None,
        rules: RuleStudio | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store or StateStore(cfg.state_db)
        self.secrets = secrets or SecretStore(cfg.secrets_path)
        self.rules = rules or RuleStudio(cfg.rules_dir)
        self.rules.ensure_initialized()
        self._on_event = on_event

    def reload_config(self, cfg: VoiceReviewConfig) -> None:
        self.cfg = cfg
        self.secrets = SecretStore(cfg.secrets_path)
        self.rules = RuleStudio(cfg.rules_dir)
        self.rules.ensure_initialized()

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self._on_event:
            try:
                self._on_event(event, data)
            except Exception:
                pass

    def _resolve_audio_path(self, rec: RecordingState) -> Path:
        if rec.audio_path:
            p = Path(rec.audio_path).expanduser()
            if p.is_file():
                return p
        return self.cfg.recordings_dir / rec.filename

    def _build_paths(self, rec: RecordingState) -> tuple[Path, Path, Path]:
        recorded_dt = datetime.fromisoformat(rec.recorded_at)
        day = recorded_dt.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d")
        stem = sanitize_filename_stem(recorded_dt, rec.title, rec.recording_id)
        transcript_path = (
            Path(rec.transcript_path) if rec.transcript_path else (self.cfg.transcripts_dir / day / f"{stem}｜转录稿.md")
        )
        analysis_path = (
            Path(rec.analysis_path) if rec.analysis_path else (self.cfg.output_dir / day / f"{stem}｜分析稿.md")
        )
        job_dir = self.cfg.work_dir / "v2_jobs" / rec.recording_id
        return transcript_path, analysis_path, job_dir

    def process_one(self, rec: RecordingState, *, auto_analyze: bool = True) -> str:
        audio_path = self._resolve_audio_path(rec)
        transcript_path, analysis_path, job_dir = self._build_paths(rec)
        template_id = rec.template_id or self.cfg.default_template_id

        ok_combo, combo_msg = validate_provider_combo(self.cfg.transcriber_provider, self.cfg.analyzer_auto_provider)
        if not ok_combo and auto_analyze:
            self.store.update(rec.recording_id, status="failed", last_error=combo_msg)
            self._emit("job", {"recording_id": rec.recording_id, "status": "failed", "error": combo_msg})
            return "failed"

        if rec.status in ("discovered", "preparing"):
            self.store.update(rec.recording_id, status="preparing", last_error="")
            self._emit("job", {"recording_id": rec.recording_id, "status": "preparing"})
            glossary = self.rules.get_glossary() or self.cfg.asr_context
            try:
                recorded_dt = datetime.fromisoformat(rec.recorded_at)
                prep = prepare_audio_job(
                    audio_path,
                    cfg=self.cfg,
                    job_dir=job_dir,
                    transcript_path=transcript_path,
                    title=rec.title,
                    recorded_at=recorded_dt,
                    asr_context=glossary,
                )
            except NoSpeechDetectedError as exc:
                self.store.update(rec.recording_id, status="skipped", last_error=str(exc), reset_retry=True)
                shutil.rmtree(job_dir, ignore_errors=True)
                self._emit("job", {"recording_id": rec.recording_id, "status": "skipped", "error": str(exc)})
                return "skipped"
            except Exception as exc:
                st = self.store.record_failure(rec.recording_id, str(exc), retry_stage="discovered")
                self._emit("job", {"recording_id": rec.recording_id, "status": st, "error": str(exc)})
                return st

            self.store.update(
                rec.recording_id,
                status="transcribing",
                transcript_path=str(prep.transcript_path),
                analysis_path=str(analysis_path),
                speakers_summary=prep.speakers_summary,
                batch_count=len(prep.batches),
                segment_count=len(prep.segments),
                last_error="",
            )
            self.store.update_progress(rec.recording_id, 0, max(1, len(prep.batches)))
            self._emit("job", {"recording_id": rec.recording_id, "status": "transcribing", "done": 0, "total": len(prep.batches)})

            def _on_prog(done: int, total: int) -> None:
                self.store.update_progress(rec.recording_id, done, total)
                self._emit("progress", {"recording_id": rec.recording_id, "done": done, "total": total})

            try:
                transcriber = get_transcriber(self.cfg, self.secrets)
                outcome = transcriber.transcribe(prep, on_progress=_on_prog, cleanup_clips=True)
            except Exception as exc:
                st = self.store.record_failure(rec.recording_id, str(exc), retry_stage="discovered")
                self._emit("job", {"recording_id": rec.recording_id, "status": st, "error": str(exc)})
                return st

            self.store.update(
                rec.recording_id,
                status="prepared" if outcome.mode == "delegated_workflow" else "transcribed",
                transcript_path=str(outcome.transcript_path),
                analysis_path=str(analysis_path),
                workflow_script_path=str(outcome.workflow_script_path) if outcome.workflow_script_path else "",
                speakers_summary=outcome.speakers_summary,
                batch_count=outcome.batch_count,
                segment_count=outcome.segment_count,
                last_error="",
            )
            rec = self.store.get(rec.recording_id) or rec

        if rec.status in ("prepared", "transcribed", "sent"):
            if not auto_analyze or self.cfg.analyzer_auto_provider in ("none", "mcp-host"):
                if rec.status == "prepared":
                    # antigravity-flash 仅完成物理切段尚未转写，保留 prepared 状态而非误标 awaiting_analysis
                    self._emit("job", {"recording_id": rec.recording_id, "status": "prepared"})
                    return "prepared"
                self.store.update(rec.recording_id, status="awaiting_analysis", last_error="", reset_retry=True)
                self._emit("job", {"recording_id": rec.recording_id, "status": "awaiting_analysis"})
                return "awaiting_analysis"

            res = self.run_analysis_only(rec.recording_id, template_id=template_id)
            if res.ok and res.status == "done":
                shutil.rmtree(job_dir, ignore_errors=True)
                if rec.source_kind == "drop_folder" and self.cfg.drop_folder_archive_done and audio_path.is_file():
                    arch_dir = audio_path.parent / "_processed"
                    arch_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.move(str(audio_path), str(arch_dir / audio_path.name))
                    except OSError:
                        pass
            return res.status

        return rec.status

    def run_analysis_only(
        self,
        recording_id: str,
        *,
        template_id: str | None = None,
        override_provider: str | None = None,
    ) -> AnalysisOutcome:
        rec = self.store.get(recording_id)
        if rec is None:
            return AnalysisOutcome(False, "failed", None, error=f"未找到记录：{recording_id}")
        transcript_path, analysis_path, _ = self._build_paths(rec)
        wf_path = Path(rec.workflow_script_path) if rec.workflow_script_path else None
        if wf_path is None and not transcript_path.is_file():
            return AnalysisOutcome(False, "failed", None, error=f"转录稿不存在：{transcript_path}")

        self.store.update(recording_id, status="analyzing", template_id=template_id or rec.template_id or self.cfg.default_template_id, last_error="")
        self._emit("job", {"recording_id": recording_id, "status": "analyzing"})

        analyzer = get_analyzer(self.cfg, self.secrets, self.store, self.rules, override_provider=override_provider)
        outcome = analyzer.analyze(
            AnalysisRequest(
                recording_id=rec.recording_id,
                title=rec.title,
                recorded_at=rec.recorded_at,
                duration_sec=rec.duration_sec,
                speakers_summary=rec.speakers_summary,
                transcript_path=transcript_path,
                analysis_path=analysis_path,
                template_id=template_id or rec.template_id or self.cfg.default_template_id,
                workflow_script_path=wf_path,
                batch_count=rec.batch_count,
                segment_count=rec.segment_count,
            )
        )
        if outcome.ok:
            self.store.update(
                recording_id,
                status=outcome.status,
                analysis_path=str(outcome.analysis_path) if outcome.analysis_path else rec.analysis_path,
                last_error="",
                reset_retry=True,
            )
            self._emit("job", {"recording_id": recording_id, "status": outcome.status})
        else:
            new_st = self.store.record_failure(recording_id, outcome.error, retry_stage="transcribed" if wf_path is None else "prepared")
            self._emit("job", {"recording_id": recording_id, "status": new_st, "error": outcome.error})
        return outcome

    def submit_single_file(
        self,
        audio_path: Path,
        *,
        title: str = "",
        template_id: str = "",
        source_kind: str = "single_file",
        auto_analyze: bool = True,
    ) -> RecordingState:
        src = Path(audio_path).expanduser().resolve()
        if not src.is_file():
            raise FileNotFoundError(f"音频文件不存在：{src}")
        now_dt = datetime.now(SHANGHAI_TZ).replace(microsecond=0)
        dur = probe_duration_sec(src, self.cfg.ffprobe_bin) or 0.0
        rec_id = f"job_{uuid.uuid4().hex[:12]}"
        self.store.upsert_discovered(
            recording_id=rec_id,
            filename=src.name,
            title=(title or src.stem).strip() or "上传录音",
            recorded_at=now_dt.isoformat(),
            recorded_date=now_dt.strftime("%Y-%m-%d"),
            duration_sec=dur,
            source_kind=source_kind,
            audio_path=str(src),
            template_id=template_id or self.cfg.default_template_id,
            auto_analyze=auto_analyze,
        )
        rec = self.store.get(rec_id)
        assert rec is not None
        self._emit("job", {"recording_id": rec_id, "status": "discovered", "title": rec.title})
        return rec


class _DaemonLock:
    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._fp = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fp = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fp.close()
            raise DaemonAlreadyRunningError(f"已有 voice-memo-review 实例正在运行（锁文件：{self.lock_path}）") from exc
        fp.seek(0)
        fp.truncate()
        fp.write(f"{os.getpid()}\n")
        fp.flush()
        self._fp = fp

    def release(self) -> None:
        if self._fp is not None:
            try:
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
                self._fp.close()
            except OSError:
                pass
            self._fp = None


class VmrDaemon:
    """统一单守护：单实例锁 + 后台热启停监听 + 本地 127.0.0.1 REST/SSE API。"""

    def __init__(
        self,
        cfg: VoiceReviewConfig,
        *,
        host: str = "127.0.0.1",
        port: int | None = None,
        start_watcher: bool = True,
    ) -> None:
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("安全策略禁止绑定非回环地址，仅允许 127.0.0.1")
        self.cfg = cfg
        self.host = "127.0.0.1"
        self._requested_port = cfg.ui_port if port is None else int(port)
        self.port = 0
        self.token = consume_restart_token(cfg.vmr_home) or py_secrets.token_urlsafe(24)
        self._start_watcher = start_watcher
        self._autostart = AutostartManager.default(cfg.vmr_home)

        self.broadcaster = EventBroadcaster()
        self.store = StateStore(cfg.state_db)
        self.secrets = SecretStore(cfg.secrets_path)
        self.rules = RuleStudio(cfg.rules_dir)
        self.rules.ensure_initialized()
        self.executor = PipelineExecutor(
            cfg,
            store=self.store,
            secrets=self.secrets,
            rules=self.rules,
            on_event=self.broadcaster.publish,
        )

        self._lock = _DaemonLock(cfg.vmr_home / "daemon.lock")
        self._info_path = cfg.vmr_home / "daemon.json"
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._wake_w_fd: int = -1
        self._cfg_lock = threading.RLock()
        self._pending: dict[str, AudioCandidate] = {}
        self._scanner = self._build_scanner(cfg)

        self._httpd: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._update_thread: threading.Thread | None = None

    def active_jobs_count(self) -> int:
        recs = self.store.list_recent(limit=50)
        return sum(1 for r in recs if r.status in ("preparing", "transcribing", "analyzing"))

    def schedule_graceful_restart(self, *, use_launchd: bool, disable_launchd_before_exit: bool = False) -> None:
        """延迟 0.35s 待 HTTP 响应发回浏览器后，启动脱离接力进程并优雅关闭当前守护。"""
        def _do() -> None:
            time.sleep(0.35)
            try:
                if disable_launchd_before_exit:
                    # 先准备接力进程，再移除 launchd 注册并退出，确保控制台不断联
                    spawn_restart_helper(
                        vmr_bin=self._autostart.vmr_bin,
                        vmr_home=self.cfg.vmr_home,
                        old_pid=os.getpid(),
                        token=self.token,
                        use_launchd=False,
                    )
                    self._autostart.disable(unload_now=True)
                else:
                    spawn_restart_helper(
                        vmr_bin=self._autostart.vmr_bin,
                        vmr_home=self.cfg.vmr_home,
                        old_pid=os.getpid(),
                        token=self.token,
                        use_launchd=use_launchd,
                    )
            except Exception as exc:
                logger.warning("触发热重启接力异常：%s", exc)
            finally:
                self._stop_event.set()
                self.wake_worker()

        threading.Thread(target=_do, daemon=True, name="vmr-restart-trigger").start()

    def _update_check_loop(self) -> None:
        """方案 B：启动 15 秒后静默检查一次（可被 stop 中断），随后每 24 小时检查一次，零干扰不撞限流。"""
        if self._stop_event.wait(timeout=15.0):
            return
        while not self._stop_event.is_set():
            try:
                from voice_review.updater import SelfUpdater
                upd = SelfUpdater.default(self.cfg)
                info = upd.check_latest(use_cache=True)
                if info.ok and info.has_update:
                    self.broadcaster.publish("update", info.to_dict())
            except Exception:
                pass
            if self._stop_event.wait(timeout=86400.0):
                break

    def _build_scanner(self, cfg: VoiceReviewConfig) -> MultiSourceScanner:
        since_dt = datetime.fromisoformat(cfg.import_since)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=SHANGHAI_TZ)
        return MultiSourceScanner(
            voice_memos_enabled=cfg.voice_memos_enabled,
            recordings_dir=cfg.recordings_dir,
            drop_folder_enabled=cfg.drop_folder_enabled,
            drop_folder_dir=cfg.drop_folder_dir,
            import_since=since_dt,
            min_duration_sec=cfg.min_duration_sec,
            debounce_sec=cfg.debounce_sec,
            ffprobe_bin=cfg.ffprobe_bin,
        )

    def _write_daemon_info(self) -> None:
        self._info_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"pid": os.getpid(), "host": self.host, "port": self.port, "token": self.token},
            ensure_ascii=False,
        )
        fd, tmp_name = tempfile.mkstemp(prefix=".daemon-", suffix=".tmp", dir=str(self._info_path.parent))
        tmp_path = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._info_path)
            os.chmod(self._info_path, 0o600)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def reload_from_disk(self) -> VoiceReviewConfig:
        with self._cfg_lock:
            new_cfg = load_config(self.cfg.config_path, vmr_home=self.cfg.vmr_home)
            self.cfg = new_cfg
            self.secrets = SecretStore(new_cfg.secrets_path)
            self.rules = RuleStudio(new_cfg.rules_dir)
            self.executor.reload_config(new_cfg)
            self._scanner = self._build_scanner(new_cfg)
            self._wake_event.set()
            return new_cfg

    def wake_worker(self) -> None:
        self._wake_event.set()
        fd = self._wake_w_fd
        if fd >= 0:
            try:
                os.write(fd, b"w")
            except OSError:
                pass

    def _scan_sources(self) -> int:
        if not self._start_watcher:
            return 0
        with self._cfg_lock:
            scanner = self._scanner
            if not (self.cfg.voice_memos_enabled or self.cfg.drop_folder_enabled):
                return 0
        now = time.monotonic()
        for cand in scanner.scan_candidates():
            if self.store.get(cand.recording_id) is None:
                self._pending[cand.recording_id] = cand
        enqueued = 0
        for rec_id in list(self._pending):
            cand = self._pending[rec_id]
            if self.store.get(rec_id) is not None:
                self._pending.pop(rec_id, None)
                continue
            if scanner.is_file_stable(cand, now_mono=now):
                inserted = self.store.upsert_discovered(
                    recording_id=cand.recording_id,
                    filename=cand.filename,
                    title=cand.title,
                    recorded_at=cand.recorded_at.isoformat(),
                    recorded_date=cand.recorded_date,
                    duration_sec=cand.duration_sec,
                    source_kind=cand.source_kind,
                    audio_path=str(cand.audio_path),
                    template_id=self.cfg.default_template_id,
                )
                if inserted:
                    enqueued += 1
                    self.broadcaster.publish("job", {"recording_id": cand.recording_id, "status": "discovered", "title": cand.title})
                self._pending.pop(rec_id, None)
        return enqueued

    def _worker_loop(self) -> None:
        self.store.reset_in_flight()
        kq = select.kqueue() if hasattr(select, "kqueue") else None
        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_r, False)
        os.set_blocking(wake_w, False)
        self._wake_w_fd = wake_w
        watch_fds: dict[Path, int] = {}

        def _sync_kqueue() -> None:
            if kq is None or not self._start_watcher:
                return
            with self._cfg_lock:
                desired = set(self._scanner.watch_paths())
            for old_p in list(watch_fds):
                if old_p not in desired:
                    try:
                        os.close(watch_fds.pop(old_p))
                    except OSError:
                        pass
            for p in desired:
                if p in watch_fds or not p.exists():
                    continue
                try:
                    fd = os.open(str(p), getattr(os, "O_EVTONLY", os.O_RDONLY))
                    ev = select.kevent(
                        fd,
                        filter=select.KQ_FILTER_VNODE,
                        flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                        fflags=(
                            select.KQ_NOTE_WRITE
                            | select.KQ_NOTE_EXTEND
                            | select.KQ_NOTE_RENAME
                            | select.KQ_NOTE_DELETE
                        ),
                    )
                    kq.control([ev], 0, 0)
                    watch_fds[p] = fd
                except OSError:
                    pass

        try:
            if kq is not None:
                kq.control(
                    [select.kevent(wake_r, filter=select.KQ_FILTER_READ, flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR)],
                    0,
                    0,
                )
            while not self._stop_event.is_set():
                # 源扫描与队列消费彻底异常隔离：即使无全盘权限也绝不阻塞投放箱或手动上传任务
                try:
                    _sync_kqueue()
                    self._scan_sources()
                except Exception as exc:
                    logger.warning("后台源扫描异常（已隔离，不影响队列消费）：%s", exc)

                try:
                    while not self._stop_event.is_set():
                        nxt = self.store.next_pending()
                        if nxt is None:
                            break
                        self.executor.process_one(nxt, auto_analyze=nxt.auto_analyze)
                except Exception as exc:
                    logger.exception("后台队列任务处理异常：%s", exc)

                wait_sec = 2.0 if self._pending else (30.0 if kq is not None else 5.0)
                if kq is not None:
                    try:
                        events = kq.control(None, 8, wait_sec)
                        for ev in events:
                            if ev.ident == wake_r:
                                try:
                                    os.read(wake_r, 256)
                                except OSError:
                                    pass
                            elif ev.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME):
                                for p_key, fd in list(watch_fds.items()):
                                    if fd == ev.ident:
                                        try:
                                            os.close(fd)
                                        except OSError:
                                            pass
                                        watch_fds.pop(p_key, None)
                    except OSError:
                        self._wake_event.wait(timeout=min(wait_sec, 5.0))
                else:
                    self._wake_event.wait(timeout=wait_sec)
                self._wake_event.clear()
        finally:
            self._wake_w_fd = -1
            for fd in watch_fds.values():
                try:
                    os.close(fd)
                except OSError:
                    pass
            if kq is not None:
                try:
                    kq.close()
                except OSError:
                    pass
            for fd in (wake_r, wake_w):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _make_handler(self):
        daemon = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args) -> None:  # noqa: A002
                logger.debug("HTTP %s", format % args)

            def _send_json(self, code: int, obj: dict[str, Any]) -> None:
                raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def _send_bytes(self, code: int, data: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _check_security(self, *, query_token: str = "") -> bool:
                raw_host = (self.headers.get("Host") or "").strip()
                host_name = (urllib.parse.urlsplit(f"//{raw_host}").hostname or "").lower() if raw_host else ""
                if not host_name or host_name not in _ALLOWED_HOSTS:
                    self._send_json(403, {"error": "缺失或非法 Host 请求头"})
                    return False
                origin = (self.headers.get("Origin") or "").strip()
                if origin:
                    parsed = urllib.parse.urlparse(origin)
                    if (parsed.hostname or "").lower() not in _ALLOWED_HOSTS:
                        self._send_json(403, {"error": "跨站 Origin 已被拒绝"})
                        return False
                supplied = (self.headers.get("X-VMR-Token") or query_token or "").strip()
                if not supplied or not hmac.compare_digest(supplied, daemon.token):
                    self._send_json(403, {"error": "缺少或无效的会话令牌（X-VMR-Token）"})
                    return False
                return True

            def _read_json(self) -> dict[str, Any] | None:
                length = int(self.headers.get("Content-Length") or "0")
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._send_json(400, {"ok": False, "error": f"无效 JSON 请求体：{exc}"})
                    return None
                if not isinstance(parsed, dict):
                    self._send_json(400, {"ok": False, "error": "JSON 请求体必须为对象"})
                    return None
                return parsed

            def _serve_static(self, rel_path: str) -> None:
                ui_root = WEB_UI_DIR.resolve()
                target = (ui_root / rel_path).resolve()
                if not target.is_relative_to(ui_root) or not target.is_file():
                    self._send_json(404, {"error": "资源不存在"})
                    return
                ext_map = {
                    ".html": "text/html; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".svg": "image/svg+xml",
                }
                self._send_bytes(200, target.read_bytes(), ext_map.get(target.suffix.lower(), "application/octet-stream"))

            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                qs = urllib.parse.parse_qs(parsed.query)
                q_tok = (qs.get("token") or [""])[0]

                if path == "/api/v1/health":
                    self._send_json(200, {"ok": True, "version": __version__, "pid": os.getpid()})
                    return
                if path in ("/", "/index.html"):
                    self._serve_static("index.html")
                    return
                if path.startswith("/assets/"):
                    self._serve_static(path[len("/assets/") :])
                    return

                if not self._check_security(query_token=q_tok):
                    return

                if path == "/api/v1/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    q = daemon.broadcaster.subscribe()
                    try:
                        self.wfile.write(b"event: hello\ndata: {\"ok\": true}\n\n")
                        self.wfile.flush()
                        while not daemon._stop_event.is_set():
                            try:
                                chunk = q.get(timeout=15.0)
                                if not chunk:
                                    break
                                self.wfile.write(chunk.encode("utf-8"))
                                self.wfile.flush()
                            except queue.Empty:
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                    except OSError:
                        pass
                    finally:
                        daemon.broadcaster.unsubscribe(q)
                    return

                if path == "/api/v1/status":
                    cfg = daemon.cfg
                    autostart_st = daemon._autostart.status()
                    from voice_review.updater import SelfUpdater
                    cached_upd = SelfUpdater.default(cfg).read_cached_update()
                    self._send_json(
                        200,
                        {
                            "version": __version__,
                            "pid": os.getpid(),
                            "port": daemon.port,
                            "watching": daemon._start_watcher and (cfg.voice_memos_enabled or cfg.drop_folder_enabled),
                            "voice_memos_enabled": cfg.voice_memos_enabled,
                            "drop_folder_enabled": cfg.drop_folder_enabled,
                            "drop_folder_dir": str(cfg.drop_folder_dir),
                            "autostart_supported": autostart_st.supported,
                            "autostart_enabled": autostart_st.enabled,
                            "autostart_loaded": autostart_st.loaded,
                            "active_jobs": daemon.active_jobs_count(),
                            "preprocessor_mode": cfg.preprocessor_mode,
                            "transcriber_provider": cfg.transcriber_provider,
                            "analyzer_auto_provider": cfg.analyzer_auto_provider,
                            "fluidaudio_ready": cfg.fluidaudio_bin is not None,
                            "ffmpeg_ready": bool(shutil.which(cfg.ffmpeg_bin) or Path(cfg.ffmpeg_bin).is_file()),
                            "agentapi_ready": cfg.agentapi_bin.is_file(),
                            "codex_ready": bool(shutil.which(cfg.codex_bin) or Path(cfg.codex_bin).is_file()),
                            "claude_ready": bool(shutil.which(cfg.claude_bin) or Path(cfg.claude_bin).is_file()),
                            "update_available": bool(cached_upd and cached_upd.has_update),
                            "latest_version": cached_upd.latest_version if cached_upd else __version__,
                            "release_notes": cached_upd.release_notes if cached_upd else "",
                            "release_url": cached_upd.release_url if cached_upd else "",
                        },
                    )
                    return

                if path == "/api/v1/autostart":
                    self._send_json(200, daemon._autostart.status().to_dict())
                    return

                if path == "/api/v1/config":
                    self._send_json(
                        200,
                        {
                            "config": config_to_dict(daemon.cfg),
                            "secrets": daemon.secrets.masked_all(),
                        },
                    )
                    return

                if path == "/api/v1/jobs":
                    status_filter = (qs.get("status") or [None])[0]
                    limit = int((qs.get("limit") or ["50"])[0])
                    jobs = [r.to_dict() for r in daemon.store.list_recent(limit=limit, status=status_filter)]
                    self._send_json(200, {"jobs": jobs})
                    return

                m_content = re.fullmatch(r"/api/v1/jobs/([^/]+)/content", path)
                if m_content:
                    rec = daemon.store.get(urllib.parse.unquote(m_content.group(1)))
                    if rec is None:
                        self._send_json(404, {"error": "记录不存在"})
                        return
                    t_text = Path(rec.transcript_path).read_text(encoding="utf-8") if rec.transcript_path and Path(rec.transcript_path).is_file() else ""
                    a_text = Path(rec.analysis_path).read_text(encoding="utf-8") if rec.analysis_path and Path(rec.analysis_path).is_file() else ""
                    self._send_json(200, {"job": rec.to_dict(), "transcript": t_text, "analysis": a_text})
                    return

                if path == "/api/v1/templates":
                    self._send_json(200, {"templates": [t.to_dict() for t in daemon.rules.list_templates()]})
                    return

                if path == "/api/v1/rules/meta":
                    self._send_json(
                        200,
                        {
                            "glossary": daemon.rules.get_glossary(raw=True),
                            "persona": daemon.rules.get_persona(raw=True),
                        },
                    )
                    return

                if path == "/api/v1/voiceprints":
                    vp = VoiceprintStore(daemon.cfg.voiceprints_path)
                    self._send_json(
                        200,
                        {"voiceprints": [{"name": n, "samples": c} for n, c in vp.list_summary()]},
                    )
                    return

                if path == "/api/v1/integrations":
                    try:
                        from voice_review.installer import ClientInstaller
                        inst = ClientInstaller.default()
                        detected = {k: v.to_dict() for k, v in inst.detect_clients().items()}
                        self._send_json(200, {"clients": detected})
                    except Exception as exc:
                        self._send_json(200, {"clients": {}, "error": str(exc)})
                    return

                if path == "/api/v1/update/check":
                    try:
                        from voice_review.updater import SelfUpdater
                        upd = SelfUpdater.default(daemon.cfg)
                        use_cache = (qs.get("cached") or ["0"])[0] in ("1", "true", "yes")
                        info = upd.check_latest(use_cache=use_cache)
                        self._send_json(200, info.to_dict())
                    except Exception as exc:
                        self._send_json(200, {"ok": False, "current_version": __version__, "error": str(exc)})
                    return

                self._send_json(404, {"error": f"未知接口：{path}"})

            def do_PUT(self) -> None:  # noqa: N802
                if not self._check_security():
                    return
                path = urllib.parse.urlparse(self.path).path
                body = self._read_json()
                if body is None:
                    return

                if path == "/api/v1/config":
                    merged = config_to_dict(daemon.cfg)
                    secrets_patch = body.pop("secrets", None)
                    delete_secrets = body.pop("delete_secrets", None)
                    for k, v in body.items():
                        # 切换转写或分析插槽时整体替换对应配置表，彻底清除旧引擎残留的 base_url / model
                        if k in ("transcriber", "analyzer") and isinstance(v, dict):
                            merged[k] = dict(v)
                        elif isinstance(v, dict) and isinstance(merged.get(k), dict):
                            merged[k].update(v)
                        else:
                            merged[k] = v
                    t_prov = str((merged.get("transcriber") or {}).get("provider") or daemon.cfg.transcriber_provider)
                    a_prov = str((merged.get("analyzer") or {}).get("auto_provider") or daemon.cfg.analyzer_auto_provider)
                    ok, msg = validate_provider_combo(t_prov, a_prov)
                    if not ok:
                        self._send_json(400, {"ok": False, "error": msg})
                        return
                    if isinstance(secrets_patch, dict) or isinstance(delete_secrets, list):
                        daemon.secrets.update_many(
                            secrets_patch if isinstance(secrets_patch, dict) else {},
                            delete_keys=delete_secrets if isinstance(delete_secrets, list) else None,
                        )
                    cfg_target = daemon.cfg.config_path or (daemon.cfg.vmr_home / "config.toml")
                    save_config_dict(cfg_target, merged)
                    new_cfg = daemon.reload_from_disk()
                    self._send_json(
                        200,
                        {"ok": True, "config": config_to_dict(new_cfg), "secrets": daemon.secrets.masked_all()},
                    )
                    return

                if path == "/api/v1/rules/meta":
                    if "glossary" in body:
                        daemon.rules.save_glossary(str(body.get("glossary") or ""))
                    if "persona" in body:
                        daemon.rules.save_persona(str(body.get("persona") or ""))
                    self._send_json(200, {"ok": True})
                    return

                if path == "/api/v1/autostart":
                    want = bool(body.get("enabled", False))
                    st_before = daemon._autostart.status()
                    if not st_before.supported:
                        self._send_json(400, {"ok": False, "error": "开机自启目前仅支持 macOS（launchd）"})
                        return
                    if want:
                        daemon._autostart.write_plist()
                        if not st_before.loaded:
                            # 交由 launchd 接管并沿用当前会话令牌
                            daemon.schedule_graceful_restart(use_launchd=True)
                            self._send_json(
                                200,
                                {
                                    "ok": True,
                                    "enabled": True,
                                    "restarting": True,
                                    "message": "已开启开机自启，正在交由 macOS 后台接管…",
                                },
                            )
                            return
                        self._send_json(200, {"ok": True, "enabled": True, "restarting": False, "message": "开机自启已启用"})
                        return
                    else:
                        if st_before.loaded:
                            daemon.schedule_graceful_restart(use_launchd=False, disable_launchd_before_exit=True)
                            self._send_json(
                                200,
                                {
                                    "ok": True,
                                    "enabled": False,
                                    "restarting": True,
                                    "message": "已关闭开机自启（本次控制台保持在线）",
                                },
                            )
                            return
                        ok_dis, msg_dis = daemon._autostart.disable(unload_now=False)
                        self._send_json(200 if ok_dis else 400, {"ok": ok_dis, "enabled": False, "restarting": False, "message": msg_dis})
                        return

                self._send_json(404, {"error": f"未知接口：{path}"})

            def do_POST(self) -> None:  # noqa: N802
                if not self._check_security():
                    return
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path

                if path == "/api/v1/jobs/upload":
                    qs = urllib.parse.parse_qs(parsed.query)
                    raw_name = (qs.get("filename") or ["upload.m4a"])[0]
                    safe_name = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", Path(raw_name).name) or "upload.m4a"
                    template_id = (qs.get("template_id") or [""])[0]
                    length = int(self.headers.get("Content-Length") or "0")
                    if length <= 0:
                        self._send_json(400, {"error": "上传音频为空"})
                        return
                    up_dir = daemon.cfg.work_dir / "uploads"
                    up_dir.mkdir(parents=True, exist_ok=True)
                    dst = up_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
                    dst.write_bytes(self.rfile.read(length))
                    rec = daemon.executor.submit_single_file(dst, title=Path(raw_name).stem, template_id=template_id)
                    daemon.wake_worker()
                    self._send_json(200, {"ok": True, "recording_id": rec.recording_id, "job": rec.to_dict()})
                    return

                if path == "/api/v1/voiceprints/upload":
                    qs = urllib.parse.parse_qs(parsed.query)
                    name = (qs.get("name") or [""])[0].strip()
                    raw_name = (qs.get("filename") or ["sample.m4a"])[0]
                    if not name:
                        self._send_json(400, {"error": "请填写说话人姓名"})
                        return
                    length = int(self.headers.get("Content-Length") or "0")
                    if length <= 0:
                        self._send_json(400, {"error": "上传样音为空"})
                        return
                    safe_vp_name = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", Path(raw_name).name) or "sample.m4a"
                    with tempfile.TemporaryDirectory(prefix="vmr-vp-") as td:
                        tmp_audio = Path(td) / safe_vp_name
                        tmp_audio.write_bytes(self.rfile.read(length))
                        try:
                            diarizer = FluidAudioDiarizer(
                                fluidaudio_bin=daemon.cfg.fluidaudio_bin,
                                ffmpeg_bin=daemon.cfg.ffmpeg_bin,
                                voiceprints_path=daemon.cfg.voiceprints_path,
                            )
                            count, talk_sec = diarizer.enroll_from_audio(name, tmp_audio)
                        except Exception as exc:
                            self._send_json(400, {"ok": False, "error": str(exc)})
                            return
                    self._send_json(200, {"ok": True, "name": name, "samples": count, "talk_sec": round(talk_sec, 1)})
                    return

                body = self._read_json()
                if body is None:
                    return

                if path == "/api/v1/jobs/wake":
                    daemon.wake_worker()
                    self._send_json(200, {"ok": True})
                    return

                if path == "/api/v1/jobs/submit":
                    audio_p = Path(str(body.get("audio_path") or "")).expanduser()
                    try:
                        rec = daemon.executor.submit_single_file(
                            audio_p,
                            title=str(body.get("title") or ""),
                            template_id=str(body.get("template_id") or ""),
                            auto_analyze=bool(body.get("auto_analyze", True)),
                        )
                    except Exception as exc:
                        self._send_json(400, {"ok": False, "error": str(exc)})
                        return
                    daemon.wake_worker()
                    self._send_json(200, {"ok": True, "recording_id": rec.recording_id, "job": rec.to_dict()})
                    return

                m_ana = re.fullmatch(r"/api/v1/jobs/([^/]+)/analyze", path)
                if m_ana:
                    rec_id = urllib.parse.unquote(m_ana.group(1))
                    res = daemon.executor.run_analysis_only(
                        rec_id,
                        template_id=body.get("template_id"),
                        override_provider=body.get("provider"),
                    )
                    self._send_json(200 if res.ok else 400, {"ok": res.ok, "status": res.status, "error": res.error})
                    return

                m_retry = re.fullmatch(r"/api/v1/jobs/([^/]+)/retry", path)
                if m_retry:
                    rec_id = urllib.parse.unquote(m_retry.group(1))
                    existing = daemon.store.get(rec_id)
                    if existing and existing.status in ("preparing", "transcribing", "analyzing"):
                        self._send_json(200, {"ok": True, "already_running": True, "status": existing.status})
                        return
                    daemon.store.update(rec_id, status="discovered", last_error="", reset_retry=True)
                    daemon.wake_worker()
                    self._send_json(200, {"ok": True})
                    return

                if path == "/api/v1/providers/test":
                    kind = str(body.get("kind") or "transcriber")
                    test_cfg = daemon.cfg
                    test_secrets = daemon.secrets
                    if isinstance(body.get("transcriber"), dict) or isinstance(body.get("analyzer"), dict) or isinstance(body.get("secrets"), dict):
                        with tempfile.TemporaryDirectory(prefix="vmr-test-") as tdir:
                            td = Path(tdir)
                            tmp_dict = config_to_dict(daemon.cfg)
                            if isinstance(body.get("transcriber"), dict):
                                tmp_dict["transcriber"] = dict(body["transcriber"])
                            if isinstance(body.get("analyzer"), dict):
                                tmp_dict["analyzer"] = dict(body["analyzer"])
                            tmp_cfg_p = td / "config.toml"
                            save_config_dict(tmp_cfg_p, tmp_dict)
                            test_cfg = load_config(tmp_cfg_p, vmr_home=td)
                            if daemon.secrets.path.is_file():
                                shutil.copy2(daemon.secrets.path, test_cfg.secrets_path)
                            test_secrets = SecretStore(test_cfg.secrets_path)
                            if isinstance(body.get("secrets"), dict):
                                test_secrets.update_many(body["secrets"])
                            if kind == "analyzer":
                                ok, msg = test_analyzer_connection(test_cfg, test_secrets)
                            else:
                                ok, msg = test_transcriber_connection(test_cfg, test_secrets)
                            self._send_json(200, {"ok": ok, "message": msg})
                            return
                    if kind == "analyzer":
                        ok, msg = test_analyzer_connection(test_cfg, test_secrets)
                    else:
                        ok, msg = test_transcriber_connection(test_cfg, test_secrets)
                    self._send_json(200, {"ok": ok, "message": msg})
                    return

                if path == "/api/v1/templates":
                    try:
                        tpl = daemon.rules.save_template(
                            str(body.get("id") or ""),
                            name=str(body.get("name") or ""),
                            description=str(body.get("description") or ""),
                            body=str(body.get("body") or ""),
                            is_default=bool(body.get("is_default", False)),
                        )
                    except Exception as exc:
                        self._send_json(400, {"ok": False, "error": str(exc)})
                        return
                    self._send_json(200, {"ok": True, "template": tpl.to_dict()})
                    return

                if path == "/api/v1/templates/preview":
                    try:
                        draft_body = body.get("body")
                        if isinstance(draft_body, str) and draft_body.strip():
                            tpl_name = str(body.get("name") or body.get("template_id") or "当前编辑模板")
                            persona = daemon.rules.get_persona()
                            parts = [
                                "## 录音元数据",
                                f"- **录制时间**：{datetime.now(SHANGHAI_TZ).replace(microsecond=0).isoformat()}",
                                f"- **标题**：{str(body.get('title') or '示例项目周会')}",
                                "- **音频时长**：30分00秒",
                                f"- **选用分析模板**：{tpl_name}（草稿实时预览）",
                                "- **声纹与说话人概况**：",
                                "- SPEAKER_00 -> 已确认真名：说话人A（约 15.0 分钟）",
                            ]
                            if persona:
                                parts.extend(["", "## 用户背景说明", persona])
                            parts.extend(["", f"## 分析规则（模板：{tpl_name}）", draft_body.strip()])
                            parts.extend(["", "## 完整转录稿正文", str(body.get("sample_transcript") or "## 00:00:05 **说话人A**\n（此处将填入完整转录稿正文）")])
                            prompt = "\n".join(parts).strip() + "\n"
                        else:
                            prompt = daemon.rules.compose_analysis_prompt(
                                template_id=body.get("template_id"),
                                title=str(body.get("title") or "示例项目周会"),
                                recorded_at=datetime.now(SHANGHAI_TZ).replace(microsecond=0).isoformat(),
                                duration_sec=1800.0,
                                speakers_summary="- SPEAKER_00 -> 已确认真名：说话人A（约 15.0 分钟）",
                                transcript_text=str(body.get("sample_transcript") or "## 00:00:05 **说话人A**\n（此处将填入完整转录稿正文）"),
                            )
                    except Exception as exc:
                        self._send_json(400, {"ok": False, "error": str(exc)})
                        return
                    self._send_json(200, {"ok": True, "prompt": prompt})
                    return

                m_reset = re.fullmatch(r"/api/v1/templates/([^/]+)/reset", path)
                if m_reset:
                    try:
                        tpl = daemon.rules.reset_builtin_template(urllib.parse.unquote(m_reset.group(1)))
                    except Exception as exc:
                        self._send_json(400, {"ok": False, "error": str(exc)})
                        return
                    self._send_json(200, {"ok": True, "template": tpl.to_dict()})
                    return

                if path == "/api/v1/integrations/install":
                    try:
                        from voice_review.installer import ClientInstaller
                        targets = body.get("targets")
                        res = ClientInstaller.default().install(targets=targets)
                        self._send_json(200, {"ok": True, "results": {k: v.to_dict() for k, v in res.items()}})
                    except Exception as exc:
                        self._send_json(400, {"ok": False, "error": str(exc)})
                    return

                if path == "/api/v1/integrations/uninstall":
                    try:
                        from voice_review.installer import ClientInstaller
                        targets = body.get("targets")
                        res = ClientInstaller.default().uninstall(targets=targets)
                        self._send_json(200, {"ok": True, "results": {k: v.to_dict() for k, v in res.items()}})
                    except Exception as exc:
                        self._send_json(400, {"ok": False, "error": str(exc)})
                    return

                if path == "/api/v1/update/apply":
                    try:
                        force = bool(body.get("force", False))
                        auto_restart = bool(body.get("auto_restart", True))
                        busy_cnt = daemon.active_jobs_count()
                        if busy_cnt > 0 and not force:
                            self._send_json(
                                409,
                                {
                                    "ok": False,
                                    "busy": True,
                                    "error": f"当前有 {busy_cnt} 条录音正在转写或分析，为防止中断请等待完成后再一键更新。",
                                },
                            )
                            return
                        from voice_review.updater import SelfUpdater
                        upd = SelfUpdater.default(daemon.cfg)
                        info = upd.check_latest(use_cache=False)
                        if not info.ok or not info.download_url:
                            self._send_json(400, {"ok": False, "error": info.error or "当前无可用下载包"})
                            return
                        res = upd.apply_update(info.download_url, info.sha256)
                        if not res.ok:
                            self._send_json(400, res.to_dict())
                            return
                        payload = res.to_dict()
                        if auto_restart:
                            use_ld = daemon._autostart.status().enabled
                            daemon.schedule_graceful_restart(use_launchd=use_ld)
                            payload["restarting"] = True
                            payload["message"] = (res.message + "；正在自动重启后台守护并刷新页面…")
                        self._send_json(200, payload)
                    except Exception as exc:
                        self._send_json(400, {"ok": False, "error": str(exc)})
                    return

                self._send_json(404, {"error": f"未知接口：{path}"})

            def do_DELETE(self) -> None:  # noqa: N802
                if not self._check_security():
                    return
                path = urllib.parse.urlparse(self.path).path
                m_tpl = re.fullmatch(r"/api/v1/templates/([^/]+)", path)
                if m_tpl:
                    ok = daemon.rules.delete_template(urllib.parse.unquote(m_tpl.group(1)))
                    self._send_json(200 if ok else 404, {"ok": ok})
                    return
                m_vp = re.fullmatch(r"/api/v1/voiceprints/([^/]+)", path)
                if m_vp:
                    vp = VoiceprintStore(daemon.cfg.voiceprints_path)
                    ok = vp.delete(urllib.parse.unquote(m_vp.group(1)))
                    self._send_json(200 if ok else 404, {"ok": ok})
                    return
                self._send_json(404, {"error": f"未知接口：{path}"})

        return _Handler

    def start_in_thread(self) -> DaemonInfo:
        self._lock.acquire()
        try:
            self._stop_event.clear()
            handler_cls = self._make_handler()
            try:
                self._httpd = ThreadingHTTPServer((self.host, self._requested_port), handler_cls)
            except OSError:
                self._httpd = ThreadingHTTPServer((self.host, 0), handler_cls)
            self._httpd.daemon_threads = True
            self.port = int(self._httpd.server_address[1])
            self._write_daemon_info()

            self._http_thread = threading.Thread(target=self._httpd.serve_forever, daemon=True, name="vmr-http")
            self._http_thread.start()
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="vmr-worker")
            self._worker_thread.start()
            self._update_thread = threading.Thread(target=self._update_check_loop, daemon=True, name="vmr-update-check")
            self._update_thread.start()
            return DaemonInfo(pid=os.getpid(), host=self.host, port=self.port, token=self.token)
        except Exception:
            self._lock.release()
            raise

    def stop(self) -> None:
        self._stop_event.set()
        self.wake_worker()
        self.broadcaster.close_all()
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=3.0)
            self._worker_thread = None
        if self._http_thread is not None:
            self._http_thread.join(timeout=3.0)
            self._http_thread = None
        if self._update_thread is not None:
            self._update_thread.join(timeout=2.0)
            self._update_thread = None
        try:
            self._info_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._lock.release()

    def serve_forever(self) -> None:
        info = self.start_in_thread()
        prev_int = signal.getsignal(signal.SIGINT)
        prev_term = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, lambda *_: self._stop_event.set())
        signal.signal(signal.SIGTERM, lambda *_: self._stop_event.set())
        logger.info("voice-memo-review 单守护已启动（PID=%d，控制台=%s）", info.pid, info.base_url)
        try:
            while not self._stop_event.is_set():
                time.sleep(0.5)
        finally:
            self.stop()
            signal.signal(signal.SIGINT, prev_int)
            signal.signal(signal.SIGTERM, prev_term)
