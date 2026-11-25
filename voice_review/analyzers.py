"""插槽 4：可插拔分析引擎（mcp-host / antigravity-agentapi / codex-cli / claude-cli / openai-compatible-llm / gemini-llm）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

from voice_review.agent_bridge import (
    AgentBridge,
    compute_dynamic_timeout_sec,
    render_v2_review_prompt,
    resolve_bucket,
)
from voice_review.config import VoiceReviewConfig, resolve_claude_binary, resolve_codex_binary
from voice_review.rules import RuleStudio
from voice_review.secrets import SecretStore
from voice_review.state import StateStore

DEFAULT_MAX_CHUNK_CHARS = 80_000


@dataclass(frozen=True)
class AnalysisRequest:
    recording_id: str
    title: str
    recorded_at: str
    duration_sec: float
    speakers_summary: str
    transcript_path: Path
    analysis_path: Path
    template_id: str | None = None
    workflow_script_path: Path | None = None
    batch_count: int = 0
    segment_count: int = 0
    extra_prompt_sections: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnalysisOutcome:
    ok: bool
    status: str  # "done" | "awaiting_analysis" | "failed"
    analysis_path: Path | None
    prompt_preview: str = ""
    error: str = ""
    chat_url: str = ""


def chunk_text_for_summary(text: str, *, max_chars: int = DEFAULT_MAX_CHUNK_CHARS) -> list[str]:
    """按段落安全切分超长转录稿，避免超出低上下文 LLM 窗口限制。"""
    clean = (text or "").strip()
    if not clean:
        return []
    if len(clean) <= max_chars:
        return [clean]
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for para in clean.split("\n\n"):
        if cur and cur_len + len(para) + 2 > max_chars:
            chunks.append("\n\n".join(cur))
            cur = []
            cur_len = 0
        if len(para) > max_chars:
            for i in range(0, len(para), max_chars):
                chunks.append(para[i : i + max_chars])
        else:
            cur.append(para)
            cur_len += len(para) + 2
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


class AnalyzerProvider(ABC):
    provider_id: str = ""

    @abstractmethod
    def analyze(self, req: AnalysisRequest) -> AnalysisOutcome:
        """执行分析并落盘或返回等待宿主分析状态。"""


class McpHostAnalyzer(AnalyzerProvider):
    """后台不主动调远端 LLM，转入 awaiting_analysis 等待用户通过 MCP 或控制台触发。"""

    provider_id = "none"

    def __init__(self, cfg: VoiceReviewConfig, rules: RuleStudio) -> None:
        self.cfg = cfg
        self.rules = rules

    def analyze(self, req: AnalysisRequest) -> AnalysisOutcome:
        prompt = self.rules.compose_analysis_prompt(
            template_id=req.template_id or self.cfg.default_template_id,
            title=req.title,
            recorded_at=req.recorded_at,
            duration_sec=req.duration_sec,
            speakers_summary=req.speakers_summary,
            transcript_path=req.transcript_path,
            analysis_path=req.analysis_path,
            extra_sections=req.extra_prompt_sections,
        )
        return AnalysisOutcome(
            ok=True,
            status="awaiting_analysis",
            analysis_path=None,
            prompt_preview=prompt,
        )


class AntigravityAgentAnalyzer(AnalyzerProvider):
    provider_id = "antigravity-agentapi"

    def __init__(self, cfg: VoiceReviewConfig, store: StateStore, rules: RuleStudio) -> None:
        self.cfg = cfg
        self.store = store
        self.rules = rules
        self.bridge = AgentBridge(cfg, store)

    def analyze(self, req: AnalysisRequest) -> AnalysisOutcome:
        if req.workflow_script_path is not None:
            prompt = render_v2_review_prompt(
                self.cfg,
                title=req.title,
                recorded_at=req.recorded_at,
                duration_sec=req.duration_sec,
                speakers_summary=req.speakers_summary,
                workflow_script_path=req.workflow_script_path,
                transcript_path=req.transcript_path,
                analysis_path=req.analysis_path,
                batch_count=req.batch_count,
                segment_count=req.segment_count,
                rules=self.rules,
                template_id=req.template_id,
                extra_sections=req.extra_prompt_sections,
            )
        else:
            prompt = self.rules.compose_analysis_prompt(
                template_id=req.template_id or self.cfg.default_template_id,
                title=req.title,
                recorded_at=req.recorded_at,
                duration_sec=req.duration_sec,
                speakers_summary=req.speakers_summary,
                transcript_path=req.transcript_path,
                analysis_path=req.analysis_path,
                extra_sections=req.extra_prompt_sections,
            ) + (
                f"\n请用 `read_file` 读取 `{req.transcript_path.resolve()}`，"
                f"按上述显化规则完成分析，并用 `write_file(Overwrite=true)` 写入 `{req.analysis_path.resolve()}`。\n"
            )
        try:
            self.bridge.deliver(prompt=prompt, title=req.title, recorded_at=req.recorded_at)
        except Exception as exc:
            return AnalysisOutcome(ok=False, status="failed", analysis_path=None, error=str(exc))

        timeout_sec = compute_dynamic_timeout_sec(self.cfg.analysis_timeout_sec, batch_count=req.batch_count)
        ok = self.bridge.wait_for_file(req.analysis_path, timeout_sec=timeout_sec)
        if not ok:
            return AnalysisOutcome(
                ok=False,
                status="failed",
                analysis_path=None,
                error=f"等待 Antigravity 分析稿落盘超时（{int(timeout_sec)}s）：{req.analysis_path}",
            )
        return AnalysisOutcome(ok=True, status="done", analysis_path=req.analysis_path)


class OpenAICompatibleLlmAnalyzer(AnalyzerProvider):
    provider_id = "openai-compatible-llm"

    def __init__(self, cfg: VoiceReviewConfig, secrets: SecretStore, rules: RuleStudio) -> None:
        self.cfg = cfg
        self.secrets = secrets
        self.rules = rules
        st = cfg.analyzer_settings
        self.base_url = str(st.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        self.model = str(st.get("model") or "gpt-4o-mini").strip()
        self.max_chunk_chars = max(10_000, int(st.get("max_chunk_chars", DEFAULT_MAX_CHUNK_CHARS)))
        self.timeout_sec = max(30, int(st.get("timeout_sec", 180)))

    def _api_key(self) -> str:
        return self.secrets.get("openai_llm_api_key", env_var="OPENAI_API_KEY")

    def _chat(self, system_prompt: str, user_prompt: str) -> str:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {"Content-Type": "application/json"}
        key = self._api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        last_err = ""
        for attempt in range(1, 4):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                    obj = json.loads(resp.read().decode("utf-8"))
                choices = obj.get("choices") or []
                if choices:
                    content = (choices[0].get("message") or {}).get("content") or ""
                    if str(content).strip():
                        return str(content).strip()
                raise RuntimeError("模型返回空内容")
            except urllib.error.HTTPError as exc:
                last_err = f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='ignore')[:240]}"
                if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2.5 * attempt)
                    continue
                raise RuntimeError(f"OpenAI 兼容 LLM 分析失败：{last_err}") from exc
            except Exception as exc:
                last_err = str(exc)
                if attempt < 3:
                    time.sleep(2.0 * attempt)
                    continue
                raise RuntimeError(f"OpenAI 兼容 LLM 分析失败：{last_err}") from exc
        raise RuntimeError(f"OpenAI 兼容 LLM 分析失败：{last_err}")

    def analyze(self, req: AnalysisRequest) -> AnalysisOutcome:
        try:
            transcript_text = req.transcript_path.read_text(encoding="utf-8")
        except OSError as exc:
            return AnalysisOutcome(ok=False, status="failed", analysis_path=None, error=f"读取转录稿失败：{exc}")
        chunks = chunk_text_for_summary(transcript_text, max_chars=self.max_chunk_chars)
        sys_msg = "你是一位严谨、客观的中文录音分析专家，严格依据转录稿事实输出高质量 Markdown 文档。"
        try:
            if len(chunks) <= 1:
                prompt = self.rules.compose_analysis_prompt(
                    template_id=req.template_id or self.cfg.default_template_id,
                    title=req.title,
                    recorded_at=req.recorded_at,
                    duration_sec=req.duration_sec,
                    speakers_summary=req.speakers_summary,
                    transcript_text=transcript_text,
                    extra_sections=req.extra_prompt_sections,
                )
                result_md = self._chat(sys_msg, prompt)
            else:
                partials: list[str] = []
                for idx, chunk in enumerate(chunks, 1):
                    sub_prompt = (
                        f"这是超长录音《{req.title}》的第 {idx}/{len(chunks)} 段转录稿。"
                        "请高密度提炼本段核心事实、关键数字、人物承诺、争议分歧与情绪波动（保留原话 `[HH:MM:SS]` 时间戳），"
                        "不要遗漏关键业务信息：\n\n" + chunk
                    )
                    partials.append(f"### 分段提炼 {idx}/{len(chunks)}\n" + self._chat(sys_msg, sub_prompt))
                merged_input = "\n\n".join(partials)
                final_prompt = self.rules.compose_analysis_prompt(
                    template_id=req.template_id or self.cfg.default_template_id,
                    title=req.title,
                    recorded_at=req.recorded_at,
                    duration_sec=req.duration_sec,
                    speakers_summary=req.speakers_summary,
                    transcript_text=merged_input,
                    extra_sections=req.extra_prompt_sections,
                )
                result_md = self._chat(sys_msg, final_prompt)
        except Exception as exc:
            return AnalysisOutcome(ok=False, status="failed", analysis_path=None, error=str(exc))

        req.analysis_path.parent.mkdir(parents=True, exist_ok=True)
        req.analysis_path.write_text(result_md.rstrip() + "\n", encoding="utf-8")
        return AnalysisOutcome(ok=True, status="done", analysis_path=req.analysis_path)


class GeminiLlmAnalyzer(AnalyzerProvider):
    provider_id = "gemini-llm"

    def __init__(self, cfg: VoiceReviewConfig, secrets: SecretStore, rules: RuleStudio) -> None:
        self.cfg = cfg
        self.secrets = secrets
        self.rules = rules
        st = cfg.analyzer_settings
        self.base_url = str(st.get("base_url") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.model = str(st.get("model") or "gemini-2.5-pro").strip()
        self.timeout_sec = max(30, int(st.get("timeout_sec", 180)))

    def _api_key(self) -> str:
        key = self.secrets.get("gemini_api_key", env_var="GEMINI_API_KEY")
        if not key:
            raise RuntimeError("未配置 Gemini API Key（请在控制台「引擎插槽」填入或设置 GEMINI_API_KEY）")
        return key

    def analyze(self, req: AnalysisRequest) -> AnalysisOutcome:
        try:
            transcript_text = req.transcript_path.read_text(encoding="utf-8")
            prompt = self.rules.compose_analysis_prompt(
                template_id=req.template_id or self.cfg.default_template_id,
                title=req.title,
                recorded_at=req.recorded_at,
                duration_sec=req.duration_sec,
                speakers_summary=req.speakers_summary,
                transcript_text=transcript_text,
                extra_sections=req.extra_prompt_sections,
            )
            key = self._api_key()
            url = f"{self.base_url}/models/{self.model}:generateContent?key={key}"
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2},
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            result_md = ""
            last_err = ""
            for attempt in range(1, 4):
                http_req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
                try:
                    with urllib.request.urlopen(http_req, timeout=self.timeout_sec) as resp:
                        obj = json.loads(resp.read().decode("utf-8"))
                    cands = obj.get("candidates") or []
                    parts = (cands[0].get("content") or {}).get("parts") or [] if cands else []
                    result_md = "".join(str(p.get("text") or "") for p in parts).strip()
                    if result_md:
                        break
                    raise RuntimeError("Gemini 返回空分析内容")
                except urllib.error.HTTPError as exc:
                    last_err = f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='ignore')[:200]}"
                    if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                        time.sleep(2.5 * attempt)
                        continue
                    raise RuntimeError(last_err) from exc
                except Exception as exc:
                    last_err = str(exc)
                    if attempt < 3:
                        time.sleep(2.0 * attempt)
                        continue
                    raise
            if not result_md:
                raise RuntimeError(last_err or "Gemini 返回空分析内容")
        except Exception as exc:
            return AnalysisOutcome(ok=False, status="failed", analysis_path=None, error=str(exc))

        req.analysis_path.parent.mkdir(parents=True, exist_ok=True)
        req.analysis_path.write_text(result_md + "\n", encoding="utf-8")
        return AnalysisOutcome(ok=True, status="done", analysis_path=req.analysis_path)


def _resolve_cli_binary(bin_spec: str) -> str | None:
    raw = (bin_spec or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if p.is_file():
        return str(p.resolve())
    return shutil.which(raw)


def _extract_codex_thread_id(stdout_text: str) -> str:
    for line in (stdout_text or "").splitlines():
        s = line.strip()
        if not s or not s.startswith("{"):
            continue
        try:
            ev = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "thread.started":
            raw_id = str(ev.get("thread_id") or "").strip()
            try:
                return str(uuid.UUID(raw_id))
            except ValueError:
                continue
    return ""


def _write_codex_webloc(output_dir: Path, *, recorded_at: str, title: str, recording_id: str, chat_url: str) -> Path | None:
    try:
        links_dir = output_dir / "录音对话"
        links_dir.mkdir(parents=True, exist_ok=True)
        try:
            dt = datetime.fromisoformat(recorded_at).astimezone()
            stamp = dt.strftime("%Y-%m-%d %H%M%S")
        except Exception:
            stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
        safe_title = re.sub(r'[\\/:*?"<>|\n\r]+', "_", (title or "录音分析").strip())[:36].strip("_") or "录音分析"
        short_key = hashlib.sha256(recording_id.encode("utf-8")).hexdigest()[:8]
        target = links_dir / f"{stamp} {safe_title} · {short_key}.webloc"
        data = plistlib.dumps({"URL": chat_url})
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        return target
    except Exception:
        return None


def _open_codex_desktop_url(chat_url: str) -> bool:
    if sys.platform != "darwin" or not chat_url.startswith("codex://"):
        return False
    open_bin = "/usr/bin/open"
    if not Path(open_bin).is_file():
        return False
    try:
        res = subprocess.run(
            [open_bin, "-b", "com.openai.codex", chat_url],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


def _parse_claude_json_output(stdout_text: str) -> tuple[str, str, bool]:
    """解析 `claude -p --output-format json` 输出，兼容纯文本回退。返回 (text, session_id, is_error)。"""
    raw = (stdout_text or "").strip()
    if not raw:
        return "", "", False
    parsed: dict | None = None
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            parsed = obj
    except json.JSONDecodeError:
        for line in reversed(raw.splitlines()):
            s = line.strip()
            if not s.startswith("{"):
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict) and ("result" in obj or "session_id" in obj):
                    parsed = obj
                    break
            except json.JSONDecodeError:
                continue
    if parsed is None:
        return raw, "", False
    text = str(parsed.get("result") or "").strip()
    raw_sid = str(parsed.get("session_id") or "").strip()
    sid = ""
    if raw_sid:
        try:
            sid = str(uuid.UUID(raw_sid))
        except ValueError:
            sid = ""
    is_err = bool(parsed.get("is_error"))
    return text, sid, is_err


def _write_claude_launcher(
    output_dir: Path,
    *,
    recorded_at: str,
    title: str,
    recording_id: str,
    claude_bin: str,
    session_id: str,
) -> Path | None:
    try:
        links_dir = output_dir / "录音对话"
        links_dir.mkdir(parents=True, exist_ok=True)
        try:
            dt = datetime.fromisoformat(recorded_at).astimezone()
            stamp = dt.strftime("%Y-%m-%d %H%M%S")
        except Exception:
            stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
        safe_title = re.sub(r'[\\/:*?"<>|\n\r]+', "_", (title or "录音分析").strip())[:36].strip("_") or "录音分析"
        short_key = hashlib.sha256(recording_id.encode("utf-8")).hexdigest()[:8]
        target = links_dir / f"{stamp} {safe_title} · {short_key}.command"
        script = (
            "#!/usr/bin/env bash\n"
            f"cd {shlex.quote(str(output_dir.resolve()))} || exit 1\n"
            f"exec {shlex.quote(claude_bin)} --desktop --resume {shlex.quote(session_id)}\n"
        )
        tmp = target.with_suffix(".tmp")
        tmp.write_text(script, encoding="utf-8")
        os.chmod(tmp, 0o700)
        tmp.replace(target)
        return target
    except Exception:
        return None


def open_claude_desktop_session(claude_bin: str, session_id: str, work_cwd: Path) -> bool:
    """非阻塞唤起 Claude 桌面版并定位到对应分析会话。"""
    if sys.platform != "darwin":
        return False
    try:
        sid = str(uuid.UUID(session_id.strip()))
    except ValueError:
        return False
    exe = resolve_claude_binary(claude_bin)
    if not exe:
        return False
    try:
        work_cwd.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [exe, "--desktop", "--resume", sid],
            cwd=str(work_cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


def open_job_desktop_chat(cfg: VoiceReviewConfig, chat_url: str) -> tuple[bool, str]:
    """统一桌面端唤起入口（供控制台一键按钮与自动打开共用）。"""
    url = (chat_url or "").strip()
    if not url:
        return False, "该记录暂无关联的桌面对话"
    if url.startswith("codex://"):
        ok = _open_codex_desktop_url(url)
        return (True, "已在 Codex 桌面版打开对话") if ok else (False, "唤起 Codex 桌面版失败，请确认已安装 Codex / ChatGPT 桌面版")
    if url.startswith("claude-session://"):
        sid = url[len("claude-session://"):].strip()
        ok = open_claude_desktop_session(cfg.claude_bin, sid, cfg.output_dir)
        return (True, "已在 Claude 桌面版打开对话") if ok else (False, "唤起 Claude 桌面版失败，请确认已安装并登录 Claude 桌面版")
    return False, f"不支持的桌面对话协议：{url[:40]}"


class CodexCliAnalyzer(AnalyzerProvider):
    """免 API Key：直接调用本机 Codex 桌面版内置引擎或 Codex CLI 自动分析，支持按策略分对话及自动打开 Codex 桌面版。"""

    provider_id = "codex-cli"

    def __init__(self, cfg: VoiceReviewConfig, rules: RuleStudio, store: StateStore | None = None) -> None:
        self.cfg = cfg
        self.rules = rules
        self.store = store
        st = cfg.analyzer_settings
        self.model = str(st.get("model") or "").strip()
        self.max_chunk_chars = max(10_000, int(st.get("max_chunk_chars", DEFAULT_MAX_CHUNK_CHARS)))
        self.timeout_sec = max(60, int(st.get("timeout_sec", 420)))
        self.open_desktop_chat = bool(st.get("open_desktop_chat", True))

    def _run_codex(
        self,
        prompt: str,
        *,
        work_cwd: Path,
        ephemeral: bool = False,
        resume_thread_id: str = "",
    ) -> tuple[str, str]:
        exe = resolve_codex_binary(self.cfg.codex_bin)
        if not exe:
            raise RuntimeError(
                f"未检测到本机 Codex 桌面版或 Codex CLI（{self.cfg.codex_bin}），请先安装并登录 Codex，或切换其他分析引擎。"
            )
        work_cwd.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="vmr-codex-", suffix=".md", delete=False) as tf:
            out_file = Path(tf.name)
        cmd = [
            exe,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-C",
            str(work_cwd),
            "-o",
            str(out_file),
        ]
        if self.model:
            cmd += ["-m", self.model]
        if ephemeral:
            cmd.append("--ephemeral")
        else:
            cmd.append("--json")
            if resume_thread_id:
                cmd += ["resume", resume_thread_id]
        cmd.append("-")
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                check=False,
            )
            text = out_file.read_text(encoding="utf-8").strip() if out_file.is_file() else ""
            tid = "" if ephemeral else (_extract_codex_thread_id(proc.stdout) or resume_thread_id)
            if proc.returncode != 0 and not text:
                # 若旧分桶会话被删导致 resume 失败，自动自愈重开新对话重试一次
                if resume_thread_id and not ephemeral:
                    return self._run_codex(prompt, work_cwd=work_cwd, ephemeral=False, resume_thread_id="")
                detail = (proc.stderr or proc.stdout or "").strip()[-320:]
                raise RuntimeError(f"Codex 桌面版分析失败（exit={proc.returncode}）：{detail}")
            if not text and ephemeral:
                text = (proc.stdout or "").strip()
            if not text:
                raise RuntimeError("Codex 桌面版返回空分析内容")
            return text, tid
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Codex 桌面版分析超时（{self.timeout_sec}s）") from exc
        finally:
            out_file.unlink(missing_ok=True)

    def analyze(self, req: AnalysisRequest) -> AnalysisOutcome:
        try:
            transcript_text = req.transcript_path.read_text(encoding="utf-8")
        except OSError as exc:
            return AnalysisOutcome(ok=False, status="failed", analysis_path=None, error=f"读取转录稿失败：{exc}")
        chunks = chunk_text_for_summary(transcript_text, max_chars=self.max_chunk_chars)
        stamp_short = (req.recorded_at or "")[:16].replace("T", " ")
        lead = (
            f"请分析 {stamp_short} 的录音《{req.title}》。"
            f"后续追问可直接查阅原始转录稿：`{req.transcript_path.resolve()}`。\n\n"
            "你是一位严谨、客观的中文录音分析专家。"
            "请直接输出最终完整的 Markdown 分析正文（不要输出执行过程旁白，也不要尝试修改本地文件）。\n\n"
        )

        # 按配置 rotation 策略决定独立对话还是按天/按周/单窗续聊
        bucket, _ = resolve_bucket(self.cfg, req.recorded_at)
        meta_key = f"codex_{bucket}" if bucket else None
        prev_tid = (self.store.get_meta(meta_key) or "") if (self.store is not None and meta_key) else ""

        thread_id = ""
        try:
            if len(chunks) <= 1:
                prompt = lead + self.rules.compose_analysis_prompt(
                    template_id=req.template_id or self.cfg.default_template_id,
                    title=req.title,
                    recorded_at=req.recorded_at,
                    duration_sec=req.duration_sec,
                    speakers_summary=req.speakers_summary,
                    transcript_text=transcript_text,
                    extra_sections=req.extra_prompt_sections,
                )
                result_md, thread_id = self._run_codex(
                    prompt,
                    work_cwd=self.cfg.output_dir,
                    ephemeral=False,
                    resume_thread_id=prev_tid,
                )
            else:
                partials: list[str] = []
                for idx, chunk in enumerate(chunks, 1):
                    sub_prompt = (
                        "你是一位严谨、客观的中文录音分析专家。请直接输出高密度要点，不要多余旁白。\n\n"
                        + f"这是超长录音《{req.title}》的第 {idx}/{len(chunks)} 段转录稿。"
                        "请高密度提炼本段核心事实、关键数字、人物承诺、争议分歧与情绪波动（保留原话 `[HH:MM:SS]` 时间戳），"
                        "不要遗漏关键业务信息：\n\n"
                        + chunk
                    )
                    sub_md, _ = self._run_codex(sub_prompt, work_cwd=self.cfg.output_dir, ephemeral=True)
                    partials.append(f"### 分段提炼 {idx}/{len(chunks)}\n" + sub_md)
                final_prompt = lead + self.rules.compose_analysis_prompt(
                    template_id=req.template_id or self.cfg.default_template_id,
                    title=req.title,
                    recorded_at=req.recorded_at,
                    duration_sec=req.duration_sec,
                    speakers_summary=req.speakers_summary,
                    transcript_text="\n\n".join(partials),
                    extra_sections=req.extra_prompt_sections,
                )
                result_md, thread_id = self._run_codex(
                    final_prompt,
                    work_cwd=self.cfg.output_dir,
                    ephemeral=False,
                    resume_thread_id=prev_tid,
                )
        except Exception as exc:
            return AnalysisOutcome(ok=False, status="failed", analysis_path=None, error=str(exc))

        req.analysis_path.parent.mkdir(parents=True, exist_ok=True)
        req.analysis_path.write_text(result_md.rstrip() + "\n", encoding="utf-8")

        chat_url = ""
        if thread_id:
            if self.store is not None and meta_key:
                self.store.set_meta(meta_key, thread_id)
            chat_url = f"codex://threads/{thread_id}"
            _write_codex_webloc(
                self.cfg.output_dir,
                recorded_at=req.recorded_at,
                title=req.title,
                recording_id=req.recording_id,
                chat_url=chat_url,
            )
            if self.open_desktop_chat:
                _open_codex_desktop_url(chat_url)

        return AnalysisOutcome(ok=True, status="done", analysis_path=req.analysis_path, chat_url=chat_url)


class ClaudeCliAnalyzer(AnalyzerProvider):
    """免 API Key：调用本机 Claude 桌面版内置引擎或 Claude Code CLI 自动分析，支持按策略分对话及自动打开 Claude 桌面版。"""

    provider_id = "claude-cli"

    def __init__(self, cfg: VoiceReviewConfig, rules: RuleStudio, store: StateStore | None = None) -> None:
        self.cfg = cfg
        self.rules = rules
        self.store = store
        st = cfg.analyzer_settings
        self.model = str(st.get("model") or "").strip()
        self.max_chunk_chars = max(10_000, int(st.get("max_chunk_chars", DEFAULT_MAX_CHUNK_CHARS)))
        self.timeout_sec = max(60, int(st.get("timeout_sec", 420)))
        self.open_desktop_chat = bool(st.get("open_desktop_chat", True))

    def _run_claude(
        self,
        prompt: str,
        *,
        work_cwd: Path,
        session_name: str = "",
        ephemeral: bool = False,
        resume_session_id: str = "",
    ) -> tuple[str, str]:
        exe = resolve_claude_binary(self.cfg.claude_bin)
        if not exe:
            raise RuntimeError(
                f"未检测到本机 Claude 桌面版或 Claude Code CLI（{self.cfg.claude_bin}），请先安装并登录 Claude，或切换其他分析引擎。"
            )
        work_cwd.mkdir(parents=True, exist_ok=True)
        cmd = [exe, "-p", "--output-format", "json", "--tools", ""]
        if self.model:
            cmd += ["--model", self.model]
        if ephemeral:
            cmd.append("--no-session-persistence")
        elif resume_session_id:
            cmd += ["--resume", resume_session_id]
        elif session_name:
            cmd += ["--name", session_name]
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                cwd=str(work_cwd),
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                check=False,
            )
            text, sid, is_err = _parse_claude_json_output(proc.stdout)
            sid = "" if ephemeral else (sid or resume_session_id)
            if is_err:
                low = text.lower()
                if "not logged in" in low or "/login" in low:
                    raise RuntimeError(
                        "本机 Claude 尚未登录：请打开 Claude 桌面版「Code」页签登录账号，或在终端运行 `claude /login` 后重试。"
                    )
                if resume_session_id and not ephemeral:
                    return self._run_claude(
                        prompt,
                        work_cwd=work_cwd,
                        session_name=session_name,
                        ephemeral=False,
                        resume_session_id="",
                    )
                raise RuntimeError(f"Claude 桌面版分析返回错误：{text[-320:]}")
            if proc.returncode != 0 and not text:
                if resume_session_id and not ephemeral:
                    return self._run_claude(
                        prompt,
                        work_cwd=work_cwd,
                        session_name=session_name,
                        ephemeral=False,
                        resume_session_id="",
                    )
                detail = (proc.stderr or proc.stdout or "").strip()[-320:]
                if "not logged in" in detail.lower() or "/login" in detail.lower():
                    raise RuntimeError(
                        "本机 Claude 尚未登录：请打开 Claude 桌面版「Code」页签登录账号，或在终端运行 `claude /login` 后重试。"
                    )
                raise RuntimeError(f"Claude 桌面版分析失败（exit={proc.returncode}）：{detail}")
            if not text:
                raise RuntimeError("Claude 桌面版返回空分析内容")
            return text, sid
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Claude 桌面版分析超时（{self.timeout_sec}s）") from exc

    def analyze(self, req: AnalysisRequest) -> AnalysisOutcome:
        try:
            transcript_text = req.transcript_path.read_text(encoding="utf-8")
        except OSError as exc:
            return AnalysisOutcome(ok=False, status="failed", analysis_path=None, error=f"读取转录稿失败：{exc}")
        chunks = chunk_text_for_summary(transcript_text, max_chars=self.max_chunk_chars)
        stamp_short = (req.recorded_at or "")[:16].replace("T", " ")
        lead = (
            f"请分析 {stamp_short} 的录音《{req.title}》。"
            f"后续追问可直接查阅原始转录稿：`{req.transcript_path.resolve()}`。\n\n"
            "你是一位严谨、客观的中文录音分析专家。"
            "请直接输出最终完整的 Markdown 分析正文（不要输出执行过程旁白，也不要尝试修改本地文件）。\n\n"
        )

        bucket, bucket_title = resolve_bucket(self.cfg, req.recorded_at)
        meta_key = f"claude_{bucket}" if bucket else None
        prev_sid = (self.store.get_meta(meta_key) or "") if (self.store is not None and meta_key) else ""
        session_name = bucket_title if bucket else f"录音分析 · {req.title[:28]}"

        session_id = ""
        try:
            if len(chunks) <= 1:
                prompt = lead + self.rules.compose_analysis_prompt(
                    template_id=req.template_id or self.cfg.default_template_id,
                    title=req.title,
                    recorded_at=req.recorded_at,
                    duration_sec=req.duration_sec,
                    speakers_summary=req.speakers_summary,
                    transcript_text=transcript_text,
                    extra_sections=req.extra_prompt_sections,
                )
                result_md, session_id = self._run_claude(
                    prompt,
                    work_cwd=self.cfg.output_dir,
                    session_name=session_name,
                    ephemeral=False,
                    resume_session_id=prev_sid,
                )
            else:
                partials: list[str] = []
                for idx, chunk in enumerate(chunks, 1):
                    sub_prompt = (
                        "你是一位严谨、客观的中文录音分析专家。请直接输出高密度要点，不要多余旁白。\n\n"
                        + f"这是超长录音《{req.title}》的第 {idx}/{len(chunks)} 段转录稿。"
                        "请高密度提炼本段核心事实、关键数字、人物承诺、争议分歧与情绪波动（保留原话 `[HH:MM:SS]` 时间戳），"
                        "不要遗漏关键业务信息：\n\n"
                        + chunk
                    )
                    sub_md, _ = self._run_claude(
                        sub_prompt,
                        work_cwd=self.cfg.output_dir,
                        ephemeral=True,
                    )
                    partials.append(f"### 分段提炼 {idx}/{len(chunks)}\n" + sub_md)
                final_prompt = lead + self.rules.compose_analysis_prompt(
                    template_id=req.template_id or self.cfg.default_template_id,
                    title=req.title,
                    recorded_at=req.recorded_at,
                    duration_sec=req.duration_sec,
                    speakers_summary=req.speakers_summary,
                    transcript_text="\n\n".join(partials),
                    extra_sections=req.extra_prompt_sections,
                )
                result_md, session_id = self._run_claude(
                    final_prompt,
                    work_cwd=self.cfg.output_dir,
                    session_name=session_name,
                    ephemeral=False,
                    resume_session_id=prev_sid,
                )
        except Exception as exc:
            return AnalysisOutcome(ok=False, status="failed", analysis_path=None, error=str(exc))

        req.analysis_path.parent.mkdir(parents=True, exist_ok=True)
        req.analysis_path.write_text(result_md.rstrip() + "\n", encoding="utf-8")

        chat_url = ""
        if session_id:
            if self.store is not None and meta_key:
                self.store.set_meta(meta_key, session_id)
            chat_url = f"claude-session://{session_id}"
            exe = resolve_claude_binary(self.cfg.claude_bin) or self.cfg.claude_bin
            _write_claude_launcher(
                self.cfg.output_dir,
                recorded_at=req.recorded_at,
                title=req.title,
                recording_id=req.recording_id,
                claude_bin=exe,
                session_id=session_id,
            )
            if self.open_desktop_chat:
                open_claude_desktop_session(exe, session_id, self.cfg.output_dir)

        return AnalysisOutcome(ok=True, status="done", analysis_path=req.analysis_path, chat_url=chat_url)


def get_analyzer(
    cfg: VoiceReviewConfig,
    secrets: SecretStore,
    store: StateStore,
    rules: RuleStudio,
    *,
    override_provider: str | None = None,
) -> AnalyzerProvider:
    pid = (override_provider or cfg.analyzer_auto_provider or "antigravity-agentapi").strip().lower()
    if pid in ("none", "mcp-host"):
        return McpHostAnalyzer(cfg, rules)
    if pid == "antigravity-agentapi":
        return AntigravityAgentAnalyzer(cfg, store, rules)
    if pid == "codex-cli":
        return CodexCliAnalyzer(cfg, rules, store=store)
    if pid == "claude-cli":
        return ClaudeCliAnalyzer(cfg, rules, store=store)
    if pid == "openai-compatible-llm":
        return OpenAICompatibleLlmAnalyzer(cfg, secrets, rules)
    if pid == "gemini-llm":
        return GeminiLlmAnalyzer(cfg, secrets, rules)
    raise ValueError(f"未知的分析引擎：{pid}")


def test_analyzer_connection(cfg: VoiceReviewConfig, secrets: SecretStore) -> tuple[bool, str]:
    pid = cfg.analyzer_auto_provider
    if pid in ("none", "mcp-host"):
        return True, "当前为手动/MCP 接手分析模式（后台仅自动完成转写）"
    if pid == "antigravity-agentapi":
        ok = cfg.agentapi_bin.is_file()
        return (True, f"本机 Antigravity 可用：{cfg.agentapi_bin}") if ok else (False, f"未检测到 agentapi：{cfg.agentapi_bin}")
    if pid == "codex-cli":
        exe = resolve_codex_binary(cfg.codex_bin)
        return (
            (True, f"本机 Codex 桌面版 / Codex CLI 可用（免 Key）：{exe}")
            if exe
            else (False, f"未检测到 Codex 桌面版或 Codex CLI：{cfg.codex_bin}")
        )
    if pid == "claude-cli":
        exe = resolve_claude_binary(cfg.claude_bin)
        if not exe:
            return False, f"未检测到 Claude 桌面版或 Claude Code CLI：{cfg.claude_bin}"
        try:
            proc = subprocess.run(
                [exe, "auth", "status"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=2.5,
                check=False,
            )
            raw_auth = (proc.stdout or "").strip()
            if raw_auth.startswith("{"):
                st = json.loads(raw_auth)
                if isinstance(st, dict) and st.get("loggedIn") is False:
                    return (
                        False,
                        f"已检测到 Claude 桌面版 / Claude Code CLI（{exe}），但尚未登录，请先在 Claude 桌面版登录或运行 `claude /login`",
                    )
        except Exception:
            pass
        return True, f"本机 Claude 桌面版 / Claude Code CLI 可用（免 Key）：{exe}"
    if pid == "openai-compatible-llm":
        base = str(cfg.analyzer_settings.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        key = secrets.get("openai_llm_api_key", env_var="OPENAI_API_KEY")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        req = urllib.request.Request(f"{base}/models", headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                return (200 <= resp.status < 300), f"OpenAI 兼容 LLM 端点连通正常（HTTP {resp.status}）"
        except Exception as exc:
            return False, f"OpenAI 兼容 LLM 连通测试失败：{exc}"
    if pid == "gemini-llm":
        key = secrets.get("gemini_api_key", env_var="GEMINI_API_KEY")
        if not key:
            return False, "缺少 Gemini API Key"
        base = str(cfg.analyzer_settings.get("base_url") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        model = str(cfg.analyzer_settings.get("model") or "gemini-2.5-pro")
        req = urllib.request.Request(f"{base}/models/{model}?key={key}", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                return (200 <= resp.status < 300), f"Gemini 分析模型 {model} 连通正常"
        except Exception as exc:
            return False, f"Gemini 分析连通测试失败：{exc}"
    return False, f"未知分析引擎：{pid}"
