"""v1.3.0 单元测试：通用本地私有扩展引擎（before_prompt / after_analysis / on_daemon_tick）、异常隔离与 REST 开关。"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import urllib.request

from voice_review.config import load_config
from voice_review.daemon import VmrDaemon
from voice_review.extensions import ExtensionManager
from voice_review.mcp_server import McpStdioServer


def _create_ext(
    extensions_dir: Path,
    ext_id: str,
    *,
    name: str,
    hooks: list[str],
    code: str,
    enabled: bool = True,
) -> Path:
    d = extensions_dir / ext_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "extension.json").write_text(
        json.dumps(
            {
                "name": name,
                "description": f"测试扩展 {ext_id}",
                "enabled": enabled,
                "entry": "plugin.py",
                "hooks": hooks,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (d / "plugin.py").write_text(code, encoding="utf-8")
    return d


class TestLocalExtensions(unittest.TestCase):
    def test_hooks_lifecycle_enable_disable_and_fault_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ext_dir = Path(tmp) / "extensions"
            mgr = ExtensionManager(ext_dir)
            self.assertEqual(mgr.list_extensions(), [])

            _create_ext(
                ext_dir,
                "demo_ctx",
                name="知识上下文示例",
                hooks=["before_prompt", "after_analysis", "on_daemon_tick"],
                code=(
                    "from pathlib import Path\n"
                    "def before_prompt(ctx):\n"
                    "    return '## 外部知识上下文\\n- 来源标题：' + str(ctx.get('title', ''))\n"
                    "def after_analysis(ctx):\n"
                    "    p = Path(ctx['extension_dir']) / 'after.log'\n"
                    "    p.write_text(str(ctx.get('analysis_path', '')), encoding='utf-8')\n"
                    "    return 'ok'\n"
                    "def on_daemon_tick(ctx):\n"
                    "    return {'tick': True}\n"
                ),
            )
            # 故意放入一个抛异常的扩展，验证完全隔离不崩溃
            _create_ext(
                ext_dir,
                "broken_ext",
                name="异常扩展",
                hooks=["before_prompt", "after_analysis", "on_daemon_tick"],
                code=(
                    "def before_prompt(ctx): raise RuntimeError('boom1')\n"
                    "def after_analysis(ctx): raise RuntimeError('boom2')\n"
                    "def on_daemon_tick(ctx): raise RuntimeError('boom3')\n"
                ),
            )

            items = mgr.list_extensions()
            self.assertEqual([e.id for e in items], ["broken_ext", "demo_ctx"])

            blocks = mgr.run_before_prompt({"title": "周例会"})
            self.assertEqual(len(blocks), 1)
            self.assertIn("来源标题：周例会", blocks[0])

            after_res = mgr.run_after_analysis({"analysis_path": "/tmp/a.md"})
            self.assertEqual(len(after_res), 2)
            self.assertTrue((ext_dir / "demo_ctx" / "after.log").read_text(encoding="utf-8") == "/tmp/a.md")

            tick_res = mgr.run_daemon_tick({"now_ts": 1.0})
            self.assertEqual(len(tick_res), 2)

            # 停用 demo_ctx 后不再触发
            upd = mgr.set_enabled("demo_ctx", False)
            self.assertFalse(upd.enabled)
            self.assertEqual(mgr.run_before_prompt({"title": "周例会"}), [])

    def test_daemon_rest_api_and_mcp_integrate_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            _create_ext(
                home / "extensions",
                "custom_kb",
                name="本地知识挂载",
                hooks=["before_prompt", "after_analysis"],
                code=(
                    "from pathlib import Path\n"
                    "def before_prompt(ctx):\n"
                    "    return '## 本地知识库参考\\n- 附带外部背景文档'\n"
                    "def after_analysis(ctx):\n"
                    "    (Path(ctx['extension_dir']) / 'saved.txt').write_text(ctx['recording_id'], encoding='utf-8')\n"
                ),
            )
            cfg = load_config(vmr_home=home)
            daemon = VmrDaemon(cfg, port=0, start_watcher=False)
            info = daemon.start_in_thread()
            try:
                # 1) GET /api/v1/extensions 返回本地扩展
                req = urllib.request.Request(
                    f"{info.base_url}/api/v1/extensions",
                    headers={"X-VMR-Token": info.token},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    exts = json.loads(resp.read().decode("utf-8"))["extensions"]
                self.assertEqual(len(exts), 1)
                self.assertEqual(exts[0]["id"], "custom_kb")
                self.assertTrue(exts[0]["enabled"])

                # 2) 模板预览自动透出 before_prompt 扩展段落
                preq = urllib.request.Request(
                    f"{info.base_url}/api/v1/templates/preview",
                    data=json.dumps({"template_id": "general_review"}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "X-VMR-Token": info.token},
                    method="POST",
                )
                with urllib.request.urlopen(preq, timeout=5) as resp:
                    prompt = json.loads(resp.read().decode("utf-8"))["prompt"]
                self.assertIn("本地知识库参考", prompt)

                # 3) PUT /api/v1/extensions/custom_kb 停用后不再出现在预览里
                ureq = urllib.request.Request(
                    f"{info.base_url}/api/v1/extensions/custom_kb",
                    data=json.dumps({"enabled": False}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "X-VMR-Token": info.token},
                    method="PUT",
                )
                with urllib.request.urlopen(ureq, timeout=5) as resp:
                    self.assertFalse(json.loads(resp.read().decode("utf-8"))["extension"]["enabled"])

                with urllib.request.urlopen(preq, timeout=5) as resp:
                    prompt2 = json.loads(resp.read().decode("utf-8"))["prompt"]
                self.assertNotIn("本地知识库参考", prompt2)
            finally:
                daemon.stop()

            # 4) 重新启用并验证 MCP vmr_save_analysis 触发 after_analysis
            ExtensionManager.from_vmr_home(home).set_enabled("custom_kb", True)
            srv = McpStdioServer(cfg)
            srv.store.upsert_discovered(
                recording_id="rec-ext-1",
                filename="a.m4a",
                title="测试录音",
                recorded_at="2026-11-25T21:00:00+08:00",
                recorded_date="2026-11-25",
                duration_sec=30.0,
                source_kind="upload",
                audio_path=str(home / "a.m4a"),
            )
            srv.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {
                        "name": "vmr_save_analysis",
                        "arguments": {"recording_id": "rec-ext-1", "markdown": "# 分析正文"},
                    },
                }
            )
            self.assertEqual((home / "extensions" / "custom_kb" / "saved.txt").read_text(encoding="utf-8"), "rec-ext-1")


if __name__ == "__main__":
    unittest.main()
