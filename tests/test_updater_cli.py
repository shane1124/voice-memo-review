"""Task 9 单元测试：GitHub Releases 自更新器 SHA256 校验、开发仓自覆盖保护、CLI 全部子命令与脚本语法检查。"""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from voice_review.cli import build_parser, main as cli_main
from voice_review.updater import SelfUpdater


def _make_fake_release_archive(dest_tar: Path, version_text: str) -> str:
    with tempfile.TemporaryDirectory() as src_tmp:
        root = Path(src_tmp) / "voice-memo-review"
        (root / "voice_review").mkdir(parents=True)
        (root / "voice_review" / "__init__.py").write_text(f'__version__ = "{version_text}"\n', encoding="utf-8")
        (root / "vmr").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        with tarfile.open(dest_tar, "w:gz") as tf:
            tf.add(root, arcname="voice-memo-review")
    return hashlib.sha256(dest_tar.read_bytes()).hexdigest()


class TestUpdaterAndCli(unittest.TestCase):
    def test_updater_verifies_sha256_refuses_git_dev_dir_and_preserves_user_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            tar_path = tmp / "vmr-macos-arm64-v1.1.0.tar.gz"
            real_sha = _make_fake_release_archive(tar_path, "1.1.0")

            user_home = tmp / "vmr_home"
            user_home.mkdir()
            secret_file = user_home / "secrets.json"
            secret_file.write_text('{"gemini_api_key": "KEEP"}', encoding="utf-8")

            # 1) 错误 SHA256 必须被拒绝
            managed = tmp / "managed"
            managed.mkdir()
            (managed / "voice_review").mkdir()
            (managed / "voice_review" / "__init__.py").write_text('__version__ = "1.0.0"\n', encoding="utf-8")
            upd = SelfUpdater(repo="shane1124/voice-memo-review", current_version="1.0.0", managed_dir=managed, vmr_home=user_home)
            bad = upd.apply_update(str(tar_path), expected_sha256="0" * 64)
            self.assertFalse(bad.ok)
            self.assertIn("SHA256", bad.message)

            # 2) 开发仓含 .git 必须拒绝自覆盖
            dev_dir = tmp / "dev_repo"
            (dev_dir / ".git").mkdir(parents=True)
            upd_dev = SelfUpdater(repo="shane1124/voice-memo-review", current_version="1.0.0", managed_dir=dev_dir, vmr_home=user_home)
            refused = upd_dev.apply_update(str(tar_path), expected_sha256=real_sha)
            self.assertFalse(refused.ok)
            self.assertIn("开发", refused.message)

            # 3) 正常受管目录原子更新且完全保留用户 secrets.json
            ok = upd.apply_update(str(tar_path), expected_sha256=real_sha)
            self.assertTrue(ok.ok)
            self.assertIn("1.1.0", (managed / "voice_review" / "__init__.py").read_text(encoding="utf-8"))
            self.assertEqual(secret_file.read_text(encoding="utf-8"), '{"gemini_api_key": "KEEP"}')

    def test_cli_subcommands_parsing_and_doctor(self) -> None:
        parser = build_parser()
        for argv in (
            ["doctor"],
            ["ui"],
            ["serve"],
            ["watch"],
            ["mcp"],
            ["install", "--target", "antigravity"],
            ["uninstall", "--target", "codex"],
            ["update", "--check"],
            ["run", "sample.m4a", "--template", "meeting_minutes"],
            ["prep", "sample.m4a"],
        ):
            parsed = parser.parse_args(argv)
            self.assertEqual(parsed.cmd, argv[0])

        with tempfile.TemporaryDirectory() as tmp:
            code = cli_main(["--vmr-home", tmp, "voiceprints"])
            self.assertEqual(code, 0)

    def test_install_and_build_scripts_syntax(self) -> None:
        repo = Path(__file__).resolve().parent.parent
        for rel in ("scripts/install.sh", "scripts/build_release.sh", "vmr"):
            script = repo / rel
            self.assertTrue(script.is_file(), f"缺少脚本：{rel}")
            res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
            self.assertEqual(res.returncode, 0, f"{rel} 语法错误：{res.stderr}")


if __name__ == "__main__":
    unittest.main()
