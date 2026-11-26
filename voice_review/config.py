"""可插拔四插槽配置加载、保存与组合兼容性校验（零第三方依赖，纯标准库 tomllib）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib
from typing import Any
from zoneinfo import ZoneInfo

from voice_review.diarizer import find_fluidaudio_bin
from voice_review.segmenter import SegmenterConfig

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_RECORDINGS_DIR = "~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
DEFAULT_VMR_HOME = "~/.config/voice-memo-review"
DEFAULT_MANAGED_DIR = "~/.local/share/voice-memo-review"
DEFAULT_UPDATE_REPO = "shane1124/voice-memo-review"

VALID_PREPROCESSORS = ("fluidaudio", "none")
VALID_TRANSCRIBERS = ("antigravity-flash", "gemini-api", "openai-compatible-asr", "command-asr")
VALID_ANALYZERS = (
    "antigravity-agentapi",
    "codex-cli",
    "claude-cli",
    "openai-compatible-llm",
    "gemini-llm",
    "none",
)


def default_vmr_home() -> Path:
    env_home = os.environ.get("VMR_HOME", "").strip()
    return Path(env_home or DEFAULT_VMR_HOME).expanduser().resolve()


def _managed_bin_dirs() -> list[Path]:
    pkg_root = Path(__file__).resolve().parent.parent
    return [
        pkg_root / "bin",
        Path(DEFAULT_MANAGED_DIR).expanduser() / "bin",
        default_vmr_home() / "bin",
    ]


def _find_bin(raw: str, fallback_name: str) -> str:
    val = (raw or "").strip()
    if val:
        return str(Path(val).expanduser())
    for bdir in _managed_bin_dirs():
        cand = bdir / fallback_name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand.resolve())
    found = shutil.which(fallback_name)
    if found:
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin")):
        cand = Path(prefix) / fallback_name
        if cand.is_file():
            return str(cand)
    return fallback_name


def _find_fluidaudio(raw: Any) -> Path | None:
    if raw:
        found = find_fluidaudio_bin(raw)
        if found is not None:
            return found
    for bdir in _managed_bin_dirs():
        cand = bdir / "fluidaudiocli"
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand.resolve()
    return find_fluidaudio_bin(None)


def _parse_version_tuple(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for seg in raw.strip().split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _is_exec_file(p: Path) -> bool:
    try:
        return p.is_file() and os.access(p, os.X_OK)
    except OSError:
        return False


def resolve_codex_binary(raw: str = "") -> str | None:
    """定位本机 Codex 桌面版内置引擎（优先桌面 App 本体，系统可执行文件仅作兜底）。"""
    val = (raw or "").strip()
    if val and ("/" in val or "\\" in val):
        p = Path(val).expanduser()
        return str(p.resolve()) if _is_exec_file(p) else None
    home = Path.home()
    # 1. 优先直接调用桌面 App（ChatGPT / Codex）内置本体，无需用户安装任何外部 CLI
    desktop_candidates = [
        Path("/Applications/Codex.app/Contents/Resources/codex-cli/bin/codex"),
        Path("/Applications/Codex.app/Contents/Resources/codex"),
        home / "Applications/Codex.app/Contents/Resources/codex-cli/bin/codex",
        home / "Applications/Codex.app/Contents/Resources/codex",
        Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"),
        home / "Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
    ]
    for cand in desktop_candidates:
        if _is_exec_file(cand):
            return str(cand.resolve())
    # 2. 兜底查找受管目录或系统 PATH
    for bdir in _managed_bin_dirs():
        cand = bdir / "codex"
        if _is_exec_file(cand):
            return str(cand.resolve())
    for name in ([val] if val else []) + ["codex"]:
        hit = shutil.which(name)
        if hit:
            return hit
    for cand in (Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex"), home / ".local/bin/codex"):
        if _is_exec_file(cand):
            return str(cand.resolve())
    return None


def resolve_claude_binary(raw: str = "") -> str | None:
    """定位本机 Claude 桌面版内置引擎（优先官方 Claude.app 内置本体，系统可执行文件仅作兜底）。"""
    val = (raw or "").strip()
    if val and ("/" in val or "\\" in val):
        p = Path(val).expanduser()
        return str(p.resolve()) if _is_exec_file(p) else None
    home = Path.home()
    # 1. 优先直接探测官方 Claude 桌面版内置引擎（按版本号降序选最新，零外部 CLI 依赖）
    app_support = home / "Library/Application Support/Claude"
    cc_root = app_support / "claude-code"
    if cc_root.is_dir():
        ver_dirs = sorted(
            (d for d in cc_root.iterdir() if d.is_dir()),
            key=lambda d: _parse_version_tuple(d.name),
            reverse=True,
        )
        for vdir in ver_dirs:
            for cand in sorted(vdir.glob("*/claude.app/Contents/MacOS/claude"), reverse=True):
                if _is_exec_file(cand):
                    return str(cand.resolve())
    vm_root = app_support / "claude-code-vm"
    if vm_root.is_dir():
        ver_dirs = sorted(
            (d for d in vm_root.iterdir() if d.is_dir()),
            key=lambda d: _parse_version_tuple(d.name),
            reverse=True,
        )
        for vdir in ver_dirs:
            cand = vdir / "claude"
            if _is_exec_file(cand):
                return str(cand.resolve())
    # 2. 兜底查找受管目录或系统 PATH
    for bdir in _managed_bin_dirs():
        cand = bdir / "claude"
        if _is_exec_file(cand):
            return str(cand.resolve())
    for name in ([val] if val else []) + ["claude"]:
        hit = shutil.which(name)
        if hit:
            return hit
    for cand in (home / ".local/bin/claude", Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")):
        if _is_exec_file(cand):
            return str(cand.resolve())
    return None


def resolve_antigravity_command(raw: str = "") -> list[str] | None:
    """定位本机 Antigravity 桌面版调用命令（支持桌面 App 内置 language_server 及包装脚本）。"""
    val = (raw or "").strip()
    if val:
        p = Path(val).expanduser()
        if _is_exec_file(p):
            resolved = str(p.resolve())
            return [resolved, "agentapi"] if p.name == "language_server" else [resolved]
    home = Path.home()
    # 优先使用 Antigravity 桌面版内置本体或官方包装脚本
    for app_ls in (
        Path("/Applications/Antigravity.app/Contents/Resources/bin/language_server"),
        home / "Applications/Antigravity.app/Contents/Resources/bin/language_server",
    ):
        if _is_exec_file(app_ls):
            return [str(app_ls.resolve()), "agentapi"]
    wrapper = home / ".gemini/antigravity/bin/agentapi"
    if _is_exec_file(wrapper):
        return [str(wrapper.resolve())]
    env_val = os.environ.get("ANTIGRAVITY_AGENTAPI_EXE", "").strip()
    if env_val:
        ep = Path(env_val).expanduser()
        if _is_exec_file(ep):
            resolved = str(ep.resolve())
            return [resolved, "agentapi"] if ep.name == "language_server" else [resolved]
    return None


def check_claude_auth_status(raw: str = "") -> tuple[str | None, bool | None, str]:
    """检测本机 Claude 桌面版引擎是否存在及其官方登录态。

    返回 (exe_path, logged_in, detail)：
      - logged_in 为 True 表示已完成官方登录，可直接后台 Push 自动弹对话；
      - logged_in 为 False 表示已安装但尚未完成 1 次浏览器授权；
      - logged_in 为 None 表示未安装或当前二进制不支持 JSON 状态探测。
    """
    exe = resolve_claude_binary(raw)
    if not exe:
        return None, None, "未检测到官方 Claude 桌面版"
    try:
        proc = subprocess.run(
            [exe, "auth", "status"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=2.5,
            check=False,
        )
        raw_out = (proc.stdout or "").strip()
        if raw_out.startswith("{"):
            st = json.loads(raw_out)
            if isinstance(st, dict) and isinstance(st.get("loggedIn"), bool):
                if st["loggedIn"]:
                    return exe, True, "已登录官方账号"
                return exe, False, "尚未完成 1 次官方授权（点击控制台「一键登录官方 Claude 账号」或运行 vmr auth-claude）"
    except Exception:
        pass
    return exe, None, "已检测到桌面版引擎"


def _find_agentapi(raw: str) -> Path:
    val = (raw or "").strip()
    if val:
        return Path(val).expanduser().resolve()
    default_wrapper = (Path.home() / ".gemini/antigravity/bin/agentapi").resolve()
    if default_wrapper.is_file():
        return default_wrapper
    env_val = os.environ.get("ANTIGRAVITY_AGENTAPI_EXE", "").strip()
    if env_val:
        return Path(env_val).expanduser().resolve()
    return default_wrapper


def _resolve_path(raw: str, base: Path) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = base / p
    return p.resolve()


def validate_provider_combo(transcriber: str, analyzer_auto: str) -> tuple[bool, str]:
    """校验转写插槽与后台自动分析插槽的合法组合，阻断脆弱半截跨宿主链。"""
    t = (transcriber or "").strip().lower()
    a = (analyzer_auto or "").strip().lower()
    if t not in VALID_TRANSCRIBERS:
        return False, f"不支持的转写引擎：{t}（可选：{', '.join(VALID_TRANSCRIBERS)}）"
    if a not in VALID_ANALYZERS:
        return False, f"不支持的后台分析引擎：{a}（可选：{', '.join(VALID_ANALYZERS)}）"
    if t == "antigravity-flash" and a in ("openai-compatible-llm", "gemini-llm", "codex-cli", "claude-cli"):
        return (
            False,
            "转写引擎 antigravity-flash 依赖 Antigravity 一体化工作流，不能直接搭配独立分析引擎（如 Codex / Claude / 外部 API）；"
            "请将后台分析器设为 antigravity-agentapi，或将转写引擎切换为 gemini-api / openai-compatible-asr / command-asr。",
        )
    return True, "组合有效"


def _toml_value(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, list):
        return "[" + ", ".join(_toml_value(x) for x in val) + "]"
    return json.dumps(str(val), ensure_ascii=False)


def save_config_dict(config_path: Path, data: dict[str, Any]) -> None:
    """用纯标准库将配置字典序列化为规范 TOML 文件并原子写入。"""
    target = Path(config_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = ["# voice-memo-review 配置文件（敏感 API Key 独立存放于同目录 secrets.json）", ""]
    tables: list[tuple[str, dict[str, Any]]] = []
    for key, val in data.items():
        if val is None:
            continue
        if isinstance(val, dict):
            tables.append((key, val))
        else:
            lines.append(f"{key} = {_toml_value(val)}")
    for tbl_name, tbl in tables:
        lines.append("")
        lines.append(f"[{tbl_name}]")
        for k, v in tbl.items():
            if v is None or isinstance(v, dict):
                continue
            lines.append(f"{k} = {_toml_value(v)}")
    payload = "\n".join(lines).rstrip() + "\n"
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)


@dataclass(frozen=True)
class VoiceReviewConfig:
    project_root: Path
    recordings_dir: Path
    output_dir: Path
    transcripts_dir: Path
    work_dir: Path
    state_db: Path
    voiceprints_path: Path
    review_prompt_path: Path
    fluidaudio_bin: Path | None
    ffmpeg_bin: str
    ffprobe_bin: str
    agentapi_bin: Path
    import_since: str
    min_duration_sec: float
    debounce_sec: float
    fallback_poll_sec: float
    analysis_timeout_sec: float
    model: str
    conversation_title: str
    rotation: str
    asr_context: str
    segmenter: SegmenterConfig
    # --- Phase 1 四插槽与产品级扩展字段（全部带安全默认值，保持完全向后兼容）---
    config_path: Path | None = None
    vmr_home: Path = field(default_factory=default_vmr_home)
    secrets_path: Path = field(default_factory=lambda: default_vmr_home() / "secrets.json")
    rules_dir: Path = field(default_factory=lambda: default_vmr_home() / "rules")
    voice_memos_enabled: bool = True
    drop_folder_enabled: bool = False
    drop_folder_dir: Path = field(default_factory=lambda: Path("~/Desktop/录音自动分析投放箱").expanduser())
    drop_folder_archive_done: bool = False
    preprocessor_mode: str = "fluidaudio"
    uniform_chunk_sec: float = 600.0
    transcriber_provider: str = "antigravity-flash"
    transcriber_settings: dict[str, Any] = field(default_factory=dict)
    analyzer_auto_provider: str = "antigravity-agentapi"
    analyzer_settings: dict[str, Any] = field(default_factory=dict)
    default_template_id: str = "general_review"
    update_repo: str = DEFAULT_UPDATE_REPO
    ui_port: int = 18765
    codex_bin: str = "codex"
    claude_bin: str = "claude"


def load_config(config_path: Path | None = None, *, vmr_home: Path | None = None) -> VoiceReviewConfig:
    home = Path(vmr_home).expanduser().resolve() if vmr_home is not None else default_vmr_home()
    if config_path is not None:
        cfg_file = Path(config_path).expanduser().resolve()
        root = cfg_file.parent
        raw = tomllib.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.is_file() else {}
        effective_home = root if vmr_home is None else home
    else:
        cfg_file = home / "config.toml"
        root = home
        raw = tomllib.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.is_file() else {}
        effective_home = home

    default_since = datetime.now(SHANGHAI_TZ).replace(microsecond=0).isoformat()
    v2_raw = raw.get("v2") if isinstance(raw.get("v2"), dict) else {}
    sources_raw = raw.get("sources") if isinstance(raw.get("sources"), dict) else {}
    pre_raw = raw.get("preprocessor") if isinstance(raw.get("preprocessor"), dict) else {}
    trans_raw = raw.get("transcriber") if isinstance(raw.get("transcriber"), dict) else {}
    ana_raw = raw.get("analyzer") if isinstance(raw.get("analyzer"), dict) else {}
    upd_raw = raw.get("update") if isinstance(raw.get("update"), dict) else {}
    ui_raw = raw.get("ui") if isinstance(raw.get("ui"), dict) else {}

    seg_cfg = SegmenterConfig(
        min_segment_sec=float(v2_raw.get("min_segment_sec", 0.35)),
        gap_merge_sec=float(v2_raw.get("gap_merge_sec", 2.0)),
        max_turn_sec=float(v2_raw.get("max_turn_sec", 45.0)),
        pad_before_sec=float(v2_raw.get("pad_before_sec", 0.25)),
        pad_after_sec=float(v2_raw.get("pad_after_sec", 0.15)),
        batch_max_segments=int(v2_raw.get("batch_max_segments", 15)),
        batch_max_speech_sec=float(v2_raw.get("batch_max_speech_sec", 120.0)),
        mp3_bitrate=str(v2_raw.get("mp3_bitrate", "32k")),
        max_concurrency=max(1, int(v2_raw.get("max_concurrency", 25))),
        agent_timeout_sec=max(30, int(v2_raw.get("agent_timeout_sec", 120))),
        stagger_sec=max(0.0, float(v2_raw.get("stagger_sec", 0.25))),
        max_turns_per_worker=max(1, int(v2_raw.get("max_turns_per_worker", 6))),
        worker_pool_size=max(0, int(v2_raw.get("worker_pool_size", 0))),
    )

    output_dir = _resolve_path(str(raw.get("output_dir", "output")), root)
    transcripts_raw = str(raw.get("transcripts_dir", "")).strip()
    transcripts_dir = _resolve_path(transcripts_raw, root) if transcripts_raw else (output_dir / "_转录稿")
    pkg_root = Path(__file__).resolve().parent.parent
    raw_prompt = str(raw.get("review_prompt_path", "")).strip()
    if raw_prompt:
        review_prompt_path = _resolve_path(raw_prompt, root)
    else:
        cand_prompt = (root / "prompts/review_default.md").resolve()
        review_prompt_path = cand_prompt if cand_prompt.is_file() else (pkg_root / "prompts/review_default.md").resolve()

    drop_dir_raw = str(sources_raw.get("drop_folder_dir", "~/Desktop/录音自动分析投放箱")).strip()
    trans_provider = str(trans_raw.get("provider", "antigravity-flash")).strip().lower() or "antigravity-flash"
    ana_provider = str(ana_raw.get("auto_provider", "antigravity-agentapi")).strip().lower() or "antigravity-agentapi"
    pre_mode = str(pre_raw.get("mode", "fluidaudio")).strip().lower() or "fluidaudio"
    if pre_mode not in VALID_PREPROCESSORS:
        pre_mode = "fluidaudio"

    trans_settings = {k: v for k, v in trans_raw.items() if k != "provider"}
    ana_settings = {k: v for k, v in ana_raw.items() if k != "auto_provider"}

    return VoiceReviewConfig(
        project_root=root,
        recordings_dir=_resolve_path(str(sources_raw.get("recordings_dir", raw.get("recordings_dir", DEFAULT_RECORDINGS_DIR))), root),
        output_dir=output_dir,
        transcripts_dir=transcripts_dir,
        work_dir=_resolve_path(str(raw.get("work_dir", "work")), root),
        state_db=_resolve_path(str(raw.get("state_db", "state/state.sqlite")), root),
        voiceprints_path=_resolve_path(str(raw.get("voiceprints_path", "voiceprints.json")), root),
        review_prompt_path=review_prompt_path,
        fluidaudio_bin=_find_fluidaudio(raw.get("fluidaudio_bin")),
        ffmpeg_bin=_find_bin(str(raw.get("ffmpeg_bin", "")), "ffmpeg"),
        ffprobe_bin=_find_bin(str(raw.get("ffprobe_bin", "")), "ffprobe"),
        agentapi_bin=_find_agentapi(str(raw.get("agentapi_bin", ""))),
        import_since=str(raw.get("import_since", default_since)),
        min_duration_sec=float(raw.get("min_duration_sec", 10.0)),
        debounce_sec=float(raw.get("debounce_sec", 12.0)),
        fallback_poll_sec=float(raw.get("fallback_poll_sec", 300.0)),
        analysis_timeout_sec=float(raw.get("analysis_timeout_sec", 900.0)),
        model=str(raw.get("model", "gemini-4-neon-xhigh")),
        conversation_title=str(raw.get("conversation_title", "录音分析")),
        rotation=str(raw.get("rotation", "daily")),
        asr_context=str(raw.get("asr_context", "")).strip(),
        segmenter=seg_cfg,
        config_path=cfg_file,
        vmr_home=effective_home,
        secrets_path=effective_home / "secrets.json",
        rules_dir=effective_home / "rules",
        voice_memos_enabled=bool(sources_raw.get("voice_memos_enabled", True)),
        drop_folder_enabled=bool(sources_raw.get("drop_folder_enabled", False)),
        drop_folder_dir=_resolve_path(drop_dir_raw, root),
        drop_folder_archive_done=bool(sources_raw.get("drop_folder_archive_done", False)),
        preprocessor_mode=pre_mode,
        uniform_chunk_sec=max(30.0, float(pre_raw.get("uniform_chunk_sec", 600.0))),
        transcriber_provider=trans_provider,
        transcriber_settings=trans_settings,
        analyzer_auto_provider=ana_provider,
        analyzer_settings=ana_settings,
        default_template_id=str(raw.get("default_template_id", "general_review")).strip() or "general_review",
        update_repo=str(upd_raw.get("repo", DEFAULT_UPDATE_REPO)).strip() or DEFAULT_UPDATE_REPO,
        ui_port=int(ui_raw.get("port", 18765)),
        codex_bin=resolve_codex_binary(str(raw.get("codex_bin", ""))) or _find_bin(str(raw.get("codex_bin", "")), "codex"),
        claude_bin=resolve_claude_binary(str(raw.get("claude_bin", ""))) or _find_bin(str(raw.get("claude_bin", "")), "claude"),
    )


def config_to_dict(cfg: VoiceReviewConfig) -> dict[str, Any]:
    """将 VoiceReviewConfig 转为可保存的嵌套配置字典（不含任何密钥，完整保留旧版自定义字段）。"""
    out: dict[str, Any] = {
        "output_dir": str(cfg.output_dir),
        "transcripts_dir": str(cfg.transcripts_dir),
        "work_dir": str(cfg.work_dir),
        "state_db": str(cfg.state_db),
        "voiceprints_path": str(cfg.voiceprints_path),
        "review_prompt_path": str(cfg.review_prompt_path),
        "import_since": cfg.import_since,
        "min_duration_sec": cfg.min_duration_sec,
        "debounce_sec": cfg.debounce_sec,
        "fallback_poll_sec": cfg.fallback_poll_sec,
        "analysis_timeout_sec": cfg.analysis_timeout_sec,
        "model": cfg.model,
        "conversation_title": cfg.conversation_title,
        "rotation": cfg.rotation,
        "asr_context": cfg.asr_context,
        "default_template_id": cfg.default_template_id,
        "ffmpeg_bin": cfg.ffmpeg_bin,
        "ffprobe_bin": cfg.ffprobe_bin,
        "agentapi_bin": str(cfg.agentapi_bin),
        "codex_bin": cfg.codex_bin,
        "claude_bin": cfg.claude_bin,
        "sources": {
            "voice_memos_enabled": cfg.voice_memos_enabled,
            "recordings_dir": str(cfg.recordings_dir),
            "drop_folder_enabled": cfg.drop_folder_enabled,
            "drop_folder_dir": str(cfg.drop_folder_dir),
            "drop_folder_archive_done": cfg.drop_folder_archive_done,
        },
        "preprocessor": {
            "mode": cfg.preprocessor_mode,
            "uniform_chunk_sec": cfg.uniform_chunk_sec,
        },
        "transcriber": {
            "provider": cfg.transcriber_provider,
            **cfg.transcriber_settings,
        },
        "analyzer": {
            "auto_provider": cfg.analyzer_auto_provider,
            **cfg.analyzer_settings,
        },
        "v2": {
            "min_segment_sec": cfg.segmenter.min_segment_sec,
            "gap_merge_sec": cfg.segmenter.gap_merge_sec,
            "max_turn_sec": cfg.segmenter.max_turn_sec,
            "pad_before_sec": cfg.segmenter.pad_before_sec,
            "pad_after_sec": cfg.segmenter.pad_after_sec,
            "batch_max_segments": cfg.segmenter.batch_max_segments,
            "batch_max_speech_sec": cfg.segmenter.batch_max_speech_sec,
            "mp3_bitrate": cfg.segmenter.mp3_bitrate,
            "max_concurrency": cfg.segmenter.max_concurrency,
            "agent_timeout_sec": cfg.segmenter.agent_timeout_sec,
            "stagger_sec": cfg.segmenter.stagger_sec,
            "max_turns_per_worker": cfg.segmenter.max_turns_per_worker,
            "worker_pool_size": cfg.segmenter.worker_pool_size,
        },
        "update": {
            "repo": cfg.update_repo,
        },
        "ui": {
            "port": cfg.ui_port,
        },
    }
    if cfg.fluidaudio_bin is not None:
        out["fluidaudio_bin"] = str(cfg.fluidaudio_bin)
    return out
