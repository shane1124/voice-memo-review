"""Task 1 单元测试：0600 密钥存储、四插槽配置读写、旧版扁平配置兼容与插槽组合校验。"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest

from voice_review.config import load_config, save_config_dict, validate_provider_combo
from voice_review.secrets import SecretStore


class TestConfigAndSecrets(unittest.TestCase):
    def test_secret_store_chmod_0600_and_masking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "secrets.json"
            store = SecretStore(sp)
            store.set("gemini_api_key", "AIzaSySecret12345678")
            mode = stat.S_IMODE(os.stat(sp).st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(store.get("gemini_api_key"), "AIzaSySecret12345678")
            masked = store.masked_all()["gemini_api_key"]
            self.assertNotIn("Secret", masked)
            self.assertTrue(masked.endswith("5678"))
            self.assertTrue(store.delete("gemini_api_key"))
            self.assertEqual(store.get("gemini_api_key"), "")

    def test_secret_store_env_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecretStore(Path(tmp) / "secrets.json")
            prev = os.environ.get("VMR_TEST_KEY")
            os.environ["VMR_TEST_KEY"] = "env-secret-9999"
            try:
                self.assertEqual(store.get("test_key", env_var="VMR_TEST_KEY"), "env-secret-9999")
            finally:
                if prev is None:
                    os.environ.pop("VMR_TEST_KEY", None)
                else:
                    os.environ["VMR_TEST_KEY"] = prev

    def test_legacy_flat_config_and_provider_combo_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_p = Path(tmp) / "config.toml"
            cfg_p.write_text('output_dir = "out"\nmodel = "gemini-4-neon-xhigh"\n', encoding="utf-8")
            cfg = load_config(cfg_p)
            self.assertEqual(cfg.output_dir.name, "out")
            self.assertEqual(cfg.transcriber_provider, "antigravity-flash")
            self.assertEqual(cfg.analyzer_auto_provider, "antigravity-agentapi")
            self.assertEqual(cfg.update_repo, "shane1124/voice-memo-review")

            ok1, _ = validate_provider_combo("antigravity-flash", "antigravity-agentapi")
            ok2, msg2 = validate_provider_combo("antigravity-flash", "openai-compatible-llm")
            ok3, msg3 = validate_provider_combo("antigravity-flash", "gemini-llm")
            ok4, _ = validate_provider_combo("gemini-api", "none")
            ok5, _ = validate_provider_combo("openai-compatible-asr", "openai-compatible-llm")
            self.assertTrue(ok1)
            self.assertFalse(ok2)
            self.assertIn("antigravity-flash", msg2)
            self.assertFalse(ok3)
            self.assertIn("antigravity-flash", msg3)
            self.assertTrue(ok4)
            self.assertTrue(ok5)

    def test_save_and_reload_pluggable_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_p = Path(tmp) / "config.toml"
            drop_dir = Path(tmp) / "drop box"
            save_config_dict(
                cfg_p,
                {
                    "output_dir": str(Path(tmp) / "out"),
                    "sources": {
                        "voice_memos_enabled": False,
                        "drop_folder_enabled": True,
                        "drop_folder_dir": str(drop_dir),
                    },
                    "preprocessor": {"mode": "none"},
                    "transcriber": {
                        "provider": "openai-compatible-asr",
                        "base_url": "https://api.groq.com/openai/v1",
                        "model": "whisper-large-v3-turbo",
                    },
                    "analyzer": {
                        "auto_provider": "openai-compatible-llm",
                        "base_url": "https://api.deepseek.com/v1",
                        "model": "deepseek-chat",
                    },
                    "update": {"repo": "shane1124/voice-memo-review"},
                    "ui": {"port": 18765},
                },
            )
            cfg = load_config(cfg_p)
            self.assertFalse(cfg.voice_memos_enabled)
            self.assertTrue(cfg.drop_folder_enabled)
            self.assertEqual(cfg.drop_folder_dir, drop_dir.resolve())
            self.assertEqual(cfg.preprocessor_mode, "none")
            self.assertEqual(cfg.transcriber_provider, "openai-compatible-asr")
            self.assertEqual(cfg.transcriber_settings["model"], "whisper-large-v3-turbo")
            self.assertEqual(cfg.analyzer_auto_provider, "openai-compatible-llm")
            self.assertEqual(cfg.analyzer_settings["model"], "deepseek-chat")
            self.assertEqual(cfg.update_repo, "shane1124/voice-memo-review")
            self.assertEqual(cfg.ui_port, 18765)


if __name__ == "__main__":
    unittest.main()
