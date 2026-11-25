"""通用本地扩展钩子引擎（ExtensionManager）：物理隔离于用户数据目录，公共主仓零侵入。"""
from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)

EXT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
SUPPORTED_HOOKS = ("before_prompt", "after_analysis", "on_daemon_tick")


@dataclass(frozen=True)
class ExtensionInfo:
    id: str
    name: str
    description: str
    enabled: bool
    entry: str
    hooks: tuple[str, ...]
    path: str
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "entry": self.entry,
            "hooks": list(self.hooks),
            "path": self.path,
            "error": self.error,
        }


def validate_extension_id(ext_id: str) -> str:
    clean = (ext_id or "").strip()
    if not EXT_ID_RE.fullmatch(clean):
        raise ValueError("扩展 ID 仅支持小写字母、数字、短横线与下划线（长度 1～48）")
    return clean


class ExtensionManager:
    """加载并隔离执行位于 {vmr_home}/extensions/ 下的本机私有扩展模块。"""

    def __init__(self, extensions_dir: Path) -> None:
        self.extensions_dir = Path(extensions_dir).expanduser().resolve()
        self._module_cache: dict[str, tuple[int, ModuleType]] = {}

    @classmethod
    def from_vmr_home(cls, vmr_home: Path) -> "ExtensionManager":
        return cls(Path(vmr_home).expanduser().resolve() / "extensions")

    def _resolve_entry(self, ext_dir: Path, entry_name: str) -> Path:
        entry = (ext_dir / (entry_name or "plugin.py")).resolve()
        if not entry.is_relative_to(ext_dir.resolve()):
            raise ValueError(f"扩展入口路径越界：{entry_name}")
        return entry

    def _load_manifest(self, ext_dir: Path) -> tuple[ExtensionInfo, Path | None]:
        ext_id = ext_dir.name
        manifest_path = ext_dir / "extension.json"
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("extension.json 顶层必须为 JSON 对象")
        except Exception as exc:
            return (
                ExtensionInfo(
                    id=ext_id,
                    name=ext_id,
                    description="",
                    enabled=False,
                    entry="plugin.py",
                    hooks=(),
                    path=str(ext_dir),
                    error=f"清单解析失败：{exc}",
                ),
                None,
            )

        entry_name = str(raw.get("entry") or "plugin.py").strip() or "plugin.py"
        hooks_raw = raw.get("hooks") if isinstance(raw.get("hooks"), list) else []
        hooks = tuple(str(h).strip() for h in hooks_raw if str(h).strip() in SUPPORTED_HOOKS)
        enabled = bool(raw.get("enabled", True))
        name = str(raw.get("name") or ext_id).strip() or ext_id
        desc = str(raw.get("description") or "").strip()

        try:
            entry_path = self._resolve_entry(ext_dir, entry_name)
        except ValueError as exc:
            return (
                ExtensionInfo(
                    id=ext_id,
                    name=name,
                    description=desc,
                    enabled=False,
                    entry=entry_name,
                    hooks=hooks,
                    path=str(ext_dir),
                    error=str(exc),
                ),
                None,
            )
        if not entry_path.is_file():
            return (
                ExtensionInfo(
                    id=ext_id,
                    name=name,
                    description=desc,
                    enabled=False,
                    entry=entry_name,
                    hooks=hooks,
                    path=str(ext_dir),
                    error=f"入口文件不存在：{entry_name}",
                ),
                None,
            )
        return (
            ExtensionInfo(
                id=ext_id,
                name=name,
                description=desc,
                enabled=enabled,
                entry=entry_name,
                hooks=hooks,
                path=str(ext_dir),
            ),
            entry_path,
        )

    def _load_module(self, ext_id: str, entry_path: Path) -> ModuleType:
        mtime_ns = entry_path.stat().st_mtime_ns
        cached = self._module_cache.get(ext_id)
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]
        mod_name = f"vmr_local_ext_{ext_id.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(mod_name, str(entry_path))
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载扩展模块：{entry_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self._module_cache[ext_id] = (mtime_ns, mod)
        return mod

    def list_extensions(self) -> list[ExtensionInfo]:
        if not self.extensions_dir.is_dir():
            return []
        items: list[ExtensionInfo] = []
        for child in sorted(self.extensions_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if not EXT_ID_RE.fullmatch(child.name):
                continue
            if not (child / "extension.json").is_file():
                continue
            info, _ = self._load_manifest(child)
            items.append(info)
        return items

    def set_enabled(self, ext_id: str, enabled: bool) -> ExtensionInfo:
        clean = validate_extension_id(ext_id)
        ext_dir = self.extensions_dir / clean
        manifest_path = ext_dir / "extension.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"未找到本地扩展：{clean}")
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("extension.json 格式无效")
        raw["enabled"] = bool(enabled)
        fd, tmp_name = tempfile.mkstemp(prefix=".ext-", suffix=".tmp", dir=str(ext_dir))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
            os.replace(tmp_path, manifest_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        info, _ = self._load_manifest(ext_dir)
        return info

    def _iter_enabled_modules(self, hook_name: str):
        if not self.extensions_dir.is_dir():
            return
        for child in sorted(self.extensions_dir.iterdir()):
            if not child.is_dir() or child.name.startswith(".") or not EXT_ID_RE.fullmatch(child.name):
                continue
            if not (child / "extension.json").is_file():
                continue
            info, entry_path = self._load_manifest(child)
            if not info.enabled or entry_path is None:
                continue
            if info.hooks and hook_name not in info.hooks:
                continue
            try:
                mod = self._load_module(info.id, entry_path)
            except Exception as exc:
                logger.warning("加载本地扩展 [%s] 失败（已隔离）：%s", info.id, exc)
                continue
            fn = getattr(mod, hook_name, None)
            if callable(fn):
                yield info, fn

    def run_before_prompt(self, ctx: dict[str, Any]) -> list[str]:
        blocks: list[str] = []
        for info, fn in self._iter_enabled_modules("before_prompt"):
            try:
                res = fn(dict(ctx, extension_dir=info.path, extension_id=info.id))
                if isinstance(res, str) and res.strip():
                    blocks.append(res.strip())
            except Exception as exc:
                logger.warning("本地扩展 [%s].before_prompt 执行异常（已隔离）：%s", info.id, exc)
        return blocks

    def run_after_analysis(self, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for info, fn in self._iter_enabled_modules("after_analysis"):
            try:
                out = fn(dict(ctx, extension_dir=info.path, extension_id=info.id))
                results.append({"id": info.id, "ok": True, "result": out})
            except Exception as exc:
                logger.warning("本地扩展 [%s].after_analysis 执行异常（已隔离）：%s", info.id, exc)
                results.append({"id": info.id, "ok": False, "error": str(exc)})
        return results

    def run_daemon_tick(self, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for info, fn in self._iter_enabled_modules("on_daemon_tick"):
            try:
                out = fn(dict(ctx, extension_dir=info.path, extension_id=info.id))
                results.append({"id": info.id, "ok": True, "result": out})
            except Exception as exc:
                logger.warning("本地扩展 [%s].on_daemon_tick 执行异常（已隔离）：%s", info.id, exc)
                results.append({"id": info.id, "ok": False, "error": str(exc)})
        return results
