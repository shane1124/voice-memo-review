"""插槽 4：可插拔分析引擎（mcp-host / antigravity-agentapi / codex-cli / claude-cli / openai-compatible-llm / gemini-llm）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from voice_review.agent_bridge import AgentBridge, compute_dynamic_timeout_sec, render_v2_review_prompt
from voice_review.config import VoiceReviewConfig
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


class CodexCliAnalyzer(AnalyzerProvider):
    """免 API Key：直接调用本机已登录的 Codex CLI（codex exec）按显化模板自动生成复盘。"""

    provider_id = "codex-cli"

    def __init__(self, cfg: VoiceReviewConfig, rules: RuleStudio) -> None:
        self.cfg = cfg
        self.rules = rules
        st = cfg.analyzer_settings
        self.model = str(st.get("model") or "").strip()
        self.max_chunk_chars = max(10_000, int(st.get("max_chunk_chars", DEFAULT_MAX_CHUNK_CHARS)))
        self.timeout_sec = max(60, int(st.get("timeout_sec", 420)))

    def _run_codex(self, prompt: str, *, work_cwd: Path) -> str:
        exe = _resolve_cli_binary(self.cfg.codex_bin)
        if not exe:
            raise RuntimeError(
                f"未检测到本机 Codex CLI（{self.cfg.codex_bin}），请先安装并登录 Codex，或切换其他分析引擎。"
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
            if proc.returncode != 0 and not text:
                detail = (proc.stderr or proc.stdout or "").strip()[-320:]
                raise RuntimeError(f"Codex CLI 分析失败（exit={proc.returncode}）：{detail}")
            if not text:
                text = (proc.stdout or "").strip()
            if not text:
                raise RuntimeError("Codex CLI 返回空分析内容")
            return text
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Codex CLI 分析超时（{self.timeout_sec}s）") from exc
        finally:
            out_file.unlink(missing_ok=True)

    def analyze(self, req: AnalysisRequest) -> AnalysisOutcome:
        try:
            transcript_text = req.transcript_path.read_text(encoding="utf-8")
        except OSError as exc:
            return AnalysisOutcome(ok=False, status="failed", analysis_path=None, error=f"读取转录稿失败：{exc}")
        chunks = chunk_text_for_summary(transcript_text, max_chars=self.max_chunk_chars)
        header = (
            "你是一位严谨、客观的中文录音分析专家。"
            "请直接输出最终完整的 Markdown 复盘正文（不要输出执行过程旁白，也不要尝试修改本地文件）。\n\n"
        )
        try:
            if len(chunks) <= 1:
                prompt = header + self.rules.compose_analysis_prompt(
                    template_id=req.template_id or self.cfg.default_template_id,
                    title=req.title,
                    recorded_at=req.recorded_at,
                    duration_sec=req.duration_sec,
                    speakers_summary=req.speakers_summary,
                    transcript_text=transcript_text,
                    extra_sections=req.extra_prompt_sections,
                )
                result_md = self._run_codex(prompt, work_cwd=self.cfg.output_dir)
            else:
                partials: list[str] = []
                for idx, chunk in enumerate(chunks, 1):
                    sub_prompt = (
                        header
                        + f"这是超长录音《{req.title}》的第 {idx}/{len(chunks)} 段转录稿。"
                        "请高密度提炼本段核心事实、关键数字、人物承诺、争议分歧与情绪波动（保留原话 `[HH:MM:SS]` 时间戳），"
                        "不要遗漏关键业务信息：\n\n"
                        + chunk
                    )
                    partials.append(f"### 分段提炼 {idx}/{len(chunks)}\n" + self._run_codex(sub_prompt, work_cwd=self.cfg.output_dir))
                final_prompt = header + self.rules.compose_analysis_prompt(
                    template_id=req.template_id or self.cfg.default_template_id,
                    title=req.title,
                    recorded_at=req.recorded_at,
                    duration_sec=req.duration_sec,
                    speakers_summary=req.speakers_summary,
                    transcript_text="\n\n".join(partials),
                    extra_sections=req.extra_prompt_sections,
                )
                result_md = self._run_codex(final_prompt, work_cwd=self.cfg.output_dir)
        except Exception as exc:
            return AnalysisOutcome(ok=False, status="failed", analysis_path=None, error=str(exc))

        req.analysis_path.parent.mkdir(parents=True, exist_ok=True)
        req.analysis_path.write_text(result_md.rstrip() + "\n", encoding="utf-8")
        return AnalysisOutcome(ok=True, status="done", analysis_path=req.analysis_path)


class ClaudeCliAnalyzer(AnalyzerProvider):
    """免 API Key：直接调用本机已登录的 Claude Code CLI（claude -p）按显化模板自动生成复盘。"""

    provider_id = "claude-cli"

    def __init__(self, cfg: VoiceReviewConfig, rules: RuleStudio) -> None:
        self.cfg = cfg
        self.rules = rules
        st = cfg.analyzer_settings
        self.model = str(st.get("model") or "").strip()
        self.max_chunk_chars = max(10_000, int(st.get("max_chunk_chars", DEFAULT_MAX_CHUNK_CHARS)))
        self.timeout_sec = max(60, int(st.get("timeout_sec", 420)))

    def _run_claude(self, prompt: str) -> str:
        exe = _resolve_cli_binary(self.cfg.claude_bin)
        if not exe:
            raise RuntimeError(
                f"未检测到本机 Claude Code CLI（{self.cfg.claude_bin}），请先安装并登录 Claude Code，或切换其他分析引擎。"
            )
        cmd = [exe, "-p", "--output-format", "text"]
        if self.model:
            cmd += ["--model", self.model]
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                check=False,
            )
            text = (proc.stdout or "").strip()
            if proc.returncode != 0 and not text:
                detail = (proc.stderr or proc.stdout or "").strip()[-320:]
                raise RuntimeError(f"Claude Code CLI 分析失败（exit={proc.returncode}）：{detail}")
            if not text:
                raise RuntimeError("Claude Code CLI 返回空分析内容")
            return text
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Claude Code CLI 分析超时（{self.timeout_sec}s）") from exc

    def analyze(self, req: AnalysisRequest) -> AnalysisOutcome:
        try:
            transcript_text = req.transcript_path.read_text(encoding="utf-8")
        except OSError as exc:
            return AnalysisOutcome(ok=False, status="failed", analysis_path=None, error=f"读取转录稿失败：{exc}")
        chunks = chunk_text_for_summary(transcript_text, max_chars=self.max_chunk_chars)
        header = (
            "你是一位严谨、客观的中文录音分析专家。"
            "请直接输出最终完整的 Markdown 复盘正文（不要输出执行过程旁白）。\n\n"
        )
        try:
            if len(chunks) <= 1:
                prompt = header + self.rules.compose_analysis_prompt(
                    template_id=req.template_id or self.cfg.default_template_id,
                    title=req.title,
                    recorded_at=req.recorded_at,
                    duration_sec=req.duration_sec,
                    speakers_summary=req.speakers_summary,
                    transcript_text=transcript_text,
                    extra_sections=req.extra_prompt_sections,
                )
                result_md = self._run_claude(prompt)
            else:
                partials: list[str] = []
                for idx, chunk in enumerate(chunks, 1):
                    sub_prompt = (
                        header
                        + f"这是超长录音《{req.title}》的第 {idx}/{len(chunks)} 段转录稿。"
                        "请高密度提炼本段核心事实、关键数字、人物承诺、争议分歧与情绪波动（保留原话 `[HH:MM:SS]` 时间戳），"
                        "不要遗漏关键业务信息：\n\n"
                        + chunk
                    )
                    partials.append(f"### 分段提炼 {idx}/{len(chunks)}\n" + self._run_claude(sub_prompt))
                final_prompt = header + self.rules.compose_analysis_prompt(
                    template_id=req.template_id or self.cfg.default_template_id,
                    title=req.title,
                    recorded_at=req.recorded_at,
                    duration_sec=req.duration_sec,
                    speakers_summary=req.speakers_summary,
                    transcript_text="\n\n".join(partials),
                    extra_sections=req.extra_prompt_sections,
                )
                result_md = self._run_claude(final_prompt)
        except Exception as exc:
            return AnalysisOutcome(ok=False, status="failed", analysis_path=None, error=str(exc))

        req.analysis_path.parent.mkdir(parents=True, exist_ok=True)
        req.analysis_path.write_text(result_md.rstrip() + "\n", encoding="utf-8")
        return AnalysisOutcome(ok=True, status="done", analysis_path=req.analysis_path)


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
        return CodexCliAnalyzer(cfg, rules)
    if pid == "claude-cli":
        return ClaudeCliAnalyzer(cfg, rules)
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
        exe = _resolve_cli_binary(cfg.codex_bin)
        return (True, f"本机 Codex CLI 可用（免 Key）：{exe}") if exe else (False, f"未检测到 Codex CLI：{cfg.codex_bin}")
    if pid == "claude-cli":
        exe = _resolve_cli_binary(cfg.claude_bin)
        return (True, f"本机 Claude Code CLI 可用（免 Key）：{exe}") if exe else (False, f"未检测到 Claude Code CLI：{cfg.claude_bin}")
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
