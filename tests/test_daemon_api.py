"""Task 6 单元测试：单守护进程锁、127.0.0.1 Token/Origin 防护、密钥脱敏、单文件任务队列与 Prompt 预览。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from voice_review.analyzers import AnalysisOutcome
from voice_review.config import load_config, save_config_dict
from voice_review.daemon import (
    DaemonAlreadyRunningError,
    VmrDaemon,
    discover_running_daemon,
)
from voice_review.transcribers import PreparedAudioJob, TranscriptOutcome


def _req(url: str, *, method: str = "GET", body: dict | None = None, token: str = "", origin: str = ""):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["X-VMR-Token"] = token
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


class TestDaemonApi(unittest.TestCase):
    def test_daemon_binds_loopback_enforces_token_and_masks_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            cfg = load_config(vmr_home=tmp)
            daemon = VmrDaemon(cfg, port=0, start_watcher=False)
            daemon.start_in_thread()
            try:
                base = f"http://127.0.0.1:{daemon.port}"
                # daemon.json 权限应为 0600
                info_p = tmp / "daemon.json"
                self.assertEqual(stat.S_IMODE(os.stat(info_p).st_mode), 0o600)

                # 无 Token 写配置 -> 403
                with self.assertRaises(urllib.error.HTTPError) as ctx1:
                    _req(f"{base}/api/v1/config", method="PUT", body={"ui": {"port": 18765}})
                self.assertEqual(ctx1.exception.code, 403)

                # 非法跨站 Origin 即使带 Token -> 403
                with self.assertRaises(urllib.error.HTTPError) as ctx2:
                    _req(
                        f"{base}/api/v1/config",
                        method="PUT",
                        body={"ui": {"port": 18765}},
                        token=daemon.token,
                        origin="https://evil.example.com",
                    )
                self.assertEqual(ctx2.exception.code, 403)

                # 正确 Token 保存配置与密钥，再读取验证脱敏
                st, res = _req(
                    f"{base}/api/v1/config",
                    method="PUT",
                    body={
                        "transcriber": {"provider": "gemini-api", "model": "gemini-2.5-flash"},
                        "analyzer": {"auto_provider": "none"},
                        "secrets": {"gemini_api_key": "AIzaTopSecret9876"},
                    },
                    token=daemon.token,
                )
                self.assertEqual(st, 200)
                self.assertTrue(res["ok"])

                _, got = _req(f"{base}/api/v1/config", token=daemon.token)
                masked = got["secrets"]["gemini_api_key"]
                self.assertNotIn("TopSecret", masked)
                self.assertTrue(masked.endswith("9876"))

                # 不兼容组合应返回 400
                with self.assertRaises(urllib.error.HTTPError) as ctx3:
                    _req(
                        f"{base}/api/v1/config",
                        method="PUT",
                        body={
                            "transcriber": {"provider": "antigravity-flash"},
                            "analyzer": {"auto_provider": "openai-compatible-llm"},
                        },
                        token=daemon.token,
                    )
                self.assertEqual(ctx3.exception.code, 400)
            finally:
                daemon.stop()

    def test_daemon_lock_prevents_duplicate_and_discover_reuses_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            cfg = load_config(vmr_home=tmp)
            d1 = VmrDaemon(cfg, port=0, start_watcher=False)
            d1.start_in_thread()
            try:
                found = discover_running_daemon(tmp)
                self.assertIsNotNone(found)
                self.assertEqual(found.port, d1.port)
                self.assertEqual(found.token, d1.token)

                d2 = VmrDaemon(cfg, port=0, start_watcher=False)
                with self.assertRaises(DaemonAlreadyRunningError):
                    d2.start_in_thread()
            finally:
                d1.stop()

    def test_daemon_submits_job_awaiting_analysis_then_manual_analyze_and_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            cfg_p = tmp / "config.toml"
            save_config_dict(cfg_p, {
                "output_dir": str(tmp / "out"),
                "preprocessor": {"mode": "none"},
                "transcriber": {"provider": "gemini-api"},
                "analyzer": {"auto_provider": "none"},
            })
            cfg = load_config(cfg_p, vmr_home=tmp)
            audio = tmp / "demo.m4a"
            audio.write_bytes(b"fake-audio-data")

            def _fake_prep(audio_path, *, cfg, job_dir, transcript_path, title="", recorded_at=None, asr_context="", **kw):
                return PreparedAudioJob(
                    job_dir=job_dir,
                    audio_path=Path(audio_path),
                    transcript_path=Path(transcript_path),
                    title=title or "demo",
                    recorded_at=recorded_at,
                    speakers_summary="- SPEAKER_00 -> 测试人",
                    segments=[],
                    batches=[],
                    duration_sec=30.0,
                )

            class _FakeTr:
                def transcribe(self, job, *, on_progress=None, cleanup_clips=True):
                    job.transcript_path.parent.mkdir(parents=True, exist_ok=True)
                    job.transcript_path.write_text("## 00:00:01 **测试人**\n测试转录正文", encoding="utf-8")
                    if on_progress:
                        on_progress(1, 1)
                    return TranscriptOutcome("direct", job.transcript_path, None, 1, 1, job.speakers_summary, 1, 20)

            class _FakeAna:
                def analyze(self, req):
                    req.analysis_path.parent.mkdir(parents=True, exist_ok=True)
                    req.analysis_path.write_text("# 复盘\n分析完成", encoding="utf-8")
                    return AnalysisOutcome(True, "done", req.analysis_path)

            daemon = VmrDaemon(cfg, port=0, start_watcher=False)
            daemon.start_in_thread()
            try:
                base = f"http://127.0.0.1:{daemon.port}"
                with patch("voice_review.daemon.prepare_audio_job", side_effect=_fake_prep), \
                     patch("voice_review.daemon.get_transcriber", return_value=_FakeTr()), \
                     patch("voice_review.daemon.probe_duration_sec", return_value=30.0):
                    st, created = _req(
                        f"{base}/api/v1/jobs/submit",
                        method="POST",
                        body={"audio_path": str(audio), "template_id": "general_review"},
                        token=daemon.token,
                    )
                    self.assertEqual(st, 200)
                    rec_id = created["recording_id"]

                    # 轮询直至进入 awaiting_analysis
                    final_status = ""
                    for _ in range(40):
                        _, jobs_resp = _req(f"{base}/api/v1/jobs", token=daemon.token)
                        rec = next((x for x in jobs_resp["jobs"] if x["recording_id"] == rec_id), None)
                        if rec and rec["status"] == "awaiting_analysis":
                            final_status = rec["status"]
                            break
                        time.sleep(0.05)
                    self.assertEqual(final_status, "awaiting_analysis")

                    _, content = _req(f"{base}/api/v1/jobs/{rec_id}/content", token=daemon.token)
                    self.assertIn("测试转录正文", content["transcript"])

                # 手动触发分析（显式指定外部模拟分析器）
                with patch("voice_review.daemon.get_analyzer", return_value=_FakeAna()):
                    st2, ana_res = _req(
                        f"{base}/api/v1/jobs/{rec_id}/analyze",
                        method="POST",
                        body={"template_id": "meeting_minutes", "provider": "openai-compatible-llm"},
                        token=daemon.token,
                    )
                    self.assertEqual(st2, 200)
                    self.assertEqual(ana_res["status"], "done")

                # Prompt 预览接口
                _, prev = _req(
                    f"{base}/api/v1/templates/preview",
                    method="POST",
                    body={"template_id": "sales_communication", "title": "演示"},
                    token=daemon.token,
                )
                self.assertIn("客户画像", prev["prompt"])
            finally:
                daemon.stop()


if __name__ == "__main__":
    unittest.main()
