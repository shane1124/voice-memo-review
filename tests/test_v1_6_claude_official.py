"""v1.6.0 专项测试：官方 Claude 桌面版 MCP 接入、真实登录态体检、vmr auth-claude 与控制台一键授权接口。"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import urllib.request

from voice_review.cli import _cmd_doctor, build_parser
from voice_review.config import (
    check_claude_auth_status,
    load_config,
    resolve_antigravity_command,
    resolve_codex_binary,
    save_config_dict,
)
from voice_review.daemon import VmrDaemon
from voice_review.installer import ClientInstaller
from voice_review.mcp_server import McpStdioServer


def _make_exec(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, stat.S_IRWXU)
    return path


class TestV16ClaudeOfficialPush(unittest.TestCase):
    def test_installer_supports_claude_desktop_config_losslessly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cd_dir = home / "Library" / "Application Support" / "Claude"
            cd_dir.mkdir(parents=True)
            cd_cfg = cd_dir / "claude_desktop_config.json"
            cd_cfg.write_text(
                json.dumps(
                    {
                        "preferences": {"quickEntryShortcut": {"accelerator": "Alt+Space"}},
                        "mcpServers": {"keep-existing": {"command": "node", "args": ["app.js"]}},
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            skill_src = Path(__file__).resolve().parent.parent / "skills" / "voice-memo-review"
            inst = ClientInstaller(vmr_bin=home / ".local/bin/vmr", skill_source_dir=skill_src, home_dir=home)

            det = inst.detect_clients()
            self.assertTrue(det["claude_desktop"].detected)
            self.assertFalse(det["claude_desktop"].installed)

            res = inst.install(["claude_desktop"])
            self.assertTrue(res["claude_desktop"].ok)
            updated = json.loads(cd_cfg.read_text(encoding="utf-8"))
            self.assertIn("voice-memo-review", updated["mcpServers"])
            self.assertIn("keep-existing", updated["mcpServers"])
            self.assertEqual(updated["preferences"]["quickEntryShortcut"]["accelerator"], "Alt+Space")
            self.assertTrue(cd_cfg.with_name("claude_desktop_config.json.bak").is_file())

            inst.uninstall(["claude_desktop"])
            after = json.loads(cd_cfg.read_text(encoding="utf-8"))
            self.assertNotIn("voice-memo-review", after["mcpServers"])
            self.assertIn("keep-existing", after["mcpServers"])

    def test_claude_auth_status_doctor_and_mcp_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_unauth = _make_exec(
                root / "claude_unauth.py",
                """#!/opt/homebrew/bin/python3.13
import json, sys
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": False, "authMethod": "none"}))
""",
            )
            fake_auth = _make_exec(
                root / "claude_auth.py",
                """#!/opt/homebrew/bin/python3.13
import json, sys
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claudeai"}))
""",
            )
            exe1, ok1, msg1 = check_claude_auth_status(str(fake_unauth))
            self.assertEqual(exe1, str(fake_unauth.resolve()))
            self.assertFalse(ok1)
            self.assertIn("vmr auth-claude", msg1)

            exe2, ok2, _ = check_claude_auth_status(str(fake_auth))
            self.assertEqual(exe2, str(fake_auth.resolve()))
            self.assertTrue(ok2)

            cfg_path = root / "config.toml"
            save_config_dict(cfg_path, {"claude_bin": str(fake_unauth)})
            cfg = load_config(cfg_path, vmr_home=root)

            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                _cmd_doctor(cfg)
            doc_out = buf.getvalue()
            self.assertIn("尚未授权登录", doc_out)
            self.assertIn("vmr auth-claude", doc_out)

            srv = McpStdioServer(cfg)
            st, is_err = srv._call_tool("vmr_status", {})
            self.assertFalse(is_err)
            self.assertTrue(st["claude_ready"])
            self.assertFalse(st["claude_logged_in"])

    def test_claude_login_endpoint_and_cli_parser(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["auth-claude"])
        self.assertEqual(args.cmd, "auth-claude")
        self.assertFalse(args.console)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_unauth = _make_exec(
                root / "claude_unauth.py",
                """#!/opt/homebrew/bin/python3.13
import json, sys
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": False}))
""",
            )
            cfg_path = root / "config.toml"
            save_config_dict(
                cfg_path,
                {
                    "claude_bin": str(fake_unauth),
                    "sources": {"voice_memos_enabled": False, "drop_folder_enabled": False},
                },
            )
            cfg = load_config(cfg_path, vmr_home=root)
            daemon = VmrDaemon(cfg, port=0, start_watcher=False)
            daemon.start_in_thread()
            try:
                with (
                    mock.patch(
                        "voice_review.config.check_claude_auth_status",
                        return_value=(str(fake_unauth), False, "尚未授权"),
                    ),
                    mock.patch("voice_review.daemon.subprocess.Popen") as mocked_popen,
                ):
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{daemon.port}/api/v1/providers/claude-login",
                        data=b"{}",
                        headers={"Content-Type": "application/json", "X-VMR-Token": daemon.token},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(data["ok"])
                    self.assertFalse(data["already_logged_in"])
                    mocked_popen.assert_called_once()
                    launched_cmd = mocked_popen.call_args[0][0]
                    self.assertEqual(launched_cmd[1:], ["auth", "login", "--claudeai"])
            finally:
                daemon.stop()

    def test_desktop_app_builtin_takes_precedence_over_path_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            desktop_codex = _make_exec(
                fake_home / "Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                "#!/bin/sh\necho desktop-codex\n",
            )
            fake_ls = _make_exec(
                fake_home / "Applications/Antigravity.app/Contents/Resources/bin/language_server",
                "#!/bin/sh\necho agy-ls\n",
            )
            with (
                mock.patch("pathlib.Path.home", return_value=fake_home),
                mock.patch("shutil.which", return_value="/opt/homebrew/bin/codex"),
                mock.patch(
                    "voice_review.config._is_exec_file",
                    side_effect=lambda p: str(p).startswith(str(fake_home)) and p.is_file() and os.access(p, os.X_OK),
                ),
            ):
                # 即使系统 PATH 有外部 codex CLI，也优先走桌面 App 内置本体
                self.assertEqual(resolve_codex_binary(""), str(desktop_codex.resolve()))
                # Antigravity 桌面版内置 language_server 自动补传 agentapi 子命令
                self.assertEqual(resolve_antigravity_command(""), [str(fake_ls.resolve()), "agentapi"])


if __name__ == "__main__":
    unittest.main()
