"""v1.1.0 新增功能单元测试：Codex/Claude 免 Key 自动分析插槽、实战默认模板、引导注释过滤与 MCP 画像/配置工具。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from voice_review.analyzers import (
    AnalysisRequest,
    ClaudeCliAnalyzer,
    CodexCliAnalyzer,
    get_analyzer,
    test_analyzer_connection,
)
from voice_review.config import load_config, save_config_dict, validate_provider_combo
from voice_review.installer import ClientInstaller
from voice_review.mcp_server import McpStdioServer
from voice_review.rules import RuleStudio
from voice_review.secrets import SecretStore
from voice_review.state import StateStore


def _make_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class TestV11Features(unittest.TestCase):
    def test_provider_combo_for_codex_and_claude_cli(self) -> None:
        ok_bad1, msg1 = validate_provider_combo("antigravity-flash", "codex-cli")
        self.assertFalse(ok_bad1)
        self.assertIn("antigravity-flash", msg1)

        ok_bad2, _ = validate_provider_combo("antigravity-flash", "claude-cli")
        self.assertFalse(ok_bad2)

        for tr in ("gemini-api", "openai-compatible-asr", "command-asr"):
            for an in ("codex-cli", "claude-cli"):
                ok, _ = validate_provider_combo(tr, an)
                self.assertTrue(ok, f"{tr} + {an} 应放行")

    def test_codex_cli_analyzer_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = _make_executable(
                root / "fake_codex.sh",
                """#!/usr/bin/env bash
set -euo pipefail
OUT=""
MODEL=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    -m) MODEL="$2"; shift 2 ;;
    *) shift ;;
  esac
done
STDIN_CONTENT="$(cat)"
printf '# Codex 自动复盘\\n\\n模型：%s\\n收到字数：%s\\n' "${MODEL:-default}" "${#STDIN_CONTENT}" > "$OUT"
""",
            )
            cfg_path = root / "config.toml"
            save_config_dict(
                cfg_path,
                {
                    "codex_bin": str(fake_codex),
                    "transcriber": {"provider": "openai-compatible-asr"},
                    "analyzer": {"auto_provider": "codex-cli", "model": "gpt-test"},
                },
            )
            cfg = load_config(cfg_path, vmr_home=root)
            rules = RuleStudio(cfg.rules_dir)
            rules.ensure_initialized()
            secrets = SecretStore(cfg.secrets_path)
            store = StateStore(cfg.state_db)

            ok_conn, conn_msg = test_analyzer_connection(cfg, secrets)
            self.assertTrue(ok_conn)
            self.assertIn("Codex CLI", conn_msg)

            analyzer = get_analyzer(cfg, secrets, store, rules)
            self.assertIsInstance(analyzer, CodexCliAnalyzer)

            transcript = root / "t.md"
            transcript.write_text("[00:00:02] 我：今天我们把演示口径对齐一下。", encoding="utf-8")
            analysis = root / "out" / "a.md"
            outcome = analyzer.analyze(
                AnalysisRequest(
                    recording_id="rec-codex-1",
                    title="方案对齐",
                    recorded_at="2026-11-25T19:00:00+08:00",
                    duration_sec=120,
                    speakers_summary="SPEAKER_00 -> 我",
                    transcript_path=transcript,
                    analysis_path=analysis,
                )
            )
            self.assertTrue(outcome.ok, outcome.error)
            self.assertEqual(outcome.status, "done")
            md = analysis.read_text(encoding="utf-8")
            self.assertIn("# Codex 自动复盘", md)
            self.assertIn("模型：gpt-test", md)

    def test_claude_cli_analyzer_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_claude = _make_executable(
                root / "fake_claude.sh",
                """#!/usr/bin/env bash
set -euo pipefail
STDIN_CONTENT="$(cat)"
printf '# Claude 自动复盘\\n\\n字数：%s\\n' "${#STDIN_CONTENT}"
""",
            )
            cfg_path = root / "config.toml"
            save_config_dict(
                cfg_path,
                {
                    "claude_bin": str(fake_claude),
                    "transcriber": {"provider": "gemini-api"},
                    "analyzer": {"auto_provider": "claude-cli"},
                },
            )
            cfg = load_config(cfg_path, vmr_home=root)
            rules = RuleStudio(cfg.rules_dir)
            rules.ensure_initialized()
            secrets = SecretStore(cfg.secrets_path)
            store = StateStore(cfg.state_db)

            ok_conn, conn_msg = test_analyzer_connection(cfg, secrets)
            self.assertTrue(ok_conn)
            self.assertIn("Claude Code CLI", conn_msg)

            analyzer = get_analyzer(cfg, secrets, store, rules)
            self.assertIsInstance(analyzer, ClaudeCliAnalyzer)

            transcript = root / "t.md"
            transcript.write_text("[00:00:05] 客户：预算大概在二十万左右。", encoding="utf-8")
            analysis = root / "out" / "b.md"
            outcome = analyzer.analyze(
                AnalysisRequest(
                    recording_id="rec-claude-1",
                    title="客户调研",
                    recorded_at="2026-11-25T19:05:00+08:00",
                    duration_sec=90,
                    speakers_summary="SPEAKER_00 -> 客户",
                    transcript_path=transcript,
                    analysis_path=analysis,
                )
            )
            self.assertTrue(outcome.ok, outcome.error)
            self.assertIn("# Claude 自动复盘", analysis.read_text(encoding="utf-8"))

    def test_default_template_rich_and_guides_strip_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            studio = RuleStudio(Path(tmp) / "rules")
            studio.ensure_initialized()

            # 1) 默认模板保持通用高含金量且严格脱敏
            tpl = studio.get_template("general_review")
            self.assertIn("30 秒速览", tpl.body)
            self.assertIn("核心摘要", tpl.body)
            self.assertIn("严格区分事实与推断", tpl.body)
            for forbidden in ("obsidian", "微信快报", "hermes", "clawbot", "私人业务", "茶局"):
                self.assertNotIn(forbidden, tpl.body.lower())

            # 2) 初始化时词表与背景自带清晰引导文案，但不会污染实际 Prompt
            self.assertTrue(studio.get_glossary(raw=True).startswith("#"))
            self.assertEqual(studio.get_glossary(raw=False), "")
            self.assertIn("<!--", studio.get_persona(raw=True))
            self.assertEqual(studio.get_persona(raw=False), "")

            # 3) 用户在注释外填写真实 Markdown 背景时保留标题与正文
            studio.save_persona("<!-- 注释 -->\n## 我的背景\n我是 B2B 顾问，关注客户预算与决策链。")
            self.assertIn("## 我的背景", studio.get_persona())
            self.assertNotIn("注释", studio.get_persona())

            studio.save_glossary("# 示例注释\nSaaS ARR 飞书")
            self.assertEqual(studio.get_glossary(), "SaaS ARR 飞书")

    def test_mcp_profile_and_settings_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(vmr_home=root)
            srv = McpStdioServer(cfg)

            # tools/list 包含全部 13 个工具
            list_resp = srv.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            assert list_resp is not None
            names = {t["name"] for t in list_resp["result"]["tools"]}
            self.assertEqual(len(names), 13)
            self.assertTrue({"vmr_get_profile", "vmr_update_profile", "vmr_update_settings", "vmr_check_update"}.issubset(names))

            # vmr_update_profile 写入个人背景、词表与默认模板
            upd_prof = srv.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "vmr_update_profile",
                        "arguments": {
                            "persona": "我是产品负责人，关注交付节奏与需求边界。",
                            "glossary": "OKR SLA 飞书",
                            "default_template_id": "sales_communication",
                        },
                    },
                }
            )
            assert upd_prof is not None
            self.assertFalse(upd_prof["result"]["isError"])
            prof_data = json.loads(upd_prof["result"]["content"][0]["text"])
            self.assertEqual(prof_data["default_template"], "sales_communication")
            self.assertIn("产品负责人", prof_data["persona_effective"])

            # vmr_update_settings 拒绝非法组合
            bad_set = srv.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "vmr_update_settings",
                        "arguments": {
                            "transcriber_provider": "antigravity-flash",
                            "analyzer_auto_provider": "codex-cli",
                        },
                    },
                }
            )
            assert bad_set is not None
            self.assertTrue(bad_set["result"]["isError"])

            # vmr_update_settings 合法切换为 openai-compatible-asr + codex-cli 并写入运行偏好
            custom_out = root / "my_notes"
            ok_set = srv.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "vmr_update_settings",
                        "arguments": {
                            "transcriber_provider": "openai-compatible-asr",
                            "transcriber_model": "whisper-1",
                            "analyzer_auto_provider": "codex-cli",
                            "drop_folder_enabled": True,
                            "rotation": "daily",
                            "output_dir": str(custom_out),
                            "min_duration_sec": 30,
                        },
                    },
                }
            )
            assert ok_set is not None
            self.assertFalse(ok_set["result"]["isError"])
            set_data = json.loads(ok_set["result"]["content"][0]["text"])
            self.assertEqual(set_data["transcriber_provider"], "openai-compatible-asr")
            self.assertEqual(set_data["analyzer_auto_provider"], "codex-cli")
            self.assertTrue(set_data["drop_folder_enabled"])
            self.assertEqual(set_data["rotation"], "daily")
            self.assertEqual(set_data["min_duration_sec"], 30.0)

            reloaded = load_config(vmr_home=root)
            self.assertEqual(reloaded.analyzer_auto_provider, "codex-cli")
            self.assertEqual(reloaded.transcriber_settings.get("model"), "whisper-1")
            self.assertEqual(reloaded.rotation, "daily")
            self.assertEqual(reloaded.output_dir, custom_out.resolve())
            self.assertEqual(reloaded.min_duration_sec, 30.0)

    def test_codex_installer_mentions_new_chat_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            (fake_home / ".codex").mkdir(parents=True)
            pkg_root = Path(__file__).resolve().parent.parent
            inst = ClientInstaller(
                vmr_bin=pkg_root / "vmr",
                skill_source_dir=pkg_root / "skills" / "voice-memo-review",
                home_dir=fake_home,
            )
            res = inst.install(["codex"])["codex"]
            self.assertTrue(res.ok)
            self.assertIn("新开", res.message)
            skill_file = fake_home / ".codex" / "skills" / "voice-memo-review" / "SKILL.md"
            self.assertTrue(skill_file.is_file())
            self.assertIn("vmr_update_profile", skill_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
