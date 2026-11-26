"""V2 核心物理切段引擎：声纹边界清洗、同人合并、能量谷切分超长段、250ms 前向防咬字、智能分包。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import os
from pathlib import Path
import struct
import subprocess
import wave

from voice_review.diarizer import DEFAULT_SPLIT_CANDIDATE_THRESHOLD, DiarizationResult


@dataclass(frozen=True)
class SegmenterConfig:
    min_segment_sec: float = 0.35
    gap_merge_sec: float = 2.0
    max_turn_sec: float = 45.0
    pad_before_sec: float = 0.25   # 前向借 250ms 气口防咬字（解决首字半拍被切问题）
    pad_after_sec: float = 0.15
    batch_max_segments: int = 15   # 兼顾并发速度与 Antigravity 额度消耗
    batch_max_speech_sec: float = 120.0  # 单包有效人声总时长上限（秒），保丰满上下文同时削平巨包超时
    mp3_bitrate: str = "32k"
    max_concurrency: int = 25      # 稳态并发上限（规避客户端高并发状态表竞态，遇限流自动减半降级）
    agent_timeout_sec: int = 120   # 单包硬超时熔断（秒）
    stagger_sec: float = 0.25      # 错峰启动间隔（秒）
    max_turns_per_worker: int = 6  # 单复用工位最多连续精转轮数（满轮自动换新，防上下文膨胀同时削减 80%+ 会话数）
    worker_pool_size: int = 0      # 0=自动按批次数与并发上限自适应取最优工位数（上限 12，防打爆本地 500 会话保留池）


@dataclass
class Segment:
    seg_id: str
    sid: str
    speaker_label: str
    confirmed: bool
    true_start: float
    true_end: float
    cut_start: float
    cut_end: float
    dur: float
    hms_start: str
    hms_end: str
    mp3_path: str = ""
    bytes_size: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SplitIdCheck:
    """裂变声纹核对对：当某未确认 ID 与已确认真名高度相似（如 >=0.78）时，各抽最长清晰段让模型一次性听音归并。"""
    unconfirmed_sid: str
    unconfirmed_label: str
    unconfirmed_seg_id: str
    unconfirmed_mp3: str
    unconfirmed_dur: float
    target_name: str
    target_sid: str
    target_seg_id: str
    target_mp3: str
    target_dur: float
    similarity: float

    def to_dict(self) -> dict:
        return asdict(self)


def format_hms(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _find_wav_energy_valley(wav_path: Path, start_sec: float, end_sec: float, *, window_sec: float = 0.35) -> float:
    """用纯标准库读取 WAV 中间 60% 区间，找到最低能量谷作为超长独白的切点，避免从字中间腰斩。"""
    mid = (start_sec + end_sec) / 2.0
    try:
        with wave.open(str(wav_path), "rb") as wf:
            sr = wf.getframerate()
            sampwidth = wf.getsampwidth()
            nchannels = wf.getnchannels()
            if sampwidth != 2 or sr <= 0:
                return mid
            total_frames = wf.getnframes()
            s_frame = max(0, int((start_sec + (end_sec - start_sec) * 0.2) * sr))
            e_frame = min(total_frames, int((start_sec + (end_sec - start_sec) * 0.8) * sr))
            if e_frame <= s_frame + int(window_sec * sr):
                return mid

            wf.setpos(s_frame)
            raw = wf.readframes(e_frame - s_frame)
            count = len(raw) // 2
            samples = struct.unpack(f"<{count}h", raw)
            if nchannels > 1:
                samples = samples[::nchannels]

            win = max(1, int(window_sec * sr))
            step = max(1, win // 3)
            best_energy = float("inf")
            best_offset = len(samples) // 2
            for pos in range(0, len(samples) - win, step):
                chunk = samples[pos : pos + win]
                energy = math.fsum(x * x for x in chunk) / win
                if energy < best_energy:
                    best_energy = energy
                    best_offset = pos + win // 2
            return (s_frame + best_offset) / sr
    except (OSError, wave.Error, struct.error):
        return mid


def _split_long_turn(
    start: float,
    end: float,
    sid: str,
    max_turn_sec: float,
    wav_path: Path | None,
) -> list[tuple[float, float, str]]:
    if end - start <= max_turn_sec:
        return [(start, end, sid)]
    cut = _find_wav_energy_valley(wav_path, start, end) if (wav_path and wav_path.is_file()) else (start + end) / 2.0
    if cut <= start + 2.0 or cut >= end - 2.0:
        cut = (start + end) / 2.0
    return _split_long_turn(start, cut, sid, max_turn_sec, wav_path) + _split_long_turn(
        cut, end, sid, max_turn_sec, wav_path
    )


def clean_and_merge_segments(
    diar: DiarizationResult,
    *,
    config: SegmenterConfig | None = None,
    wav_path: Path | None = None,
    window_start: float | None = None,
    window_end: float | None = None,
) -> list[Segment]:
    """将原始声纹片段清洗为干净的发言轮次，带前向 250ms 防咬字保护。"""
    cfg = config or SegmenterConfig()
    lo = 0.0 if window_start is None else max(0.0, float(window_start))
    hi = float("inf") if window_end is None else float(window_end)

    filtered: list[list] = []
    for s, e, sid in diar.raw_segments:
        if e <= lo or s >= hi:
            continue
        cs = max(s, lo)
        ce = min(e, hi)
        if ce - cs < cfg.min_segment_sec:
            continue
        filtered.append([cs, ce, sid])

    merged: list[list] = []
    for s, e, sid in filtered:
        if (
            merged
            and merged[-1][2] == sid
            and (s - merged[-1][1]) <= cfg.gap_merge_sec
            and (e - merged[-1][0]) <= cfg.max_turn_sec
        ):
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e, sid])

    split_turns: list[tuple[float, float, str]] = []
    for s, e, sid in merged:
        split_turns.extend(_split_long_turn(s, e, sid, cfg.max_turn_sec, wav_path))

    max_audio_end = max((e for _, e, _ in split_turns), default=0.0)
    upper_bound = hi if math.isfinite(hi) else max_audio_end + cfg.pad_after_sec

    segments: list[Segment] = []
    for idx, (s, e, sid) in enumerate(split_turns, start=1):
        # 刻意允许适度前向借 250ms 气口（不受上一段紧贴阻挡），避免快切时咬掉首字
        cut_s = max(lo, s - cfg.pad_before_sec)
        next_s = split_turns[idx][0] if idx < len(split_turns) else upper_bound
        # 向后最多扩到下段起点 + 0.05s，不越界太多
        cut_e = min(upper_bound, max(e, min(e + cfg.pad_after_sec, next_s + 0.05)))
        if cut_e <= cut_s:
            cut_e = e

        sp = diar.speakers.get(sid)
        label = sp.label if sp else sid
        confirmed = sp.confirmed if sp else False

        segments.append(
            Segment(
                seg_id=f"S{idx:03d}",
                sid=sid,
                speaker_label=label,
                confirmed=confirmed,
                true_start=round(s, 2),
                true_end=round(e, 2),
                cut_start=round(cut_s, 2),
                cut_end=round(cut_e, 2),
                dur=round(cut_e - cut_s, 2),
                hms_start=format_hms(s),
                hms_end=format_hms(e),
            )
        )
    return segments


def _build_env() -> dict[str, str]:
    env = dict(os.environ)
    path_parts = env.get("PATH", "").split(":") if env.get("PATH") else []
    for extra in ("/opt/homebrew/bin", "/usr/local/bin"):
        if extra not in path_parts:
            path_parts.insert(0, extra)
    env["PATH"] = ":".join(path_parts)
    return env


def cut_segments_to_mp3(
    wav_path: Path,
    segments: list[Segment],
    clips_dir: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
    mp3_bitrate: str = "32k",
    wav_offset_sec: float = 0.0,
) -> list[Segment]:
    """按物理发言段从本地 WAV 快速切出单声道轻量 MP3。"""
    clips_dir = Path(clips_dir).expanduser().resolve()
    clips_dir.mkdir(parents=True, exist_ok=True)
    env = _build_env()

    for seg in segments:
        out_mp3 = clips_dir / f"{seg.seg_id}.mp3"
        rel_start = max(0.0, seg.cut_start - wav_offset_sec)
        dur = max(0.25, seg.cut_end - seg.cut_start)
        res = subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-loglevel",
                "error",
                "-ss",
                f"{rel_start:.3f}",
                "-t",
                f"{dur:.3f}",
                "-i",
                str(wav_path),
                "-codec:a",
                "libmp3lame",
                "-b:a",
                mp3_bitrate,
                str(out_mp3),
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        if res.returncode != 0 or not out_mp3.is_file():
            detail = (res.stderr or res.stdout or "").strip()[-300:]
            raise RuntimeError(f"切段 {seg.seg_id} 失败：{detail}")
        seg.mp3_path = str(out_mp3)
        seg.bytes_size = out_mp3.stat().st_size
    return segments


def pack_batches(
    segments: list[Segment],
    *,
    max_segments: int = 15,
    max_speech_sec: float = 120.0,
) -> list[list[Segment]]:
    """把连续发言段按数量与总人声时长打成并发小包。"""
    batches: list[list[Segment]] = []
    cur: list[Segment] = []
    cur_dur = 0.0
    for seg in segments:
        if cur and (len(cur) >= max_segments or (cur_dur + seg.dur) > max_speech_sec):
            batches.append(cur)
            cur = []
            cur_dur = 0.0
        cur.append(seg)
        cur_dur += seg.dur
    if cur:
        batches.append(cur)
    return batches


def detect_split_id_checks(
    diar: DiarizationResult,
    segments: list[Segment],
    *,
    min_similarity: float = DEFAULT_SPLIT_CANDIDATE_THRESHOLD,
    min_sample_dur: float = 2.0,
) -> list[SplitIdCheck]:
    """找出可能属于已确认说话人的裂变声纹 ID，各挑一段清晰样本供工作流做一次性听音归并。"""
    confirmed_by_name = {sp.label: sp.sid for sp in diar.speakers.values() if sp.confirmed}
    checks: list[SplitIdCheck] = []
    for sp in sorted(diar.speakers.values(), key=lambda x: x.sid):
        if sp.confirmed or sp.top1_score < min_similarity:
            continue
        target_sid = confirmed_by_name.get(sp.top1_name)
        if not target_sid:
            continue
        cand_a = sorted(
            (s for s in segments if s.sid == sp.sid and s.dur >= min_sample_dur and s.mp3_path),
            key=lambda s: -min(s.dur, 20.0),
        )
        cand_b = sorted(
            (s for s in segments if s.sid == target_sid and s.dur >= min_sample_dur and s.mp3_path),
            key=lambda s: -min(s.dur, 20.0),
        )
        if cand_a and cand_b:
            checks.append(
                SplitIdCheck(
                    unconfirmed_sid=sp.sid,
                    unconfirmed_label=sp.label,
                    unconfirmed_seg_id=cand_a[0].seg_id,
                    unconfirmed_mp3=cand_a[0].mp3_path,
                    unconfirmed_dur=cand_a[0].dur,
                    target_name=sp.top1_name,
                    target_sid=target_sid,
                    target_seg_id=cand_b[0].seg_id,
                    target_mp3=cand_b[0].mp3_path,
                    target_dur=cand_b[0].dur,
                    similarity=sp.top1_score,
                )
            )
    return checks


def _probe_duration(audio_path: Path, ffprobe_bin: str = "ffprobe") -> float:
    res = subprocess.run(
        [
            str(ffprobe_bin),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        env=_build_env(),
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe 读取音频时长失败：{(res.stderr or res.stdout or '').strip()[-200:]}")
    val = float((res.stdout or "").strip())
    if val <= 0:
        raise RuntimeError("音频时长为 0，无法切块")
    return val


def build_uniform_chunks(
    audio_path: Path,
    clips_dir: Path,
    *,
    chunk_sec: float = 600.0,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    mp3_bitrate: str = "32k",
    speaker_label: str = "说话人",
) -> tuple[list[Segment], float]:
    """关闭声纹预处理（mode='none'）时的均匀安全切块器，产出与声纹模式一致的 Segment 列表。"""
    src = Path(audio_path).expanduser().resolve()
    clips_dir = Path(clips_dir).expanduser().resolve()
    clips_dir.mkdir(parents=True, exist_ok=True)
    total_dur = _probe_duration(src, ffprobe_bin=ffprobe_bin)
    step = max(30.0, float(chunk_sec))
    env = _build_env()

    segments: list[Segment] = []
    idx = 1
    pos = 0.0
    while pos < total_dur:
        end = min(total_dur, pos + step)
        dur = max(0.5, end - pos)
        seg_id = f"S{idx:03d}"
        out_mp3 = clips_dir / f"{seg_id}.mp3"
        res = subprocess.run(
            [
                str(ffmpeg_bin),
                "-y",
                "-loglevel",
                "error",
                "-ss",
                f"{pos:.3f}",
                "-t",
                f"{dur:.3f}",
                "-i",
                str(src),
                "-ac",
                "1",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                mp3_bitrate,
                str(out_mp3),
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        if res.returncode != 0 or not out_mp3.is_file():
            detail = (res.stderr or res.stdout or "").strip()[-300:]
            raise RuntimeError(f"均匀切块 {seg_id} 失败：{detail}")
        segments.append(
            Segment(
                seg_id=seg_id,
                sid="SPEAKER_00",
                speaker_label=speaker_label,
                confirmed=False,
                true_start=round(pos, 2),
                true_end=round(end, 2),
                cut_start=round(pos, 2),
                cut_end=round(end, 2),
                dur=round(end - pos, 2),
                hms_start=format_hms(pos),
                hms_end=format_hms(end),
                mp3_path=str(out_mp3),
                bytes_size=out_mp3.stat().st_size,
            )
        )
        idx += 1
        pos = end
    return segments, total_dur
