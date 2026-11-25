"""公开 GitHub Releases 静默检查更新（24h 缓存 + ETag）与受管目录原子自更新器（SHA256 强校验 + 开发仓保护）。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

from voice_review import __version__
from voice_review.config import DEFAULT_MANAGED_DIR, VoiceReviewConfig

UPDATE_CACHE_FILENAME = "update_cache.json"
DEFAULT_CHECK_INTERVAL_SEC = 86400.0


def _summarize_release_notes(raw_body: str, *, max_len: int = 240) -> str:
    """将 GitHub Release Markdown 正文压成 1~2 句简洁纯文本亮点，供控制台横幅与 AI 顺口提醒展示。"""
    lines: list[str] = []
    for raw in (raw_body or "").replace("\r\n", "\n").splitlines():
        s = raw.strip()
        if not s or s.startswith("---"):
            continue
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"^[-*•]\s*", "", s)
        s = re.sub(r"`([^`]+)`", r"\1", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
        s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
        s = s.strip()
        if not s or s.startswith("本次") and len(s) < 16:
            continue
        lines.append(s)
        if len("；".join(lines)) >= max_len:
            break
    summary = "；".join(lines).strip()
    if len(summary) > max_len:
        summary = summary[: max_len - 1].rstrip("；;，, ") + "…"
    return summary


@dataclass(frozen=True)
class UpdateInfo:
    ok: bool
    current_version: str
    latest_version: str = ""
    has_update: bool = False
    download_url: str = ""
    sha256: str = ""
    release_url: str = ""
    release_notes: str = ""
    checked_at: float = 0.0
    from_cache: bool = False
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
            "release_notes": self.release_notes,
            "checked_at": self.checked_at,
            "from_cache": self.from_cache,
            "error": self.error,
        }


@dataclass(frozen=True)
class UpdateResult:
    ok: bool
    message: str
    installed_version: str = ""
    restarting: bool = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "message": self.message,
            "installed_version": self.installed_version,
            "restarting": self.restarting,
        }


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

    @property
    def cache_path(self) -> Path:
        return self.vmr_home / UPDATE_CACHE_FILENAME

    def _load_cache_dict(self) -> dict | None:
        if not self.cache_path.is_file():
            return None
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("repo") == self.repo:
                return data
        except Exception:
            pass
        return None

    def _save_cache_dict(self, data: dict) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError:
            pass

    def _info_from_cache_dict(self, data: dict, *, from_cache: bool = True) -> UpdateInfo:
        latest = str(data.get("latest_version") or "").lstrip("vV")
        has_upd = bool(latest) and _parse_ver(latest) > _parse_ver(self.current_version)
        return UpdateInfo(
            ok=True,
            current_version=self.current_version,
            latest_version=latest or self.current_version,
            has_update=has_upd,
            download_url=str(data.get("download_url") or ""),
            sha256=str(data.get("sha256") or ""),
            release_url=str(data.get("release_url") or ""),
            release_notes=str(data.get("release_notes") or ""),
            checked_at=float(data.get("checked_at") or 0.0),
            from_cache=from_cache,
        )

    def read_cached_update(self) -> UpdateInfo | None:
        """零网络开销读取本地上次静默检查结果，并基于当前运行版本实时重算 has_update。"""
        cached = self._load_cache_dict()
        if not cached or not cached.get("latest_version"):
            return None
        return self._info_from_cache_dict(cached, from_cache=True)

    def check_latest(self, *, use_cache: bool = False, max_age_sec: float = DEFAULT_CHECK_INTERVAL_SEC) -> UpdateInfo:
        now = time.time()
        cached = self._load_cache_dict()
        if use_cache and cached and cached.get("latest_version"):
            age = now - float(cached.get("checked_at") or 0.0)
            if 0 <= age < max_age_sec:
                return self._info_from_cache_dict(cached, from_cache=True)

        url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"voice-memo-review/{self.current_version}",
        }
        etag = str((cached or {}).get("etag") or "").strip()
        if etag:
            headers["If-None-Match"] = etag

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                new_etag = str(resp.headers.get("ETag") or "").strip()
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and cached and cached.get("latest_version"):
                cached["checked_at"] = now
                self._save_cache_dict(cached)
                return self._info_from_cache_dict(cached, from_cache=True)
            return UpdateInfo(ok=False, current_version=self.current_version, error=f"请求 GitHub Releases 失败：HTTP {exc.code}")
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

        notes = _summarize_release_notes(str(data.get("body") or ""))
        cache_obj = {
            "repo": self.repo,
            "etag": new_etag or etag,
            "latest_version": tag or self.current_version,
            "download_url": download_url,
            "sha256": expected_sha,
            "release_url": str(data.get("html_url") or ""),
            "release_notes": notes,
            "checked_at": now,
        }
        self._save_cache_dict(cache_obj)
        return self._info_from_cache_dict(cache_obj, from_cache=False)

    def apply_update(self, archive_url_or_path: str, expected_sha256: str = "") -> UpdateResult:
        if (self.managed_dir / ".git").exists():
            return UpdateResult(False, f"检测到当前运行在 Git 开发仓库（{self.managed_dir}），拒绝执行自动自覆盖；请使用 git pull。")

        installed_ver = ""
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

            entries = [p for p in stage.iterdir() if not p.name.startswith(".")]
            payload_root = entries[0] if (len(entries) == 1 and entries[0].is_dir()) else stage
            if not (payload_root / "voice_review").is_dir():
                return UpdateResult(False, "更新包结构无效：缺少 voice_review 目录")

            init_py = payload_root / "voice_review" / "__init__.py"
            if init_py.is_file():
                m = re.search(r'__version__\s*=\s*"([^"]+)"', init_py.read_text(encoding="utf-8", errors="ignore"))
                if m:
                    installed_ver = m.group(1)

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

        ver_note = f" v{installed_ver}" if installed_ver else ""
        return UpdateResult(
            True,
            f"已安全更新至{ver_note}（受管目录 {self.managed_dir}，个人配置与模板完整保留）",
            installed_version=installed_ver,
        )
