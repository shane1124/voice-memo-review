"""守护运行器兼容封装（委托统一单守护 VmrDaemon 与四插槽 PipelineExecutor）。"""
from __future__ import annotations

from voice_review.config import VoiceReviewConfig
from voice_review.daemon import PipelineExecutor, VmrDaemon
from voice_review.state import RecordingState, StateStore


class VoiceReviewRunner:
    """保持与既有 `VoiceReviewRunner(cfg).run_forever()` 入口向后兼容，内部统一走单守护架构。"""

    def __init__(self, cfg: VoiceReviewConfig) -> None:
        self.cfg = cfg
        self.store = StateStore(cfg.state_db)
        self.executor = PipelineExecutor(cfg, store=self.store)
        self._daemon = VmrDaemon(cfg, start_watcher=True)

    def request_stop(self) -> None:
        self._daemon._stop_event.set()

    def process_one(self, rec: RecordingState) -> str:
        return self.executor.process_one(rec, auto_analyze=True)

    def run_forever(self) -> None:
        self._daemon.serve_forever()
