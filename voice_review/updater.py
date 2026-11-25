"""公开 GitHub Releases 检查更新与受管目录原子自更新器（含 SHA256 校验与开发仓保护）。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request

from voice_review import __version__
from voice_review.config import DEFAULT_MANAGED_DIR, VoiceReviewConfig


@dataclass(frozen=True)
class UpdateInfo:
    ok: bool
    current_version: str
    latest_version: str = ""
    has_update: bool = False
    download_url: str = ""
    sha256: str = ""
    release_url: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "has_update": self.has_update,
            "download_url": self.download_url,
            "sha256": self.sha256,
            "release_url": self.release_url,
            "error": self.error,
        }


@dataclass(frozen=True)
class UpdateResult:
    ok: bool
    message: str
    installed_version: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "message": self.message, "installed_version": self.installed_version}


def _parse_ver(v: str) -> tuple[int, ...]:
    clean = (v or "").strip().lstrip("vV")
    nums: list[int] = []
    for part in clean.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums or [0])


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class SelfUpdater:
    def __init__(
        self,
        *,
        repo: str,
        current_version: str,
        managed_dir: Path,
        vmr_home: Path,
    ) -> None:
        self.repo = repo.strip()
        self.current_version = current_version.strip().lstrip("vV")
        self.managed_dir = Path(managed_dir).expanduser().resolve()
        self.vmr_home = Path(vmr_home).expanduser().resolve()

    @classmethod
    def default(cls, cfg: VoiceReviewConfig) -> "SelfUpdater":
        pkg_root = Path(__file__).resolve().parent.parent
        managed = Path(DEFAULT_MANAGED_DIR).expanduser().resolve()
        target = pkg_root if (pkg_root / ".git").exists() else managed
        return cls(repo=cfg.update_repo, current_version=__version__, managed_dir=target, vmr_home=cfg.vmr_home)

    def check_latest(self) -> UpdateInfo:
        url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": f"voice-memo-review/{self.current_version}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            return UpdateInfo(ok=False, current_version=self.current_version, error=f"请求 GitHub Releases 失败：{exc}")

        tag = str(data.get("tag_name") or "").lstrip("vV")
        assets = data.get("assets") or []
        tar_asset = next(
            (a for a in assets if str(a.get("name") or "").endswith(".tar.gz") and "macos" in str(a.get("name") or "").lower()),
            next((a for a in assets if str(a.get("name") or "").endswith(".tar.gz")), None),
        )
        sha_asset = next((a for a in assets if "sha256" in str(a.get("name") or "").lower()), None)
        download_url = str(tar_asset.get("browser_download_url") or "") if tar_asset else ""
        expected_sha = ""
        if sha_asset and tar_asset:
            try:
                sha_req = urllib.request.Request(
                    str(sha_asset["browser_download_url"]),
                    headers={"User-Agent": f"voice-memo-review/{self.current_version}"},
                )
                with urllib.request.urlopen(sha_req, timeout=12) as sresp:
                    target_name = str(tar_asset["name"])
                    for line in sresp.read().decode("utf-8", errors="ignore").splitlines():
                        parts = line.strip().split()
                        if len(parts) >= 2 and Path(parts[-1].lstrip("*")).name == target_name:
                            expected_sha = parts[0].lower()
                            break
            except Exception:
                expected_sha = ""

        has_upd = bool(tag) and _parse_ver(tag) > _parse_ver(self.current_version)
        return UpdateInfo(
            ok=True,
            current_version=self.current_version,
            latest_version=tag or self.current_version,
            has_update=has_upd,
            download_url=download_url,
            sha256=expected_sha,
            release_url=str(data.get("html_url") or ""),
        )

    def apply_update(self, archive_url_or_path: str, expected_sha256: str = "") -> UpdateResult:
        if (self.managed_dir / ".git").exists():
            return UpdateResult(False, f"检测到当前运行在 Git 开发仓库（{self.managed_dir}），拒绝执行自动自覆盖；请使用 git pull。")

        with tempfile.TemporaryDirectory(prefix="vmr-update-") as tmp_s:
            tmp = Path(tmp_s)
            src_str = archive_url_or_path.strip()
            is_remote = src_str.startswith(("http://", "https://"))
            if is_remote and not expected_sha256.strip():
                return UpdateResult(False, "远程更新缺少 SHA256 校验值（SHA256SUMS 缺失或未匹配），为保障安全已中止更新。")
            if is_remote:
                archive_path = tmp / "release.tar.gz"
                req = urllib.request.Request(src_str, headers={"User-Agent": f"voice-memo-review/{self.current_version}"})
                try:
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        archive_path.write_bytes(resp.read())
                except Exception as exc:
                    return UpdateResult(False, f"下载更新包失败：{exc}")
            else:
                archive_path = Path(src_str).expanduser().resolve()
                if not archive_path.is_file():
                    return UpdateResult(False, f"本地更新包不存在：{archive_path}")

            if expected_sha256:
                actual = _sha256_file(archive_path).lower()
                if actual != expected_sha256.strip().lower():
                    return UpdateResult(False, f"SHA256 校验未通过（期望 {expected_sha256[:12]}，实际 {actual[:12]}），已中止更新。")

            stage = tmp / "extracted"
            stage.mkdir()
            try:
                with tarfile.open(archive_path, "r:gz") as tf:
                    tf.extractall(stage, filter="data")
            except Exception as exc:
                return UpdateResult(False, f"解压更新包失败：{exc}")

            # 兼容顶层包含单一子目录（如 voice-memo-review/）或平铺文件
            entries = [p for p in stage.iterdir() if not p.name.startswith(".")]
            payload_root = entries[0] if (len(entries) == 1 and entries[0].is_dir()) else stage
            if not (payload_root / "voice_review").is_dir():
                return UpdateResult(False, "更新包结构无效：缺少 voice_review 目录")

            parent = self.managed_dir.parent
            parent.mkdir(parents=True, exist_ok=True)
            staging_target = parent / f".{self.managed_dir.name}.new"
            backup_target = parent / f".{self.managed_dir.name}.old"
            if staging_target.exists():
                shutil.rmtree(staging_target)
            if backup_target.exists():
                shutil.rmtree(backup_target)
            shutil.copytree(payload_root, staging_target)

            if self.managed_dir.exists():
                self.managed_dir.rename(backup_target)
            try:
                staging_target.rename(self.managed_dir)
            except Exception as exc:
                if backup_target.exists() and not self.managed_dir.exists():
                    backup_target.rename(self.managed_dir)
                return UpdateResult(False, f"原子切换受管目录失败：{exc}")
            finally:
                if backup_target.exists():
                    shutil.rmtree(backup_target, ignore_errors=True)

        # 更新程序后自动刷新已接入 AI 客户端的 Skill/MCP 定义（失败不阻断升级）
        try:
            from voice_review.installer import ClientInstaller
            inst = ClientInstaller.default()
            already = [k for k, st in inst.detect_clients().items() if st.installed]
            if already:
                inst.install(targets=already)
        except Exception:
            pass

        return UpdateResult(True, f"已安全更新至受管目录 {self.managed_dir}（用户数据目录 {self.vmr_home} 完整保留）")
