"""Task 5 单元测试：升级状态机自动迁移、awaiting_analysis 暂停态、超长稿分块汇总、Gemini/Antigravity 分析器。"""
from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from voice_review.agent_bridge import compute_dynamic_timeout_sec, render_v2_review_prompt
from voice_review.analyzers import AnalysisRequest, get_analyzer
from voice_review.config import load_config, save_config_dict
from voice_review.rules import RuleStudio
from voice_review.secrets import SecretStore
from voice_review.state import StateStore


def _fake_http_resp(payload: dict):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    class _Resp:
        status = 200

        def read(self) -> bytes:
            return raw

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return _Resp()


class TestAnalyzersAndState(unittest.TestCase):
    def test_state_store_migrates_legacy_db_and_respects_awaiting_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_p = Path(tmp) / "state.sqlite"
            # 先手工构造旧版 12 列 recordings 表
            conn = sqlite3.connect(str(db_p))
            conn.execute(
                """
                CREATE TABLE recordings (
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
                "INSERT INTO recordings VALUES ('old1','a.m4a','旧录音','2026-11-20T10:00:00+08:00','2026-11-20',120,'awaiting_analysis','/t.md','','','',1000)"
            )
            conn.commit()
            conn.close()

            store = StateStore(db_p)
            old_rec = store.get("old1")
            self.assertIsNotNone(old_rec)
            self.assertEqual(old_rec.status, "awaiting_analysis")
            # awaiting_analysis 属于合法暂停态，绝不被 next_pending 重复拉取
            self.assertIsNone(store.next_pending())

            # 测试失败退避：前 2 次回到 discovered 待重试，第 3 次进入终态 failed
            store.upsert_discovered(
                recording_id="new1",
                filename="b.m4a",
                title="新录音",
                recorded_at="2026-11-25T10:00:00+08:00",
                recorded_date="2026-11-25",
                duration_sec=60.0,
                source_kind="drop_folder",
                audio_path="/tmp/b.m4a",
            )
            s1 = store.record_failure("new1", "err1", max_retries=3, retry_stage="discovered", now_ts=1000.0)
            self.assertEqual(s1, "discovered")
            self.assertIsNone(store.next_pending(now_ts=1005.0))  # 处于退避期
            self.assertIsNotNone(store.next_pending(now_ts=1200.0))  # 退避期过恢复可拉取
            store.record_failure("new1", "err2", max_retries=3, retry_stage="discovered", now_ts=1200.0)
            s3 = store.record_failure("new1", "err3", max_retries=3, retry_stage="discovered", now_ts=1500.0)
            self.assertEqual(s3, "failed")
            self.assertIsNone(store.next_pending(now_ts=9999.0))

    def test_openai_llm_analyzer_chunks_long_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            cfg_p = tmp / "config.toml"
            save_config_dict(cfg_p, {
                "transcriber": {"provider": "openai-compatible-asr"},
                "analyzer": {
                    "auto_provider": "openai-compatible-llm",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                },
            })
            cfg = load_config(cfg_p, vmr_home=tmp)
            secrets = SecretStore(cfg.secrets_path)
            secrets.set("openai_llm_api_key", "sk-llm-7777")
            rules = RuleStudio(cfg.rules_dir)
            rules.ensure_initialized()
            store = StateStore(cfg.state_db)

            t_path = tmp / "long_transcript.md"
            # 构造 170,000 字超长转录稿（超过 80,000 字阈值应切成 3 块 + 1 次汇总）
            line = "## 00:01:00 **张三**\n今天讨论核心架构升级与项目节奏安排细节记录。\n\n"
            t_path.write_text(line * 4500, encoding="utf-8")
            a_path = tmp / "out" / "analysis.md"

            calls = {"n": 0}

            def _mock_open(req, timeout=180):
                calls["n"] += 1
                return _fake_http_resp({"choices": [{"message": {"content": f"第{calls['n']}次分析结果"}}]})

            analyzer = get_analyzer(cfg, secrets, store, rules)
            with patch("voice_review.analyzers.urllib.request.urlopen", side_effect=_mock_open):
                res = analyzer.analyze(
                    AnalysisRequest(
                        recording_id="r1",
                        title="超长会议",
                        recorded_at="2026-11-25T10:00:00+08:00",
                        duration_sec=7200.0,
                        speakers_summary="- SPEAKER_00 -> 张三",
                        transcript_path=t_path,
                        analysis_path=a_path,
                        template_id="general_review",
                    )
                )
            self.assertTrue(res.ok)
            self.assertGreaterEqual(calls["n"], 3)
            self.assertTrue(a_path.is_file())
            self.assertIn("分析结果", a_path.read_text(encoding="utf-8"))

    def test_gemini_llm_analyzer_writes_analysis_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            cfg_p = tmp / "config.toml"
            save_config_dict(cfg_p, {
                "transcriber": {"provider": "gemini-api"},
                "analyzer": {"auto_provider": "gemini-llm", "model": "gemini-2.5-pro"},
            })
            cfg = load_config(cfg_p, vmr_home=tmp)
            secrets = SecretStore(cfg.secrets_path)
            secrets.set("gemini_api_key", "AIzaKey8888")
            rules = RuleStudio(cfg.rules_dir)
            store = StateStore(cfg.state_db)
            t_path = tmp / "t.md"
            t_path.write_text("## 00:00:01 **张三**\n确定周五发版。", encoding="utf-8")
            a_path = tmp / "out" / "a.md"

            analyzer = get_analyzer(cfg, secrets, store, rules)
            with patch(
                "voice_review.analyzers.urllib.request.urlopen",
                return_value=_fake_http_resp({"candidates": [{"content": {"parts": [{"text": "# 复盘\n确认周五发版"}]}}]}),
            ):
                res = analyzer.analyze(
                    AnalysisRequest(
                        recording_id="r2",
                        title="发版确认",
                        recorded_at="2026-11-25T10:00:00+08:00",
                        duration_sec=120.0,
                        speakers_summary="- SPEAKER_00 -> 张三",
                        transcript_path=t_path,
                        analysis_path=a_path,
                        template_id="meeting_minutes",
                    )
                )
            self.assertTrue(res.ok)
            self.assertIn("确认周五发版", a_path.read_text(encoding="utf-8"))

    def test_antigravity_analyzer_dynamic_timeout_and_honest_failure(self) -> None:
        self.assertGreaterEqual(compute_dynamic_timeout_sec(900.0, batch_count=40), 2400.0)
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            cfg = load_config(vmr_home=tmp)
            prompt = render_v2_review_prompt(
                cfg,
                title="测试",
                recorded_at="2026-11-25T10:00:00+08:00",
                duration_sec=300.0,
                speakers_summary="- SPEAKER_00",
                workflow_script_path=tmp / "wf.py",
                transcript_path=tmp / "t.md",
                analysis_path=tmp / "a.md",
                batch_count=2,
                segment_count=10,
                rules=RuleStudio(cfg.rules_dir),
                template_id="general_review",
            )
            self.assertIn("audio_transcriber_lite", prompt)
            self.assertIn("define_subagent", prompt)

            secrets = SecretStore(cfg.secrets_path)
            rules = RuleStudio(cfg.rules_dir)
            store = StateStore(cfg.state_db)
            analyzer = get_analyzer(cfg, secrets, store, rules)
            with patch.object(analyzer.bridge, "deliver", return_value="cid-1"), \
                 patch.object(analyzer.bridge, "wait_for_file", return_value=False):
                res = analyzer.analyze(
                    AnalysisRequest(
                        recording_id="r3",
                        title="测试超时",
                        recorded_at="2026-11-25T10:00:00+08:00",
                        duration_sec=300.0,
                        speakers_summary="",
                        transcript_path=tmp / "t.md",
                        analysis_path=tmp / "missing_a.md",
                        workflow_script_path=tmp / "wf.py",
                        batch_count=2,
                        segment_count=10,
                    )
                )
            # 绝不误标成功
            self.assertFalse(res.ok)
            self.assertIn("超时", res.error)


if __name__ == "__main__":
    unittest.main()
