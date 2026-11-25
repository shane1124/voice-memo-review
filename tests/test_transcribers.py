"""Task 4 单元测试：4 大可插拔转写引擎、整轨声纹对齐、断点缓存 429 退避、无 shell 防注入与工作流节流锁。"""
from __future__ import annotations

from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from voice_review.config import load_config, save_config_dict
from voice_review.secrets import SecretStore
from voice_review.segmenter import Segment
from voice_review.transcribers import (
    PreparedAudioJob,
    align_asr_segments_to_diarization,
    get_transcriber,
)
from voice_review.voice_memos import SHANGHAI_TZ
from voice_review.workflow_builder import render_workflow_script, sanitize_transcript_field


def _fake_http_resp(payload: dict | str, status: int = 200):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") if isinstance(payload, dict) else payload.encode("utf-8")

    class _Resp:
        def __init__(self) -> None:
            self.status = status
            self._buf = io.BytesIO(raw)

        def read(self) -> bytes:
            return self._buf.read()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return _Resp()


def _make_job(tmp: Path) -> PreparedAudioJob:
    clips = tmp / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    audio_src = tmp / "会议; $(echo HACKED).m4a"
    audio_src.write_bytes(b"fake-audio-src")
    segs = []
    for i in (1, 2):
        mp3 = clips / f"S00{i}.mp3"
        mp3.write_bytes(b"ID3-fake-mp3")
        segs.append(
            Segment(
                seg_id=f"S00{i}",
                sid="SPEAKER_00" if i == 1 else "SPEAKER_01",
                speaker_label="张三" if i == 1 else "李四",
                confirmed=True,
                true_start=float((i - 1) * 10),
                true_end=float(i * 10),
                cut_start=float((i - 1) * 10),
                cut_end=float(i * 10),
                dur=10.0,
                hms_start=f"00:00:{(i - 1) * 10:02d}",
                hms_end=f"00:00:{i * 10:02d}",
                mp3_path=str(mp3),
                bytes_size=12,
            )
        )
    return PreparedAudioJob(
        job_dir=tmp / "job",
        audio_path=audio_src,
        transcript_path=tmp / "out" / "transcript.md",
        title="测试会议",
        recorded_at=datetime(2026, 11, 25, 14, 0, tzinfo=SHANGHAI_TZ),
        speakers_summary="- SPEAKER_00 -> 张三\n- SPEAKER_01 -> 李四",
        segments=segs,
        batches=[[segs[0]], [segs[1]]],
        diarization_segments=[(0.0, 10.0, "SPEAKER_00"), (10.0, 20.0, "SPEAKER_01")],
        speaker_labels={"SPEAKER_00": "张三", "SPEAKER_01": "李四"},
        asr_context="SaaS; $(echo INJECT)",
    )


class TestTranscribers(unittest.TestCase):
    def test_workflow_builder_has_global_launch_pacing_and_parser_err_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_p = Path(tmp) / "manifest_v2.json"
            manifest_p.write_text(json.dumps({"transcript_path": str(Path(tmp) / "out.md")}), encoding="utf-8")
            script = render_workflow_script(manifest_p, max_concurrency=25)
            self.assertIn("LAUNCH_PACE_SEC", script)
            self.assertIn("_PARSER_ERR_RE", script)
            self.assertEqual(
                sanitize_transcript_field("我觉得没问题 JsonParserError: unexpected token"),
                "我觉得没问题",
            )
            self.assertEqual(
                sanitize_transcript_field("好的稍等 Oops, I forgot to close the bracket"),
                "好的稍等",
            )

    def test_align_asr_segments_to_diarization(self) -> None:
        asr = [
            {"start": 0.5, "end": 4.5, "text": "大家好"},
            {"start": 9.0, "end": 15.0, "text": "我来补充"},
            {"start": 50.0, "end": 52.0, "text": "尾声"},
        ]
        diar = [(0.0, 10.0, "SPEAKER_00"), (10.0, 20.0, "SPEAKER_01")]
        labels = {"SPEAKER_00": "张三", "SPEAKER_01": "李四"}
        aligned = align_asr_segments_to_diarization(asr, diar, labels)
        self.assertEqual([x["speaker"] for x in aligned], ["张三", "李四", "未知说话人"])

    def test_gemini_api_transcriber_resume_cache_and_429_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            cfg_p = tmp / "config.toml"
            save_config_dict(cfg_p, {
                "transcriber": {"provider": "gemini-api", "model": "gemini-2.5-flash", "max_concurrency": 2},
                "analyzer": {"auto_provider": "none"},
            })
            cfg = load_config(cfg_p, vmr_home=tmp)
            secrets = SecretStore(cfg.secrets_path)
            secrets.set("gemini_api_key", "AIzaFakeKey12345")
            job = _make_job(tmp)
            # 预置 B01 断点缓存，验证不会重跑第 1 批
            cache_dir = job.job_dir / "batches_out"
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / "B01.json").write_text(
                json.dumps({"batch_id": "B01", "items": [{"seg_id": "S001", "text": "来自断点缓存", "tone_note": "", "multi_speaker_note": ""}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            calls = {"n": 0}

            def _mock_urlopen(req, timeout=60):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"rate limit"))
                body = {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {
                                                "batch_id": "B02",
                                                "items": [
                                                    {
                                                        "seg_id": "S002",
                                                        "text": "第二段现场精转",
                                                        "tone_note": "语气肯定果断",
                                                        "multi_speaker_note": "",
                                                    }
                                                ],
                                            },
                                            ensure_ascii=False,
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                }
                return _fake_http_resp(body)

            provider = get_transcriber(cfg, secrets)
            progress_events: list[tuple[int, int]] = []
            with patch("voice_review.transcribers.urllib.request.urlopen", side_effect=_mock_urlopen), \
                 patch("voice_review.transcribers.time.sleep", return_value=None):
                outcome = provider.transcribe(job, on_progress=lambda d, t: progress_events.append((d, t)))
            self.assertEqual(outcome.mode, "direct")
            self.assertEqual(calls["n"], 2)  # B01 命中缓存未发请求，B02 遇 429 重试 1 次成功
            md = outcome.transcript_path.read_text(encoding="utf-8")
            self.assertIn("来自断点缓存", md)
            self.assertIn("第二段现场精转", md)
            self.assertIn("语气肯定果断", md)
            self.assertTrue(any(d == 2 and t == 2 for d, t in progress_events))

    def test_openai_compatible_asr_multipart_and_transcript_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            cfg_p = tmp / "config.toml"
            save_config_dict(cfg_p, {
                "transcriber": {
                    "provider": "openai-compatible-asr",
                    "base_url": "https://api.example.com/v1",
                    "model": "whisper-1",
                    "max_concurrency": 2,
                },
                "analyzer": {"auto_provider": "none"},
            })
            cfg = load_config(cfg_p, vmr_home=tmp)
            secrets = SecretStore(cfg.secrets_path)
            secrets.set("openai_asr_api_key", "sk-asr-9999")
            job = _make_job(tmp)
            seen_headers: list[dict] = []

            def _mock_urlopen(req, timeout=60):
                seen_headers.append(dict(req.header_items()))
                return _fake_http_resp({"text": "Whisper 转写正文"})

            provider = get_transcriber(cfg, secrets)
            with patch("voice_review.transcribers.urllib.request.urlopen", side_effect=_mock_urlopen):
                outcome = provider.transcribe(job)
            self.assertEqual(outcome.mode, "direct")
            self.assertEqual(len(seen_headers), 2)
            self.assertIn("multipart/form-data", seen_headers[0].get("Content-type", ""))
            self.assertEqual(seen_headers[0].get("Authorization"), "Bearer sk-asr-9999")
            md = outcome.transcript_path.read_text(encoding="utf-8")
            self.assertIn("Whisper 转写正文", md)
            self.assertIn("**张三**", md)

    def test_command_asr_full_audio_no_shell_injection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            pwn_marker = tmp / "PWNED"
            helper = tmp / "fake_asr.py"
            helper.write_text(
                "import json, sys\n"
                "print(json.dumps({'segments': [{'start': 1.0, 'end': 5.0, 'text': '整轨第一句'}, {'start': 12.0, 'end': 18.0, 'text': '整轨第二句'}]}, ensure_ascii=False))\n",
                encoding="utf-8",
            )
            cfg_p = tmp / "config.toml"
            save_config_dict(cfg_p, {
                "transcriber": {
                    "provider": "command-asr",
                    "mode": "full_audio",
                    "command": ["/opt/homebrew/bin/python3.13", str(helper), "{audio}", "{glossary}"],
                },
                "analyzer": {"auto_provider": "none"},
            })
            cfg = load_config(cfg_p, vmr_home=tmp)
            secrets = SecretStore(cfg.secrets_path)
            job = _make_job(tmp)
            # 注入测试文件名与词表
            job = PreparedAudioJob(
                **{**job.__dict__, "asr_context": f"term; touch {pwn_marker}"}
            )
            provider = get_transcriber(cfg, secrets)
            outcome = provider.transcribe(job)
            self.assertFalse(pwn_marker.exists())
            md = outcome.transcript_path.read_text(encoding="utf-8")
            self.assertIn("整轨第一句", md)
            self.assertIn("**张三**", md)
            self.assertIn("整轨第二句", md)
            self.assertIn("**李四**", md)


if __name__ == "__main__":
    unittest.main()
