"""voice-memo-review 核心单元测试（纯标准库 unittest，零外部依赖）。"""
from __future__ import annotations

import json
import math
from pathlib import Path
import struct
import tempfile
import unittest
import wave

from voice_review.config import load_config
from voice_review.diarizer import (
    VoiceprintStore,
    cosine_similarity,
    parse_fluidaudio_json,
)
from voice_review.segmenter import (
    SegmenterConfig,
    clean_and_merge_segments,
    detect_split_id_checks,
    pack_batches,
)
from voice_review.voice_memos import parse_filename_metadata, sanitize_filename_stem
from voice_review.workflow_builder import render_workflow_script, sanitize_transcript_field


def _make_wav_with_valley(path: Path, dur_sec: float = 60.0, sr: int = 16000) -> None:
    """生成一个 60 秒测试 WAV，在 32 秒处制造明显静音谷，验证能量谷切分精准落点。"""
    n = int(dur_sec * sr)
    frames = bytearray()
    for i in range(n):
        t = i / sr
        amp = 20 if 31.5 <= t <= 32.5 else 4000
        val = int(amp * math.sin(2 * math.pi * 440 * t))
        frames.extend(struct.pack("<h", val))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(bytes(frames))


class TestVoiceReviewCore(unittest.TestCase):
    def test_cosine_and_voiceprint_store(self) -> None:
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0, places=5)
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0, places=5)
        with tempfile.TemporaryDirectory() as tmp:
            vp_path = Path(tmp) / "voiceprints.json"
            store = VoiceprintStore(vp_path)
            self.assertEqual(store.add_embedding("Alice", [1.0, 0.0, 0.0]), 1)
            self.assertEqual(store.add_embedding("Bob", [0.0, 1.0, 0.0]), 1)
            loaded = store.load()
            self.assertEqual(set(loaded.keys()), {"Alice", "Bob"})
            self.assertTrue(store.delete("Bob"))
            self.assertEqual(store.list_summary(), [("Alice", 1)])

    def test_diarization_matching_and_split_id_detection(self) -> None:
        diar_json = {
            "segments": [
                {"speakerId": "rawA", "startTimeSeconds": 1.0, "endTimeSeconds": 8.0},
                {"speakerId": "rawA", "startTimeSeconds": 9.2, "endTimeSeconds": 14.0},
                {"speakerId": "rawB", "startTimeSeconds": 14.5, "endTimeSeconds": 22.0},
                {"speakerId": "rawC", "startTimeSeconds": 22.5, "endTimeSeconds": 26.0},
            ],
            "speakerEmbeddings": {
                "rawA": [0.99, 0.14, 0.0],   # 高度匹配 Alice (>0.95)
                "rawB": [0.82, 0.57, 0.0],   # 接近 Alice(0.82) 与 Bob(0.57)，但假设次像差不够导致未自动锁 -> 裂变候选
                "rawC": [0.0, 0.0, 1.0],     # 陌生人
            },
        }
        vps = {
            "Alice": [[1.0, 0.0, 0.0]],
            "Bob": [[0.25, 0.968, 0.0]],
        }
        res = parse_fluidaudio_json(diar_json, vps, threshold=0.80, margin=0.15)
        self.assertTrue(res.speakers["SPEAKER_00"].confirmed)
        self.assertEqual(res.speakers["SPEAKER_00"].label, "Alice")
        # SPEAKER_01 与 Alice=0.82，与 Bob≈0.757，margin<0.15 未自动锁，但 >=0.78 应标疑并触发裂变核对
        self.assertFalse(res.speakers["SPEAKER_01"].confirmed)
        self.assertIn("Alice", res.speakers["SPEAKER_01"].label)
        self.assertEqual(res.speakers["SPEAKER_02"].label, "SPEAKER_02")

        segs = clean_and_merge_segments(res, config=SegmenterConfig(gap_merge_sec=2.0, pad_before_sec=0.25))
        # rawA 两段间隔 1.2s <= 2.0s 应合并成 1 段
        self.assertEqual(len(segs), 3)
        self.assertAlmostEqual(segs[0].true_start, 1.0)
        self.assertAlmostEqual(segs[0].true_end, 14.0)
        # 验证前向 250ms 防咬字气口
        self.assertAlmostEqual(segs[1].cut_start, 14.25, places=2)
        for s in segs:
            s.mp3_path = f"/tmp/{s.seg_id}.mp3"
        checks = detect_split_id_checks(res, segs)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].unconfirmed_sid, "SPEAKER_01")
        self.assertEqual(checks[0].target_name, "Alice")

    def test_energy_valley_split_and_batch_packing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wav_p = Path(tmp) / "long.wav"
            _make_wav_with_valley(wav_p, dur_sec=60.0)
            diar_json = {
                "segments": [{"speakerId": "0", "startTimeSeconds": 0.0, "endTimeSeconds": 60.0}],
                "speakerEmbeddings": {},
            }
            res = parse_fluidaudio_json(diar_json, {})
            segs = clean_and_merge_segments(
                res,
                config=SegmenterConfig(max_turn_sec=45.0),
                wav_path=wav_p,
            )
            self.assertEqual(len(segs), 2)
            # 切点应落在 31.5~32.5 秒的静音谷附近
            self.assertGreaterEqual(segs[0].true_end, 31.0)
            self.assertLessEqual(segs[0].true_end, 33.0)

            batches = pack_batches(segs, max_segments=15, max_speech_sec=120.0)
            self.assertEqual(len(batches), 1)

            # 验证 120s 时长上限精准切包（每段 35s，4 段 140s 应切成 3+1 两包）
            long_diar = {
                "segments": [
                    {"speakerId": "0", "startTimeSeconds": 0.0, "endTimeSeconds": 35.0},
                    {"speakerId": "1", "startTimeSeconds": 36.0, "endTimeSeconds": 71.0},
                    {"speakerId": "0", "startTimeSeconds": 72.0, "endTimeSeconds": 107.0},
                    {"speakerId": "1", "startTimeSeconds": 108.0, "endTimeSeconds": 143.0},
                ],
                "speakerEmbeddings": {},
            }
            long_segs = clean_and_merge_segments(parse_fluidaudio_json(long_diar, {}))
            split_batches = pack_batches(long_segs)  # 默认 max_speech_sec=120.0
            self.assertEqual([len(b) for b in split_batches], [3, 1])

    def test_workflow_script_compiles_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_p = Path(tmp) / "manifest_v2.json"
            manifest_p.write_text(json.dumps({"transcript_path": str(Path(tmp) / "out.md")}), encoding="utf-8")
            script = render_workflow_script(manifest_p, cleanup_clips=True, max_concurrency=40, agent_timeout_sec=120)
            # 确认生成的脚本含 tone_note、前向去重、防死磕护栏、超时自动对半拆包、缺段补录与自适应限流降级
            self.assertIn("tone_note", script)
            self.assertIn("0.25 秒", script)
            self.assertIn("[听不清]", script)
            self.assertIn("asyncio.wait_for", script)
            self.assertIn("_split_segments_balanced", script)
            self.assertIn("[超时拆包]", script)
            self.assertIn("AdaptiveLimiter", script)
            self.assertIn("_patch", script)
            # 允许 top-level await（run_workflow 语法），包进 async def 验证语法 100% 合法
            indented = "async def _wf():\n" + "\n".join("    " + line for line in script.splitlines())
            compile(indented, "run_v2_transcribe.py", "exec")

    def test_workflow_timeout_auto_bisect_runtime(self) -> None:
        """端到端异步运行生成脚本中的超时对半拆包逻辑，验证按时长均衡拆分且零死锁合并。"""
        import asyncio

        with tempfile.TemporaryDirectory() as tmp:
            manifest_p = Path(tmp) / "manifest_v2.json"
            segs = [
                {"seg_id": f"S{i:03d}", "sid": "SPEAKER_00", "speaker_label": "Alice", "hms_start": "00:00:00", "hms_end": "00:00:10", "dur": d, "mp3_path": f"/tmp/S{i:03d}.mp3"}
                for i, d in enumerate([10.0, 10.0, 10.0, 10.0, 40.0, 40.0], start=1)
            ]
            manifest_p.write_text(
                json.dumps({"transcript_path": str(Path(tmp) / "out.md"), "clips_dir": tmp, "segments": segs, "batches": [segs]}),
                encoding="utf-8",
            )
            script = render_workflow_script(manifest_p, cleanup_clips=False, max_concurrency=2, agent_timeout_sec=120)
            # 截取到 t_total 之前的函数定义，隔离执行 _invoke_with_retry
            defs_part = script.split("\nt_total = time.monotonic()")[0]
            logs: list[str] = []
            seen_roles: list[str] = []

            def fake_agent(prompt, schema=None, role="", type_name="self", model="flash"):
                seen_roles.append(role)
                async def _coro():
                    if role == "精转 B01":
                        raise asyncio.TimeoutError()
                    import re
                    ids = re.findall(r"`(S\d{3})`", prompt)
                    return {"batch_id": role, "items": [{"seg_id": sid, "text": f"ok-{sid}"} for sid in ids]}
                return _coro()

            ns: dict = {"log": logs.append, "agent": fake_agent}
            exec(compile(defs_part, "wf_defs.py", "exec"), ns)

            ok, items, err = asyncio.run(ns["_invoke_with_retry"]("B01", segs, {}, "self"))
            self.assertTrue(ok)
            self.assertEqual(err, "")
            self.assertEqual([x["seg_id"] for x in items], [f"S{i:03d}" for i in range(1, 7)])
            # [10,10,10,10,40,40] 总120s，均衡中点应在 40+40=80 vs 40 或前4段(40s)+后2段(80s)：
            # i=4时acc=40(diff=20)，i=5时acc=80(diff=20)，先取 best_idx=4 -> 4段(40s) + 2段(80s)
            self.assertIn("精转 B01a", seen_roles)
            self.assertIn("精转 B01b", seen_roles)
            self.assertTrue(any("[超时拆包]" in l and "4段/40.0s" in l and "2段/80.0s" in l for l in logs))

    def test_per_segment_embeddings_and_dict_voiceprint_format(self) -> None:
        """验证真实 FluidAudio 逐段 embedding 加权质心与 {姓名: {录音: 向量}} 字典格式声纹库。"""
        with tempfile.TemporaryDirectory() as tmp:
            vp_path = Path(tmp) / "vp.json"
            vp_path.write_text(
                json.dumps({"Alice": {"rec1": [1.0, 0.0, 0.0], "rec2": [0.98, 0.1, 0.0]}}),
                encoding="utf-8",
            )
            store = VoiceprintStore(vp_path)
            vps = store.load()
            self.assertEqual(len(vps["Alice"]), 2)

            diar = {
                "segments": [
                    {"speakerId": "0", "startTimeSeconds": 0.0, "endTimeSeconds": 5.0, "embedding": [0.99, 0.05, 0.0]},
                    {"speakerId": "0", "startTimeSeconds": 6.0, "endTimeSeconds": 10.0, "embedding": [1.0, 0.0, 0.0]},
                ]
            }
            res = parse_fluidaudio_json(diar, vps)
            self.assertTrue(res.speakers["SPEAKER_00"].confirmed)
            self.assertEqual(res.speakers["SPEAKER_00"].label, "Alice")

    def test_config_and_memo_filename(self) -> None:
        dt, short_id = parse_filename_metadata("20261123 141827-4DB2F94C")
        self.assertIsNotNone(dt)
        self.assertEqual(short_id, "4DB2F94C")
        stem = sanitize_filename_stem(dt, "客户拜访/测试:1", "4DB2F94C")
        self.assertEqual(stem, "1418_客户拜访_测试_1_4DB2F9")

        cfg = load_config(Path(__file__).resolve().parent.parent / "config.example.toml")
        self.assertEqual(cfg.segmenter.batch_max_segments, 15)
        self.assertAlmostEqual(cfg.segmenter.batch_max_speech_sec, 120.0)
        self.assertAlmostEqual(cfg.segmenter.pad_before_sec, 0.25)

    def test_workflow_worker_pool_reuses_sessions_and_rotates(self) -> None:
        """验证 ReusableWorkerPool 会话复用、满轮自动换新与异常立即作废重建，防止冲爆 500 会话上限。"""
        import asyncio
        import re

        with tempfile.TemporaryDirectory() as tmp:
            manifest_p = Path(tmp) / "manifest_v2.json"
            segs = [
                {"seg_id": f"S{i:03d}", "sid": "SPEAKER_00", "speaker_label": "Alice", "hms_start": "00:00:00", "hms_end": "00:00:05", "dur": 5.0, "mp3_path": f"/tmp/S{i:03d}.mp3"}
                for i in range(1, 19)
            ]
            batches = [[s] for s in segs]
            manifest_p.write_text(
                json.dumps({"transcript_path": str(Path(tmp) / "out.md"), "clips_dir": tmp, "segments": segs, "batches": batches}),
                encoding="utf-8",
            )
            script = render_workflow_script(
                manifest_p,
                cleanup_clips=False,
                max_concurrency=10,
                agent_timeout_sec=60,
                stagger_sec=0.0,
                max_turns_per_worker=6,
                worker_pool_size=3,
            )
            defs_part = script.split("\nt_total = time.monotonic()")[0]
            logs: list[str] = []
            created_roles: list[str] = []
            failed_once = {"done": False}

            def fake_reusable_agent(prompt, schema=None, role="", type_name="self", model="flash"):
                created_roles.append(role)

                class _Handle:
                    def __call__(self, turn_prompt, turn_schema=None):
                        async def _run():
                            ids = re.findall(r"`(S\d{3})`", turn_prompt)
                            # 模拟 S005 首次超时，验证损坏工位立刻作废换新
                            if ids == ["S005"] and not failed_once["done"]:
                                failed_once["done"] = True
                                raise asyncio.TimeoutError()
                            return {"batch_id": role, "items": [{"seg_id": sid, "text": f"ok-{sid}"} for sid in ids]}
                        return _run()

                return _Handle()

            ns: dict = {"log": logs.append, "agent": fake_reusable_agent}
            exec(compile(defs_part, "wf_pool_defs.py", "exec"), ns)

            # 1) 验证自动工位池规模计算（60 批仅开 12 工位）
            self.assertEqual(ns["compute_worker_pool_size"](18), 3)

            async def _run_all():
                pool = ns["ReusableWorkerPool"](3, "self", max_turns=6)
                # 测试 18 个 batch（外加 S005 一次超时重试）
                tasks = [
                    ns["_invoke_with_retry"](f"B{i + 1:02d}", b, {}, "self", stagger_delay=0.0, pool=pool)
                    for i, b in enumerate(batches)
                ]
                results = await asyncio.gather(*tasks)
                pool.close()
                return pool, results

            # 加快退避以便测试秒跑
            ns["random"].uniform = lambda a, b: 0.01
            pool, results = asyncio.run(_run_all())
            self.assertTrue(all(r[0] for r in results))
            # 18 批本来会创建 19 个独立会话；工位池（3 工位 * 6 轮 + 1 次超时换新）仅创建 4~6 个会话，远少于 18
            self.assertLessEqual(pool.sessions_created, 6)
            self.assertEqual(pool.turns_completed, 18)
            self.assertTrue(pool.reusable_supported)

    def test_sanitize_transcript_field_strips_schema_leaks(self) -> None:
        self.assertEqual(
            sanitize_transcript_field('我觉得可以先试点一下"},{multi_speaker_note: ""'),
            "我觉得可以先试点一下",
        )
        self.assertEqual(
            sanitize_transcript_field('语气略显犹豫,seg_id:"S0042"'),
            "语气略显犹豫",
        )
        self.assertEqual(
            sanitize_transcript_field('正常中文和 "quoted" 保持原样'),
            '正常中文和 "quoted" 保持原样',
        )


if __name__ == "__main__":
    unittest.main()
