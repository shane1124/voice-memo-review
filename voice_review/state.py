"""轻量 SQLite 状态存储（自动兼容旧库迁移、支持 awaiting_analysis/skipped、退避重试与包级进度）。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time
from typing import Iterator


@dataclass(frozen=True)
class RecordingState:
    recording_id: str
    filename: str
    title: str
    recorded_at: str
    recorded_date: str
    duration_sec: float
    status: str
    transcript_path: str
    analysis_path: str
    workflow_script_path: str
    last_error: str
    source_kind: str = "voice_memos"
    audio_path: str = ""
    template_id: str = ""
    speakers_summary: str = ""
    retry_count: int = 0
    next_retry_at: float = 0.0
    progress_done: int = 0
    progress_total: int = 0
    batch_count: int = 0
    segment_count: int = 0
    auto_analyze: bool = True
    chat_url: str = ""

    def to_dict(self) -> dict:
        return {
            "recording_id": self.recording_id,
            "filename": self.filename,
            "title": self.title,
            "recorded_at": self.recorded_at,
            "recorded_date": self.recorded_date,
            "duration_sec": self.duration_sec,
            "status": self.status,
            "transcript_path": self.transcript_path,
            "analysis_path": self.analysis_path,
            "workflow_script_path": self.workflow_script_path,
            "last_error": self.last_error,
            "source_kind": self.source_kind,
            "audio_path": self.audio_path,
            "template_id": self.template_id,
            "speakers_summary": self.speakers_summary,
            "retry_count": self.retry_count,
            "next_retry_at": self.next_retry_at,
            "progress_done": self.progress_done,
            "progress_total": self.progress_total,
            "batch_count": self.batch_count,
            "segment_count": self.segment_count,
            "chat_url": self.chat_url,
        }


_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("source_kind", "TEXT DEFAULT 'voice_memos'"),
    ("audio_path", "TEXT DEFAULT ''"),
    ("template_id", "TEXT DEFAULT ''"),
    ("speakers_summary", "TEXT DEFAULT ''"),
    ("retry_count", "INTEGER DEFAULT 0"),
    ("next_retry_at", "REAL DEFAULT 0"),
    ("progress_done", "INTEGER DEFAULT 0"),
    ("progress_total", "INTEGER DEFAULT 0"),
    ("batch_count", "INTEGER DEFAULT 0"),
    ("segment_count", "INTEGER DEFAULT 0"),
    ("auto_analyze", "INTEGER DEFAULT 1"),
    ("chat_url", "TEXT DEFAULT ''"),
)

_RETRY_BACKOFFS = (30.0, 120.0, 300.0)


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recordings (
                    recording_id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    title TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    recorded_date TEXT NOT NULL,
                    duration_sec REAL NOT NULL,
                    status TEXT NOT NULL,
                    transcript_path TEXT DEFAULT '',
                    analysis_path TEXT DEFAULT '',
                    workflow_script_path TEXT DEFAULT '',
                    last_error TEXT DEFAULT '',
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(recordings)").fetchall()}
            for col_name, col_def in _EXTRA_COLUMNS:
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE recordings ADD COLUMN {col_name} {col_def}")

    @staticmethod
    def _row_to_state(row: sqlite3.Row) -> RecordingState:
        keys = set(row.keys())
        return RecordingState(
            recording_id=str(row["recording_id"]),
            filename=str(row["filename"]),
            title=str(row["title"]),
            recorded_at=str(row["recorded_at"]),
            recorded_date=str(row["recorded_date"]),
            duration_sec=float(row["duration_sec"] or 0.0),
            status=str(row["status"]),
            transcript_path=str(row["transcript_path"] or ""),
            analysis_path=str(row["analysis_path"] or ""),
            workflow_script_path=str(row["workflow_script_path"] or ""),
            last_error=str(row["last_error"] or ""),
            source_kind=str(row["source_kind"] or "voice_memos") if "source_kind" in keys else "voice_memos",
            audio_path=str(row["audio_path"] or "") if "audio_path" in keys else "",
            template_id=str(row["template_id"] or "") if "template_id" in keys else "",
            speakers_summary=str(row["speakers_summary"] or "") if "speakers_summary" in keys else "",
            retry_count=int(row["retry_count"] or 0) if "retry_count" in keys else 0,
            next_retry_at=float(row["next_retry_at"] or 0.0) if "next_retry_at" in keys else 0.0,
            progress_done=int(row["progress_done"] or 0) if "progress_done" in keys else 0,
            progress_total=int(row["progress_total"] or 0) if "progress_total" in keys else 0,
            batch_count=int(row["batch_count"] or 0) if "batch_count" in keys else 0,
            segment_count=int(row["segment_count"] or 0) if "segment_count" in keys else 0,
            auto_analyze=bool(row["auto_analyze"]) if ("auto_analyze" in keys and row["auto_analyze"] is not None) else True,
            chat_url=str(row["chat_url"] or "") if "chat_url" in keys else "",
        )

    def get(self, recording_id: str) -> RecordingState | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM recordings WHERE recording_id = ?", (recording_id,)).fetchone()
        return self._row_to_state(row) if row else None

    def upsert_discovered(
        self,
        *,
        recording_id: str,
        filename: str,
        title: str,
        recorded_at: str,
        recorded_date: str,
        duration_sec: float,
        source_kind: str = "voice_memos",
        audio_path: str = "",
        template_id: str = "",
        auto_analyze: bool = True,
    ) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO recordings
                (recording_id, filename, title, recorded_at, recorded_date, duration_sec, status,
                 source_kind, audio_path, template_id, auto_analyze, retry_count, next_retry_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'discovered', ?, ?, ?, ?, 0, 0, ?)
                """,
                (
                    recording_id,
                    filename,
                    title,
                    recorded_at,
                    recorded_date,
                    duration_sec,
                    source_kind,
                    audio_path,
                    template_id,
                    1 if auto_analyze else 0,
                    time.time(),
                ),
            )
            return cur.rowcount > 0

    def update(
        self,
        recording_id: str,
        *,
        status: str,
        transcript_path: str | None = None,
        analysis_path: str | None = None,
        workflow_script_path: str | None = None,
        last_error: str | None = None,
        speakers_summary: str | None = None,
        template_id: str | None = None,
        batch_count: int | None = None,
        segment_count: int | None = None,
        chat_url: str | None = None,
        reset_retry: bool = False,
    ) -> None:
        fields = ["status = ?", "updated_at = ?"]
        vals: list = [status, time.time()]
        optional = {
            "transcript_path": transcript_path,
            "analysis_path": analysis_path,
            "workflow_script_path": workflow_script_path,
            "last_error": last_error,
            "speakers_summary": speakers_summary,
            "template_id": template_id,
            "batch_count": batch_count,
            "segment_count": segment_count,
            "chat_url": chat_url,
        }
        for col, val in optional.items():
            if val is not None:
                fields.append(f"{col} = ?")
                vals.append(val)
        if reset_retry:
            fields.extend(["retry_count = 0", "next_retry_at = 0"])
        vals.append(recording_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE recordings SET {', '.join(fields)} WHERE recording_id = ?", vals)

    def update_progress(self, recording_id: str, done: int, total: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE recordings SET progress_done = ?, progress_total = ?, updated_at = ? WHERE recording_id = ?",
                (max(0, int(done)), max(0, int(total)), time.time(), recording_id),
            )

    def record_failure(
        self,
        recording_id: str,
        error: str,
        *,
        max_retries: int = 3,
        retry_stage: str = "discovered",
        now_ts: float | None = None,
    ) -> str:
        ts = time.time() if now_ts is None else float(now_ts)
        rec = self.get(recording_id)
        next_count = (rec.retry_count if rec else 0) + 1
        if next_count < max_retries:
            backoff = _RETRY_BACKOFFS[min(next_count - 1, len(_RETRY_BACKOFFS) - 1)]
            new_status = retry_stage
            next_retry_at = ts + backoff
        else:
            new_status = "failed"
            next_retry_at = 0.0
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE recordings
                SET status = ?, last_error = ?, retry_count = ?, next_retry_at = ?, updated_at = ?
                WHERE recording_id = ?
                """,
                (new_status, (error or "")[:800], next_count, next_retry_at, ts, recording_id),
            )
        return new_status

    def reset_in_flight(self) -> int:
        """按中断阶段精准恢复：analyzing 保留既有转录稿回退至 transcribed/prepared，不重跑整条转写。"""
        now = time.time()
        with self._connect() as conn:
            c1 = conn.execute(
                """
                UPDATE recordings
                SET status = CASE
                    WHEN transcript_path IS NOT NULL AND transcript_path != '' THEN 'transcribed'
                    WHEN workflow_script_path IS NOT NULL AND workflow_script_path != '' THEN 'prepared'
                    ELSE 'discovered'
                END,
                updated_at = ?
                WHERE status = 'analyzing'
                """,
                (now,),
            ).rowcount
            c2 = conn.execute(
                """
                UPDATE recordings
                SET status = 'discovered', updated_at = ?
                WHERE status IN ('preparing', 'transcribing')
                """,
                (now,),
            ).rowcount
            return c1 + c2

    def next_pending(self, *, now_ts: float | None = None) -> RecordingState | None:
        ts = time.time() if now_ts is None else float(now_ts)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM recordings
                WHERE status IN ('discovered', 'prepared', 'sent')
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY recorded_at ASC
                LIMIT 1
                """,
                (ts,),
            ).fetchone()
        return self._row_to_state(row) if row else None

    def list_recent(self, *, limit: int = 50, status: str | None = None) -> list[RecordingState]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM recordings WHERE status = ? ORDER BY recorded_at DESC LIMIT ?",
                    (status, max(1, int(limit))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM recordings ORDER BY recorded_at DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
        return [self._row_to_state(r) for r in rows]

    def get_meta(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
