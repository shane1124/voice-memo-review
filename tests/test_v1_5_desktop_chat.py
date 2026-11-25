"""v1.5.0 专项测试：Claude 桌面版原生直开对话、桌面 App 内置引擎自动探测、默认 per_recording 策略与 open-chat 接口。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import urllib.request

from voice_review.agent_bridge import resolve_bucket
from voice_review.analyzers import (
    AnalysisRequest,
    ClaudeCliAnalyzer,
    get_analyzer,
    test_analyzer_connection,
)
from voice_review.config import (
    load_config,
    resolve_claude_binary,
    resolve_codex_binary,
    save_config_dict,
)
from voice_review.daemon import VmrDaemon
from voice_review.mcp_server import McpStdioServer
from voice_review.rules import RuleStudio
from voice_review.secrets import SecretStore
from voice_review.state import StateStore


def _make_exec(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, stat.S_IRWXU)
    return path


class TestV15DesktopChat(unittest.TestCase):
    def test_default_rotation_is_daily_and_title_puts_date_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(vmr_home=root)
            self.assertEqual(cfg.rotation, "daily")
            self.assertEqual(cfg.conversation_title, "录音分析")
            bucket, title = resolve_bucket(cfg, "2026-11-25T14:30:00+08:00")
            self.assertEqual(bucket, "conv:daily-2026-11-25")
            self.assertEqual(title, "11-25-2026 录音分析")

            cfg_path = root / "per.toml"
            save_config_dict(cfg_path, {"rotation": "per_recording"})
            cfg_per = load_config(cfg_path, vmr_home=root)
            b_per, t_per = resolve_bucket(cfg_per, "2026-11-25T14:30:00+08:00")
            self.assertIsNone(b_per)
            self.assertEqual(t_per, "11-25-2026 14:30 录音分析")

    def test_claude_desktop_per_recording_daily_resume_and_self_heal(self) -> None:
        sid_1 = "11111111-2222-4333-8444-555555555551"
        sid_2 = "11111111-2222-4333-8444-555555555552"
        sid_3 = "11111111-2222-4333-8444-555555555553"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls_log = root / "claude_calls.jsonl"
            fake_claude = _make_exec(
                root / "fake_claude.py",
                f"""#!/opt/homebrew/bin/python3.13
import json, os, sys
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    print(json.dumps({{"loggedIn": True}}))
    sys.exit(0)
prompt = sys.stdin.read()
log_p = {str(calls_log)!r}
with open(log_p, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"args": args, "cwd": os.getcwd(), "prompt_len": len(prompt)}}, ensure_ascii=False) + "\\n")
if "--resume" in args:
    ridx = args.index("--resume") + 1
    rid = args[ridx]
    if os.environ.get("SIMULATE_DELETED_SESSION") == "1":
        print(json.dumps({{"type": "result", "is_error": True, "result": "Session not found", "session_id": ""}}))
        sys.exit(1)
    print(json.dumps({{"type": "result", "is_error": False, "result": "# 续接复盘\\n\\n继续分析", "session_id": rid}}, ensure_ascii=False))
    sys.exit(0)
if "--no-session-persistence" in args:
    print(json.dumps({{"type": "result", "is_error": False, "result": "分段摘要要点", "session_id": "{sid_3}"}}, ensure_ascii=False))
    sys.exit(0)
count = sum(1 for _ in open(log_p, encoding="utf-8"))
sid = "{sid_1}" if count == 1 else ("{sid_2}" if count == 2 else "{sid_3}")
print(json.dumps({{"type": "result", "is_error": False, "result": f"# Claude 桌面复盘 #{{count}}", "session_id": sid}}, ensure_ascii=False))
""",
            )
            cfg_path = root / "config.toml"
            save_config_dict(
                cfg_path,
                {
                    "output_dir": str(root / "out"),
                    "rotation": "daily",
                    "claude_bin": str(fake_claude),
                    "transcriber": {"provider": "gemini-api"},
                    "analyzer": {"auto_provider": "claude-cli", "open_desktop_chat": True},
                },
            )
            cfg = load_config(cfg_path, vmr_home=root)
            rules = RuleStudio(cfg.rules_dir)
            rules.ensure_initialized()
            store = StateStore(cfg.state_db)
            secrets = SecretStore(cfg.secrets_path)
            analyzer = get_analyzer(cfg, secrets, store, rules)
            self.assertIsInstance(analyzer, ClaudeCliAnalyzer)

            transcript = root / "t.md"
            transcript.write_text("[00:00:05] 说话人A：今天同步方案进度。", encoding="utf-8")

            opened: list[tuple[str, str, Path]] = []
            with mock.patch(
                "voice_review.analyzers.open_claude_desktop_session",
                side_effect=lambda exe, sid, cwd: (opened.append((exe, sid, cwd)) or True),
            ):
                # 1. 当天首条 -> 新开 session，带 --name，写 .command 并弹开 Claude 桌面版
                out1 = analyzer.analyze(
                    AnalysisRequest(
                        recording_id="rec-c1",
                        title="晨会同步",
                        recorded_at="2026-11-26T09:00:00+08:00",
                        duration_sec=60,
                        speakers_summary="说话人A",
                        transcript_path=transcript,
                        analysis_path=root / "out" / "a1.md",
                    )
                )
                self.assertTrue(out1.ok, out1.error)
                self.assertEqual(out1.chat_url, f"claude-session://{sid_1}")
                self.assertEqual(len(opened), 1)
                self.assertEqual(opened[0][1], sid_1)
                launchers = list((root / "out" / "录音对话").glob("*.command"))
                self.assertEqual(len(launchers), 1)
                self.assertIn(f"--desktop --resume {sid_1}", launchers[0].read_text(encoding="utf-8"))

                # 2. 当天第二条 -> 自动携带 --resume sid_1 续接同一桌面对话
                out2 = analyzer.analyze(
                    AnalysisRequest(
                        recording_id="rec-c2",
                        title="下午评审",
                        recorded_at="2026-11-26T15:00:00+08:00",
                        duration_sec=60,
                        speakers_summary="说话人A",
                        transcript_path=transcript,
                        analysis_path=root / "out" / "a2.md",
                    )
                )
                self.assertTrue(out2.ok, out2.error)
                self.assertEqual(out2.chat_url, f"claude-session://{sid_1}")

                # 3. 模拟旧 session 被删 -> 自动自愈重开新 session
                with mock.patch.dict(os.environ, {"SIMULATE_DELETED_SESSION": "1"}):
                    out3 = analyzer.analyze(
                        AnalysisRequest(
                            recording_id="rec-c3",
                            title="晚间复盘",
                            recorded_at="2026-11-26T20:00:00+08:00",
                            duration_sec=60,
                            speakers_summary="说话人A",
                            transcript_path=transcript,
                            analysis_path=root / "out" / "a3.md",
                        )
                    )
                self.assertTrue(out3.ok, out3.error)
                self.assertEqual(out3.chat_url, f"claude-session://{sid_3}")

            calls = [json.loads(line) for line in calls_log.read_text(encoding="utf-8").splitlines()]
            self.assertIn("--name", calls[0]["args"])
            self.assertIn("--resume", calls[1]["args"])
            self.assertEqual(calls[1]["args"][calls[1]["args"].index("--resume") + 1], sid_1)
            # 第 3 次调用先试 resume 失败，第 4 次无 resume 自愈成功
            self.assertIn("--resume", calls[2]["args"])
            self.assertNotIn("--resume", calls[3]["args"])

    def test_claude_long_audio_chunks_use_no_session_persistence(self) -> None:
        sid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls_log = root / "chunk_calls.jsonl"
            fake_claude = _make_exec(
                root / "fake_claude.py",
                f"""#!/opt/homebrew/bin/python3.13
import json, sys
args = sys.argv[1:]
_ = sys.stdin.read()
with open({str(calls_log)!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\\n")
print(json.dumps({{"type": "result", "is_error": False, "result": "总结正文", "session_id": "{sid}"}}))
""",
            )
            cfg_path = root / "config.toml"
            save_config_dict(
                cfg_path,
                {
                    "output_dir": str(root / "out"),
                    "rotation": "per_recording",
                    "claude_bin": str(fake_claude),
                    "transcriber": {"provider": "gemini-api"},
                    "analyzer": {"auto_provider": "claude-cli", "max_chunk_chars": 10000, "open_desktop_chat": False},
                },
            )
            cfg = load_config(cfg_path, vmr_home=root)
            rules = RuleStudio(cfg.rules_dir)
            rules.ensure_initialized()
            store = StateStore(cfg.state_db)
            analyzer = ClaudeCliAnalyzer(cfg, rules, store=store)

            transcript = root / "long.md"
            transcript.write_text(("这是一段很长的中文会议记录内容，包含关键数据与决策。\n" * 600), encoding="utf-8")
            out = analyzer.analyze(
                AnalysisRequest(
                    recording_id="rec-long-1",
                    title="超长复盘",
                    recorded_at="2026-11-26T10:00:00+08:00",
                    duration_sec=3600,
                    speakers_summary="说话人A",
                    transcript_path=transcript,
                    analysis_path=root / "out" / "long_a.md",
                )
            )
            self.assertTrue(out.ok, out.error)
            calls = [json.loads(line) for line in calls_log.read_text(encoding="utf-8").splitlines()]
            self.assertGreaterEqual(len(calls), 3)
            for sub_call in calls[:-1]:
                self.assertIn("--no-session-persistence", sub_call)
            self.assertNotIn("--no-session-persistence", calls[-1])

    def test_claude_not_logged_in_gives_clear_chinese_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_claude = _make_exec(
                root / "fake_claude_unauth.py",
                """#!/opt/homebrew/bin/python3.13
import json, sys
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": False}))
    sys.exit(0)
_ = sys.stdin.read()
print(json.dumps({"type": "result", "is_error": True, "result": "Not logged in · Please run /login"}))
sys.exit(0)
""",
            )
            cfg_path = root / "config.toml"
            save_config_dict(
                cfg_path,
                {
                    "output_dir": str(root / "out"),
                    "claude_bin": str(fake_claude),
                    "transcriber": {"provider": "gemini-api"},
                    "analyzer": {"auto_provider": "claude-cli"},
                },
            )
            cfg = load_config(cfg_path, vmr_home=root)
            secrets = SecretStore(cfg.secrets_path)
            ok_conn, msg_conn = test_analyzer_connection(cfg, secrets)
            self.assertFalse(ok_conn)
            self.assertIn("尚未登录", msg_conn)
            self.assertIn("MCP 接手", msg_conn)

            with mock.patch("voice_review.analyzers.subprocess.run") as malformed:
                malformed.return_value.stdout = "unrecognized status format"
                ok_unknown, msg_unknown = test_analyzer_connection(cfg, secrets)
            self.assertFalse(ok_unknown)
            self.assertIn("未能核实", msg_unknown)

            rules = RuleStudio(cfg.rules_dir)
            rules.ensure_initialized()
            analyzer = ClaudeCliAnalyzer(cfg, rules)
            t = root / "t.md"
            t.write_text("测试内容", encoding="utf-8")
            out = analyzer.analyze(
                AnalysisRequest(
                    recording_id="rec-unauth",
                    title="测试",
                    recorded_at="2026-11-26T10:00:00+08:00",
                    duration_sec=20,
                    speakers_summary="",
                    transcript_path=t,
                    analysis_path=root / "out" / "u.md",
                )
            )
            self.assertFalse(out.ok)
            self.assertIn("尚未登录", out.error)

    def test_resolve_desktop_embedded_binaries_without_cli_in_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            codex_embedded = _make_exec(
                fake_home / "Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                "#!/bin/sh\necho codex\n",
            )
            claude_old = _make_exec(
                fake_home / "Library/Application Support/Claude/claude-code/2.1.100/aaa/claude.app/Contents/MacOS/claude",
                "#!/bin/sh\necho old\n",
            )
            claude_new = _make_exec(
                fake_home / "Library/Application Support/Claude/claude-code/2.1.328/bbb/claude.app/Contents/MacOS/claude",
                "#!/bin/sh\necho new\n",
            )
            self.assertTrue(claude_old.is_file())
            with (
                mock.patch("pathlib.Path.home", return_value=fake_home),
                mock.patch("voice_review.config._managed_bin_dirs", return_value=[]),
                mock.patch("shutil.which", return_value=None),
                mock.patch(
                    "voice_review.config._is_exec_file",
                    side_effect=lambda p: str(p).startswith(str(fake_home)) and p.is_file() and os.access(p, os.X_OK),
                ),
            ):
                self.assertEqual(resolve_codex_binary(""), str(codex_embedded.resolve()))
                self.assertEqual(resolve_claude_binary(""), str(claude_new.resolve()))

    def test_daemon_open_chat_endpoint_and_mcp_open_desktop_chat_setting(self) -> None:
        sid = "99999999-8888-4777-8666-555555555555"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = root / "config.toml"
            save_config_dict(
                cfg_path,
                {
                    "output_dir": str(root / "out"),
                    "transcriber": {"provider": "gemini-api"},
                    "analyzer": {"auto_provider": "claude-cli", "open_desktop_chat": True},
                    "sources": {"voice_memos_enabled": False, "drop_folder_enabled": False},
                },
            )
            cfg = load_config(cfg_path, vmr_home=root)
            srv = McpStdioServer(cfg)
            res, is_err = srv._call_tool(
                "vmr_update_settings",
                {
                    "analyzer_auto_provider": "claude-cli",
                    "open_desktop_chat": False,
                    "rotation": "per_recording",
                },
            )
            self.assertFalse(is_err, res)
            reloaded = load_config(cfg_path, vmr_home=root)
            self.assertEqual(reloaded.analyzer_settings.get("open_desktop_chat"), False)
            self.assertEqual(reloaded.rotation, "per_recording")

            daemon = VmrDaemon(reloaded, port=0, start_watcher=False)
            daemon.start_in_thread()
            try:
                daemon.store.upsert_discovered(
                    recording_id="rec-open-1",
                    filename="demo.m4a",
                    title="桌面直开测试",
                    recorded_at="2026-11-26T11:00:00+08:00",
                    recorded_date="2026-11-26",
                    duration_sec=30.0,
                    audio_path=str(root / "demo.m4a"),
                )
                daemon.store.update("rec-open-1", status="done", chat_url=f"claude-session://{sid}")
                with mock.patch(
                    "voice_review.analyzers.open_job_desktop_chat",
                    return_value=(True, "已在 Claude 桌面版打开对话"),
                ) as mocked_open:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{daemon.port}/api/v1/jobs/rec-open-1/open-chat",
                        data=b"{}",
                        headers={"Content-Type": "application/json", "X-VMR-Token": daemon.token},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(data["ok"])
                    mocked_open.assert_called_once()
                    self.assertEqual(mocked_open.call_args[0][1], f"claude-session://{sid}")
            finally:
                daemon.stop()


if __name__ == "__main__":
    unittest.main()
