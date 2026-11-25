"""显化规则库（Rule Studio）：明文提示词模板、专有名词词表、个人背景与透明 Prompt 拼装。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from voice_review.agent_bridge import format_duration_cn

TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
BUILTIN_TEMPLATE_IDS = ("general_review", "meeting_minutes", "sales_communication", "solo_memo")

DEFAULT_GLOSSARY_GUIDE = (
    "# 高频专有名词词表（以 # 开头的说明行会自动忽略，不会污染转写）\n"
    "# 用法：在下方直接填写容易被语音转错的人名、公司名、产品名或行业缩写（空格或换行分隔）\n"
    "# 示例：SaaS ARR OKR 飞书 客户成功\n"
)

DEFAULT_PERSONA_GUIDE = (
    "<!--\n"
    "个人角色与关注重点（本段注释不会注入 Prompt，在注释外直接填写你的真实信息即可）\n"
    "提示：你也可以在 Codex / Claude Code / Antigravity 对话里直接口述，让 AI 帮你自动写进来。\n"
    "\n"
    "参考写法（直接写在下方）：\n"
    "- 我的身份：例如 业务负责人 / 产品经理 / 独立顾问 / 创业者\n"
    "- 录音中我的称呼：例如 我（与本地声纹名称保持一致）\n"
    "- 最关注什么：例如 客户真实需求与预算、报价锚点、团队交付风险、沟通表达节奏\n"
    "- 偏好的复盘风格：例如 直给结论、少客套、优先点出没接稳的地方和更高杠杆的解法\n"
    "-->\n"
)

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _strip_glossary_comments(text: str) -> str:
    lines = [
        line.strip()
        for line in (text or "").replace("\r\n", "\n").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return "\n".join(lines).strip()


def _strip_persona_comments(text: str) -> str:
    cleaned = _HTML_COMMENT_RE.sub("", (text or "").replace("\r\n", "\n"))
    return cleaned.strip()


@dataclass(frozen=True)
class PromptTemplate:
    id: str
    name: str
    description: str
    body: str
    is_default: bool
    is_builtin: bool
    path: Path
    differs_from_builtin: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "body": self.body,
            "is_default": self.is_default,
            "is_builtin": self.is_builtin,
            "differs_from_builtin": self.differs_from_builtin,
            "path": str(self.path),
        }


def validate_template_id(template_id: str) -> str:
    clean = (template_id or "").strip()
    if not TEMPLATE_ID_RE.fullmatch(clean):
        raise ValueError("模板 ID 仅支持小写字母、数字、短横线与下划线（长度 1～48）")
    return clean


def _parse_template_markdown(text: str, *, fallback_id: str) -> tuple[dict[str, str], str]:
    content = (text or "").replace("\r\n", "\n")
    meta: dict[str, str] = {"id": fallback_id, "name": fallback_id, "description": "", "default": "false"}
    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end != -1:
            header = content[4:end]
            body = content[end + 5 :].lstrip("\n")
            for line in header.splitlines():
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                key = k.strip().lower()
                val = v.strip().strip('"').strip("'")
                if key:
                    meta[key] = val
            return meta, body.strip()
    return meta, content.strip()


def _serialize_template_markdown(*, template_id: str, name: str, description: str, body: str, is_default: bool) -> str:
    safe_name = (name or template_id).replace("\n", " ").strip()
    safe_desc = (description or "").replace("\n", " ").strip()
    flag = "true" if is_default else "false"
    return (
        "---\n"
        f"id: {template_id}\n"
        f"name: {safe_name}\n"
        f"description: {safe_desc}\n"
        f"default: {flag}\n"
        "---\n"
        f"{(body or '').strip()}\n"
    )


class RuleStudio:
    """管理用户目录下完全透明、可编辑的复盘规则（模板 + 词表 + 可选背景）。"""

    def __init__(self, rules_dir: Path, builtin_templates_dir: Path | None = None) -> None:
        self.rules_dir = Path(rules_dir).expanduser().resolve()
        self.templates_dir = self.rules_dir / "templates"
        self.glossary_path = self.rules_dir / "glossary.txt"
        self.persona_path = self.rules_dir / "persona.md"
        pkg_root = Path(__file__).resolve().parent.parent
        self.builtin_templates_dir = (
            Path(builtin_templates_dir).expanduser().resolve()
            if builtin_templates_dir is not None
            else (pkg_root / "templates").resolve()
        )

    def ensure_initialized(self) -> None:
        self.templates_dir.mkdir(parents=True, exist_ok=True)
        marker = self.rules_dir / ".initialized"
        if not marker.exists():
            for tid in BUILTIN_TEMPLATE_IDS:
                src = self.builtin_templates_dir / f"{tid}.md"
                dst = self.templates_dir / f"{tid}.md"
                if src.is_file() and not dst.is_file():
                    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            marker.write_text("1\n", encoding="utf-8")
        if not self.glossary_path.exists():
            self.glossary_path.write_text(DEFAULT_GLOSSARY_GUIDE, encoding="utf-8")
        if not self.persona_path.exists():
            self.persona_path.write_text(DEFAULT_PERSONA_GUIDE, encoding="utf-8")

    def _load_file(self, path: Path) -> PromptTemplate:
        tid = path.stem
        meta, body = _parse_template_markdown(path.read_text(encoding="utf-8"), fallback_id=tid)
        resolved_id = meta.get("id") or tid
        is_default = meta.get("default", "").lower() in ("true", "1", "yes")
        builtin_file = self.builtin_templates_dir / f"{resolved_id}.md"
        is_builtin = builtin_file.is_file()
        differs = False
        if is_builtin:
            try:
                _, builtin_body = _parse_template_markdown(builtin_file.read_text(encoding="utf-8"), fallback_id=resolved_id)
                differs = body.strip() != builtin_body.strip()
            except OSError:
                differs = False
        return PromptTemplate(
            id=resolved_id,
            name=meta.get("name") or resolved_id,
            description=meta.get("description") or "",
            body=body,
            is_default=is_default,
            is_builtin=is_builtin,
            path=path,
            differs_from_builtin=differs,
        )

    def list_templates(self) -> list[PromptTemplate]:
        self.ensure_initialized()
        items: list[PromptTemplate] = []
        for p in sorted(self.templates_dir.glob("*.md")):
            if p.is_file():
                items.append(self._load_file(p))
        items.sort(key=lambda t: (0 if t.is_default else 1, 0 if t.is_builtin else 1, t.id))
        return items

    def get_template(self, template_id: str | None = None) -> PromptTemplate:
        self.ensure_initialized()
        if template_id:
            clean = validate_template_id(template_id)
            p = self.templates_dir / f"{clean}.md"
            if p.is_file():
                return self._load_file(p)
            raise FileNotFoundError(f"未找到分析模板：{clean}")
        items = self.list_templates()
        for t in items:
            if t.is_default:
                return t
        for t in items:
            if t.id == "general_review":
                return t
        if items:
            return items[0]
        raise FileNotFoundError("规则目录中没有任何可用模板")

    def save_template(
        self,
        template_id: str,
        *,
        name: str,
        description: str,
        body: str,
        is_default: bool = False,
    ) -> PromptTemplate:
        clean = validate_template_id(template_id)
        self.ensure_initialized()
        if is_default:
            for existing in self.list_templates():
                if existing.id != clean and existing.is_default:
                    existing.path.write_text(
                        _serialize_template_markdown(
                            template_id=existing.id,
                            name=existing.name,
                            description=existing.description,
                            body=existing.body,
                            is_default=False,
                        ),
                        encoding="utf-8",
                    )
        target = self.templates_dir / f"{clean}.md"
        target.write_text(
            _serialize_template_markdown(
                template_id=clean,
                name=name,
                description=description,
                body=body,
                is_default=is_default,
            ),
            encoding="utf-8",
        )
        return self._load_file(target)

    def delete_template(self, template_id: str) -> bool:
        clean = validate_template_id(template_id)
        target = self.templates_dir / f"{clean}.md"
        if not target.is_file():
            return False
        was_default = self._load_file(target).is_default
        target.unlink()
        if was_default:
            fallback = self.templates_dir / "general_review.md"
            if fallback.is_file():
                tpl = self._load_file(fallback)
                self.save_template(
                    tpl.id,
                    name=tpl.name,
                    description=tpl.description,
                    body=tpl.body,
                    is_default=True,
                )
        return True

    def reset_builtin_template(self, template_id: str) -> PromptTemplate:
        clean = validate_template_id(template_id)
        src = self.builtin_templates_dir / f"{clean}.md"
        if not src.is_file():
            raise FileNotFoundError(f"模板 {clean} 不是内置模板，无法重置")
        self.ensure_initialized()
        dst = self.templates_dir / f"{clean}.md"
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        return self._load_file(dst)

    def get_glossary(self, *, raw: bool = False) -> str:
        self.ensure_initialized()
        try:
            content = self.glossary_path.read_text(encoding="utf-8")
            return content.strip() if raw else _strip_glossary_comments(content)
        except OSError:
            return ""

    def save_glossary(self, text: str) -> None:
        self.ensure_initialized()
        self.glossary_path.write_text((text or "").strip() + ("\n" if (text or "").strip() else ""), encoding="utf-8")

    def get_persona(self, *, raw: bool = False) -> str:
        self.ensure_initialized()
        try:
            content = self.persona_path.read_text(encoding="utf-8")
            return content.strip() if raw else _strip_persona_comments(content)
        except OSError:
            return ""

    def save_persona(self, text: str) -> None:
        self.ensure_initialized()
        self.persona_path.write_text((text or "").strip() + ("\n" if (text or "").strip() else ""), encoding="utf-8")

    def compose_analysis_prompt(
        self,
        *,
        template_id: str | None = None,
        title: str,
        recorded_at: str,
        duration_sec: float,
        speakers_summary: str,
        transcript_text: str | None = None,
        transcript_path: Path | None = None,
        analysis_path: Path | None = None,
    ) -> str:
        """透明拼装最终发送给分析模型的完整 Prompt（所有引擎与 UI 预览共用同一口径）。"""
        tpl = self.get_template(template_id)
        persona = self.get_persona()
        dur_text = format_duration_cn(duration_sec)
        parts: list[str] = [
            "## 录音元数据",
            f"- **录制时间**：{recorded_at}",
            f"- **标题**：{title}",
            f"- **音频时长**：{dur_text}",
            f"- **选用分析模板**：{tpl.name} (`{tpl.id}`)",
            "- **声纹与说话人概况**：",
            speakers_summary.strip() if speakers_summary.strip() else "未识别到已注册声纹",
        ]
        if transcript_path is not None:
            parts.append(f"- **完整转录稿路径**：`{Path(transcript_path).resolve()}`")
        if analysis_path is not None:
            parts.append(f"- **目标复盘分析稿路径**：`{Path(analysis_path).resolve()}`")

        if persona:
            parts.extend(["", "## 用户背景说明", persona])

        parts.extend(["", f"## 分析规则（模板：{tpl.name}）", tpl.body])

        if transcript_text is not None:
            parts.extend(["", "## 完整转录稿正文", transcript_text.strip()])

        return "\n".join(parts).strip() + "\n"
