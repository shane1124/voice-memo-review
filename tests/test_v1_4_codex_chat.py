"""v1.4.0 单元测试：Codex CLI 原生自动开对话/分桶续聊/自愈/ephemeral 分段/.webloc 与状态库 chat_url。"""
from __future__ import annotations

import json
import plistlib
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from voice_review.analyzers import AnalysisRequest, CodexCliAnalyzer
from voice_review.config import load_config, save_config_dict
from voice_review.rules import RuleStudio
from voice_review.state import StateStore


def _make_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class TestV14CodexDesktopChat(unittest.TestCase):
    def test_codex_analyzer_creates_thread_resumes_bucket_writes_webloc_and_opens_desktop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_file = root / "invocations.log"
            tid1 = "01a2138e-a2c4-7143-b58b-bc114bca1c56"
            fake_codex = _make_executable(
                root / "fake_codex.sh",
                f"""#!/usr/bin/env bash
set -euo pipefail
echo "ARGS:$*" >> "{log_file}"
OUT=""
RESUME_ID=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    resume) RESUME_ID="$2"; shift 2 ;;
    *) shift ;;
  esac
done
cat >/dev/null
TID="${{RESUME_ID:-{tid1}}}"
printf '{{"type":"thread.started","thread_id":"%s"}}\\n' "$TID"
printf '# Codex 自动分析正文\\n\\n会话：%s\\n' "$TID" > "$OUT"
""",
            )

            cfg_path = root / "config.toml"
            save_config_dict(
                cfg_path,
                {
                    "output_dir": str(root / "output"),
                    "rotation": "daily",
                    "codex_bin": str(fake_codex),
                    "transcriber": {"provider": "command-asr"},
                    "analyzer": {"auto_provider": "codex-cli", "open_desktop_chat": True},
                },
            )
            cfg = load_config(cfg_path, vmr_home=root)
            store = StateStore(cfg.state_db)
            rules = RuleStudio(cfg.rules_dir)
            rules.ensure_initialized()
            analyzer = CodexCliAnalyzer(cfg, rules, store=store)

            t1 = root / "t1.md"
            t1.write_text("[00:00:01] 我：第一条测试。", encoding="utf-8")
            a1 = root / "output" / "a1.md"

            opened_urls: list[str] = []
            with mock.patch("voice_review.analyzers._open_codex_desktop_url", side_effect=lambda u: opened_urls.append(u) or True):
                out1 = analyzer.analyze(
                    AnalysisRequest(
                        recording_id="rec-1",
                        title="晨会沟通",
                        recorded_at="2026-11-26T09:00:00+08:00",
                        duration_sec=60,
                        speakers_summary="SPEAKER_00 -> 我",
                        transcript_path=t1,
                        analysis_path=a1,
                    )
                )
                self.assertTrue(out1.ok, out1.error)
                self.assertEqual(out1.chat_url, f"codex://threads/{tid1}")
                self.assertEqual(opened_urls, [f"codex://threads/{tid1}"])

                # 验证 .webloc 快捷方式正确生成
                weblocs = list((root / "output" / "录音对话").glob("*.webloc"))
                self.assertEqual(len(weblocs), 1)
                self.assertEqual(plistlib.loads(weblocs[0].read_bytes())["URL"], f"codex://threads/{tid1}")

                # 同一天第二条录音应自动 resume 复用同一 thread_id
                a2 = root / "output" / "a2.md"
                out2 = analyzer.analyze(
                    AnalysisRequest(
                        recording_id="rec-2",
                        title="午间同步",
                        recorded_at="2026-11-26T13:00:00+08:00",
                        duration_sec=90,
                        speakers_summary="SPEAKER_00 -> 我",
                        transcript_path=t1,
                        analysis_path=a2,
                    )
                )
                self.assertTrue(out2.ok, out2.error)
                self.assertEqual(out2.chat_url, f"codex://threads/{tid1}")

            invocations = log_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(invocations), 2)
            self.assertNotIn("resume", invocations[0])
            self.assertIn(f"resume {tid1}", invocations[1])

    def test_codex_resume_self_heals_and_state_persists_chat_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tid_new = "01a2138f-2d6d-7e30-96a1-d9e335a16b98"
            fake_codex = _make_executable(
                root / "fake_codex_heal.sh",
                f"""#!/usr/bin/env bash
set -euo pipefail
OUT=""
HAS_RESUME=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    resume) HAS_RESUME=1; shift 2 ;;
    *) shift ;;
  esac
done
cat >/dev/null
if [ "$HAS_RESUME" -eq 1 ]; then
  echo "thread not found" >&2
  exit 1
fi
printf '{{"type":"thread.started","thread_id":"{tid_new}"}}\\n'
printf '# 自愈新会话正文\\n' > "$OUT"
""",
            )
            cfg_path = root / "config.toml"
            save_config_dict(
                cfg_path,
                {
                    "output_dir": str(root / "output"),
                    "rotation": "single",
                    "codex_bin": str(fake_codex),
                    "transcriber": {"provider": "command-asr"},
                    "analyzer": {"auto_provider": "codex-cli", "open_desktop_chat": False},
                },
            )
            cfg = load_config(cfg_path, vmr_home=root)
            store = StateStore(cfg.state_db)
            store.set_meta("codex_conv:default", "00000000-0000-0000-0000-000000000000")
            rules = RuleStudio(cfg.rules_dir)
            rules.ensure_initialized()

            t1 = root / "t1.md"
            t1.write_text("[00:00:01] 我：测试自愈。", encoding="utf-8")
            a1 = root / "output" / "a1.md"

            analyzer = CodexCliAnalyzer(cfg, rules, store=store)
            with mock.patch("voice_review.analyzers._open_codex_desktop_url") as m_open:
                out = analyzer.analyze(
                    AnalysisRequest(
                        recording_id="rec-heal",
                        title="自愈测试",
                        recorded_at="2026-11-26T10:00:00+08:00",
                        duration_sec=30,
                        speakers_summary="",
                        transcript_path=t1,
                        analysis_path=a1,
                    )
                )
                self.assertTrue(out.ok, out.error)
                self.assertEqual(out.chat_url, f"codex://threads/{tid_new}")
                # open_desktop_chat=False 时不弹窗
                m_open.assert_not_called()

            # state.sqlite 正确持久化 chat_url
            store.upsert_discovered(
                recording_id="rec-heal",
                filename="a.m4a",
                title="自愈测试",
                recorded_at="2026-11-26T10:00:00+08:00",
                recorded_date="2026-11-26",
                duration_sec=30.0,
            )
            store.update("rec-heal", status="done", analysis_path=str(a1), chat_url=out.chat_url)
            loaded = store.get("rec-heal")
            assert loaded is not None
            self.assertEqual(loaded.chat_url, f"codex://threads/{tid_new}")


if __name__ == "__main__":
    unittest.main()
