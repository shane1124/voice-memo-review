"""跨 AI 客户端一键安装与卸载器（Antigravity 插件目录 / Claude Code JSON / Codex TOML / Cursor JSON）。"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from voice_review import __version__

PLUGIN_NAME = "voice-memo-review"
CODEX_BEGIN = "# >>> voice-memo-review managed >>>"
CODEX_END = "# <<< voice-memo-review managed <<<"


@dataclass(frozen=True)
class ClientStatus:
    client: str
    display_name: str
    detected: bool
    installed: bool
    config_path: str

    def to_dict(self) -> dict:
        return {
            "client": self.client,
            "display_name": self.display_name,
            "detected": self.detected,
            "installed": self.installed,
            "config_path": self.config_path,
        }


@dataclass(frozen=True)
class InstallResult:
    client: str
    ok: bool
    message: str
    config_path: str

    def to_dict(self) -> dict:
        return {
            "client": self.client,
            "ok": self.ok,
            "message": self.message,
            "config_path": self.config_path,
        }


def _backup_if_exists(path: Path) -> None:
    if path.is_file():
        bak = path.with_name(path.name + ".bak")
        shutil.copy2(path, bak)


def _copy_skill(src_dir: Path, dst_dir: Path) -> None:
    if not src_dir.is_dir():
        return
    dst_dir.parent.mkdir(parents=True, exist_ok=True)
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    shutil.copytree(src_dir, dst_dir)


def _strip_codex_section(text: str) -> str:
    # 1) 精确移除受管标记区块
    pat_managed = re.compile(rf"\n?{re.escape(CODEX_BEGIN)}.*?{re.escape(CODEX_END)}\n?", re.S)
    cleaned = pat_managed.sub("\n", text)

    # 2) 按行精确剥离手写 [mcp_servers.voice-memo-review] 及其子表，绝不误吞下一段落顶部的 # 注释
    header_re = re.compile(r'^\s*\[mcp_servers\.(?:"voice-memo-review"|voice-memo-review)(?:\.[^\]]+)?\]\s*$')
    any_table_re = re.compile(r"^\s*\[[^\]]+\]\s*$")
    lines = cleaned.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        if header_re.match(lines[i]):
            i += 1
            pending_comments: list[str] = []
            while i < len(lines) and not any_table_re.match(lines[i]):
                stripped = lines[i].strip()
                if stripped == "" or stripped.startswith("#"):
                    pending_comments.append(lines[i])
                else:
                    # 仍属于本段键值，前面缓存的注释属于本段内部，直接丢弃
                    pending_comments.clear()
                i += 1
            # 紧邻下一 [table] 之前且前面有空行分隔的注释归属下一段，原样保留
            if i < len(lines) and pending_comments:
                out.extend(pending_comments)
            continue
        out.append(lines[i])
        i += 1
    result = "".join(out)
    return re.sub(r"\n{3,}", "\n\n", result).rstrip() + ("\n" if result.strip() else "")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


class ClientInstaller:
    """无损、幂等管理 4 大主流 AI 客户端的 MCP 与 Skill 接入。"""

    def __init__(
        self,
        *,
        vmr_bin: Path,
        skill_source_dir: Path,
        home_dir: Path | None = None,
    ) -> None:
        self.vmr_bin = Path(vmr_bin).expanduser()
        self.skill_source_dir = Path(skill_source_dir).expanduser().resolve()
        self.home = Path(home_dir).expanduser().resolve() if home_dir else Path.home().resolve()

    @classmethod
    def default(cls) -> "ClientInstaller":
        pkg_root = Path(__file__).resolve().parent.parent
        local_bin = Path.home() / ".local" / "bin" / "vmr"
        vmr_bin = local_bin if local_bin.is_file() else (pkg_root / "vmr").resolve()
        return cls(vmr_bin=vmr_bin, skill_source_dir=pkg_root / "skills" / PLUGIN_NAME)

    def _paths(self) -> dict[str, dict[str, Path]]:
        return {
            "antigravity": {
                "root": self.home / ".gemini" / "config",
                "plugin_dir": self.home / ".gemini" / "config" / "plugins" / PLUGIN_NAME,
            },
            "claude": {
                "root": self.home / ".claude",
                "config": self.home / ".claude.json",
                "skill": self.home / ".claude" / "skills" / PLUGIN_NAME,
            },
            "codex": {
                "root": self.home / ".codex",
                "config": self.home / ".codex" / "config.toml",
                "skill": self.home / ".codex" / "skills" / PLUGIN_NAME,
            },
            "cursor": {
                "root": self.home / ".cursor",
                "config": self.home / ".cursor" / "mcp.json",
            },
        }

    def detect_clients(self) -> dict[str, ClientStatus]:
        p = self._paths()
        out: dict[str, ClientStatus] = {}

        # Antigravity
        agy_plug = p["antigravity"]["plugin_dir"]
        out["antigravity"] = ClientStatus(
            client="antigravity",
            display_name="Antigravity（反重力）",
            detected=p["antigravity"]["root"].is_dir(),
            installed=(agy_plug / "plugin.json").is_file() and (agy_plug / "mcp_config.json").is_file(),
            config_path=str(agy_plug),
        )

        # Claude Code
        claude_cfg = p["claude"]["config"]
        claude_inst = False
        if claude_cfg.is_file():
            try:
                claude_inst = PLUGIN_NAME in (json.loads(claude_cfg.read_text(encoding="utf-8")).get("mcpServers") or {})
            except Exception:
                claude_inst = False
        out["claude"] = ClientStatus(
            client="claude",
            display_name="Claude Code",
            detected=claude_cfg.is_file() or p["claude"]["root"].is_dir(),
            installed=claude_inst,
            config_path=str(claude_cfg),
        )

        # Codex
        codex_cfg = p["codex"]["config"]
        codex_inst = codex_cfg.is_file() and "[mcp_servers.voice-memo-review]" in codex_cfg.read_text(encoding="utf-8", errors="ignore")
        out["codex"] = ClientStatus(
            client="codex",
            display_name="Codex CLI",
            detected=p["codex"]["root"].is_dir() or codex_cfg.is_file(),
            installed=codex_inst,
            config_path=str(codex_cfg),
        )

        # Cursor
        cursor_cfg = p["cursor"]["config"]
        cursor_inst = False
        if cursor_cfg.is_file():
            try:
                cursor_inst = PLUGIN_NAME in (json.loads(cursor_cfg.read_text(encoding="utf-8")).get("mcpServers") or {})
            except Exception:
                cursor_inst = False
        out["cursor"] = ClientStatus(
            client="cursor",
            display_name="Cursor",
            detected=p["cursor"]["root"].is_dir() or cursor_cfg.is_file(),
            installed=cursor_inst,
            config_path=str(cursor_cfg),
        )
        return out

    def install(self, targets: list[str] | None = None) -> dict[str, InstallResult]:
        detected = self.detect_clients()
        if not targets:
            selected = [k for k, st in detected.items() if st.detected]
        else:
            selected = [t.strip().lower() for t in targets if t.strip().lower() in detected]

        results: dict[str, InstallResult] = {}
        for client in selected:
            if client == "antigravity":
                results[client] = self._install_antigravity()
            elif client == "claude":
                results[client] = self._install_json_mcp("claude", self._paths()["claude"]["config"], self._paths()["claude"]["skill"])
            elif client == "cursor":
                results[client] = self._install_json_mcp("cursor", self._paths()["cursor"]["config"], None)
            elif client == "codex":
                results[client] = self._install_codex()
        return results

    def uninstall(self, targets: list[str] | None = None) -> dict[str, InstallResult]:
        all_clients = ("antigravity", "claude", "codex", "cursor")
        selected = [t.strip().lower() for t in targets if t.strip().lower() in all_clients] if targets else list(all_clients)
        results: dict[str, InstallResult] = {}
        for client in selected:
            if client == "antigravity":
                results[client] = self._uninstall_antigravity()
            elif client == "claude":
                results[client] = self._uninstall_json_mcp("claude", self._paths()["claude"]["config"], self._paths()["claude"]["skill"])
            elif client == "cursor":
                results[client] = self._uninstall_json_mcp("cursor", self._paths()["cursor"]["config"], None)
            elif client == "codex":
                results[client] = self._uninstall_codex()
        return results

    def _install_antigravity(self) -> InstallResult:
        plug_dir = self._paths()["antigravity"]["plugin_dir"]
        plug_dir.mkdir(parents=True, exist_ok=True)
        # 严格遵循 Antigravity 官方插件规范：仅写插件自身目录，绝不手改全局 config.json
        (plug_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "name": PLUGIN_NAME,
                    "displayName": "语音复盘助手",
                    "version": __version__,
                    "description": "可插拔本地声纹切段、多引擎转写与显化规则语音复盘插件。",
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        (plug_dir / "mcp_config.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        PLUGIN_NAME: {
                            "command": str(self.vmr_bin),
                            "args": ["mcp"],
                        }
                    }
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        _copy_skill(self.skill_source_dir, plug_dir / "skills" / PLUGIN_NAME)
        return InstallResult("antigravity", True, "已生成独立 Antigravity 插件（自动加载，未修改全局 config.json）", str(plug_dir))

    def _uninstall_antigravity(self) -> InstallResult:
        plug_dir = self._paths()["antigravity"]["plugin_dir"]
        if plug_dir.exists():
            shutil.rmtree(plug_dir)
        return InstallResult("antigravity", True, "已移除 Antigravity 插件目录", str(plug_dir))

    def _install_json_mcp(self, client: str, cfg_path: Path, skill_dst: Path | None) -> InstallResult:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        _backup_if_exists(cfg_path)
        data: dict = {}
        if cfg_path.is_file():
            try:
                loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                return InstallResult(client, False, f"已有 JSON 配置解析失败，已中止写入：{exc}", str(cfg_path))
            if not isinstance(loaded, dict):
                return InstallResult(client, False, "已有 JSON 顶层不是对象，已中止写入防止破坏原配置", str(cfg_path))
            data = loaded
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            return InstallResult(client, False, "已有配置 mcpServers 字段不是对象，已中止写入", str(cfg_path))
        entry: dict = {"command": str(self.vmr_bin), "args": ["mcp"]}
        if client == "claude":
            entry["type"] = "stdio"
        servers[PLUGIN_NAME] = entry
        _atomic_write_text(cfg_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        if skill_dst is not None:
            _copy_skill(self.skill_source_dir, skill_dst)
        return InstallResult(
            client,
            True,
            f"已写入 MCP 配置与 Skill（已备份原文件为 {cfg_path.name}.bak）；首次接入请新开对话或重启客户端生效",
            str(cfg_path),
        )

    def _uninstall_json_mcp(self, client: str, cfg_path: Path, skill_dst: Path | None) -> InstallResult:
        if cfg_path.is_file():
            _backup_if_exists(cfg_path)
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
                    data["mcpServers"].pop(PLUGIN_NAME, None)
                    _atomic_write_text(cfg_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            except json.JSONDecodeError:
                pass
        if skill_dst is not None and skill_dst.exists():
            shutil.rmtree(skill_dst)
        return InstallResult(client, True, "已从配置中移除 voice-memo-review", str(cfg_path))

    def _install_codex(self) -> InstallResult:
        cfg_path = self._paths()["codex"]["config"]
        skill_dst = self._paths()["codex"]["skill"]
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        _backup_if_exists(cfg_path)
        existing = cfg_path.read_text(encoding="utf-8") if cfg_path.is_file() else ""
        cleaned = _strip_codex_section(existing)
        cmd_lit = json.dumps(str(self.vmr_bin), ensure_ascii=False)
        block = (
            f"{CODEX_BEGIN}\n"
            "[mcp_servers.voice-memo-review]\n"
            f"command = {cmd_lit}\n"
            'args = ["mcp"]\n'
            f"{CODEX_END}\n"
        )
        final_text = (cleaned.rstrip() + "\n\n" + block) if cleaned.strip() else block
        _atomic_write_text(cfg_path, final_text)
        _copy_skill(self.skill_source_dir, skill_dst)
        return InstallResult(
            "codex",
            True,
            f"已写入 Codex TOML 受管区块与 Skill（已备份 {cfg_path.name}.bak）；请新开 Codex 对话或重启客户端使 MCP 工具生效",
            str(cfg_path),
        )

    def _uninstall_codex(self) -> InstallResult:
        cfg_path = self._paths()["codex"]["config"]
        skill_dst = self._paths()["codex"]["skill"]
        if cfg_path.is_file():
            _backup_if_exists(cfg_path)
            cleaned = _strip_codex_section(cfg_path.read_text(encoding="utf-8"))
            _atomic_write_text(cfg_path, cleaned)
        if skill_dst.exists():
            shutil.rmtree(skill_dst)
        return InstallResult("codex", True, "已从 Codex 配置中移除 voice-memo-review", str(cfg_path))
