"""声纹分离与本地声纹库管理（FluidAudio Core ML + 纯标准库余弦相似度）。"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any


DEFAULT_MATCH_THRESHOLD = 0.80
DEFAULT_MATCH_MARGIN = 0.15
DEFAULT_SPLIT_CANDIDATE_THRESHOLD = 0.78
FLUIDAUDIO_MODELS_DIR = Path("~/Library/Application Support/FluidAudio/Models/speaker-diarization")


class NoSpeechDetectedError(RuntimeError):
    """录音中未检测到有效人声（纯静音/环境底噪），供状态机优雅跳过而不按异常重试。"""


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """纯 Python 标准库计算两个向量的余弦相似度，无需安装 numpy。"""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = math.fsum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(math.fsum(a * a for a in vec_a))
    norm_b = math.sqrt(math.fsum(b * b for b in vec_b))
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def find_fluidaudio_bin(configured: Path | str | None = None) -> Path | None:
    """按优先级探测本机 fluidaudiocli 可执行文件路径。"""
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(str(configured)).expanduser())
    env_val = os.environ.get("FLUIDAUDIO_BIN", "").strip()
    if env_val:
        candidates.append(Path(env_val).expanduser())
    which_val = shutil.which("fluidaudiocli")
    if which_val:
        candidates.append(Path(which_val))
    for extra in (
        Path("/opt/homebrew/bin/fluidaudiocli"),
        Path("/usr/local/bin/fluidaudiocli"),
        Path.home() / ".local/bin/fluidaudiocli",
    ):
        candidates.append(extra)

    for path in candidates:
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return path.resolve()
        except OSError:
            continue
    return None


@dataclass(frozen=True)
class SpeakerProfile:
    sid: str
    raw_id: str
    label: str
    confirmed: bool
    top1_name: str
    top1_score: float
    top2_name: str
    top2_score: float
    talk_sec: float


@dataclass(frozen=True)
class DiarizationResult:
    raw_segments: list[tuple[float, float, str]]  # (start_sec, end_sec, SPEAKER_XX)
    speakers: dict[str, SpeakerProfile]
    raw_embeddings: dict[str, list[float]]  # sid -> embedding vector
    diarization_json: Path | None = None

    def summary_text(self) -> str:
        lines: list[str] = []
        for sid in sorted(self.speakers):
            sp = self.speakers[sid]
            mins = sp.talk_sec / 60.0
            if sp.confirmed:
                lines.append(f"- {sid} -> 已确认真名：{sp.label}（相似度 {sp.top1_score:.2f}，约 {mins:.1f} 分钟）")
            elif sp.top1_score >= DEFAULT_SPLIT_CANDIDATE_THRESHOLD and sp.top1_name:
                lines.append(
                    f"- {sid} -> 未达自动阈值（最像 {sp.top1_name} {sp.top1_score:.2f}，次像 {sp.top2_name} {sp.top2_score:.2f}，约 {mins:.1f} 分钟）"
                )
            else:
                lines.append(f"- {sid} -> 未知/外部说话人（约 {mins:.1f} 分钟）")
        return "\n".join(lines) if lines else "未识别到有效说话人"


class VoiceprintStore:
    """本机声纹库（JSON 格式，兼容多种常见 embedding 列表结构）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()

    def load(self) -> dict[str, list[list[float]]]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        normalized: dict[str, list[list[float]]] = {}
        for name, val in raw.items():
            vecs = self._extract_vectors(val)
            if vecs:
                normalized[str(name)] = vecs
        return normalized

    @staticmethod
    def _extract_vectors(val: Any) -> list[list[float]]:
        if isinstance(val, dict):
            for key in ("embeddings", "vectors", "samples"):
                if isinstance(val.get(key), list):
                    return VoiceprintStore._extract_vectors(val[key])
            if isinstance(val.get("embedding"), list):
                return VoiceprintStore._extract_vectors(val["embedding"])
            # 兼容 {录音标签: [256维向量], ...} 格式（与 32 本地转录 voiceprints.json 100% 互通）
            out: list[list[float]] = []
            for sub in val.values():
                if isinstance(sub, list) and sub and isinstance(sub[0], (int, float)):
                    out.append([float(x) for x in sub])
            return out
        if isinstance(val, list) and val:
            if isinstance(val[0], (int, float)):
                return [[float(x) for x in val]]
            if isinstance(val[0], list):
                out = []
                for item in val:
                    if isinstance(item, list) and item and isinstance(item[0], (int, float)):
                        out.append([float(x) for x in item])
                return out
        return []

    def add_embedding(self, name: str, vector: list[float], *, max_samples: int = 12) -> int:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("说话人名称不能为空")
        if not vector:
            raise ValueError("声纹向量为空")

        existing_raw: dict[str, Any] = {}
        if self.path.is_file():
            try:
                parsed = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    existing_raw = parsed
            except (OSError, json.JSONDecodeError):
                existing_raw = {}

        vecs = self._extract_vectors(existing_raw.get(clean_name))
        vecs.append([round(float(x), 6) for x in vector])
        if len(vecs) > max_samples:
            vecs = vecs[-max_samples:]
        existing_raw[clean_name] = vecs

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(existing_raw, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(vecs)

    def delete(self, name: str) -> bool:
        if not self.path.is_file():
            return False
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or name not in raw:
            return False
        del raw[name]
        self.path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        return True

    def list_summary(self) -> list[tuple[str, int]]:
        data = self.load()
        return sorted(((k, len(v)) for k, v in data.items()), key=lambda x: x[0])


def extract_unmatched_speakers(
    diar_res: DiarizationResult,
    *,
    min_talk_sec: float = 8.0,
    max_count: int = 8,
) -> list[dict[str, Any]]:
    """提取尚未匹配到本机声纹库、且有效发言达到阈值的主要说话人（含 256 维向量供后续零样音建档）。"""
    items: list[dict[str, Any]] = []
    for sid, sp in diar_res.speakers.items():
        if sp.confirmed:
            continue
        if sp.talk_sec < min_talk_sec:
            continue
        vec = diar_res.raw_embeddings.get(sid)
        if not vec:
            continue
        items.append(
            {
                "sid": sid,
                "talk_sec": round(float(sp.talk_sec), 1),
                "top1_name": sp.top1_name if sp.top1_score >= DEFAULT_SPLIT_CANDIDATE_THRESHOLD else "",
                "top1_score": round(float(sp.top1_score), 2) if sp.top1_score >= DEFAULT_SPLIT_CANDIDATE_THRESHOLD else 0.0,
                "sample_quote": "",
                "embedding": [round(float(x), 6) for x in vec],
            }
        )
    items.sort(key=lambda x: float(x["talk_sec"]), reverse=True)
    return items[:max_count]


def enrich_unmatched_with_quotes(
    candidates: list[dict[str, Any]],
    transcript_text: str,
) -> list[dict[str, Any]]:
    """从落盘转录稿中为每位未登记说话人摘取 1 句代表性原话，方便用户一眼辨认是谁。"""
    if not candidates or not transcript_text:
        return candidates
    want = {str(c.get("sid") or "") for c in candidates if c.get("sid")}
    quotes: dict[str, str] = {}
    blocks = re.split(r"(?m)^##\s+", transcript_text)
    for blk in blocks[1:]:
        lines = [ln.strip() for ln in blk.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        head = lines[0]
        matched_sid = ""
        for sid in want:
            if f"**{sid}**" in head or re.search(rf"(?<![A-Za-z0-9_]){re.escape(sid)}(?![A-Za-z0-9_])", head):
                matched_sid = sid
                break
        if not matched_sid:
            continue
        body_lines = [ln for ln in lines[1:] if not ln.startswith(">")]
        if not body_lines:
            continue
        sent = re.sub(r"\s+", " ", " ".join(body_lines)).strip()
        if not sent:
            continue
        prev = quotes.get(matched_sid, "")
        # 优先挑长度在 12~48 字之间的清晰完整原话，避免单字“嗯/对”
        if not prev or (len(prev) < 12 and len(sent) > len(prev)):
            quotes[matched_sid] = sent[:46] + ("…" if len(sent) > 46 else "")
    enriched: list[dict[str, Any]] = []
    for c in candidates:
        item = dict(c)
        sid = str(item.get("sid") or "")
        if sid in quotes:
            item["sample_quote"] = quotes[sid]
        enriched.append(item)
    return enriched


def build_voiceprint_enroll_prompt(
    candidates: list[dict[str, Any]],
    *,
    recording_id: str = "",
) -> str:
    """拼装文末声纹建档轻提问指引：让 AI 在分析末尾顺嘴询问用户是否记录未登记说话人备注。"""
    if not candidates:
        return ""
    rows: list[str] = []
    for c in candidates:
        sid = str(c.get("sid") or "")
        sec = float(c.get("talk_sec") or 0.0)
        dur = f"{sec / 60.0:.1f} 分钟" if sec >= 60 else f"{int(round(sec))} 秒"
        quote = str(c.get("sample_quote") or "").strip()
        hint = f"（近似已有声纹：{c['top1_name']} {float(c['top1_score']):.2f}）" if c.get("top1_name") else ""
        quote_txt = f"，原话示例：「{quote}」" if quote else ""
        rows.append(f"- `{sid}`（发言约 {dur}{hint}{quote_txt}）")
    rec_note = f"（当前录音 ID：`{recording_id}`）" if recording_id else ""
    return (
        "## 文末附带：说话人声纹建档提问（请务必保留在分析正文最末尾）\n"
        f"本次录音检测到 **{len(candidates)} 位**尚未登记到本机声纹库的主要说话人{rec_note}：\n"
        + "\n".join(rows)
        + "\n\n"
        "请在完成上面的正式分析后，在回复最末尾用简短友好的中文附一段 **「🎙️ 是否记录说话人声纹？」** 提问：\n"
        f"1. 告诉用户本次识别到 **{len(candidates)} 位主要说话人** 尚未备注，列出上面每位代号、大致发言时长与原话示例；\n"
        "2. 明确说明：**记录备注后，声音特征会直接保存在本机声纹库，下一次再录到他们时系统就会自动认出并标出姓名**（不需要记录直接忽略即可）；\n"
        "3. 告诉用户只需直接回复备注即可（例如：「`SPEAKER_00` 是我，`SPEAKER_01` 是王总」），你收到后会立刻通过 `vmr_manage_voiceprints(action=\"confirm_from_recording\", ...)` 保存声纹并顺手把当前稿件里的代号替换为真名。\n"
    )


def replace_speaker_labels_in_markdown(path: Path, mappings: dict[str, str]) -> bool:
    """将落盘转录稿/分析稿中的 SPEAKER_XX 代号安全替换为确认后的备注真名。"""
    if not path or not path.is_file() or not mappings:
        return False
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return False
    updated = raw
    for sid, name in mappings.items():
        clean_sid = str(sid or "").strip()
        clean_name = str(name or "").strip()
        if not clean_sid or not clean_name:
            continue
        updated = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(clean_sid)}(?![A-Za-z0-9_])",
            clean_name,
            updated,
        )
    if updated == raw:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def _mean_normalize(vectors: list[list[float]]) -> list[float]:
    """对同一个人多条样本向量求均值并 L2 归一化（与 32 本地转录声纹比对口径一致）。"""
    if not vectors:
        return []
    dim = len(vectors[0])
    valid = [v for v in vectors if len(v) == dim]
    if not valid:
        return []
    acc = [math.fsum(v[i] for v in valid) / len(valid) for i in range(dim)]
    norm = math.sqrt(math.fsum(x * x for x in acc))
    return [x / norm for x in acc] if norm > 0.0 else acc


def _compute_segment_centroids(
    segs: list[dict[str, Any]],
    order: dict[str, str],
) -> dict[str, list[float]]:
    """从 FluidAudio 每段自带的 embedding 按时长加权平均并 L2 归一化，求每个 SPEAKER_XX 的质心向量。"""
    sums: dict[str, list[float]] = {}
    for item in segs:
        emb = item.get("embedding")
        if not (isinstance(emb, list) and emb and isinstance(emb[0], (int, float))):
            continue
        raw_id = str(item.get("speakerId", "0"))
        sid = order.get(raw_id)
        if not sid:
            continue
        start = float(item.get("startTimeSeconds", 0.0))
        end = float(item.get("endTimeSeconds", start))
        weight = max(0.01, end - start)
        if sid not in sums:
            sums[sid] = [float(x) * weight for x in emb]
        elif len(sums[sid]) == len(emb):
            acc = sums[sid]
            for idx, x in enumerate(emb):
                acc[idx] += float(x) * weight

    centroids: dict[str, list[float]] = {}
    for sid, vec in sums.items():
        norm = math.sqrt(math.fsum(x * x for x in vec))
        centroids[sid] = [x / norm for x in vec] if norm > 0.0 else vec
    return centroids


def match_speakers(
    raw_segments: list[tuple[float, float, str]],
    sid_to_raw: dict[str, str],
    raw_embeddings_map: dict[str, list[float]],
    voiceprints: dict[str, list[list[float]]],
    *,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    margin: float = DEFAULT_MATCH_MARGIN,
    split_threshold: float = DEFAULT_SPLIT_CANDIDATE_THRESHOLD,
) -> tuple[dict[str, SpeakerProfile], dict[str, list[float]]]:
    """将 FluidAudio 聚类结果与本地声纹库比对，输出标准化 SpeakerProfile。"""
    talk_time: dict[str, float] = {}
    for start, end, sid in raw_segments:
        talk_time[sid] = talk_time.get(sid, 0.0) + max(0.0, end - start)

    vp_centroids = {
        vp_name: _mean_normalize(vp_vecs)
        for vp_name, vp_vecs in voiceprints.items()
        if vp_vecs
    }

    profiles: dict[str, SpeakerProfile] = {}
    sid_embs: dict[str, list[float]] = {}

    for sid, raw_id in sorted(sid_to_raw.items()):
        vec = raw_embeddings_map.get(sid) or raw_embeddings_map.get(raw_id)
        if isinstance(vec, list) and vec and isinstance(vec[0], (int, float)):
            vec_f = [float(x) for x in vec]
            sid_embs[sid] = vec_f
        else:
            vec_f = None

        top1_name, top1_score = "", 0.0
        top2_name, top2_score = "", 0.0
        confirmed = False
        label = sid

        if vec_f is not None and vp_centroids:
            scored: list[tuple[float, str]] = []
            for vp_name, vp_vec in vp_centroids.items():
                if vp_vec:
                    scored.append((cosine_similarity(vec_f, vp_vec), vp_name))
            scored.sort(reverse=True)
            if scored:
                top1_score, top1_name = scored[0]
            if len(scored) >= 2:
                top2_score, top2_name = scored[1]
            if top1_score >= threshold and (top1_score - top2_score) >= margin:
                confirmed = True
                label = top1_name
            elif top1_score >= split_threshold and top1_name:
                label = f"{sid}(疑:{top1_name})"

        profiles[sid] = SpeakerProfile(
            sid=sid,
            raw_id=raw_id,
            label=label,
            confirmed=confirmed,
            top1_name=top1_name,
            top1_score=round(top1_score, 4),
            top2_name=top2_name,
            top2_score=round(top2_score, 4),
            talk_sec=round(talk_time.get(sid, 0.0), 2),
        )

    return profiles, sid_embs


def parse_fluidaudio_json(
    diar_data: dict[str, Any],
    voiceprints: dict[str, list[list[float]]],
    *,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    margin: float = DEFAULT_MATCH_MARGIN,
    diarization_json: Path | None = None,
) -> DiarizationResult:
    """解析 FluidAudio 输出的 diarization.json（支持逐段 embedding 加权质心与顶层 speakerEmbeddings）并执行声纹库匹配。"""
    order: dict[str, str] = {}
    raw_segments: list[tuple[float, float, str]] = []
    segs = diar_data.get("segments") or []
    sorted_segs = sorted(segs, key=lambda s: float(s.get("startTimeSeconds", 0.0)))
    for item in sorted_segs:
        raw_id = str(item.get("speakerId", "0"))
        sid = order.setdefault(raw_id, f"SPEAKER_{len(order):02d}")
        start = float(item.get("startTimeSeconds", 0.0))
        end = float(item.get("endTimeSeconds", start))
        if end > start:
            raw_segments.append((start, end, sid))

    sid_to_raw = {sid: raw_id for raw_id, sid in order.items()}
    seg_centroids = _compute_segment_centroids(sorted_segs, order)
    raw_embs = dict(diar_data.get("speakerEmbeddings") or {})
    raw_embs.update(seg_centroids)
    profiles, sid_embs = match_speakers(
        raw_segments,
        sid_to_raw,
        raw_embs,
        voiceprints,
        threshold=threshold,
        margin=margin,
    )
    return DiarizationResult(
        raw_segments=raw_segments,
        speakers=profiles,
        raw_embeddings=sid_embs,
        diarization_json=diarization_json,
    )


class FluidAudioDiarizer:
    """调用 macOS Core ML 引擎 fluidaudiocli 完成极速本地声纹分离与声纹注册。"""

    def __init__(
        self,
        *,
        fluidaudio_bin: Path | str | None = None,
        ffmpeg_bin: Path | str = "ffmpeg",
        voiceprints_path: Path | str | None = None,
        match_threshold: float = DEFAULT_MATCH_THRESHOLD,
        match_margin: float = DEFAULT_MATCH_MARGIN,
        offline_mode: bool = True,
        auto_download_if_missing: bool = True,
    ) -> None:
        self.fluidaudio_bin = find_fluidaudio_bin(fluidaudio_bin)
        self.ffmpeg_bin = str(ffmpeg_bin)
        self.voiceprint_store = VoiceprintStore(Path(voiceprints_path)) if voiceprints_path else None
        self.match_threshold = match_threshold
        self.match_margin = match_margin
        self.offline_mode = offline_mode
        self.auto_download_if_missing = auto_download_if_missing

    @staticmethod
    def _models_present() -> bool:
        model_dir = FLUIDAUDIO_MODELS_DIR.expanduser()
        if not model_dir.is_dir():
            return False
        try:
            return any(model_dir.iterdir())
        except OSError:
            return False

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        path_parts = env.get("PATH", "").split(":") if env.get("PATH") else []
        for extra in ("/opt/homebrew/bin", "/usr/local/bin"):
            if extra not in path_parts:
                path_parts.insert(0, extra)
        env["PATH"] = ":".join(path_parts)
        enforce_offline = self.offline_mode and (not self.auto_download_if_missing or self._models_present())
        if enforce_offline:
            env.pop("http_proxy", None)
            env.pop("https_proxy", None)
            env.pop("HTTP_PROXY", None)
            env.pop("HTTPS_PROXY", None)
            env["REGISTRY_URL"] = "http://127.0.0.1:9"
        return env

    def _convert_to_wav(self, audio_path: Path, wav_out: Path) -> None:
        res = subprocess.run(
            [
                self.ffmpeg_bin,
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(audio_path),
                "-ac",
                "1",
                "-ar",
                "16000",
                str(wav_out),
            ],
            capture_output=True,
            text=True,
            env=self._build_env(),
            check=False,
        )
        if res.returncode != 0 or not wav_out.is_file():
            detail = (res.stderr or res.stdout or "").strip()[-400:]
            raise RuntimeError(f"ffmpeg 转 16k WAV 失败：{detail}")

    def diarize(
        self,
        audio_path: Path,
        *,
        work_dir: Path,
        num_speakers: int | None = None,
        min_speakers: int = 1,
        max_speakers: int = 8,
        reuse_existing: bool = True,
    ) -> tuple[DiarizationResult, Path]:
        """对音频运行 FluidAudio 声纹分离，返回 (DiarizationResult, wav_path)。"""
        if self.fluidaudio_bin is None:
            raise FileNotFoundError(
                "未找到 fluidaudiocli 可执行文件；请安装 FluidAudio 或在 config.toml 中配置 fluidaudio_bin。"
            )
        src = Path(audio_path).expanduser().resolve()
        if not src.is_file():
            raise FileNotFoundError(f"音频文件不存在：{src}")

        work_dir = Path(work_dir).expanduser().resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        wav_path = work_dir / "audio_16k.wav"
        diar_json = work_dir / "diarization.json"
        diar_log = work_dir / "diarization.log"

        if not wav_path.is_file():
            self._convert_to_wav(src, wav_path)

        if not (reuse_existing and diar_json.is_file() and diar_json.stat().st_size > 10):
            cmd = [
                str(self.fluidaudio_bin),
                "process",
                str(wav_path),
                "--mode",
                "offline",
                "--output",
                str(diar_json),
            ]
            if num_speakers is not None and num_speakers > 0:
                cmd += ["--num-speakers", str(num_speakers)]
            else:
                cmd += ["--min-speakers", str(min_speakers), "--max-speakers", str(max_speakers)]

            with open(diar_log, "w", encoding="utf-8") as lf:
                res = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, env=self._build_env(), check=False)
            if res.returncode != 0 or not diar_json.is_file():
                tail = diar_log.read_text(encoding="utf-8", errors="ignore")[-600:] if diar_log.is_file() else ""
                if "nospeechdetected" in tail.lower() or "no speech detected" in tail.lower():
                    raise NoSpeechDetectedError("录音中未检测到有效人声（纯静音或环境底噪）")
                raise RuntimeError(f"FluidAudio 声纹分离失败（exit={res.returncode}）：{tail}")

        diar_data = json.loads(diar_json.read_text(encoding="utf-8"))
        voiceprints = self.voiceprint_store.load() if self.voiceprint_store else {}
        result = parse_fluidaudio_json(
            diar_data,
            voiceprints,
            threshold=self.match_threshold,
            margin=self.match_margin,
            diarization_json=diar_json,
        )
        if not result.raw_segments:
            raise NoSpeechDetectedError("声纹分离结果为空（未检测到有效人声段）")
        return result, wav_path

    def enroll_from_audio(self, name: str, audio_path: Path) -> tuple[int, float]:
        """从一段清晰单人样音（建议 10~30 秒）中提取主声纹并写入本地声纹库。"""
        if self.voiceprint_store is None:
            raise ValueError("未配置 voiceprints_path，无法注册声纹")
        with tempfile.TemporaryDirectory(prefix="vmr-enroll-") as tmp:
            result, _ = self.diarize(Path(audio_path), work_dir=Path(tmp), num_speakers=1, reuse_existing=False)
            if not result.speakers:
                raise RuntimeError("样音中未检测到清晰人声，请确保音频时长在 10~30 秒且环境安静。")
            # 取发言时长最长的主说话人向量
            main_sp = max(result.speakers.values(), key=lambda sp: sp.talk_sec)
            vec = result.raw_embeddings.get(main_sp.sid)
            if not vec:
                raise RuntimeError("FluidAudio 未输出声纹向量（speakerEmbeddings 为空）。")
            total_samples = self.voiceprint_store.add_embedding(name, vec)
            return total_samples, main_sp.talk_sec
