"""通用 Antigravity agentapi 桥接（零私人业务耦合，动态超时、RPC 断连重试与轻量子代理引导）。"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import subprocess
import time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from voice_review.config import VoiceReviewConfig
from voice_review.state import StateStore

if TYPE_CHECKING:
    from voice_review.rules import RuleStudio

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_ID_KEYS = ("conversation_id", "conversationId", "cascade_id", "cascadeId", "rootConversationId", "id")
_TRANSIENT_RPC_MARKERS = ("connection refused", "unavailable", "eof", "reset by peer", "timed out")

_LITE_SUBAGENT_GUIDE = (
    "0. **首次初始化轻量子代理（大幅节省额度）**：若本对话尚未定义 `audio_transcriber_lite`，"
    "先调用一次 `define_subagent(Name=\"audio_transcriber_lite\", Description=\"只读音频精准听写专员\", "
    "EnableWriteTools=false, EnableMCPTools=false, EnableSubagentTools=false, "
    "SystemPrompt=\"你是极简音频听写专员，仅调用 read_file 读取音频并返回严格 JSON，不做多余输出。\")`。\n"
)


def compute_dynamic_timeout_sec(base_timeout_sec: float, *, batch_count: int = 0) -> float:
    """按并发包数动态放宽等待落盘超时，避免 2h+ 超长录音被过短超时误杀。"""
    base = max(180.0, float(base_timeout_sec))
    scaled = 600.0 + max(0, int(batch_count)) * 45.0
    return max(base, scaled)


def extract_conversation_id(output: str) -> str | None:
    text = (output or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        for k in _ID_KEYS:
            val = parsed.get(k)
            if isinstance(val, str) and UUID_RE.fullmatch(val.strip()):
                return val.strip()
    m = UUID_RE.search(text)
    return m.group(0) if m else None


def format_duration_cn(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}小时{m:02d}分{s:02d}秒" if h > 0 else f"{m}分{s:02d}秒"


def render_v2_review_prompt(
    cfg: VoiceReviewConfig,
    *,
    title: str,
    recorded_at: str,
    duration_sec: float,
    speakers_summary: str,
    workflow_script_path: Path,
    transcript_path: Path,
    analysis_path: Path,
    batch_count: int,
    segment_count: int,
    rules: "RuleStudio | None" = None,
    template_id: str | None = None,
) -> str:
    """拼装发给 Antigravity 的两步执行指令（优先采用 RuleStudio 显化模板并引导定义轻量子代理）。"""
    dur_text = format_duration_cn(duration_sec)
    spk_text = speakers_summary or "未识别到已注册声纹"
    if rules is not None:
        tpl = rules.get_template(template_id or cfg.default_template_id)
        persona = rules.get_persona()
        persona_block = f"\n## 用户背景说明\n{persona}\n" if persona else ""
        return (
            "你收到一条已完成本地声纹分离与 V2 物理切段的新录音任务，请严格按步骤完成「并发精转 → 按显化规则深度复盘」：\n\n"
            "## 录音元数据\n"
            f"- **录制时间**：{recorded_at}\n"
            f"- **标题**：{title}\n"
            f"- **音频时长**：{dur_text}\n"
            f"- **选用分析模板**：{tpl.name} (`{tpl.id}`)\n"
            "- **本地声纹概况**：\n"
            f"{spk_text}\n"
            f"- **并发精转规模**：共 `{segment_count}` 个物理发言段，已打包为 `{batch_count}` 个并发小包\n"
            f"- **并发精转工作流脚本**：`{workflow_script_path.resolve()}`\n"
            f"- **目标转录稿路径**：`{transcript_path.resolve()}`\n"
            f"- **目标复盘分析稿路径**：`{analysis_path.resolve()}`\n"
            f"{persona_block}\n"
            "## 执行步骤\n"
            f"{_LITE_SUBAGENT_GUIDE}"
            f"1. **第一步（零漂移并发精转，带语气捕捉）**：调用 `run_workflow(ScriptPath=\"{workflow_script_path.resolve()}\", Description=\"并发精转：{title}\")`；"
            f"等待工作流完成并将完整转录稿落盘至 `{transcript_path.resolve()}`。\n"
            f"2. **第二步（按下方显化规则输出深度复盘）**：用 `read_file` 读取 `{transcript_path.resolve()}`（超长按行分段读全），"
            f"严格遵循下方模板规则撰写复盘，并用 `write_file(Overwrite=true)` 写入 `{analysis_path.resolve()}`：\n\n"
            f"### 显化分析规则（{tpl.name}）\n"
            f"{tpl.body}\n\n"
            "3. 最后在当前对话回复中输出同样的复盘正文，方便直接浏览与追问。\n"
        )

    template = cfg.review_prompt_path.read_text(encoding="utf-8")
    rendered = template.format(
        title=title,
        recorded_at=recorded_at,
        duration_text=dur_text,
        speakers_summary=spk_text,
        workflow_script_path=str(workflow_script_path.resolve()),
        transcript_path=str(transcript_path.resolve()),
        analysis_path=str(analysis_path.resolve()),
        batch_count=batch_count,
        segment_count=segment_count,
    )
    if "audio_transcriber_lite" not in rendered:
        rendered = rendered.replace("## 执行步骤\n", f"## 执行步骤\n{_LITE_SUBAGENT_GUIDE}", 1)
    return rendered


def resolve_bucket(cfg: VoiceReviewConfig, recorded_at: str) -> tuple[str | None, str]:
    mode = (cfg.rotation or "weekly").strip().lower()
    base = cfg.conversation_title or "语音复盘助手"
    try:
        dt = datetime.fromisoformat(recorded_at)
        dt = dt.astimezone(SHANGHAI_TZ) if dt.tzinfo else dt.replace(tzinfo=SHANGHAI_TZ)
    except ValueError:
        dt = datetime.now(SHANGHAI_TZ)
    if mode == "daily":
        d = dt.strftime("%Y-%m-%d")
        return f"conv:daily-{d}", f"{base} · {d}"
    if mode == "weekly":
        y, w, wd = dt.isocalendar()
        mon = (dt - timedelta(days=wd - 1)).strftime("%Y-%m-%d")
        sun = (dt + timedelta(days=7 - wd)).strftime("%m-%d")
        return f"conv:weekly-{y}-W{w:02d}", f"{base} · {mon}~{sun}（第{w}周）"
    if mode == "per_recording":
        return None, f"{base} · {dt.strftime('%m-%d %H:%M')}"
    return "conv:default", base


class AgentBridge:
    def __init__(self, cfg: VoiceReviewConfig, store: StateStore) -> None:
        self.cfg = cfg
        self.store = store

    def _run(self, args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.cfg.agentapi_bin), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    @staticmethod
    def _is_transient_error(text: str) -> bool:
        low = (text or "").lower()
        return any(m in low for m in _TRANSIENT_RPC_MARKERS)

    def deliver(self, *, prompt: str, title: str, recorded_at: str) -> str | None:
        bucket, conv_title = resolve_bucket(self.cfg, recorded_at)
        last_err = ""
        for attempt in range(1, 4):
            cid = self.store.get_meta(bucket) if bucket else None
            if cid:
                probe = self._run(["get-conversation-metadata", cid], timeout=20)
                if probe.returncode == 0:
                    res = self._run(["send-message", f"--title=新录音：{title}", cid, prompt], timeout=60)
                    if res.returncode == 0:
                        return cid
                    err_detail = (res.stderr or res.stdout or "").strip()[-400:]
                    if self._is_transient_error(err_detail) and attempt < 3:
                        time.sleep(2.0 * attempt)
                        last_err = err_detail
                        continue
                else:
                    probe_err = (probe.stderr or probe.stdout or "").strip()[-400:]
                    if self._is_transient_error(probe_err) and attempt < 3:
                        time.sleep(2.0 * attempt)
                        last_err = probe_err
                        continue
            res = self._run(
                ["new-conversation", f"--model={self.cfg.model}", f"--title={conv_title}", "--", prompt],
                timeout=60,
            )
            if res.returncode == 0:
                new_cid = extract_conversation_id(f"{res.stdout}\n{res.stderr}")
                if bucket and new_cid:
                    self.store.set_meta(bucket, new_cid)
                return new_cid
            last_err = (res.stderr or res.stdout or "").strip()[-400:]
            if self._is_transient_error(last_err) and attempt < 3:
                time.sleep(2.0 * attempt)
                continue
            break
        raise RuntimeError(f"agentapi 投递失败：{last_err}")

    def wait_for_file(self, path: Path, *, timeout_sec: float | None = None, poll_sec: float = 2.0) -> bool:
        deadline = time.monotonic() + (self.cfg.analysis_timeout_sec if timeout_sec is None else timeout_sec)
        while time.monotonic() < deadline:
            try:
                if path.is_file() and path.stat().st_size > 0:
                    return True
            except OSError:
                pass
            time.sleep(poll_sec)
        return False
