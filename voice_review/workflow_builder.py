"""V2 Antigravity 并发工作流生成器（高并发自适应护栏 + 120s 超时自动对半拆包 + 缺段精准补录 + 轻量子代理探活 + 断点缓存）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import time


_SCHEMA_LEAK_RE = re.compile(
    r"""(?is)(?:["']?\s*\}\s*,\s*\{?\s*|,\s*)?(?:multi_speaker_note|tone_note|seg_id|batch_id)\s*["']?\s*[:：].*$"""
)
_DANGLING_BRACE_RE = re.compile(r"""(?s)(?:["']?\s*\}\s*,\s*\{.*|["']?\s*\}\s*\]\s*\}?\s*)$""")
_PARSER_ERR_RE = re.compile(
    r"""(?is)\s*(?:JsonParserError\b|Oops,\s*I\s+forgot\b|JSONDecodeError\b).*$"""
)


def sanitize_transcript_field(raw: object) -> str:
    """清洗模型严格 JSON 输出偶发渗入正文/备注的模板字符或解析报错碎片。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    cleaned = _SCHEMA_LEAK_RE.sub("", text)
    cleaned = _DANGLING_BRACE_RE.sub("", cleaned)
    cleaned = _PARSER_ERR_RE.sub("", cleaned)
    if cleaned != text:
        cleaned = cleaned.rstrip(' "\',{}[]')
    return cleaned.strip()

from voice_review.diarizer import FluidAudioDiarizer, NoSpeechDetectedError
from voice_review.segmenter import (
    SegmenterConfig,
    clean_and_merge_segments,
    cut_segments_to_mp3,
    detect_split_id_checks,
    format_hms,
    pack_batches,
)


@dataclass(frozen=True)
class V2JobResult:
    job_dir: Path
    manifest_path: Path
    workflow_script_path: Path
    transcript_path: Path
    speakers_summary: str
    segment_count: int
    batch_count: int
    split_check_count: int
    prep_sec: float


def render_workflow_script(
    manifest_path: Path,
    *,
    cleanup_clips: bool = True,
    max_concurrency: int = 25,
    agent_timeout_sec: int = 120,
    stagger_sec: float = 0.25,
    preferred_subagent: str = "audio_transcriber_lite",
    max_turns_per_worker: int = 6,
    worker_pool_size: int = 0,
) -> str:
    """生成可直接交给 Antigravity `run_workflow(ScriptPath=...)` 执行的高性能稳态工作流脚本（支持 Worker 池复用会话，削减 80%+ 本地会话生成量）。"""
    manifest_literal = json.dumps(str(manifest_path.resolve()), ensure_ascii=False)
    cleanup_literal = "True" if cleanup_clips else "False"
    max_conc = max(1, int(max_concurrency))
    fallback_conc = max(1, max_conc // 2)
    timeout_sec = max(30, int(agent_timeout_sec))
    stagger = max(0.0, float(stagger_sec))
    max_turns = max(1, int(max_turns_per_worker))
    pool_size_cfg = max(0, int(worker_pool_size))
    pref_agent_literal = json.dumps(str(preferred_subagent or "").strip(), ensure_ascii=False)
    schema_leak_pat_literal = json.dumps(_SCHEMA_LEAK_RE.pattern, ensure_ascii=False)
    dangling_brace_pat_literal = json.dumps(_DANGLING_BRACE_RE.pattern, ensure_ascii=False)
    parser_err_pat_literal = json.dumps(_PARSER_ERR_RE.pattern, ensure_ascii=False)
    strip_chars_literal = json.dumps(' "\',{}[]', ensure_ascii=False)

    return f'''"""由 voice-memo-review 自动生成的 Antigravity V2 并发精转工作流（Worker 池会话复用 + 高并发稳态护栏 + 断点缓存 + 情绪捕捉）。"""
import asyncio
import json
import random
import re
import shutil
import time
from pathlib import Path

_SCHEMA_LEAK_RE = re.compile({schema_leak_pat_literal})
_DANGLING_BRACE_RE = re.compile({dangling_brace_pat_literal})
_PARSER_ERR_RE = re.compile({parser_err_pat_literal})
_STRIP_CHARS = {strip_chars_literal}


def _sanitize_field(raw) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    cleaned = _SCHEMA_LEAK_RE.sub("", text)
    cleaned = _DANGLING_BRACE_RE.sub("", cleaned)
    cleaned = _PARSER_ERR_RE.sub("", cleaned)
    if cleaned != text:
        cleaned = cleaned.rstrip(_STRIP_CHARS)
    return cleaned.strip()

MANIFEST_PATH = Path({manifest_literal})
CLEANUP_CLIPS = {cleanup_literal}
MAX_CONCURRENCY = {max_conc}
FALLBACK_CONCURRENCY = {fallback_conc}
AGENT_TIMEOUT_SEC = {timeout_sec}
STAGGER_SEC = {stagger}
LAUNCH_PACE_SEC = max(0.35, STAGGER_SEC)
MAX_RETRIES = 4
MAX_SPLIT_DEPTH = 2
MAX_TURNS_PER_WORKER = {max_turns}
WORKER_POOL_SIZE = {pool_size_cfg}
AUTO_POOL_CAP = 12
PREFERRED_SUBAGENT = {pref_agent_literal}

manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
job_dir = MANIFEST_PATH.parent
cache_dir = job_dir / "batches_out"
cache_dir.mkdir(parents=True, exist_ok=True)
phase0_cache_path = job_dir / "phase0_merged_cache.json"
transcript_path = Path(manifest["transcript_path"])
clips_dir = Path(manifest["clips_dir"])
asr_context = (manifest.get("asr_context") or "").strip()
context_hint = f"\\n【高频专有名词词表（遇到同音/近音词优先按此校对；严禁凭空编造未听到的词）】\\n{{asr_context}}\\n" if asr_context else ""


class AdaptiveLimiter:
    """支持限流自动降级与全局发射节流锁的稳态并发闸门（防瞬间高并发触发客户端状态表竞态）。"""

    def __init__(self, initial: int, fallback: int, launch_pace_sec: float = LAUNCH_PACE_SEC) -> None:
        self.limit = initial
        self.fallback = fallback
        self.launch_pace_sec = max(0.0, float(launch_pace_sec))
        self.active = 0
        self.rate_limit_hits = []
        self.degraded = False
        self._last_launch = 0.0
        self._pace_lock = asyncio.Lock()
        self._cond = asyncio.Condition()

    async def acquire(self) -> None:
        async with self._cond:
            while self.active >= self.limit:
                await self._cond.wait()
            self.active += 1
        if self.launch_pace_sec > 0:
            async with self._pace_lock:
                now = time.monotonic()
                wait = self.launch_pace_sec - (now - self._last_launch)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_launch = time.monotonic()

    async def release(self) -> None:
        async with self._cond:
            self.active = max(0, self.active - 1)
            self._cond.notify_all()

    async def record_rate_limit(self) -> None:
        now = time.monotonic()
        async with self._cond:
            self.rate_limit_hits = [t for t in self.rate_limit_hits if now - t <= 60.0]
            self.rate_limit_hits.append(now)
            if not self.degraded and len(self.rate_limit_hits) >= 4 and self.limit > self.fallback:
                self.limit = self.fallback
                self.degraded = True
                log(f"[自适应降级] 60秒内检测到 {{len(self.rate_limit_hits)}} 次限流，并发上限自动降至 {{self.limit}} 无缝续跑")


LIMITER = AdaptiveLimiter(MAX_CONCURRENCY, FALLBACK_CONCURRENCY)


def _is_rate_limit_err(msg: str) -> bool:
    low = (msg or "").lower()
    return any(k in low for k in ("429", "rate limit", "ratelimit", "resource_exhausted", "quota", "too many requests", "503", "overloaded"))


async def _await_agent(promise, timeout_sec: float):
    async def _inner():
        return await promise
    return await asyncio.wait_for(_inner(), timeout=timeout_sec)


PROBE_SCHEMA = {{
    "type": "object",
    "properties": {{"ok": {{"type": "boolean"}}, "heard": {{"type": "string"}}}},
    "required": ["ok"],
}}

MERGE_SCHEMA = {{
    "type": "object",
    "properties": {{
        "same_person": {{"type": "boolean"}},
        "confidence": {{"type": "string"}},
        "reason": {{"type": "string"}},
    }},
    "required": ["same_person", "confidence", "reason"],
}}

BATCH_SCHEMA = {{
    "type": "object",
    "properties": {{
        "batch_id": {{"type": "string"}},
        "items": {{
            "type": "array",
            "items": {{
                "type": "object",
                "properties": {{
                    "seg_id": {{"type": "string"}},
                    "text": {{"type": "string"}},
                    "tone_note": {{"type": "string"}},
                    "multi_speaker_note": {{"type": "string"}},
                }},
                "required": ["seg_id", "text"],
            }},
        }},
    }},
    "required": ["batch_id", "items"],
}}


async def select_worker_type() -> str:
    if not PREFERRED_SUBAGENT or PREFERRED_SUBAGENT == "self":
        return "self"
    segs = manifest.get("segments") or []
    probe_seg = next((s for s in segs if s.get("mp3_path") and Path(s["mp3_path"]).is_file()), None)
    if not probe_seg:
        return "self"
    try:
        res = await _await_agent(
            agent(
                f"请只调用 read_file 读取音频 `{{probe_seg['mp3_path']}}`，确认能否读通并简述听到的前几个字（听不清填 [听不清]），立刻调用 finish 返回 JSON。",
                schema=PROBE_SCHEMA,
                role="轻量子代理路径探活",
                type_name=PREFERRED_SUBAGENT,
                model="flash",
            ),
            timeout_sec=45,
        )
        if res.get("ok"):
            log(f"[探活成功] 启用轻量子代理 {{PREFERRED_SUBAGENT}}（甩掉主系统长提示词）")
            return PREFERRED_SUBAGENT
    except Exception as exc:
        log(f"[探活回退] 轻量子代理 {{PREFERRED_SUBAGENT}} 不可用或无路径权限（{{exc}}），自动回退 type_name='self'")
    return "self"


async def verify_split_speaker(chk, worker_type: str):
    prompt = f"""你是声纹听音复核员，请在同一回合并行调用 `read_file` 听这两段音频，判断它们是否为同一位说话人（只是麦距/环境变化导致声纹聚类裂变）：
- 音频 A（待确认 {{chk['unconfirmed_label']}}，{{chk['unconfirmed_dur']}}s）：`{{chk['unconfirmed_mp3']}}`
- 音频 B（已确认真名 {{chk['target_name']}}，{{chk['target_dur']}}s）：`{{chk['target_mp3']}}`

严禁读取其他文件或运行命令。听完直接调用 `finish` 返回判断 JSON。"""
    await LIMITER.acquire()
    try:
        res = await _await_agent(
            agent(
                prompt,
                schema=MERGE_SCHEMA,
                role=f"声纹归并 {{chk['unconfirmed_sid']}}≈{{chk['target_name']}}",
                type_name=worker_type,
                model="flash",
            ),
            timeout_sec=AGENT_TIMEOUT_SEC,
        )
        same = bool(res.get("same_person"))
        log(f"[声纹核对] {{chk['unconfirmed_sid']}} vs {{chk['target_name']}} -> same={{same}} ({{res.get('confidence')}}): {{res.get('reason')}}")
        return chk["unconfirmed_sid"], chk["target_name"] if same else None
    except Exception as exc:
        log(f"[声纹核对降级] {{chk['unconfirmed_sid']}}: {{exc}}")
        return chk["unconfirmed_sid"], None
    finally:
        await LIMITER.release()


def build_worker_standing_prompt() -> str:
    """固定工位常驻指令：仅在工位首次创建时发送，多轮复用彻底避免重复造会话。"""
    return f"""你是多轮复用的高精度中文/中英混说语音精转工位。

【严格隔离与多轮防串台纪律（必须遵守）】
- 每一轮只处理当前这一轮消息里列出的 `seg_id` 和 MP3 文件路径；**严禁复用、照抄或混入上一轮已经听写过的任何内容**。
- 只能调用 `read_file` 并行读取当前轮列出的全部 MP3 文件，听完后直接调用 `finish` 返回本轮 JSON。
- 严禁读取其他文件、严禁写文件、严禁运行命令。
{{context_hint}}
【转写与防死磕护栏（必须遵守）】
1. 结合本轮上下句语境把每一段**逐字完整听写**，精准还原中英专词、数字与人名，加自然标点；不要在 `text` 开头重复写时间戳或人名。
2. **防死磕护栏**：纯背景杂音、餐馆底噪、远场无法辨清的模糊音节直接留空 `""` 或简标 `[听不清]`，**严禁反复逐音节猜测推演消耗思考时间**。
3. **前向气口去重**：每段音频开头向前借了 0.25 秒气口防咬字；若开头听到上一段末尾残留的半个尾音/尾字，请忽略不要重复写进本段。
4. **零漏段**：本轮清单中每条 `seg_id` 都必须出现在 `items` 中（纯杂音 `text` 填 `""`）。
5. **端到端语气情绪标注（`tone_note`）**：仅当某段出现明显的情绪、气场或节奏波动时（如：语速放缓略显犹豫、强势压迫截断、自嘲大笑气氛轻松、明显焦虑急促、敷衍应付、兴奋强调等），用 6～20 字客观简注；平淡正常陈述留空 `""`。
6. **叠话/插话标注（`multi_speaker_note`）**：仅当段内清楚听出第二个人插话或双线重叠对话时简注拆分；单人正常发言留空 `""`。
"""


def build_turn_prompt(batch_id, batch, merged_names, *, is_patch: bool = False) -> str:
    """复用工位的单轮轻量化任务指令（仅传本轮音频清单，命中缓存并防串台）。"""
    lines = []
    for s in batch:
        spk = merged_names.get(s["sid"], s["speaker_label"])
        lines.append(
            f"- `{{s['seg_id']}}` | 时间 `{{s['hms_start']}}–{{s['hms_end']}}` ({{s['dur']}}s) | 说话人 `{{spk}}` | 音频：`{{s['mp3_path']}}`"
        )
    seg_list = "\\n".join(lines)
    seg_ids = ", ".join(s["seg_id"] for s in batch)
    tag = "缺段精准补录" if is_patch else "连续对话短音频"
    return f"""【全新独立轮次：批次 `{{batch_id}}`（{{tag}}，共 {{len(batch)}} 段）— 请忽略上一轮内容，仅处理下方音频】
{{seg_list}}

请在同一回合内并行调用 `read_file` 读完以上 {{len(batch)}} 个 MP3，逐字转写（`{{seg_ids}}`）并立即调用 `finish` 返回 JSON（`batch_id` 填 `{{batch_id}}`）。"""


def build_batch_prompt(batch_id, batch, merged_names, *, is_patch: bool = False):
    lines = []
    for s in batch:
        spk = merged_names.get(s["sid"], s["speaker_label"])
        lines.append(
            f"- `{{s['seg_id']}}` | 时间 `{{s['hms_start']}}–{{s['hms_end']}}` ({{s['dur']}}s) | 说话人 `{{spk}}` | 音频：`{{s['mp3_path']}}`"
        )
    seg_list = "\\n".join(lines)
    seg_ids = ", ".join(s["seg_id"] for s in batch)
    tag = "缺段精准补录" if is_patch else "连续对话短音频"
    return f"""你是高精度中文/中英混说语音精转专家（批次 `{{batch_id}}`，{{tag}}，共 {{len(batch)}} 段）。

【严格隔离纪律】
- 只能调用 `read_file` 并行读取下方列出的 {{len(batch)}} 个 MP3 文件，听完后直接调用 `finish` 返回 JSON。
- 严禁读取其他文件、严禁写文件、严禁运行命令。
{{context_hint}}
【短音频清单（请在同一个回合内并行调用 `read_file` 一次性读完）】
{{seg_list}}

【转写与防死磕护栏（必须遵守）】
1. 结合上下句语境把每一段（`{{seg_ids}}`）**逐字完整听写**，精准还原中英专词、数字与人名，加自然标点；不要在 `text` 开头重复写时间戳或人名。
2. **防死磕护栏**：纯背景杂音、餐馆底噪、远场无法辨清的模糊音节直接留空 `""` 或简标 `[听不清]`，**严禁反复逐音节猜测推演消耗思考时间**。
3. **前向气口去重**：每段音频开头向前借了 0.25 秒气口防咬字；若开头听到上一段末尾残留的半个尾音/尾字，请忽略不要重复写进本段。
4. **零漏段**：每条 `seg_id` 都必须出现在 `items` 中（纯杂音 `text` 填 `""`）。
5. **端到端语气情绪标注（`tone_note`）**：仅当某段出现明显的情绪、气场或节奏波动时（如：语速放缓略显犹豫、强势压迫截断、自嘲大笑气氛轻松、明显焦虑急促、敷衍应付、兴奋强调等），用 6～20 字客观简注；平淡正常陈述留空 `""`。
6. **叠话/插话标注（`multi_speaker_note`）**：仅当段内清楚听出第二个人插话或双线重叠对话时简注拆分；单人正常发言留空 `""`。
"""


def compute_worker_pool_size(total_batches: int) -> int:
    """根据批次数和配置自动计算最优复用工位数量，既保并发效率又将总会话数压低 80%+。"""
    n = max(1, int(total_batches))
    if WORKER_POOL_SIZE > 0:
        return max(1, min(MAX_CONCURRENCY, WORKER_POOL_SIZE, n))
    auto_cap = min(MAX_CONCURRENCY, AUTO_POOL_CAP)
    # 每工位默认目标承担 ~3 批起（最多不超过 MAX_TURNS_PER_WORKER 轮后自动轮换）
    target = max(1, (n + 2) // 3)
    return max(1, min(auto_cap, target, n))


class _WorkerSlot:
    def __init__(self, slot_id: int) -> None:
        self.slot_id = slot_id
        self.generation = 0
        self.handle = None
        self.turns_used = 0


class ReusableWorkerPool:
    """复用型 Agent 工位池：单工位串行复用 N 轮后自动销毁换新，遇超时/报错立即作废不污染后续批次。"""

    def __init__(self, pool_size: int, worker_type: str, max_turns: int = MAX_TURNS_PER_WORKER) -> None:
        self.pool_size = max(1, int(pool_size))
        self.worker_type = worker_type
        self.max_turns = max(1, int(max_turns))
        self.standing_prompt = build_worker_standing_prompt()
        self.sessions_created = 0
        self.turns_completed = 0
        self.reusable_supported = True
        self._slots = [_WorkerSlot(i + 1) for i in range(self.pool_size)]
        self._queue: asyncio.Queue = asyncio.Queue()
        for slot in self._slots:
            self._queue.put_nowait(slot)

    def _retire_slot(self, slot: _WorkerSlot) -> None:
        if slot.handle is not None:
            handle = slot.handle
            slot.handle = None
            del handle
        slot.turns_used = 0

    async def run_turn(self, batch_id: str, segs, merged_names, *, is_patch: bool = False, role_name: str = ""):
        slot = await self._queue.get()
        try:
            if not self.reusable_supported:
                self.sessions_created += 1
                res = await _await_agent(
                    agent(
                        build_batch_prompt(batch_id, segs, merged_names, is_patch=is_patch),
                        schema=BATCH_SCHEMA,
                        role=role_name or f"精转 {{batch_id}}",
                        type_name=self.worker_type,
                        model="flash",
                    ),
                    timeout_sec=AGENT_TIMEOUT_SEC,
                )
                self.turns_completed += 1
                return res

            if slot.handle is None:
                slot.generation += 1
                slot.turns_used = 0
                slot_role = f"精转工位 W{{slot.slot_id:02d}}" + (f"-G{{slot.generation}}" if slot.generation > 1 else "")
                candidate = agent(
                    self.standing_prompt,
                    schema=BATCH_SCHEMA,
                    role=slot_role,
                    type_name=self.worker_type,
                    model="flash",
                )
                if callable(candidate):
                    slot.handle = candidate
                    self.sessions_created += 1
                else:
                    # 兼容只返回一次性 coroutine 的 mock / 旧运行时
                    self.reusable_supported = False
                    if hasattr(candidate, "close"):
                        try:
                            candidate.close()
                        except Exception:
                            pass
                    self.sessions_created += 1
                    res = await _await_agent(
                        agent(
                            build_batch_prompt(batch_id, segs, merged_names, is_patch=is_patch),
                            schema=BATCH_SCHEMA,
                            role=role_name or f"精转 {{batch_id}}",
                            type_name=self.worker_type,
                            model="flash",
                        ),
                        timeout_sec=AGENT_TIMEOUT_SEC,
                    )
                    self.turns_completed += 1
                    return res

            turn_prompt = build_turn_prompt(batch_id, segs, merged_names, is_patch=is_patch)
            try:
                res = await _await_agent(slot.handle(turn_prompt, BATCH_SCHEMA), timeout_sec=AGENT_TIMEOUT_SEC)
            except Exception:
                # 任何超时或异常立即作废该工位，严防坏状态/卡死上下文污染后续批次
                self._retire_slot(slot)
                raise

            slot.turns_used += 1
            self.turns_completed += 1
            if slot.turns_used >= self.max_turns:
                self._retire_slot(slot)
            return res
        finally:
            self._queue.put_nowait(slot)

    def close(self) -> None:
        for slot in self._slots:
            self._retire_slot(slot)


def _split_segments_balanced(segs):
    """按累计音频时长寻找最均衡的对半切点（保证左右均至少 1 段）。"""
    if len(segs) < 2:
        return segs, []
    total = sum(float(s.get("dur") or 0.0) for s in segs)
    if total <= 0:
        mid = len(segs) // 2
        return segs[:mid], segs[mid:]
    half = total / 2.0
    best_idx = len(segs) // 2
    best_diff = float("inf")
    acc = 0.0
    for i in range(1, len(segs)):
        acc += float(segs[i - 1].get("dur") or 0.0)
        diff = abs(acc - half)
        if diff < best_diff:
            best_diff = diff
            best_idx = i
    return segs[:best_idx], segs[best_idx:]


async def _invoke_with_retry(
    batch_id,
    segs,
    merged_names,
    worker_type,
    *,
    is_patch=False,
    stagger_delay=0.0,
    split_depth=0,
    pool=None,
):
    if stagger_delay > 0:
        await asyncio.sleep(stagger_delay)
    last_err = ""
    for attempt in range(1, MAX_RETRIES + 1):
        timed_out = False
        await LIMITER.acquire()
        try:
            role_prefix = "补录" if is_patch else "精转"
            role_name = f"{{role_prefix}} {{batch_id}}" + (f" (重试{{attempt - 1}})" if attempt > 1 else "")
            if pool is not None:
                res = await pool.run_turn(batch_id, segs, merged_names, is_patch=is_patch, role_name=role_name)
            else:
                res = await _await_agent(
                    agent(
                        build_batch_prompt(batch_id, segs, merged_names, is_patch=is_patch),
                        schema=BATCH_SCHEMA,
                        role=role_name,
                        type_name=worker_type,
                        model="flash",
                    ),
                    timeout_sec=AGENT_TIMEOUT_SEC,
                )
            return True, (res.get("items") or []), ""
        except Exception as exc:
            timed_out = isinstance(exc, asyncio.TimeoutError)
            last_err = f"{{type(exc).__name__}}: {{exc}}"
            if _is_rate_limit_err(last_err):
                await LIMITER.record_rate_limit()
        finally:
            await LIMITER.release()

        # 释放并发槽后再执行拆包或退避，避免递归取锁死锁与睡眠占坑
        if timed_out and len(segs) >= 2 and split_depth < MAX_SPLIT_DEPTH:
            left_segs, right_segs = _split_segments_balanced(segs)
            l_dur = round(sum(float(s.get("dur") or 0.0) for s in left_segs), 1)
            r_dur = round(sum(float(s.get("dur") or 0.0) for s in right_segs), 1)
            log(
                f"[超时拆包] {{batch_id}} 命中 {{AGENT_TIMEOUT_SEC}}s 超时，"
                f"自动按时长对半拆为 {{batch_id}}a({{len(left_segs)}}段/{{l_dur}}s) + "
                f"{{batch_id}}b({{len(right_segs)}}段/{{r_dur}}s) 并行重试"
            )
            (ok_l, l_items, l_err), (ok_r, r_items, r_err) = await asyncio.gather(
                _invoke_with_retry(
                    f"{{batch_id}}a",
                    left_segs,
                    merged_names,
                    worker_type,
                    is_patch=is_patch,
                    stagger_delay=0.0,
                    split_depth=split_depth + 1,
                    pool=pool,
                ),
                _invoke_with_retry(
                    f"{{batch_id}}b",
                    right_segs,
                    merged_names,
                    worker_type,
                    is_patch=is_patch,
                    stagger_delay=0.0,
                    split_depth=split_depth + 1,
                    pool=pool,
                ),
            )
            merged_items = list(l_items) + list(r_items)
            if ok_l and ok_r:
                return True, merged_items, ""
            comb_err = "; ".join(e for e in (l_err, r_err) if e)
            if merged_items:
                return True, merged_items, comb_err
            return False, [], comb_err

        backoff = round(random.uniform(5.0, 15.0), 1)
        log(f"[WARN] {{batch_id}} 第{{attempt}}/{{MAX_RETRIES}}次失败({{last_err[:120]}})，退避 {{backoff}}s")
        if attempt < MAX_RETRIES:
            await asyncio.sleep(backoff)
    return False, [], last_err


async def run_batch(idx, batch_id, batch, merged_names, worker_type, pool=None):
    cache_file = cache_dir / f"{{batch_id}}.json"
    if cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("ok"):
                return cached
        except Exception:
            pass

    t0 = time.monotonic()
    expected_ids = [s["seg_id"] for s in batch]
    seg_by_id = {{s["seg_id"]: s for s in batch}}

    # 仅首批冷启动错峰，后续由工位池自然排队复用
    cold_stagger = (idx * STAGGER_SEC) if (pool is None or idx < pool.pool_size) else 0.0
    ok, items, err = await _invoke_with_retry(
        batch_id, batch, merged_names, worker_type, is_patch=False, stagger_delay=cold_stagger, pool=pool
    )
    got_map = {{x["seg_id"]: x for x in items if isinstance(x, dict) and x.get("seg_id") in seg_by_id}}
    missing_ids = [sid for sid in expected_ids if sid not in got_map]

    # 缺段精准补录：仅把缺失的个别 MP3 单独补跑，绝不推倒重跑整包
    if ok and missing_ids and len(missing_ids) < len(expected_ids):
        log(f"[缺段补录] {{batch_id}} 仅补录缺失 {{len(missing_ids)}} 段: {{missing_ids}}")
        miss_segs = [seg_by_id[sid] for sid in missing_ids]
        p_ok, p_items, _ = await _invoke_with_retry(
            f"{{batch_id}}_patch", miss_segs, merged_names, worker_type, is_patch=True, pool=pool
        )
        if p_ok:
            for it in p_items:
                if isinstance(it, dict) and it.get("seg_id") in seg_by_id:
                    got_map[it["seg_id"]] = it
            missing_ids = [sid for sid in expected_ids if sid not in got_map]

    final_items = [got_map[sid] for sid in expected_ids if sid in got_map]
    elapsed = round(time.monotonic() - t0, 1)
    result_obj = {{
        "batch_id": batch_id,
        "ok": ok and len(final_items) > 0,
        "elapsed_sec": elapsed,
        "items": final_items,
        "missing": missing_ids,
        "error": "" if ok else err,
    }}
    cache_file.write_text(json.dumps(result_obj, ensure_ascii=False, indent=2), encoding="utf-8")
    chars = sum(len((x.get("text") or "").strip()) for x in final_items)
    log(f"[{{'OK' if result_obj['ok'] else 'FAIL'}}] {{batch_id}} | {{elapsed}}s | {{len(final_items)}}/{{len(expected_ids)}} 段 | {{chars}} 字 | miss={{missing_ids}}")
    return result_obj


t_total = time.monotonic()
worker_type = await select_worker_type()

merged_names = {{}}
split_checks = manifest.get("split_checks") or []
if phase0_cache_path.is_file():
    try:
        p0 = json.loads(phase0_cache_path.read_text(encoding="utf-8"))
        if p0.get("completed"):
            merged_names = dict(p0.get("merged_names") or {{}})
            log(f"[Phase 0 命中缓存] 跳过声纹归并重跑 -> {{merged_names}}")
    except Exception:
        pass
elif split_checks:
    phase("声纹裂变听音复核")
    check_results = await parallel(split_checks, lambda c: verify_split_speaker(c, worker_type))
    for sid, confirmed_name in check_results:
        if confirmed_name:
            merged_names[sid] = confirmed_name
    phase0_cache_path.write_text(
        json.dumps({{"completed": True, "merged_names": merged_names}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

batches = manifest.get("batches") or []
pool_size = compute_worker_pool_size(len(batches))
worker_pool = ReusableWorkerPool(pool_size, worker_type, max_turns=MAX_TURNS_PER_WORKER)
phase(f"V2 复用工位并发精转（共 {{len(batches)}} 包，{{pool_size}} 工位复用/每工位≤{{MAX_TURNS_PER_WORKER}}轮，worker={{worker_type}}）")
indexed = [(i, f"B{{i + 1:02d}}", b) for i, b in enumerate(batches)]
try:
    batch_results = await parallel(indexed, lambda t: run_batch(t[0], t[1], t[2], merged_names, worker_type, pool=worker_pool))
finally:
    worker_pool.close()
log(f"[工位复用统计] {{len(batches)}} 批任务仅创建 {{worker_pool.sessions_created}} 个精转会话（完成 {{worker_pool.turns_completed}} 轮，复用={{worker_pool.reusable_supported}}）")

text_map = {{}}
tone_map = {{}}
multi_map = {{}}
failed_batches = []
for br in batch_results:
    if not br.get("ok"):
        failed_batches.append(br.get("batch_id"))
    for it in br.get("items") or []:
        sid = it.get("seg_id")
        if sid:
            text_map[sid] = _sanitize_field(it.get("text"))
            tone_map[sid] = _sanitize_field(it.get("tone_note"))
            multi_map[sid] = _sanitize_field(it.get("multi_speaker_note"))

segments = manifest.get("segments") or []
speakers_lines = []
for sid, sp in sorted((manifest.get("speakers") or {{}}).items()):
    final_label = merged_names.get(sid, sp["label"])
    suffix = "（经听音核对已自动归并）" if sid in merged_names else ""
    speakers_lines.append(f"- {{sid}}：{{final_label}}{{suffix}}（约 {{sp['talk_sec'] / 60.0:.1f}} 分钟）")

md_lines = [
    f"# {{manifest['title']}}｜转录稿",
    "",
    f"- 原音频：{{manifest['source_name']}}",
    f"- 录制时间：{{manifest['recorded_at']}}",
    f"- 音频时长：{{manifest['duration_hms']}}",
    f"- 转写引擎：voice-memo-review V2（FluidAudio 物理声纹切段 + Antigravity Flash 高并发精转，0 漂移含语气标注）",
    f"- 切段统计：{{len(segments)}} 段 / {{len(batches)}} 并发包 / 并发耗时 {{round(time.monotonic() - t_total, 1)}} 秒",
    "",
    "## 说话人概况",
    *(speakers_lines or ["- 未检测到有效说话人"]),
    "",
    "---",
    "",
]

rendered_turns = 0
tone_count = 0
for s in segments:
    seg_id = s["seg_id"]
    txt = text_map.get(seg_id, "")
    if not txt:
        continue
    spk = merged_names.get(s["sid"], s["speaker_label"])
    md_lines.append(f"## {{s['hms_start']}} {{spk}}")
    md_lines.append(txt)
    tn = tone_map.get(seg_id, "")
    if tn:
        md_lines.append(f"> [语气] {{tn}}")
        tone_count += 1
    mn = multi_map.get(seg_id, "")
    if mn:
        md_lines.append(f"> [叠话] {{mn}}")
    md_lines.append("")
    rendered_turns += 1

transcript_path.parent.mkdir(parents=True, exist_ok=True)
final_text = "\\n".join(md_lines)
transcript_path.write_text(final_text, encoding="utf-8")

summary_obj = {{
    "ok": len(failed_batches) == 0 and rendered_turns > 0,
    "transcript_path": str(transcript_path),
    "chars": len(final_text),
    "turns": rendered_turns,
    "tone_notes": tone_count,
    "merged_names": merged_names,
    "failed_batches": failed_batches,
    "worker_type": worker_type,
    "worker_pool_size": pool_size,
    "sessions_created": worker_pool.sessions_created,
    "reusable_supported": worker_pool.reusable_supported,
    "wall_sec": round(time.monotonic() - t_total, 1),
}}
(job_dir / "v2_result.json").write_text(
    json.dumps(summary_obj, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

if CLEANUP_CLIPS and not failed_batches and clips_dir.is_dir():
    shutil.rmtree(clips_dir, ignore_errors=True)

print(
    f"V2_TRANSCRIPT_READY | path={{transcript_path}} | chars={{len(final_text)}} | "
    f"turns={{rendered_turns}} | tones={{tone_count}} | wall={{summary_obj['wall_sec']}}s | failed={{failed_batches}}"
)
'''


def prepare_v2_job(
    audio_path: Path,
    *,
    job_dir: Path,
    transcript_path: Path,
    diarizer: FluidAudioDiarizer,
    seg_config: SegmenterConfig | None = None,
    title: str = "",
    recorded_at: datetime | str | None = None,
    asr_context: str = "",
    num_speakers: int | None = None,
    cleanup_clips: bool = True,
    keep_wav: bool = False,
) -> V2JobResult:
    """端到端执行 V2 本地极速准备：FluidAudio 21MB 声纹分离 → 物理边界切段 → 生成高并发稳态工作流。"""
    t0 = time.monotonic()
    src = Path(audio_path).expanduser().resolve()
    job_dir = Path(job_dir).expanduser().resolve()
    transcript_path = Path(transcript_path).expanduser().resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    clips_dir = job_dir / "clips"

    cfg = seg_config or SegmenterConfig()
    diar_res, wav_path = diarizer.diarize(
        src,
        work_dir=job_dir,
        num_speakers=num_speakers,
        reuse_existing=True,
    )

    try:
        segments = clean_and_merge_segments(diar_res, config=cfg, wav_path=wav_path)
        if not segments:
            raise NoSpeechDetectedError("未检测到有效人声片段（可能为纯静音或时长过短）")

        cut_segments_to_mp3(
            wav_path,
            segments,
            clips_dir,
            ffmpeg_bin=diarizer.ffmpeg_bin,
            mp3_bitrate=cfg.mp3_bitrate,
        )
    finally:
        if not keep_wav and wav_path.is_file():
            try:
                wav_path.unlink()
            except OSError:
                pass

    batches = pack_batches(
        segments,
        max_segments=cfg.batch_max_segments,
        max_speech_sec=cfg.batch_max_speech_sec,
    )
    split_checks = detect_split_id_checks(diar_res, segments)

    max_end = max((s.true_end for s in segments), default=0.0)
    rec_str = (
        recorded_at.isoformat()
        if isinstance(recorded_at, datetime)
        else (str(recorded_at) if recorded_at else datetime.now().astimezone().replace(microsecond=0).isoformat())
    )
    manifest = {
        "title": title or src.stem,
        "source_name": src.name,
        "recorded_at": rec_str,
        "duration_sec": round(max_end, 1),
        "duration_hms": format_hms(max_end),
        "transcript_path": str(transcript_path),
        "clips_dir": str(clips_dir),
        "asr_context": asr_context.strip(),
        "speakers": {
            sid: {
                "sid": sp.sid,
                "label": sp.label,
                "confirmed": sp.confirmed,
                "top1_name": sp.top1_name,
                "top1_score": sp.top1_score,
                "talk_sec": sp.talk_sec,
            }
            for sid, sp in diar_res.speakers.items()
        },
        "split_checks": [c.to_dict() for c in split_checks],
        "segments": [s.to_dict() for s in segments],
        "batches": [[s.to_dict() for s in batch] for batch in batches],
    }

    manifest_path = job_dir / "manifest_v2.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    workflow_script_path = job_dir / "run_v2_transcribe.py"
    workflow_script_path.write_text(
        render_workflow_script(
            manifest_path,
            cleanup_clips=cleanup_clips,
            max_concurrency=cfg.max_concurrency,
            agent_timeout_sec=cfg.agent_timeout_sec,
            stagger_sec=cfg.stagger_sec,
            max_turns_per_worker=cfg.max_turns_per_worker,
            worker_pool_size=cfg.worker_pool_size,
        ),
        encoding="utf-8",
    )

    return V2JobResult(
        job_dir=job_dir,
        manifest_path=manifest_path,
        workflow_script_path=workflow_script_path,
        transcript_path=transcript_path,
        speakers_summary=diar_res.summary_text(),
        segment_count=len(segments),
        batch_count=len(batches),
        split_check_count=len(split_checks),
        prep_sec=round(time.monotonic() - t0, 2),
    )
