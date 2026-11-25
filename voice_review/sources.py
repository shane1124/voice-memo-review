"""统一多音频输入源抽象：支持苹果语音备忘录（iCloud）与自定义本地投放文件夹（免 iCloud，源级权限隔离 + 元数据缓存）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import logging
from pathlib import Path
import time

from voice_review.voice_memos import (
    AUDIO_EXTENSIONS,
    SHANGHAI_TZ,
    VoiceMemoScanner,
    probe_duration_sec,
)

logger = logging.getLogger(__name__)
DROP_AUDIO_EXTENSIONS = AUDIO_EXTENSIONS | {".ogg", ".mp4", ".webm", ".opus"}


@dataclass(frozen=True)
class AudioCandidate:
    recording_id: str
    filename: str
    audio_path: Path
    title: str
    recorded_at: datetime
    recorded_date: str
    duration_sec: float
    source_kind: str  # "voice_memos" | "drop_folder" | "single_file"


def compute_drop_recording_id(path: Path) -> str:
    """以文件头尾采样 + 大小计算稳定 SHA256 防重 ID（大文件极速，不受文件名改名干扰）。"""
    h = hashlib.sha256()
    st = path.stat()
    h.update(str(st.st_size).encode("ascii"))
    with open(path, "rb") as fh:
        head = fh.read(65536)
        h.update(head)
        if st.st_size > 131072:
            fh.seek(-65536, 2)
            h.update(fh.read(65536))
    return f"drop_{h.hexdigest()[:16]}"


class DropFolderScanner:
    """监听任意本地投放文件夹，不依赖 iCloud，带 (size, mtime) 缓存避免重复起 ffprobe。"""

    def __init__(
        self,
        folder_dir: Path,
        *,
        min_duration_sec: float = 10.0,
        debounce_sec: float = 12.0,
        ffprobe_bin: str = "ffprobe",
    ) -> None:
        self.folder_dir = Path(folder_dir).expanduser()
        self.min_duration_sec = float(min_duration_sec)
        self.debounce_sec = float(debounce_sec)
        self.ffprobe_bin = str(ffprobe_bin)
        self._stability: dict[str, tuple[int, int, float]] = {}
        # 缓存未变化文件的扫描结果，防止对已处理历史音频反复拉起 ffprobe
        self._meta_cache: dict[str, tuple[int, int, AudioCandidate | None]] = {}

    def ensure_dir(self) -> None:
        try:
            self.folder_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def scan_candidates(self) -> list[AudioCandidate]:
        if not self.folder_dir.is_dir():
            return []
        out: list[AudioCandidate] = []
        seen_keys: set[str] = set()
        for entry in sorted(self.folder_dir.iterdir()):
            if not entry.is_file():
                continue
            if entry.name.startswith(".") or entry.suffix.lower() not in DROP_AUDIO_EXTENSIONS:
                continue
            key = str(entry)
            seen_keys.add(key)
            try:
                st = entry.stat()
            except OSError:
                continue
            if st.st_size <= 0:
                continue
            cached = self._meta_cache.get(key)
            if cached is not None and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
                if cached[2] is not None:
                    out.append(cached[2])
                continue
            try:
                rec_id = compute_drop_recording_id(entry)
                recorded_at = datetime.fromtimestamp(st.st_mtime, tz=SHANGHAI_TZ)
            except OSError:
                continue
            dur = probe_duration_sec(entry, self.ffprobe_bin)
            if dur is None or dur < self.min_duration_sec:
                self._meta_cache[key] = (st.st_size, st.st_mtime_ns, None)
                continue
            cand = AudioCandidate(
                recording_id=rec_id,
                filename=entry.name,
                audio_path=entry,
                title=entry.stem,
                recorded_at=recorded_at,
                recorded_date=recorded_at.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d"),
                duration_sec=dur,
                source_kind="drop_folder",
            )
            self._meta_cache[key] = (st.st_size, st.st_mtime_ns, cand)
            out.append(cand)
        for stale in list(self._meta_cache):
            if stale not in seen_keys:
                self._meta_cache.pop(stale, None)
        return out

    def is_file_stable(self, path: Path, *, now_mono: float | None = None) -> bool:
        now = time.monotonic() if now_mono is None else now_mono
        try:
            st = path.stat()
        except OSError:
            self._stability.pop(str(path), None)
            return False
        if st.st_size <= 0:
            return False
        key = str(path)
        sig = (st.st_size, st.st_mtime_ns)
        prev = self._stability.get(key)
        if prev is None or (prev[0], prev[1]) != sig:
            self._stability[key] = (sig[0], sig[1], now)
            return self.debounce_sec <= 0
        return (now - prev[2]) >= self.debounce_sec


class MultiSourceScanner:
    """按配置同时聚合苹果语音备忘录与自定义投放文件夹，各源独立异常隔离。"""

    def __init__(
        self,
        *,
        voice_memos_enabled: bool,
        recordings_dir: Path,
        drop_folder_enabled: bool,
        drop_folder_dir: Path,
        import_since: datetime,
        min_duration_sec: float = 10.0,
        debounce_sec: float = 12.0,
        ffprobe_bin: str = "ffprobe",
    ) -> None:
        self.voice_memos_enabled = voice_memos_enabled
        self.drop_folder_enabled = drop_folder_enabled
        self._warned_perm: set[str] = set()
        self.vm_scanner = (
            VoiceMemoScanner(
                recordings_dir=recordings_dir,
                import_since=import_since,
                min_duration_sec=min_duration_sec,
                debounce_sec=debounce_sec,
                ffprobe_bin=ffprobe_bin,
            )
            if voice_memos_enabled
            else None
        )
        self.drop_scanner = (
            DropFolderScanner(
                drop_folder_dir,
                min_duration_sec=min_duration_sec,
                debounce_sec=debounce_sec,
                ffprobe_bin=ffprobe_bin,
            )
            if drop_folder_enabled
            else None
        )
        if self.drop_scanner is not None:
            self.drop_scanner.ensure_dir()

    def scan_candidates(self) -> list[AudioCandidate]:
        out: list[AudioCandidate] = []
        if self.vm_scanner is not None:
            try:
                for c in self.vm_scanner.scan_candidates():
                    out.append(
                        AudioCandidate(
                            recording_id=c.recording_id,
                            filename=c.filename,
                            audio_path=c.audio_path,
                            title=c.title,
                            recorded_at=c.recorded_at,
                            recorded_date=c.recorded_date,
                            duration_sec=c.duration_sec,
                            source_kind="voice_memos",
                        )
                    )
                self._warned_perm.discard("voice_memos")
            except PermissionError:
                if "voice_memos" not in self._warned_perm:
                    self._warned_perm.add("voice_memos")
                    logger.warning("暂无苹果语音备忘录目录访问权限（需在系统设置授予完全磁盘访问权限），已隔离跳过，不影响投放文件夹与手动上传。")
            except OSError as exc:
                logger.warning("扫描语音备忘录目录异常（已隔离）：%s", exc)

        if self.drop_scanner is not None:
            try:
                out.extend(self.drop_scanner.scan_candidates())
                self._warned_perm.discard("drop_folder")
            except PermissionError:
                if "drop_folder" not in self._warned_perm:
                    self._warned_perm.add("drop_folder")
                    logger.warning("暂无投放文件夹访问权限（%s），已隔离跳过。", self.drop_scanner.folder_dir)
            except OSError as exc:
                logger.warning("扫描投放文件夹异常（已隔离）：%s", exc)
        return out

    def is_file_stable(self, cand: AudioCandidate, *, now_mono: float | None = None) -> bool:
        if cand.source_kind == "voice_memos" and self.vm_scanner is not None:
            return self.vm_scanner.is_file_stable(cand.audio_path, now_mono=now_mono)
        if cand.source_kind == "drop_folder" and self.drop_scanner is not None:
            return self.drop_scanner.is_file_stable(cand.audio_path, now_mono=now_mono)
        return cand.audio_path.is_file() and cand.audio_path.stat().st_size > 0

    def watch_paths(self) -> list[Path]:
        paths: list[Path] = []
        if self.vm_scanner is not None:
            base = self.vm_scanner.recordings_dir
            paths.extend([base, base / "CloudRecordings.db", base / "CloudRecordings.db-wal"])
        if self.drop_scanner is not None:
            paths.append(self.drop_scanner.folder_dir)
        return paths
