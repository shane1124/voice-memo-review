"""Task 7 单元测试：验证本地可视化控制台四视图资源挂载、安全 Token 传递与 SSE 接入。"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import urllib.request

from voice_review.config import load_config
from voice_review.daemon import VmrDaemon


class TestWebUI(unittest.TestCase):
    def test_ui_serves_html_css_js_with_four_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(vmr_home=Path(tmp))
            daemon = VmrDaemon(cfg, port=0, start_watcher=False)
            daemon.start_in_thread()
            try:
                base = f"http://127.0.0.1:{daemon.port}"
                html = urllib.request.urlopen(f"{base}/", timeout=5).read().decode("utf-8")
                js = urllib.request.urlopen(f"{base}/assets/app.js", timeout=5).read().decode("utf-8")
                css = urllib.request.urlopen(f"{base}/assets/style.css", timeout=5).read().decode("utf-8")
                for marker in ("工作台", "引擎插槽", "规则工作室", "AI 插件"):
                    self.assertIn(marker, html)
                self.assertIn("X-VMR-Token", js)
                self.assertIn("/api/v1/events", js)
                self.assertIn("history.replaceState", js)
                self.assertGreater(len(css), 400)
            finally:
                daemon.stop()


if __name__ == "__main__":
    unittest.main()
