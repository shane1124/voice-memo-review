"""Task 3 单元测试：投放文件夹多源扫描、特殊字符路径安全、noSpeechDetected 静音异常、首次模型下载放行与无声纹切块。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from voice_review.diarizer import FluidAudioDiarizer, NoSpeechDetectedError
from voice_review.segmenter import build_uniform_chunks
from voice_review.sources import DropFolderScanner, MultiSourceScanner
from voice_review.voice_memos import SHANGHAI_TZ


class TestSourcesAndPreprocess(unittest.TestCase):
    def test_drop_folder_special_chars_and_sha256_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "drop"
            d.mkdir()
            (d / "_processed").mkdir()
            (d / "_processed" / "ignored.m4a").write_bytes(b"ignored")
            (d / ".hidden.m4a").write_bytes(b"hidden")
            f = d / "客户会议 (第1次); $(whoami).m4a"
            f.write_bytes(b"fake-audio-bytes-123456")

            scanner = DropFolderScanner(d, min_duration_sec=5.0, debounce_sec=0.0)
            with patch("voice_review.sources.probe_duration_sec", return_value=42.0):
                cands = scanner.scan_candidates()
            self.assertEqual(len(cands), 1)
            self.assertEqual(cands[0].source_kind, "drop_folder")
            self.assertTrue(cands[0].recording_id.startswith("drop_"))
            self.assertEqual(cands[0].audio_path, f)
            self.assertTrue(scanner.is_file_stable(f))

    def test_multi_source_aggregates_voice_memos_and_drop_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            drop_d = Path(tmp) / "drop"
            drop_d.mkdir()
            (drop_d / "clip.mp3").write_bytes(b"abc")
            multi = MultiSourceScanner(
                voice_memos_enabled=False,
                recordings_dir=Path(tmp) / "missing_vm",
                drop_folder_enabled=True,
                drop_folder_dir=drop_d,
                import_since=datetime(2020, 1, 1, tzinfo=SHANGHAI_TZ),
                min_duration_sec=5.0,
                debounce_sec=0.0,
            )
            with patch("voice_review.sources.probe_duration_sec", return_value=20.0):
                cands = multi.scan_candidates()
            self.assertEqual(len(cands), 1)
            self.assertIn(drop_d, multi.watch_paths())

    def test_diarizer_raises_no_speech_and_auto_download_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "fluidaudiocli"
            fake_bin.write_text("#!/bin/sh\necho 'ERROR: Failed to process audio file (offline mode): noSpeechDetected'\nexit 1\n", encoding="utf-8")
            fake_bin.chmod(0o755)
            wav = Path(tmp) / "audio_16k.wav"
            wav.write_bytes(b"RIFF")

            d = FluidAudioDiarizer(fluidaudio_bin=fake_bin, offline_mode=True, auto_download_if_missing=True)
            with patch.object(d, "_models_present", return_value=False):
                env_dl = d._build_env()
                self.assertNotIn("REGISTRY_URL", env_dl)
            with patch.object(d, "_models_present", return_value=True):
                env_off = d._build_env()
                self.assertEqual(env_off.get("REGISTRY_URL"), "http://127.0.0.1:9")

            with self.assertRaises(NoSpeechDetectedError):
                d.diarize(wav, work_dir=Path(tmp), reuse_existing=False)

    def test_uniform_chunks_for_no_diarizer_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "talk (1); echo pwn.m4a"
            src.write_bytes(b"x")
            with patch("voice_review.segmenter.subprocess.run") as mrun, \
                 patch("voice_review.segmenter._probe_duration", return_value=1250.0):
                def _side(cmd, **kw):
                    self.assertFalse(kw.get("shell", False))
                    self.assertIsInstance(cmd, list)
                    Path(cmd[-1]).write_bytes(b"mp3-bytes")
                    return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

                mrun.side_effect = _side
                segs, total_dur = build_uniform_chunks(src, Path(tmp) / "clips", chunk_sec=600.0)
            self.assertEqual(len(segs), 3)
            self.assertEqual(segs[0].seg_id, "S001")
            self.assertEqual(segs[0].speaker_label, "说话人")
            self.assertAlmostEqual(total_dur, 1250.0)


if __name__ == "__main__":
    unittest.main()
