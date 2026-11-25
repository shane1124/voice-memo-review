"""v1.2.0 单元测试：macOS launchd 开机自启、短命接力令牌、方案 B 缓存/ETag 检查更新、忙碌防打断与模板差异检测。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

from voice_review.autostart import (
    AutostartManager,
    LAUNCH_AGENT_LABEL,
    consume_restart_token,
    write_restart_token,
)
from voice_review.config import load_config
from voice_review.daemon import VmrDaemon
from voice_review.mcp_server import McpStdioServer
from voice_review.rules import RuleStudio
from voice_review.updater import SelfUpdater, _summarize_release_notes


class TestV12Features(unittest.TestCase):
    def test_autostart_plist_enable_status_disable_and_non_darwin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / "LaunchAgents"
            vmr_bin = root / "vmr"
            vmr_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            calls: list[list[str]] = []
            loaded_state = {"loaded": False}

            def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
                calls.append(cmd)
                if cmd[:2] == ["launchctl", "bootstrap"]:
                    loaded_state["loaded"] = True
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                if cmd[:2] == ["launchctl", "bootout"]:
                    loaded_state["loaded"] = False
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                if cmd[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(cmd, 0 if loaded_state["loaded"] else 1, "", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            mgr = AutostartManager(
                vmr_bin=vmr_bin,
                vmr_home=root / "home",
                launch_agents_dir=agents_dir,
                runner=fake_runner,
                platform_name="darwin",
            )
            self.assertFalse(mgr.status().enabled)

            ok, msg = mgr.enable(load_now=True)
            self.assertTrue(ok, msg)
            self.assertTrue(mgr.plist_path.is_file())
            parsed = plistlib.loads(mgr.plist_path.read_bytes())
            self.assertEqual(parsed["Label"], LAUNCH_AGENT_LABEL)
            self.assertTrue(parsed["RunAtLoad"])
            self.assertTrue(parsed["KeepAlive"])
            self.assertIn("/opt/homebrew/bin", parsed["EnvironmentVariables"]["PATH"])
            self.assertEqual(parsed["ProgramArguments"][-1], "serve")

            st = mgr.status()
            self.assertTrue(st.supported)
            self.assertTrue(st.enabled)
            self.assertTrue(st.loaded)

            ok_dis, _ = mgr.disable(unload_now=True)
            self.assertTrue(ok_dis)
            self.assertFalse(mgr.plist_path.exists())
            self.assertFalse(mgr.status().enabled)

            # 非 macOS 优雅返回不支持
            linux_mgr = AutostartManager(
                vmr_bin=vmr_bin,
                vmr_home=root / "home",
                launch_agents_dir=agents_dir,
                runner=fake_runner,
                platform_name="linux",
            )
            self.assertFalse(linux_mgr.status().supported)
            ok_linux, _ = linux_mgr.enable()
            self.assertFalse(ok_linux)

    def test_restart_token_0600_single_use_and_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            tok_file = write_restart_token(home, "secret-relay-token-123", ttl_sec=30.0)
            self.assertEqual(stat.S_IMODE(tok_file.stat().st_mode), 0o600)

            # 首次消费拿到原令牌并立刻销毁
            self.assertEqual(consume_restart_token(home), "secret-relay-token-123")
            self.assertFalse(tok_file.exists())
            self.assertIsNone(consume_restart_token(home))

            # 过期令牌自动拒收
            tok_file.write_text(json.dumps({"token": "old", "expires_at": time.time() - 5}), encoding="utf-8")
            self.assertIsNone(consume_restart_token(home))
            self.assertFalse(tok_file.exists())

    def test_updater_cache_etag_304_and_notes_summary(self) -> None:
        summary = _summarize_release_notes("## 更新亮点\n- **新增开机自启**：重启不掉线\n- 修复 `bug`\n")
        self.assertIn("新增开机自启", summary)
        self.assertNotIn("**", summary)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upd = SelfUpdater(
                repo="example/repo",
                current_version="1.1.0",
                managed_dir=root / "managed",
                vmr_home=root / "home",
            )
            fake_release = {
                "tag_name": "v1.2.0",
                "html_url": "https://github.com/example/repo/releases/tag/v1.2.0",
                "body": "### 新能力\n- 新增开机自启与无感热重启",
                "assets": [
                    {
                        "name": "vmr-macos-arm64-v1.2.0.tar.gz",
                        "browser_download_url": "https://example.invalid/vmr.tar.gz",
                    },
                    {
                        "name": "SHA256SUMS",
                        "browser_download_url": "https://example.invalid/SHA256SUMS",
                    },
                ],
            }

            class _FakeResp:
                def __init__(self, payload: bytes, headers: dict[str, str] | None = None) -> None:
                    self._payload = payload
                    self.headers = headers or {}
                def read(self) -> bytes:
                    return self._payload
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False

            call_count = {"n": 0}
            seen_etag = {"val": ""}

            def fake_urlopen(req, timeout=12):
                call_count["n"] += 1
                url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
                if "SHA256SUMS" in url:
                    return _FakeResp(b"deadbeef1234  vmr-macos-arm64-v1.2.0.tar.gz\n")
                if req.get_header("If-none-match"):
                    seen_etag["val"] = req.get_header("If-none-match")
                    raise urllib.error.HTTPError(url, 304, "Not Modified", {}, None)
                return _FakeResp(json.dumps(fake_release).encode("utf-8"), headers={"ETag": '"etag-v120"'})

            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                info1 = upd.check_latest(use_cache=True)
                self.assertTrue(info1.ok)
                self.assertTrue(info1.has_update)
                self.assertEqual(info1.latest_version, "1.2.0")
                self.assertEqual(info1.sha256, "deadbeef1234")
                self.assertIn("开机自启", info1.release_notes)
                self.assertFalse(info1.from_cache)

                # 24 小时内再次 use_cache=True 零网络命中
                n_before = call_count["n"]
                info2 = upd.check_latest(use_cache=True)
                self.assertTrue(info2.from_cache)
                self.assertEqual(call_count["n"], n_before)

                # 强制刷新携带 ETag，304 续期复用缓存
                info3 = upd.check_latest(use_cache=False)
                self.assertTrue(info3.ok)
                self.assertEqual(seen_etag["val"], '"etag-v120"')
                self.assertTrue(info3.from_cache)

            # 升级到 1.2.0 后，读取同一缓存不会再误报 has_update=True
            upd_new = SelfUpdater(
                repo="example/repo",
                current_version="1.2.0",
                managed_dir=root / "managed",
                vmr_home=root / "home",
            )
            cached = upd_new.read_cached_update()
            assert cached is not None
            self.assertFalse(cached.has_update)

    def test_daemon_relay_token_busy_update_guard_and_mcp_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            write_restart_token(home, "fixed-relay-token-xyz")
            cfg = load_config(vmr_home=home)
            daemon = VmrDaemon(cfg, port=0, start_watcher=False)
            self.assertEqual(daemon.token, "fixed-relay-token-xyz")
            info = daemon.start_in_thread()
            try:
                # 造一条正在转写的任务，验证 /api/v1/update/apply 返回 409 防打断
                daemon.store.upsert_discovered(
                    recording_id="busy-rec-1",
                    filename="busy.m4a",
                    title="进行中录音",
                    recorded_at="2026-11-25T21:00:00+08:00",
                    recorded_date="2026-11-25",
                    duration_sec=120.0,
                    source_kind="upload",
                    audio_path=str(home / "busy.m4a"),
                )
                daemon.store.update("busy-rec-1", status="transcribing")

                req = urllib.request.Request(
                    f"{info.base_url}/api/v1/update/apply",
                    data=b"{}",
                    headers={"Content-Type": "application/json", "X-VMR-Token": info.token},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 409)
                err_body = json.loads(ctx.exception.read().decode("utf-8"))
                self.assertTrue(err_body.get("busy"))

                # /api/v1/status 包含 active_jobs / autostart_enabled / update_available
                st_req = urllib.request.Request(
                    f"{info.base_url}/api/v1/status",
                    headers={"X-VMR-Token": info.token},
                )
                with urllib.request.urlopen(st_req, timeout=5) as resp:
                    st_data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(st_data["active_jobs"], 1)
                self.assertIn("autostart_enabled", st_data)
                self.assertIn("update_available", st_data)
            finally:
                daemon.stop()

            # MCP vmr_status 返回 autostart_enabled / update_available
            srv = McpStdioServer(cfg)
            resp_msg = srv.handle_message(
                {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "vmr_status", "arguments": {}}}
            )
            assert resp_msg is not None
            mcp_st = json.loads(resp_msg["result"]["content"][0]["text"])
            self.assertIn("autostart_enabled", mcp_st)
            self.assertIn("update_available", mcp_st)

    def test_rule_studio_differs_from_builtin_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            studio = RuleStudio(Path(tmp) / "rules")
            studio.ensure_initialized()
            tpl0 = studio.get_template("general_review")
            self.assertFalse(tpl0.differs_from_builtin)

            tpl1 = studio.save_template(
                "general_review",
                name=tpl0.name,
                description=tpl0.description,
                body=tpl0.body + "\n补充一条个人要求。",
                is_default=True,
            )
            self.assertTrue(tpl1.differs_from_builtin)

            tpl2 = studio.reset_builtin_template("general_review")
            self.assertFalse(tpl2.differs_from_builtin)


if __name__ == "__main__":
    unittest.main()
