"""macOS 苹果语音备忘录扫描、元数据解析、只读 WAL 兼容查询与写稳防抖。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from zoneinfo import ZoneInfo

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
FILENAME_TS_RE = re.compile(r"^(\d{8})\s+(\d{6})-([A-Za-z0-9]+)$")
UNSAFE_PATH_CHARS_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".aac", ".flac"}


@dataclass(frozen=True)
class MemoCandidate:
    recording_id: str
    filename: str
    audio_path: Path
    title: str
    recorded_at: datetime
    recorded_date: str
    duration_sec: float


def sanitize_filename_stem(recorded_at: datetime, title: str, recording_id: str) -> str:
    local_dt = recorded_at.astimezone(SHANGHAI_TZ)
    hhmm = local_dt.strftime("%H%M")
    clean_title = UNSAFE_PATH_CHARS_RE.sub("_", (title or "").strip()).strip(" ._-")
    if not clean_title:
        clean_title = "未命名录音"
    clean_title = clean_title[:48].rstrip(" ._-") or "未命名录音"
    short_id = re.sub(r"[^A-Za-z0-9]", "", recording_id)[:6].upper() or "MEMO"
    return f"{hhmm}_{clean_title}_{short_id}"


def parse_filename_metadata(stem: str) -> tuple[datetime | None, str]:
    m = FILENAME_TS_RE.match(stem)
    if not m:
        return None, stem
    ymd, hms, short_id = m.groups()
    try:
        dt = datetime.strptime(f"{ymd}{hms}", "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI_TZ)
    except ValueError:
        return None, short_id
    return dt, short_id


def read_apple_memo_db(recordings_dir: Path) -> dict[str, tuple[str, datetime | None, float | None]]:
    """只读方式查询 CloudRecordings.db，返回 {basename: (title, recorded_at, duration_sec)}。"""
    db_path = recordings_dir / "CloudRecordings.db"
    if not db_path.is_file():
        return {}
    uri = f"file:{db_path}?mode=ro&immutable=0"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        cur = conn.cursor()
        cur.execute("SELECT ZPATH, ZCUSTOMLABEL, ZCUSTOMLABELFORSORTING, ZDATE, ZDURATION FROM ZCLOUDRECORDING")
        rows = cur.fetchall()
    except sqlite3.Error:
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    mapping: dict[str, tuple[str, datetime | None, float | None]] = {}
    for zpath, label, label_sort, zdate, zduration in rows:
        if not zpath:
            continue
        basename = Path(str(zpath)).name
        title = str(label or label_sort or "").strip()
        dt: datetime | None = None
        if isinstance(zdate, (int, float)):
            try:
                dt = (APPLE_EPOCH + timedelta(seconds=float(zdate))).astimezone(SHANGHAI_TZ)
            except (OverflowError, ValueError):
                dt = None
        dur = float(zduration) if isinstance(zduration, (int, float)) and zduration > 0 else None
        mapping[basename] = (title, dt, dur)
    return mapping


def probe_duration_sec(audio_path: Path, ffprobe_bin: str = "ffprobe") -> float | None:
    try:
        res = subprocess.run(
            [
                str(ffprobe_bin),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    try:
        val = float((res.stdout or "").strip())
        return val if val > 0 else None
    except ValueError:
        return None


class VoiceMemoScanner:
    """扫描苹果语音备忘录目录，过滤旧录音、短误触，并对 iCloud 同步写入做防抖。"""

    def __init__(
        self,
        *,
        recordings_dir: Path,
        import_since: datetime,
        min_duration_sec: float = 10.0,
        debounce_sec: float = 12.0,
        ffprobe_bin: str = "ffprobe",
    ) -> None:
        self.recordings_dir = Path(recordings_dir).expanduser()
        self.import_since = import_since.astimezone(SHANGHAI_TZ) if import_since.tzinfo else import_since.replace(tzinfo=SHANGHAI_TZ)
        self.min_duration_sec = float(min_duration_sec)
        self.debounce_sec = float(debounce_sec)
        self.ffprobe_bin = str(ffprobe_bin)
        self._stability: dict[str, tuple[int, int, float]] = {}

    def scan_candidates(self, *, force: bool = False) -> list[MemoCandidate]:
        if not self.recordings_dir.is_dir():
            return []
        db_meta = read_apple_memo_db(self.recordings_dir)
        out: list[MemoCandidate] = []
        for entry in sorted(self.recordings_dir.iterdir()):
            if not entry.is_file() or entry.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            if entry.name.startswith("."):
                continue
            fn_dt, short_id = parse_filename_metadata(entry.stem)
            db_title, db_dt, db_dur = db_meta.get(entry.name, ("", None, None))
            recorded_at = db_dt or fn_dt
            if recorded_at is None:
                try:
                    recorded_at = datetime.fromtimestamp(entry.stat().st_mtime, tz=SHANGHAI_TZ)
                except OSError:
                    continue
            if not force and recorded_at < self.import_since:
                continue
            dur = db_dur if (db_dur is not None and db_dur > 0) else probe_duration_sec(entry, self.ffprobe_bin)
            if dur is None or dur < self.min_duration_sec:
                continue
            title = db_title or entry.stem
            rec_id = f"{entry.stem}"
            out.append(
                MemoCandidate(
                    recording_id=rec_id,
                    filename=entry.name,
                    audio_path=entry,
                    title=title,
                    recorded_at=recorded_at,
                    recorded_date=recorded_at.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d"),
                    duration_sec=dur,
                )
            )
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
