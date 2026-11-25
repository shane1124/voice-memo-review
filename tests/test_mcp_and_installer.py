"""Task 8 单元测试：零依赖 Stdio MCP Server、长音频异步防超时、通用 Skill 与四客户端无损安装卸载。"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from voice_review.config import load_config, save_config_dict
from voice_review.installer import ClientInstaller
from voice_review.mcp_server import McpStdioServer


class TestMcpAndInstaller(unittest.TestCase):
    def test_mcp_handshake_tools_resources_and_template_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            cfg = load_config(vmr_home=tmp)
            srv = McpStdioServer(cfg)

            init_resp = srv.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
            self.assertEqual(init_resp["id"], 1)
            self.assertIn("serverInfo", init_resp["result"])

            tools_resp = srv.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            tool_names = {t["name"] for t in tools_resp["result"]["tools"]}
            for required in (
                "vmr_status",
                "vmr_list_recordings",
                "vmr_process_audio",
                "vmr_get_job_status",
                "vmr_list_templates",
                "vmr_get_template",
                "vmr_save_template",
                "vmr_save_analysis",
                "vmr_manage_voiceprints",
            ):
                self.assertIn(required, tool_names)

            # 保存自定义模板并通过 MCP resource 读回
            save_resp = srv.handle_message({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "vmr_save_template",
                    "arguments": {
                        "template_id": "custom_test",
                        "name": "测试模板",
                        "description": "单元测试",
                        "body": "输出 3 点总结。",
                    },
                },
            })
            self.assertFalse(save_resp["result"].get("isError", False))

            read_res = srv.handle_message({
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/read",
                "params": {"uri": "vmr://templates/custom_test"},
            })
            self.assertIn("输出 3 点总结", read_res["result"]["contents"][0]["text"])

            # 写入分析稿并更新状态为 done
            srv.store.upsert_discovered(
                recording_id="rec_mcp_1",
                filename="demo.m4a",
                title="演示录音",
                recorded_at="2026-11-25T14:00:00+08:00",
                recorded_date="2026-11-25",
                duration_sec=90.0,
            )
            ana_resp = srv.handle_message({
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "vmr_save_analysis",
                    "arguments": {
                        "recording_id": "rec_mcp_1",
                        "markdown": "# 演示复盘\n核心结论明确。",
                    },
                },
            })
            self.assertFalse(ana_resp["result"].get("isError", False))
            rec = srv.store.get("rec_mcp_1")
            self.assertEqual(rec.status, "done")
            self.assertTrue(Path(rec.analysis_path).is_file())

    def test_mcp_long_audio_returns_async_job_id_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            cfg_p = tmp / "config.toml"
            save_config_dict(cfg_p, {
                "transcriber": {"provider": "gemini-api"},
                "analyzer": {"auto_provider": "none"},
            })
            cfg = load_config(cfg_p, vmr_home=tmp)
            audio = tmp / "long_meeting.m4a"
            audio.write_bytes(b"fake-long-audio")
            srv = McpStdioServer(cfg)

            with patch("voice_review.mcp_server.probe_duration_sec", return_value=1800.0), \
                 patch.object(srv, "_dispatch_background_job", return_value="detached-worker") as m_disp:
                resp = srv.handle_message({
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        "name": "vmr_process_audio",
                        "arguments": {"audio_path": str(audio), "template_id": "meeting_minutes"},
                    },
                })
                m_disp.assert_called_once()
            payload = json.loads(resp["result"]["content"][0]["text"])
            self.assertEqual(payload["status"], "running")
            self.assertTrue(payload["job_id"].startswith("job_"))

            st_resp = srv.handle_message({
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {"name": "vmr_get_job_status", "arguments": {"job_id": payload["job_id"]}},
            })
            st_payload = json.loads(st_resp["result"]["content"][0]["text"])
            self.assertEqual(st_payload["recording_id"], payload["job_id"])

    def test_installer_preserves_existing_configs_and_isolates_antigravity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            home = Path(tmp_s)
            # 预置既有 4 家客户端配置（带注释与其它 server）
            (home / ".gemini" / "config").mkdir(parents=True)
            agy_global_cfg = home / ".gemini" / "config" / "config.json"
            agy_original = '{"plugins": {"other-plugin": {"enabled": true}}}\n'
            agy_global_cfg.write_text(agy_original, encoding="utf-8")

            claude_cfg = home / ".claude.json"
            claude_cfg.write_text(json.dumps({"mcpServers": {"keep-me": {"command": "echo"}}}, indent=2), encoding="utf-8")

            (home / ".codex").mkdir(parents=True)
            codex_cfg = home / ".codex" / "config.toml"
            codex_cfg.write_text(
                '# 用户手写顶部注释\nmodel = "o3"\n\n[mcp_servers.other]\ncommand = "node"\nargs = ["x.js"]\n',
                encoding="utf-8",
            )

            (home / ".cursor").mkdir(parents=True)
            cursor_cfg = home / ".cursor" / "mcp.json"
            cursor_cfg.write_text(json.dumps({"mcpServers": {"cursor-keep": {"command": "ls"}}}, indent=2), encoding="utf-8")

            skill_src = Path(__file__).resolve().parent.parent / "skills" / "voice-memo-review"
            inst = ClientInstaller(vmr_bin=home / ".local" / "bin" / "vmr", skill_source_dir=skill_src, home_dir=home)

            res = inst.install()
            self.assertEqual(set(res.keys()), {"antigravity", "claude", "codex", "cursor"})
            self.assertTrue(all(r.ok for r in res.values()))

            # 1) Antigravity：生成独立插件目录，绝不改动全局 config.json
            agy_plugin = home / ".gemini" / "config" / "plugins" / "voice-memo-review"
            self.assertTrue((agy_plugin / "plugin.json").is_file())
            self.assertTrue((agy_plugin / "skills" / "voice-memo-review" / "SKILL.md").is_file())
            agy_mcp = json.loads((agy_plugin / "mcp_config.json").read_text(encoding="utf-8"))
            self.assertIn("voice-memo-review", agy_mcp["mcpServers"])
            self.assertNotIn("variables", agy_mcp["mcpServers"]["voice-memo-review"])
            self.assertEqual(agy_global_cfg.read_text(encoding="utf-8"), agy_original)

            # 2) Claude Code & Cursor 保留原 server 并备份
            claude_after = json.loads(claude_cfg.read_text(encoding="utf-8"))
            self.assertIn("keep-me", claude_after["mcpServers"])
            self.assertIn("voice-memo-review", claude_after["mcpServers"])
            self.assertTrue((home / ".claude.json.bak").is_file())
            self.assertTrue((home / ".claude" / "skills" / "voice-memo-review" / "SKILL.md").is_file())

            cursor_after = json.loads(cursor_cfg.read_text(encoding="utf-8"))
            self.assertIn("cursor-keep", cursor_after["mcpServers"])
            self.assertIn("voice-memo-review", cursor_after["mcpServers"])

            # 3) Codex 保留手写注释与原 server
            codex_text = codex_cfg.read_text(encoding="utf-8")
            self.assertIn("# 用户手写顶部注释", codex_text)
            self.assertIn("[mcp_servers.other]", codex_text)
            self.assertIn("[mcp_servers.voice-memo-review]", codex_text)
            self.assertTrue((home / ".codex" / "skills" / "voice-memo-review" / "SKILL.md").is_file())

            # 4) 卸载干净回滚且保留原配置
            un = inst.uninstall()
            self.assertTrue(all(r.ok for r in un.values()))
            self.assertFalse(agy_plugin.exists())
            self.assertNotIn("voice-memo-review", json.loads(claude_cfg.read_text(encoding="utf-8"))["mcpServers"])
            self.assertIn("keep-me", json.loads(claude_cfg.read_text(encoding="utf-8"))["mcpServers"])
            codex_clean = codex_cfg.read_text(encoding="utf-8")
            self.assertIn("# 用户手写顶部注释", codex_clean)
            self.assertIn("[mcp_servers.other]", codex_clean)
            self.assertNotIn("[mcp_servers.voice-memo-review]", codex_clean)

    def test_skill_frontmatter_compatible_across_three_clients(self) -> None:
        skill_md = Path(__file__).resolve().parent.parent / "skills" / "voice-memo-review" / "SKILL.md"
        text = skill_md.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: voice-memo-review", text)
        self.assertIn("description:", text)


if __name__ == "__main__":
    unittest.main()
