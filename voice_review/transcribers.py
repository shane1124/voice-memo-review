"""插槽 3：可插拔转写引擎（antigravity-flash / gemini-api / openai-compatible-asr / command-asr）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import random
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.request
import uuid

from voice_review.config import VoiceReviewConfig
from voice_review.diarizer import DiarizationResult, FluidAudioDiarizer, NoSpeechDetectedError
from voice_review.secrets import SecretStore
from voice_review.segmenter import (
    Segment,
    build_uniform_chunks,
    clean_and_merge_segments,
    cut_segments_to_mp3,
    detect_split_id_checks,
    format_hms,
    pack_batches,
)
from voice_review.workflow_builder import prepare_v2_job, render_workflow_script, sanitize_transcript_field

ProgressCallback = Callable[[int, int], None]


@dataclass
class PreparedAudioJob:
    job_dir: Path
    audio_path: Path
    transcript_path: Path
    title: str
    recorded_at: datetime
    speakers_summary: str
    segments: list[Segment]
    batches: list[list[Segment]]
    diarization_segments: list[tuple[float, float, str]] = field(default_factory=list)
    speaker_labels: dict[str, str] = field(default_factory=dict)
    asr_context: str = ""
    diarization_result: DiarizationResult | None = None
    duration_sec: float = 0.0
    prep_sec: float = 0.0
    workflow_script_path: Path | None = None


@dataclass(frozen=True)
class TranscriptOutcome:
    mode: str  # "direct" | "delegated_workflow"
    transcript_path: Path
    workflow_script_path: Path | None
    segment_count: int
    batch_count: int
    speakers_summary: str
    turns: int = 0
    chars: int = 0


def align_asr_segments_to_diarization(
    asr_segments: list[dict[str, Any]],
    diar_segments: list[tuple[float, float, str]],
    speaker_labels: dict[str, str],
) -> list[dict[str, Any]]:
    """按最大时间重叠时长将整轨 ASR 句子精准回贴至 FluidAudio 声纹说话人。"""
    aligned: list[dict[str, Any]] = []
    for item in asr_segments:
        text = sanitize_transcript_field(item.get("text"))
        if not text:
            continue
        start = float(item.get("start", 0.0))
        end = float(item.get("end", start))
        if end < start:
            end = start
        overlap_by_sid: dict[str, float] = {}
        for ds, de, sid in diar_segments:
            ov = min(end, de) - max(start, ds)
            if ov > 0:
                overlap_by_sid[sid] = overlap_by_sid.get(sid, 0.0) + ov
        if overlap_by_sid:
            best_sid = max(overlap_by_sid.items(), key=lambda kv: kv[1])[0]
            speaker = speaker_labels.get(best_sid, best_sid)
        else:
            speaker = "未知说话人"
        aligned.append(
            {
                "start": round(start, 2),
                "end": round(end, 2),
                "hms_start": format_hms(start),
                "speaker": speaker,
                "text": text,
                "tone_note": sanitize_transcript_field(item.get("tone_note")),
                "multi_speaker_note": sanitize_transcript_field(item.get("multi_speaker_note")),
            }
        )
    return aligned


def render_transcript_markdown(
    *,
    title: str,
    source_name: str,
    recorded_at: str,
    duration_sec: float,
    engine_label: str,
    speakers_summary: str,
    turns: list[dict[str, Any]],
    elapsed_sec: float = 0.0,
) -> tuple[str, int]:
    """将任意转写引擎的标准化结果渲染为统一 Markdown 转录稿。"""
    lines = [
        f"# {title}｜转录稿",
        "",
        f"- 原音频：{source_name}",
        f"- 录制时间：{recorded_at}",
        f"- 音频时长：{format_hms(duration_sec)}",
        f"- 转写引擎：{engine_label}",
        f"- 有效发言：{len(turns)} 段｜转写耗时：{round(elapsed_sec, 1)} 秒",
        "",
        "## 说话人概况",
        *(speakers_summary.splitlines() if speakers_summary.strip() else ["- 未识别到已注册声纹"]),
        "",
        "---",
        "",
    ]
    rendered = 0
    for t in turns:
        txt = sanitize_transcript_field(t.get("text"))
        if not txt:
            continue
        hms = t.get("hms_start") or format_hms(float(t.get("start", 0.0)))
        spk = str(t.get("speaker") or "说话人").strip()
        lines.append(f"## {hms} **{spk}**")
        lines.append(txt)
        tn = sanitize_transcript_field(t.get("tone_note"))
        if tn:
            lines.append(f"> [语气] {tn}")
        mn = sanitize_transcript_field(t.get("multi_speaker_note"))
        if mn:
            lines.append(f"> [叠话] {mn}")
        lines.append("")
        rendered += 1
    return "\n".join(lines).rstrip() + "\n", rendered


def prepare_audio_job(
    audio_path: Path,
    *,
    cfg: VoiceReviewConfig,
    job_dir: Path,
    transcript_path: Path,
    title: str = "",
    recorded_at: datetime | None = None,
    asr_context: str = "",
    num_speakers: int | None = None,
    cleanup_clips: bool = True,
) -> PreparedAudioJob:
    """插槽 2 统一预处理入口：按配置运行 FluidAudio 声纹切段或无声纹均匀切块。"""
    t0 = time.monotonic()
    src = Path(audio_path).expanduser().resolve()
    job_dir = Path(job_dir).expanduser().resolve()
    transcript_path = Path(transcript_path).expanduser().resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    clips_dir = job_dir / "clips"
    rec_dt = recorded_at or datetime.now().astimezone().replace(microsecond=0)
    resolved_title = title or src.stem

    if cfg.preprocessor_mode == "none" or cfg.fluidaudio_bin is None:
        segments, total_dur = build_uniform_chunks(
            src,
            clips_dir,
            chunk_sec=cfg.uniform_chunk_sec,
            ffmpeg_bin=cfg.ffmpeg_bin,
            ffprobe_bin=cfg.ffprobe_bin,
            mp3_bitrate=cfg.segmenter.mp3_bitrate,
        )
        batches = pack_batches(
            segments,
            max_segments=cfg.segmenter.batch_max_segments,
            max_speech_sec=max(cfg.segmenter.batch_max_speech_sec, cfg.uniform_chunk_sec + 10.0),
        )
        return PreparedAudioJob(
            job_dir=job_dir,
            audio_path=src,
            transcript_path=transcript_path,
            title=resolved_title,
            recorded_at=rec_dt,
            speakers_summary="- 未启用本地声纹分离（均匀切块模式）",
            segments=segments,
            batches=batches,
            diarization_segments=[(s.true_start, s.true_end, s.sid) for s in segments],
            speaker_labels={"SPEAKER_00": "说话人"},
            asr_context=asr_context.strip(),
            duration_sec=total_dur,
            prep_sec=round(time.monotonic() - t0, 2),
        )

    diarizer = FluidAudioDiarizer(
        fluidaudio_bin=cfg.fluidaudio_bin,
        ffmpeg_bin=cfg.ffmpeg_bin,
        voiceprints_path=cfg.voiceprints_path,
    )
    diar_res, wav_path = diarizer.diarize(src, work_dir=job_dir, num_speakers=num_speakers, reuse_existing=True)
    try:
        segments = clean_and_merge_segments(diar_res, config=cfg.segmenter, wav_path=wav_path)
        if not segments:
            raise NoSpeechDetectedError("未检测到有效人声片段（可能为纯静音或时长过短）")
        cut_segments_to_mp3(
            wav_path,
            segments,
            clips_dir,
            ffmpeg_bin=cfg.ffmpeg_bin,
            mp3_bitrate=cfg.segmenter.mp3_bitrate,
        )
    finally:
        if wav_path.is_file():
            try:
                wav_path.unlink()
            except OSError:
                pass

    batches = pack_batches(
        segments,
        max_segments=cfg.segmenter.batch_max_segments,
        max_speech_sec=cfg.segmenter.batch_max_speech_sec,
    )
    max_end = max((s.true_end for s in segments), default=0.0)
    speaker_labels = {sid: sp.label for sid, sp in diar_res.speakers.items()}
    return PreparedAudioJob(
        job_dir=job_dir,
        audio_path=src,
        transcript_path=transcript_path,
        title=resolved_title,
        recorded_at=rec_dt,
        speakers_summary=diar_res.summary_text(),
        segments=segments,
        batches=batches,
        diarization_segments=list(diar_res.raw_segments),
        speaker_labels=speaker_labels,
        asr_context=asr_context.strip(),
        diarization_result=diar_res,
        duration_sec=max_end,
        prep_sec=round(time.monotonic() - t0, 2),
    )


class TranscriberProvider(ABC):
    provider_id: str = ""

    @abstractmethod
    def transcribe(
        self,
        job: PreparedAudioJob,
        *,
        on_progress: ProgressCallback | None = None,
        cleanup_clips: bool = True,
    ) -> TranscriptOutcome:
        """执行转写并输出标准化 TranscriptOutcome。"""


class AntigravityFlashTranscriber(TranscriberProvider):
    provider_id = "antigravity-flash"

    def __init__(self, cfg: VoiceReviewConfig) -> None:
        self.cfg = cfg

    def transcribe(
        self,
        job: PreparedAudioJob,
        *,
        on_progress: ProgressCallback | None = None,
        cleanup_clips: bool = True,
    ) -> TranscriptOutcome:
        job.job_dir.mkdir(parents=True, exist_ok=True)
        split_checks = (
            detect_split_id_checks(job.diarization_result, job.segments)
            if job.diarization_result is not None
            else []
        )
        speakers_dict = (
            {
                sid: {
                    "sid": sp.sid,
                    "label": sp.label,
                    "confirmed": sp.confirmed,
                    "top1_name": sp.top1_name,
                    "top1_score": sp.top1_score,
                    "talk_sec": sp.talk_sec,
                }
                for sid, sp in job.diarization_result.speakers.items()
            }
            if job.diarization_result is not None
            else {"SPEAKER_00": {"sid": "SPEAKER_00", "label": "说话人", "confirmed": False, "top1_name": "", "top1_score": 0.0, "talk_sec": job.duration_sec}}
        )
        manifest = {
            "title": job.title,
            "source_name": job.audio_path.name,
            "recorded_at": job.recorded_at.isoformat(),
            "duration_sec": round(job.duration_sec, 1),
            "duration_hms": format_hms(job.duration_sec),
            "transcript_path": str(job.transcript_path.resolve()),
            "clips_dir": str((job.job_dir / "clips").resolve()),
            "asr_context": job.asr_context,
            "speakers": speakers_dict,
            "split_checks": [c.to_dict() for c in split_checks],
            "segments": [s.to_dict() for s in job.segments],
            "batches": [[s.to_dict() for s in batch] for batch in job.batches],
        }
        manifest_path = job.job_dir / "manifest_v2.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        wf_path = job.job_dir / "run_v2_transcribe.py"
        wf_path.write_text(
            render_workflow_script(
                manifest_path,
                cleanup_clips=cleanup_clips,
                max_concurrency=self.cfg.segmenter.max_concurrency,
                agent_timeout_sec=self.cfg.segmenter.agent_timeout_sec,
                stagger_sec=self.cfg.segmenter.stagger_sec,
            ),
            encoding="utf-8",
        )
        if on_progress:
            on_progress(0, len(job.batches))
        return TranscriptOutcome(
            mode="delegated_workflow",
            transcript_path=job.transcript_path,
            workflow_script_path=wf_path,
            segment_count=len(job.segments),
            batch_count=len(job.batches),
            speakers_summary=job.speakers_summary,
        )


def _extract_json_block(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        parsed = json.loads(m.group(0))
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"无法从响应中解析 JSON：{text[:200]}")


class _ThreadPacedLimiter:
    """线程安全发射节流与 429 自适应并发降级闸门（全局至少 0.35s 错峰发射，连续限流自动减半并发）。"""

    def __init__(self, initial: int, *, launch_pace_sec: float = 0.35) -> None:
        self.limit = max(1, int(initial))
        self.fallback = max(1, self.limit // 2)
        self.launch_pace_sec = max(0.0, float(launch_pace_sec))
        self.active = 0
        self.degraded = False
        self._hits: list[float] = []
        self._last_launch = 0.0
        self._cond = threading.Condition()
        self._pace_lock = threading.Lock()

    def acquire(self) -> None:
        with self._cond:
            while self.active >= self.limit:
                self._cond.wait()
            self.active += 1
        if self.launch_pace_sec > 0:
            with self._pace_lock:
                now = time.monotonic()
                wait = self.launch_pace_sec - (now - self._last_launch)
                if wait > 0:
                    time.sleep(wait)
                self._last_launch = time.monotonic()

    def release(self) -> None:
        with self._cond:
            self.active = max(0, self.active - 1)
            self._cond.notify_all()

    def record_rate_limit(self) -> None:
        now = time.monotonic()
        with self._cond:
            self._hits = [t for t in self._hits if now - t <= 60.0]
            self._hits.append(now)
            if not self.degraded and len(self._hits) >= 3 and self.limit > self.fallback:
                self.limit = self.fallback
                self.degraded = True


def _retry_delay_from_error(exc: urllib.error.HTTPError, attempt: int) -> float:
    hdr = exc.headers.get("Retry-After") if exc.headers else None
    if hdr:
        try:
            return min(60.0, max(1.0, float(hdr)))
        except ValueError:
            pass
    return min(45.0, (3.0 ** attempt) + random.uniform(0.5, 2.5))


class GeminiApiTranscriber(TranscriberProvider):
    provider_id = "gemini-api"

    def __init__(self, cfg: VoiceReviewConfig, secrets: SecretStore) -> None:
        self.cfg = cfg
        self.secrets = secrets
        st = cfg.transcriber_settings
        self.base_url = str(st.get("base_url") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.model = str(st.get("model") or "gemini-2.5-flash").strip()
        self.max_concurrency = max(1, min(20, int(st.get("max_concurrency", 6))))
        self.timeout_sec = max(30, int(st.get("timeout_sec", 120)))

    def _api_key(self) -> str:
        key = self.secrets.get("gemini_api_key", env_var="GEMINI_API_KEY")
        if not key:
            raise RuntimeError("未配置 Gemini API Key（请在控制台「引擎插槽」填入或设置 GEMINI_API_KEY）")
        return key

    def _call_batch(
        self,
        batch_id: str,
        segs: list[Segment],
        asr_context: str,
        limiter: _ThreadPacedLimiter | None = None,
    ) -> list[dict[str, Any]]:
        api_key = self._api_key()
        glossary = f"\n专有名词参考词表（仅同音校对，严禁凭空编造）：{asr_context}\n" if asr_context else ""
        desc_lines = [
            f"- {s.seg_id} | {s.hms_start}-{s.hms_end} | 说话人={s.speaker_label}"
            for s in segs
        ]
        prompt_text = (
            f"你是高精度中英混合语音精转专家（批次 {batch_id}）。请依次听取随后附带的 {len(segs)} 段音频，"
            "按顺序返回严格 JSON：{\"batch_id\": \"...\", \"items\": [{\"seg_id\": \"S001\", \"text\": \"...\", "
            "\"tone_note\": \"\", \"multi_speaker_note\": \"\"}]}。\n"
            "规则：1) 纯杂音/无法辨清处留空或填 [听不清]；2) 仅情绪明显波动时在 tone_note 写 6~18 字客观简注，平常留空；"
            "3) 仅双人重叠插话时写 multi_speaker_note，单人留空；4) 每条 seg_id 均须出现。"
            f"{glossary}\n音频列表：\n" + "\n".join(desc_lines)
        )
        parts: list[dict[str, Any]] = [{"text": prompt_text}]
        for s in segs:
            raw_bytes = Path(s.mp3_path).read_bytes()
            parts.append({"text": f"[音频 {s.seg_id}]"})
            parts.append({"inline_data": {"mime_type": "audio/mpeg", "data": base64.b64encode(raw_bytes).decode("ascii")}})

        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
        }
        url = f"{self.base_url}/models/{self.model}:generateContent?key={api_key}"
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        last_err = ""
        for attempt in range(1, 5):
            req = urllib.request.Request(url, data=body_bytes, headers={"Content-Type": "application/json"}, method="POST")
            if limiter is not None:
                limiter.acquire()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                    resp_obj = json.loads(resp.read().decode("utf-8"))
                cands = resp_obj.get("candidates") or []
                text_out = ""
                if cands:
                    p_list = (cands[0].get("content") or {}).get("parts") or []
                    text_out = "".join(str(p.get("text") or "") for p in p_list)
                parsed = _extract_json_block(text_out)
                return list(parsed.get("items") or [])
            except urllib.error.HTTPError as exc:
                last_err = f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='ignore')[:200]}"
                if exc.code == 429 and limiter is not None:
                    limiter.record_rate_limit()
                if exc.code in (429, 500, 502, 503, 504) and attempt < 4:
                    delay = _retry_delay_from_error(exc, attempt)
                else:
                    raise RuntimeError(f"Gemini API 转写失败（{batch_id}）：{last_err}") from exc
            except Exception as exc:
                last_err = str(exc)
                if attempt < 4:
                    delay = min(15.0, 2.0 * attempt)
                else:
                    raise RuntimeError(f"Gemini API 转写失败（{batch_id}）：{last_err}") from exc
            finally:
                if limiter is not None:
                    limiter.release()
            time.sleep(delay)
        raise RuntimeError(f"Gemini API 转写失败（{batch_id}）：{last_err}")

    def transcribe(
        self,
        job: PreparedAudioJob,
        *,
        on_progress: ProgressCallback | None = None,
        cleanup_clips: bool = True,
    ) -> TranscriptOutcome:
        t0 = time.monotonic()
        cache_dir = job.job_dir / "batches_out"
        cache_dir.mkdir(parents=True, exist_ok=True)
        total = len(job.batches)
        done_lock = threading.Lock()
        done_count = 0
        items_by_seg: dict[str, dict[str, Any]] = {}
        limiter = _ThreadPacedLimiter(self.max_concurrency, launch_pace_sec=0.35)

        def _worker(idx: int, segs: list[Segment]) -> None:
            nonlocal done_count
            batch_id = f"B{idx + 1:02d}"
            cache_file = cache_dir / f"{batch_id}.json"
            items: list[dict[str, Any]] | None = None
            if cache_file.is_file():
                try:
                    cached = json.loads(cache_file.read_text(encoding="utf-8"))
                    if isinstance(cached.get("items"), list) and cached["items"]:
                        items = cached["items"]
                except Exception:
                    items = None
            if items is None:
                items = self._call_batch(batch_id, segs, job.asr_context, limiter=limiter)
                cache_file.write_text(
                    json.dumps({"batch_id": batch_id, "ok": True, "items": items}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            with done_lock:
                for it in items:
                    if isinstance(it, dict) and it.get("seg_id"):
                        items_by_seg[str(it["seg_id"])] = it
                done_count += 1
                if on_progress:
                    on_progress(done_count, total)

        with ThreadPoolExecutor(max_workers=self.max_concurrency) as pool:
            futs = [pool.submit(_worker, i, b) for i, b in enumerate(job.batches)]
            for f in as_completed(futs):
                f.result()

        turns: list[dict[str, Any]] = []
        for s in job.segments:
            it = items_by_seg.get(s.seg_id) or {}
            txt = sanitize_transcript_field(it.get("text"))
            if not txt:
                continue
            turns.append(
                {
                    "start": s.true_start,
                    "hms_start": s.hms_start,
                    "speaker": s.speaker_label,
                    "text": txt,
                    "tone_note": sanitize_transcript_field(it.get("tone_note")),
                    "multi_speaker_note": sanitize_transcript_field(it.get("multi_speaker_note")),
                }
            )
        md, rendered = render_transcript_markdown(
            title=job.title,
            source_name=job.audio_path.name,
            recorded_at=job.recorded_at.isoformat(),
            duration_sec=job.duration_sec,
            engine_label=f"Gemini API ({self.model}，直连多模态含语气)",
            speakers_summary=job.speakers_summary,
            turns=turns,
            elapsed_sec=time.monotonic() - t0,
        )
        job.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        job.transcript_path.write_text(md, encoding="utf-8")
        if cleanup_clips and (job.job_dir / "clips").is_dir():
            shutil.rmtree(job.job_dir / "clips", ignore_errors=True)
        return TranscriptOutcome(
            mode="direct",
            transcript_path=job.transcript_path,
            workflow_script_path=None,
            segment_count=len(job.segments),
            batch_count=total,
            speakers_summary=job.speakers_summary,
            turns=rendered,
            chars=len(md),
        )


def _encode_multipart(fields: dict[str, str], file_field: str, filename: str, file_bytes: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = f"----VmrBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for k, v in fields.items():
        if not v:
            continue
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        chunks.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode("utf-8"))
        chunks.append(v.encode("utf-8") + b"\r\n")
    chunks.append(f"--{boundary}\r\n".encode("ascii"))
    chunks.append(f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode("utf-8"))
    chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode("ascii"))
    chunks.append(file_bytes + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class OpenAICompatibleAsrTranscriber(TranscriberProvider):
    provider_id = "openai-compatible-asr"

    def __init__(self, cfg: VoiceReviewConfig, secrets: SecretStore) -> None:
        self.cfg = cfg
        self.secrets = secrets
        st = cfg.transcriber_settings
        self.base_url = str(st.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        self.model = str(st.get("model") or "whisper-1").strip()
        self.language = str(st.get("language") or "zh").strip()
        self.max_concurrency = max(1, min(16, int(st.get("max_concurrency", 4))))
        self.timeout_sec = max(30, int(st.get("timeout_sec", 120)))

    def _api_key(self) -> str:
        return self.secrets.get("openai_asr_api_key", env_var="OPENAI_API_KEY")

    def _call_segment(
        self,
        seg: Segment,
        asr_context: str,
        limiter: _ThreadPacedLimiter | None = None,
    ) -> str:
        url = f"{self.base_url}/audio/transcriptions"
        fields = {"model": self.model, "response_format": "json"}
        if self.language:
            fields["language"] = self.language
        if asr_context:
            fields["prompt"] = asr_context[:500]
        raw_bytes = Path(seg.mp3_path).read_bytes()
        body, ctype = _encode_multipart(fields, "file", f"{seg.seg_id}.mp3", raw_bytes, "audio/mpeg")
        headers = {"Content-Type": ctype}
        key = self._api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"

        last_err = ""
        for attempt in range(1, 5):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            if limiter is not None:
                limiter.acquire()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(raw)
                    return sanitize_transcript_field(parsed.get("text"))
                except json.JSONDecodeError:
                    return sanitize_transcript_field(raw)
            except urllib.error.HTTPError as exc:
                last_err = f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='ignore')[:200]}"
                if exc.code == 429 and limiter is not None:
                    limiter.record_rate_limit()
                if exc.code in (429, 500, 502, 503, 504) and attempt < 4:
                    delay = _retry_delay_from_error(exc, attempt)
                else:
                    raise RuntimeError(f"OpenAI 兼容 ASR 转写失败（{seg.seg_id}）：{last_err}") from exc
            except Exception as exc:
                last_err = str(exc)
                if attempt < 4:
                    delay = min(15.0, 2.0 * attempt)
                else:
                    raise RuntimeError(f"OpenAI 兼容 ASR 转写失败（{seg.seg_id}）：{last_err}") from exc
            finally:
                if limiter is not None:
                    limiter.release()
            time.sleep(delay)
        raise RuntimeError(f"OpenAI 兼容 ASR 转写失败（{seg.seg_id}）：{last_err}")

    def transcribe(
        self,
        job: PreparedAudioJob,
        *,
        on_progress: ProgressCallback | None = None,
        cleanup_clips: bool = True,
    ) -> TranscriptOutcome:
        t0 = time.monotonic()
        cache_dir = job.job_dir / "batches_out"
        cache_dir.mkdir(parents=True, exist_ok=True)
        total = len(job.segments)
        done_lock = threading.Lock()
        done_count = 0
        text_by_seg: dict[str, str] = {}
        limiter = _ThreadPacedLimiter(self.max_concurrency, launch_pace_sec=0.35)

        def _worker(seg: Segment) -> None:
            nonlocal done_count
            cfile = cache_dir / f"{seg.seg_id}.json"
            txt: str | None = None
            if cfile.is_file():
                try:
                    txt = str(json.loads(cfile.read_text(encoding="utf-8")).get("text") or "")
                except Exception:
                    txt = None
            if txt is None:
                txt = self._call_segment(seg, job.asr_context, limiter=limiter)
                cfile.write_text(json.dumps({"seg_id": seg.seg_id, "text": txt}, ensure_ascii=False), encoding="utf-8")
            with done_lock:
                text_by_seg[seg.seg_id] = txt
                done_count += 1
                if on_progress:
                    on_progress(done_count, total)

        with ThreadPoolExecutor(max_workers=self.max_concurrency) as pool:
            futs = [pool.submit(_worker, s) for s in job.segments]
            for f in as_completed(futs):
                f.result()

        turns = [
            {"start": s.true_start, "hms_start": s.hms_start, "speaker": s.speaker_label, "text": text_by_seg.get(s.seg_id, "")}
            for s in job.segments
            if text_by_seg.get(s.seg_id, "").strip()
        ]
        md, rendered = render_transcript_markdown(
            title=job.title,
            source_name=job.audio_path.name,
            recorded_at=job.recorded_at.isoformat(),
            duration_sec=job.duration_sec,
            engine_label=f"OpenAI 兼容 ASR ({self.model})",
            speakers_summary=job.speakers_summary,
            turns=turns,
            elapsed_sec=time.monotonic() - t0,
        )
        job.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        job.transcript_path.write_text(md, encoding="utf-8")
        if cleanup_clips and (job.job_dir / "clips").is_dir():
            shutil.rmtree(job.job_dir / "clips", ignore_errors=True)
        return TranscriptOutcome(
            mode="direct",
            transcript_path=job.transcript_path,
            workflow_script_path=None,
            segment_count=total,
            batch_count=total,
            speakers_summary=job.speakers_summary,
            turns=rendered,
            chars=len(md),
        )


class CommandAsrTranscriber(TranscriberProvider):
    """通用命令适配器：支持整轨对齐（full_audio）与逐段（segment），严格 argv + shell=False。"""

    provider_id = "command-asr"

    def __init__(self, cfg: VoiceReviewConfig) -> None:
        self.cfg = cfg
        st = cfg.transcriber_settings
        self.mode = str(st.get("mode") or "full_audio").strip().lower()
        raw_cmd = st.get("command")
        if isinstance(raw_cmd, list):
            self.command_template = [str(x) for x in raw_cmd if str(x)]
        elif isinstance(raw_cmd, str) and raw_cmd.strip():
            import shlex
            self.command_template = shlex.split(raw_cmd)
        else:
            self.command_template = []
        self.language = str(st.get("language") or "zh").strip()
        self.timeout_sec = max(30, int(st.get("timeout_sec", 600)))

    def _build_argv(self, audio_path: Path, glossary: str) -> list[str]:
        if not self.command_template:
            raise RuntimeError("command-asr 未配置可执行命令列表 transcriber.command")
        out: list[str] = []
        for token in self.command_template:
            replaced = (
                token.replace("{audio}", str(audio_path))
                .replace("{lang}", self.language)
                .replace("{glossary}", glossary)
            )
            out.append(replaced)
        return out

    def transcribe(
        self,
        job: PreparedAudioJob,
        *,
        on_progress: ProgressCallback | None = None,
        cleanup_clips: bool = True,
    ) -> TranscriptOutcome:
        t0 = time.monotonic()
        if self.mode == "segment":
            turns: list[dict[str, Any]] = []
            total = len(job.segments)
            for idx, seg in enumerate(job.segments, 1):
                argv = self._build_argv(Path(seg.mp3_path), job.asr_context)
                res = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout_sec, shell=False, check=False)
                if res.returncode != 0:
                    raise RuntimeError(f"command-asr 执行失败（exit={res.returncode}）：{(res.stderr or res.stdout)[:240]}")
                txt = sanitize_transcript_field(res.stdout)
                if txt:
                    turns.append({"start": seg.true_start, "hms_start": seg.hms_start, "speaker": seg.speaker_label, "text": txt})
                if on_progress:
                    on_progress(idx, total)
        else:
            argv = self._build_argv(job.audio_path, job.asr_context)
            res = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout_sec, shell=False, check=False)
            if res.returncode != 0:
                raise RuntimeError(f"command-asr 整轨执行失败（exit={res.returncode}）：{(res.stderr or res.stdout)[:240]}")
            raw_out = (res.stdout or "").strip()
            try:
                parsed = json.loads(raw_out)
                asr_segs = parsed.get("segments") if isinstance(parsed, dict) else (parsed if isinstance(parsed, list) else [])
                turns = align_asr_segments_to_diarization(asr_segs, job.diarization_segments, job.speaker_labels)
            except json.JSONDecodeError:
                turns = [{"start": 0.0, "hms_start": "00:00:00", "speaker": "说话人", "text": sanitize_transcript_field(raw_out)}]
            if on_progress:
                on_progress(1, 1)

        md, rendered = render_transcript_markdown(
            title=job.title,
            source_name=job.audio_path.name,
            recorded_at=job.recorded_at.isoformat(),
            duration_sec=job.duration_sec,
            engine_label=f"自定义命令 ASR ({self.mode})",
            speakers_summary=job.speakers_summary,
            turns=turns,
            elapsed_sec=time.monotonic() - t0,
        )
        job.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        job.transcript_path.write_text(md, encoding="utf-8")
        if cleanup_clips and (job.job_dir / "clips").is_dir():
            shutil.rmtree(job.job_dir / "clips", ignore_errors=True)
        return TranscriptOutcome(
            mode="direct",
            transcript_path=job.transcript_path,
            workflow_script_path=None,
            segment_count=len(job.segments),
            batch_count=1 if self.mode == "full_audio" else len(job.segments),
            speakers_summary=job.speakers_summary,
            turns=rendered,
            chars=len(md),
        )


def get_transcriber(cfg: VoiceReviewConfig, secrets: SecretStore) -> TranscriberProvider:
    pid = (cfg.transcriber_provider or "antigravity-flash").strip().lower()
    if pid == "antigravity-flash":
        return AntigravityFlashTranscriber(cfg)
    if pid == "gemini-api":
        return GeminiApiTranscriber(cfg, secrets)
    if pid == "openai-compatible-asr":
        return OpenAICompatibleAsrTranscriber(cfg, secrets)
    if pid == "command-asr":
        return CommandAsrTranscriber(cfg)
    raise ValueError(f"未知的转写引擎：{pid}")


def test_transcriber_connection(cfg: VoiceReviewConfig, secrets: SecretStore) -> tuple[bool, str]:
    """轻量检测当前转写插槽配置与网络/本地可达性。"""
    pid = cfg.transcriber_provider
    if pid == "antigravity-flash":
        ok = cfg.agentapi_bin.is_file()
        return (True, f"本机 Antigravity 可用：{cfg.agentapi_bin}") if ok else (False, f"未检测到 Antigravity agentapi：{cfg.agentapi_bin}")
    if pid == "gemini-api":
        key = secrets.get("gemini_api_key", env_var="GEMINI_API_KEY")
        if not key:
            return False, "缺少 Gemini API Key"
        base = str(cfg.transcriber_settings.get("base_url") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        model = str(cfg.transcriber_settings.get("model") or "gemini-2.5-flash")
        req = urllib.request.Request(f"{base}/models/{model}?key={key}", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                return (200 <= resp.status < 300), f"Gemini 模型 {model} 连通正常"
        except Exception as exc:
            return False, f"Gemini 连通测试失败：{exc}"
    if pid == "openai-compatible-asr":
        base = str(cfg.transcriber_settings.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        key = secrets.get("openai_asr_api_key", env_var="OPENAI_API_KEY")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        req = urllib.request.Request(f"{base}/models", headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                return (200 <= resp.status < 300), f"OpenAI 兼容端点连通正常（HTTP {resp.status}）"
        except Exception as exc:
            return False, f"OpenAI 兼容 ASR 连通测试失败：{exc}"
    if pid == "command-asr":
        tr = CommandAsrTranscriber(cfg)
        if not tr.command_template:
            return False, "未配置命令行模板"
        exe = tr.command_template[0]
        found = shutil.which(exe) or (exe if Path(exe).is_file() else None)
        return (True, f"可执行程序就绪：{found}") if found else (False, f"未找到命令：{exe}")
    return False, f"未知转写引擎：{pid}"
