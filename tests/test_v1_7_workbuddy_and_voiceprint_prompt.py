"""v1.7.0 专项测试：WorkBuddy 桌面版完整适配 + 未登记主要说话人声纹提问与一键/一句话建档回填。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import urllib.request

from voice_review.analyzers import (
    AnalysisRequest,
    WorkBuddyDesktopAnalyzer,
    get_analyzer,
    open_job_desktop_chat,
    test_analyzer_connection,
)
from voice_review.config import (
    load_config,
    resolve_workbuddy_binary,
    save_config_dict,
    validate_provider_combo,
)
from voice_review.daemon import PipelineExecutor, VmrDaemon
from voice_review.diarizer import (
    DiarizationResult,
    SpeakerProfile,
    VoiceprintStore,
    build_voiceprint_enroll_prompt,
    enrich_unmatched_with_quotes,
    extract_unmatched_speakers,
)
from voice_review.installer import ClientInstaller
from voice_review.mcp_server import McpStdioServer
from voice_review.rules import RuleStudio
from voice_review.secrets import SecretStore
from voice_review.state import StateStore


def _make_exec(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, stat.S_IRWXU)
    return path


class TestV17WorkBuddyAndVoiceprintPrompt(unittest.TestCase):
    def test_workbuddy_desktop_analyzer_end_to_end_resume_and_deeplink(self) -> None:
        sid_1 = "11111111-aaaa-4bbb-8ccc-111111111111"
        sid_2 = "22222222-aaaa-4bbb-8ccc-222222222222"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls_log = root / "wb_calls.jsonl"
            fake_wb = _make_exec(
                root / "fake_codebuddy.py",
                f"""#!/opt/homebrew/bin/python3.13
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
log_p = {str(calls_log)!r}
with open(log_p, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{
        "args": args,
        "cfg_dir": os.environ.get("CODEBUDDY_CONFIG_DIR", ""),
        "prompt_len": len(prompt),
    }}, ensure_ascii=False) + "\\n")
if "--resume" in args:
    rid = args[args.index("--resume") + 1]
    if os.environ.get("WB_SIMULATE_DELETED") == "1":
        print(json.dumps([{{"type": "result", "is_error": True, "result": "session missing"}}]))
        sys.exit(1)
    print(json.dumps([
        {{"type": "message", "role": "assistant", "content": [{{"type": "output_text", "text": "# 续聊分析"}}]}},
        {{"type": "result", "is_error": False, "result": "# WorkBuddy 续聊分析", "session_id": rid}},
    ], ensure_ascii=False))
    sys.exit(0)
count = sum(1 for _ in open(log_p, encoding="utf-8"))
sid = "{sid_1}" if count == 1 else "{sid_2}"
print(json.dumps([
    {{"type": "message", "role": "assistant", "content": [{{"type": "output_text", "text": "# WorkBuddy 首次分析"}}]}},
    {{"type": "result", "is_error": False, "result": "# WorkBuddy 自动分析\\n\\n核心结论清晰。", "session_id": sid}},
], ensure_ascii=False))
""",
            )
            ok_bad, _ = validate_provider_combo("antigravity-flash", "workbuddy-desktop")
            self.assertFalse(ok_bad)
            ok_good, _ = validate_provider_combo("command-asr", "workbuddy-desktop")
            self.assertTrue(ok_good)

            cfg_path = root / "config.toml"
            save_config_dict(
                cfg_path,
                {
                    "output_dir": str(root / "out"),
                    "rotation": "daily",
                    "workbuddy_bin": str(fake_wb),
                    "transcriber": {"provider": "gemini-api"},
                    "analyzer": {"auto_provider": "workbuddy-desktop", "open_desktop_chat": True},
                },
            )
            cfg = load_config(cfg_path, vmr_home=root)
            rules = RuleStudio(cfg.rules_dir)
            rules.ensure_initialized()
            store = StateStore(cfg.state_db)
            secrets = SecretStore(cfg.secrets_path)

            ok_conn, msg_conn = test_analyzer_connection(cfg, secrets)
            self.assertTrue(ok_conn)
            self.assertIn("WorkBuddy 桌面版", msg_conn)

            analyzer = get_analyzer(cfg, secrets, store, rules)
            self.assertIsInstance(analyzer, WorkBuddyDesktopAnalyzer)

            t = root / "t.md"
            t.write_text("[00:00:05] SPEAKER_00：今天对齐一下项目进度。", encoding="utf-8")

            opened: list[str] = []
            with mock.patch("voice_review.analyzers._open_workbuddy_desktop_url", side_effect=lambda u: (opened.append(u) or True)):
                out1 = analyzer.analyze(
                    AnalysisRequest(
                        recording_id="rec-wb-1",
                        title="项目晨会",
                        recorded_at="2026-11-26T09:30:00+08:00",
                        duration_sec=120,
                        speakers_summary="- SPEAKER_00 -> 未知说话人",
                        transcript_path=t,
                        analysis_path=root / "out" / "wb1.md",
                    )
                )
                self.assertTrue(out1.ok, out1.error)
                self.assertTrue(out1.chat_url.startswith("workbuddy://task?action=start"))
                self.assertEqual(len(opened), 1)
                self.assertEqual(len(list((root / "out" / "录音对话").glob("*.webloc"))), 1)

                # 同天第二条自动续接 sid_1
                out2 = analyzer.analyze(
                    AnalysisRequest(
                        recording_id="rec-wb-2",
                        title="下午对齐",
                        recorded_at="2026-11-26T15:00:00+08:00",
                        duration_sec=90,
                        speakers_summary="",
                        transcript_path=t,
                        analysis_path=root / "out" / "wb2.md",
                    )
                )
                self.assertTrue(out2.ok, out2.error)

                # 旧会话丢失自动自愈
                with mock.patch.dict(os.environ, {"WB_SIMULATE_DELETED": "1"}):
                    out3 = analyzer.analyze(
                        AnalysisRequest(
                            recording_id="rec-wb-3",
                            title="晚间沟通",
                            recorded_at="2026-11-26T20:00:00+08:00",
                            duration_sec=60,
                            speakers_summary="",
                            transcript_path=t,
                            analysis_path=root / "out" / "wb3.md",
                        )
                    )
                self.assertTrue(out3.ok, out3.error)

            calls = [json.loads(ln) for ln in calls_log.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(calls[0]["cfg_dir"].endswith("/.workbuddy"))
            self.assertIn("--resume", calls[1]["args"])
            self.assertEqual(calls[1]["args"][calls[1]["args"].index("--resume") + 1], sid_1)
            self.assertIn("--resume", calls[2]["args"])
            self.assertNotIn("--resume", calls[3]["args"])

            with mock.patch("voice_review.analyzers._open_workbuddy_desktop_url", return_value=True) as m_open:
                ok_open, msg_open = open_job_desktop_chat(cfg, out1.chat_url)
                self.assertTrue(ok_open)
                self.assertIn("WorkBuddy", msg_open)
                m_open.assert_called_once_with(out1.chat_url)

    def test_workbuddy_installer_and_binary_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            wb_dir = home / ".workbuddy"
            wb_dir.mkdir(parents=True)
            mcp_p = wb_dir / ".mcp.json"
            mcp_p.write_text(
                json.dumps({"mcpServers": {"connector-proxy": {"type": "http", "url": "http://127.0.0.1:49611/mcp"}}}),
                encoding="utf-8",
            )
            wb_app_bin = _make_exec(
                home / "Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy",
                "#!/bin/sh\necho wb\n",
            )
            with (
                mock.patch("pathlib.Path.home", return_value=home),
                mock.patch("shutil.which", return_value=None),
                mock.patch(
                    "voice_review.config._is_exec_file",
                    side_effect=lambda p: str(p).startswith(str(home)) and p.is_file() and os.access(p, os.X_OK),
                ),
            ):
                self.assertEqual(resolve_workbuddy_binary(""), str(wb_app_bin.resolve()))

            skill_src = Path(__file__).resolve().parent.parent / "skills" / "voice-memo-review"
            inst = ClientInstaller(vmr_bin=home / ".local/bin/vmr", skill_source_dir=skill_src, home_dir=home)
            det = inst.detect_clients()
            self.assertTrue(det["workbuddy"].detected)
            self.assertFalse(det["workbuddy"].installed)

            res = inst.install(["workbuddy"])
            self.assertTrue(res["workbuddy"].ok)
            updated = json.loads(mcp_p.read_text(encoding="utf-8"))
            self.assertIn("voice-memo-review", updated["mcpServers"])
            self.assertIn("connector-proxy", updated["mcpServers"])
            self.assertTrue((wb_dir / "skills" / "voice-memo-review" / "SKILL.md").is_file())

            inst.uninstall(["workbuddy"])
            after = json.loads(mcp_p.read_text(encoding="utf-8"))
            self.assertNotIn("voice-memo-review", after["mcpServers"])
            self.assertIn("connector-proxy", after["mcpServers"])

    def test_unmatched_speakers_prompt_mcp_and_web_confirm_rewrite_docs(self) -> None:
        diar = DiarizationResult(
            raw_segments=[(0.0, 25.0, "SPEAKER_00"), (25.0, 45.0, "SPEAKER_01"), (46.0, 48.0, "SPEAKER_02")],
            speakers={
                "SPEAKER_00": SpeakerProfile(
                    sid="SPEAKER_00", raw_id="0", label="SPEAKER_00",
                    confirmed=False, top1_name="", top1_score=0.0, top2_name="", top2_score=0.0, talk_sec=25.0,
                ),
                "SPEAKER_01": SpeakerProfile(
                    sid="SPEAKER_01", raw_id="1", label="SPEAKER_01",
                    confirmed=False, top1_name="", top1_score=0.0, top2_name="", top2_score=0.0, talk_sec=20.0,
                ),
                "SPEAKER_02": SpeakerProfile(
                    sid="SPEAKER_02", raw_id="2", label="SPEAKER_02",
                    confirmed=False, top1_name="", top1_score=0.0, top2_name="", top2_score=0.0, talk_sec=2.0,
                ),
                "SPEAKER_03": SpeakerProfile(
                    sid="SPEAKER_03", raw_id="3", label="已登记同事",
                    confirmed=True, top1_name="已登记同事", top1_score=0.92, top2_name="", top2_score=0.0, talk_sec=30.0,
                ),
            },
            raw_embeddings={
                "SPEAKER_00": [0.6, 0.8, 0.0],
                "SPEAKER_01": [0.0, 0.6, 0.8],
                "SPEAKER_02": [1.0, 0.0, 0.0],
                "SPEAKER_03": [0.0, 0.0, 1.0],
            },
        )
        # 仅保留未登记且发言 >= 8s 的主要说话人（排除 2s 杂音 SPEAKER_02 和已确认 SPEAKER_03）
        cands = extract_unmatched_speakers(diar, min_talk_sec=8.0)
        self.assertEqual([c["sid"] for c in cands], ["SPEAKER_00", "SPEAKER_01"])

        transcript_text = (
            "## 00:00:01 **SPEAKER_00**\n"
            "今天我们先对齐一下下周的交付排期和核心风险。\n\n"
            "## 00:00:25 **SPEAKER_01**\n"
            "好的，预算和商务条款我周二前给最终答复。\n"
        )
        cands = enrich_unmatched_with_quotes(cands, transcript_text)
        self.assertIn("交付排期", cands[0]["sample_quote"])
        self.assertIn("预算和商务条款", cands[1]["sample_quote"])

        prompt_sec = build_voiceprint_enroll_prompt(cands, recording_id="rec-vp-1")
        self.assertIn("2 位", prompt_sec)
        self.assertIn("下一次再录到他们时系统就会自动认出", prompt_sec)
        self.assertIn("SPEAKER_00", prompt_sec)
        self.assertIn("SPEAKER_01", prompt_sec)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = root / "config.toml"
            save_config_dict(
                cfg_path,
                {
                    "output_dir": str(root / "out"),
                    "sources": {"voice_memos_enabled": False, "drop_folder_enabled": False},
                },
            )
            cfg = load_config(cfg_path, vmr_home=root)
            daemon = VmrDaemon(cfg, port=0, start_watcher=False)
            daemon.start_in_thread()
            try:
                t_path = root / "out" / "_转录稿" / "t.md"
                a_path = root / "out" / "a.md"
                t_path.parent.mkdir(parents=True, exist_ok=True)
                t_path.write_text(transcript_text, encoding="utf-8")
                a_path.write_text("# 分析稿\n\nSPEAKER_00 负责推进排期，SPEAKER_01 负责预算确认。\n", encoding="utf-8")

                daemon.store.upsert_discovered(
                    recording_id="rec-vp-1",
                    filename="meet.m4a",
                    title="排期会",
                    recorded_at="2026-11-26T14:00:00+08:00",
                    recorded_date="2026-11-26",
                    duration_sec=50.0,
                    audio_path=str(root / "meet.m4a"),
                )
                daemon.store.update(
                    "rec-vp-1",
                    status="done",
                    transcript_path=str(t_path),
                    analysis_path=str(a_path),
                    speakers_summary="- SPEAKER_00 -> 未知\n- SPEAKER_01 -> 未知",
                )
                daemon.executor._save_unmatched_speakers("rec-vp-1", cands)

                # 1) Web GET content 返回轻量未登记说话人（不含大向量）
                req_get = urllib.request.Request(
                    f"http://127.0.0.1:{daemon.port}/api/v1/jobs/rec-vp-1/content",
                    headers={"X-VMR-Token": daemon.token},
                )
                with urllib.request.urlopen(req_get, timeout=5) as resp:
                    content_data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(len(content_data["unmatched_speakers"]), 2)
                self.assertNotIn("embedding", content_data["unmatched_speakers"][0])

                # 2) Web POST voiceprints 记住 SPEAKER_00 -> "我" 并自动替换稿件
                req_post = urllib.request.Request(
                    f"http://127.0.0.1:{daemon.port}/api/v1/jobs/rec-vp-1/voiceprints",
                    data=json.dumps({"mappings": {"SPEAKER_00": "我"}, "rewrite_docs": True}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "X-VMR-Token": daemon.token},
                    method="POST",
                )
                with urllib.request.urlopen(req_post, timeout=5) as resp:
                    vp_res = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(vp_res["ok"])
                self.assertEqual(len(vp_res["remaining_unmatched"]), 1)
                self.assertIn("**我**", t_path.read_text(encoding="utf-8"))
                self.assertIn("我 负责推进排期", a_path.read_text(encoding="utf-8"))

                # 3) MCP 工具自动匹配最新录音记住 SPEAKER_01 -> "王总"
                srv = McpStdioServer(cfg)
                mcp_res, is_err = srv._call_tool(
                    "vmr_manage_voiceprints",
                    {"action": "confirm_from_recording", "mappings": {"SPEAKER_01": "王总"}},
                )
                self.assertFalse(is_err, mcp_res)
                self.assertTrue(mcp_res["ok"])
                self.assertEqual(mcp_res["remaining_unmatched"], [])
                self.assertIn("王总 负责预算确认", a_path.read_text(encoding="utf-8"))

                # 4) 验证两者均已写入 voiceprints.json 供下次自动识别
                vp_names = dict(VoiceprintStore(cfg.voiceprints_path).list_summary())
                self.assertEqual(vp_names.get("我"), 1)
                self.assertEqual(vp_names.get("王总"), 1)
            finally:
                daemon.stop()


if __name__ == "__main__":
    unittest.main()
