"""API 密钥与敏感凭证本地隔离存储（独立 secrets.json，强制 0600 权限，绝不输出明文）。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any


def mask_secret(value: str) -> str:
    """将密钥脱敏为掩码展示，保留末尾最多 4 位以便辨识。"""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "****"
    if len(text) <= 8:
        return "****" + text[-2:]
    prefix = text[:3] if text.startswith(("sk-", "AIz", "gsk")) else ""
    return f"{prefix}****{text[-4:]}"


class SecretStore:
    """独立管理本机 secrets.json，所有写入均为原子替换并强制 chmod 0600。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def _load_raw(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v.strip()}

    def _save_raw(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".secrets-", suffix=".tmp", dir=str(self.path.parent))
        tmp_path = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def get(self, key: str, *, env_var: str = "") -> str:
        val = self._load_raw().get(key, "").strip()
        if val:
            return val
        if env_var:
            return os.environ.get(env_var, "").strip()
        return ""

    def set(self, key: str, value: str) -> None:
        clean_key = key.strip()
        if not clean_key:
            raise ValueError("密钥名称不能为空")
        data = self._load_raw()
        clean_val = (value or "").strip()
        if clean_val:
            data[clean_key] = clean_val
        else:
            data.pop(clean_key, None)
        self._save_raw(data)

    def update_many(self, mapping: dict[str, Any], *, delete_keys: list[str] | None = None) -> None:
        """批量更新密钥；空字符串与含掩码 '****' 均保持原值不覆盖；仅 delete_keys 显式删除。"""
        data = self._load_raw()
        changed = False
        for k in (delete_keys or []):
            clean_del = str(k).strip()
            if clean_del in data:
                del data[clean_del]
                changed = True
        for k, v in mapping.items():
            if not isinstance(v, str):
                continue
            clean_key = str(k).strip()
            clean_val = v.strip()
            if not clean_key or not clean_val or "****" in clean_val:
                continue
            if data.get(clean_key) != clean_val:
                data[clean_key] = clean_val
                changed = True
        if changed or not self.path.exists():
            self._save_raw(data)

    def delete(self, key: str) -> bool:
        data = self._load_raw()
        if key not in data:
            return False
        del data[key]
        self._save_raw(data)
        return True

    def keys(self) -> list[str]:
        return sorted(self._load_raw().keys())

    def masked_all(self) -> dict[str, str]:
        return {k: mask_secret(v) for k, v in self._load_raw().items()}
