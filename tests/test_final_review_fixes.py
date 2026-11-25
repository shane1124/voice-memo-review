"""终审 Critical & Important 全量 TDD 回归测试（先 RED 再 GREEN 锁定修复）。"""
from __future__ import annotations

from datetime import datetime
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import voice_review
from voice_review.analyzers import AnalysisRequest, GeminiLlmAnalyzer, OpenAICompatibleLlmAnalyzer
from voice_review.config import config_to_dict, load_config, save_config_dict
from voice_review.daemon import PipelineExecutor, VmrDaemon
from voice_review.installer import ClientInstaller
from voice_review.mcp_server import McpStdioServer
from voice_review.rules import RuleStudio
from voice_review.secrets import SecretStore
from voice_review.sources import DropFolderScanner, MultiSourceScanner
from voice_review.state import StateStore
from voice_review.transcribers import CommandAsrTranscriber, PreparedAudioJob, TranscriptOutcome
from voice_review.updater import SelfUpdater
from voice_review.voice_memos import SHANGHAI_TZ


def _http_json(url: str, *, method: str = "GET", body: dict | None = None, token: str = "", host: str = ""):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["X-VMR-Token"] = token
    if host:
        headers["Host"] = host
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


class TestFinalReviewFixes(unittest.TestCase):
    # C1: voice_review 顶级导出必须保持下游向后兼容
    def test_c1_top_level_exports_backward_compatible(self) -> None:
        for sym in (
            "FluidAudioDiarizer",
            "VoiceprintStore",
            "SegmenterConfig",
            "prepare_v2_job",
            "render_workflow_script",
            "sanitize_transcript_field",
            "clean_and_merge_segments",
            "load_config",
            "VoiceReviewConfig",
            "__version__",
        ):
            self.assertTrue(hasattr(voice_review, sym), f"voice_review 缺少向后兼容导出：{sym}")

    # C2: macOS 无完全磁盘访问权限时，VoiceMemoScanner 抛 PermissionError 不得阻断投放文件夹与手动任务
    def test_c2_permission_error_isolated_per_source_and_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            drop_d = tmp / "drop"
            drop_d.mkdir()
            (drop_d / "ok.m4a").write_bytes(b"audio-ok")
            multi = MultiSourceScanner(
                voice_memos_enabled=True,
                recordings_dir=tmp / "vm_denied",
                drop_folder_enabled=True,
                drop_folder_dir=drop_d,
                import_since=datetime(2020, 1, 1, tzinfo=SHANGHAI_TZ),
                min_duration_sec=5.0,
                debounce_sec=0.0,
            )
            with patch.object(multi.vm_scanner, "scan_candidates", side_effect=PermissionError("Operation not permitted")), \
                 patch("voice_review.sources.probe_duration_sec", return_value=30.0):
                cands = multi.scan_candidates()
            self.assertEqual(len(cands), 1)
            self.assertEqual(cands[0].source_kind, "drop_folder")

    # C3: 密钥留空不删除、切换引擎无参数残留、config_to_dict 保留旧字段
    def test_c3_config_put_preserves_secrets_clears_stale_provider_and_keeps_legacy_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            cfg_p = tmp / "config.toml"
            cfg_p.write_text(
                'output_dir = "out"\n'
                'asr_context = "保留专词"\n'
                'ffmpeg_bin = "/custom/ffmpeg"\n'
                '[transcriber]\n'
                'provider = "gemini-api"\n'
                'base_url = "https://gemini.example/v1beta"\n'
                'model = "gemini-2.5-flash"\n'
                '[analyzer]\n'
                'auto_provider = "none"\n',
                encoding="utf-8",
            )
            cfg = load_config(cfg_p, vmr_home=tmp)
            dumped = config_to_dict(cfg)
            self.assertEqual(dumped["asr_context"], "保留专词")
            self.assertEqual(dumped["ffmpeg_bin"], "/custom/ffmpeg")

            secrets = SecretStore(cfg.secrets_path)
            secrets.set("gemini_api_key", "AIzaKeepMe1234")
            # 空字符串默认不得删除已有密钥
            secrets.update_many({"gemini_api_key": ""})
            self.assertEqual(secrets.get("gemini_api_key"), "AIzaKeepMe1234")

            daemon = VmrDaemon(cfg, port=0, start_watcher=False)
            daemon.start_in_thread()
            try:
                base = f"http://127.0.0.1:{daemon.port}"
                # 从 gemini-api 切换到 openai-compatible-asr（不带 base_url），旧 gemini base_url 绝不能残留
                st, res = _http_json(
                    f"{base}/api/v1/config",
                    method="PUT",
                    body={
                        "transcriber": {"provider": "openai-compatible-asr", "model": "whisper-1"},
                        "secrets": {"gemini_api_key": ""},
                    },
                    token=daemon.token,
                )
                self.assertEqual(st, 200)
                self.assertNotIn("base_url", res["config"]["transcriber"])
                self.assertEqual(daemon.secrets.get("gemini_api_key"), "AIzaKeepMe1234")
            finally:
                daemon.stop()

    # C4: vmr 启动脚本必须使用 python3 -P 防止 cwd 劫持受管模块
    def test_c4_vmr_launcher_uses_safe_path_flag(self) -> None:
        vmr_text = (Path(__file__).resolve().parent.parent / "vmr").read_text(encoding="utf-8")
        self.assertIn("-P -m voice_review.cli", vmr_text)

    # I1: 远程更新缺少 SHA256 必须拒绝；pyproject 版本为 1.0.0；build_release 无本机个人绝对路径
    def test_i1_updater_requires_sha256_for_remote_and_pyproject_version(self) -> None:
        repo = Path(__file__).resolve().parent.parent
        self.assertIn(f'version = "{voice_review.__version__}"', (repo / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertNotIn("/Users/", (repo / "scripts" / "build_release.sh").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            upd = SelfUpdater(repo="shane1124/voice-memo-review", current_version="1.0.0", managed_dir=tmp / "m", vmr_home=tmp / "h")
            res = upd.apply_update("https://example.com/vmr.tar.gz", expected_sha256="")
            self.assertFalse(res.ok)
            self.assertIn("SHA256", res.message)

    # I2: 分析器缺少转录稿返回 AnalysisOutcome(ok=False) 而非未捕获崩溃；GeminiLlmAnalyzer 支持 429 重试
    def test_i2_analyzers_handle_missing_file_and_gemini_retries_429(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            cfg = load_config(vmr_home=tmp)
            secrets = SecretStore(cfg.secrets_path)
            secrets.set("gemini_api_key", "AIzaRetryKey")
            rules = RuleStudio(cfg.rules_dir)
            rules.ensure_initialized()

            missing_req = AnalysisRequest(
                recording_id="m1", title="缺稿", recorded_at="2026-11-25T10:00:00+08:00",
                duration_sec=10.0, speakers_summary="", transcript_path=tmp / "nope.md", analysis_path=tmp / "a.md",
            )
            r_oa = OpenAICompatibleLlmAnalyzer(cfg, secrets, rules).analyze(missing_req)
            r_gm = GeminiLlmAnalyzer(cfg, secrets, rules).analyze(missing_req)
            self.assertFalse(r_oa.ok)
            self.assertFalse(r_gm.ok)

            t_p = tmp / "ok.md"
            t_p.write_text("## 00:00:01 **张三**\n你好", encoding="utf-8")
            ok_req = AnalysisRequest(
                recording_id="m2", title="重试", recorded_at="2026-11-25T10:00:00+08:00",
                duration_sec=10.0, speakers_summary="", transcript_path=t_p, analysis_path=tmp / "a2.md",
            )
            calls = {"n": 0}

            def _mock_open(req, timeout=180):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"rate"))

                class _R:
                    status = 200
                    def read(self):
                        return json.dumps({"candidates": [{"content": {"parts": [{"text": "# 复盘\n成功"}]}}]}).encode("utf-8")
                    def __enter__(self): return self
                    def __exit__(self, *a): return False
                return _R()

            with patch("voice_review.analyzers.urllib.request.urlopen", side_effect=_mock_open), \
                 patch("voice_review.analyzers.time.sleep", return_value=None):
                res = GeminiLlmAnalyzer(cfg, secrets, rules).analyze(ok_req)
            self.assertTrue(res.ok)
            self.assertEqual(calls["n"], 2)

    # I3: reset_in_flight 保留已转写状态；prepared 不被误标 awaiting_analysis；运行中任务拒绝 /retry 重置
    def test_i3_state_machine_reset_in_flight_prepared_and_retry_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            cfg = load_config(vmr_home=tmp)
            store = StateStore(cfg.state_db)
            store.upsert_discovered(
                recording_id="ana1", filename="a.m4a", title="已转待析",
                recorded_at="2026-11-25T10:00:00+08:00", recorded_date="2026-11-25", duration_sec=60.0,
            )
            t_file = tmp / "t.md"
            t_file.write_text("已有转录", encoding="utf-8")
            store.update("ana1", status="analyzing", transcript_path=str(t_file))
            store.reset_in_flight()
            # analyzing 中断重启应回退至 transcribed，不重跑整条转写！
            self.assertEqual(store.get("ana1").status, "transcribed")

            # antigravity-flash + auto_analyze=False 应保持 prepared 而非 awaiting_analysis
            exe = PipelineExecutor(cfg, store=store)
            store.upsert_discovered(
                recording_id="wf1", filename="w.m4a", title="工作流",
                recorded_at="2026-11-25T10:01:00+08:00", recorded_date="2026-11-25", duration_sec=60.0,
            )
            store.update("wf1", status="prepared", workflow_script_path=str(tmp / "wf.py"))
            st = exe.process_one(store.get("wf1"), auto_analyze=False)
            self.assertEqual(st, "prepared")

            # 运行中任务调 /retry 不可重置回 discovered
            daemon = VmrDaemon(cfg, port=0, start_watcher=False)
            daemon.start_in_thread()
            try:
                time.sleep(0.05)
                daemon.store.update("wf1", status="transcribing")
                base = f"http://127.0.0.1:{daemon.port}"
                _, r = _http_json(f"{base}/api/v1/jobs/wf1/retry", method="POST", body={}, token=daemon.token)
                self.assertTrue(r.get("already_running"))
                self.assertEqual(daemon.store.get("wf1").status, "transcribing")
            finally:
                daemon.stop()

    # I4: 内置模板删除后不自动复活；command-asr 保留带空格引号路径；非法 JSON 返回 400；IPv6 Host 校验正确
    def test_i4_rules_delete_no_resurrect_command_quotes_and_bad_json_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            studio = RuleStudio(tmp / "rules")
            studio.ensure_initialized()
            self.assertTrue(studio.delete_template("solo_memo"))
            self.assertNotIn("solo_memo", {t.id for t in studio.list_templates()})

            cfg_p = tmp / "config.toml"
            save_config_dict(cfg_p, {
                "transcriber": {
                    "provider": "command-asr",
                    "command": '"/Applications/My ASR/cli" -m "/Users/me/My Models/m.bin" -f {audio}',
                },
                "analyzer": {"auto_provider": "none"},
            })
            cfg = load_config(cfg_p, vmr_home=tmp)
            tr = CommandAsrTranscriber(cfg)
            self.assertEqual(tr.command_template[0], "/Applications/My ASR/cli")
            self.assertEqual(tr.command_template[2], "/Users/me/My Models/m.bin")

            # 非法 JSON body 应返回 400 而非 500
            daemon = VmrDaemon(cfg, port=0, start_watcher=False)
            daemon.start_in_thread()
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{daemon.port}/api/v1/config",
                    data=b"{bad json",
                    headers={"Content-Type": "application/json", "X-VMR-Token": daemon.token},
                    method="PUT",
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 400)
            finally:
                daemon.stop()

    # I5: DropFolderScanner 缓存未变历史文件不重复拉起 ffprobe；Codex 移除段落不误吞下段注释；非 dict JSON 安全中止
    def test_i5_drop_folder_caches_unchanged_and_codex_preserves_next_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            drop_d = tmp / "drop"
            drop_d.mkdir()
            (drop_d / "hist.m4a").write_bytes(b"same-audio-content")
            scanner = DropFolderScanner(drop_d, min_duration_sec=5.0, debounce_sec=0.0)
            with patch("voice_review.sources.probe_duration_sec", return_value=25.0) as m_probe:
                scanner.scan_candidates()
                scanner.scan_candidates()
                scanner.scan_candidates()
                self.assertEqual(m_probe.call_count, 1)

            # Codex 相邻段落注释保护 & 非 dict JSON 不覆写
            home = tmp / "home"
            (home / ".codex").mkdir(parents=True)
            codex_cfg = home / ".codex" / "config.toml"
            codex_cfg.write_text(
                '[mcp_servers.voice-memo-review]\ncommand = "old"\nargs = ["mcp"]\n\n'
                '# 下面这个注释属于 other 服务器\n'
                '[mcp_servers.other]\ncommand = "node"\n',
                encoding="utf-8",
            )
            bad_claude = home / ".claude.json"
            bad_claude.write_text("[]", encoding="utf-8")

            skill_src = Path(__file__).resolve().parent.parent / "skills" / "voice-memo-review"
            inst = ClientInstaller(vmr_bin=home / "vmr", skill_source_dir=skill_src, home_dir=home)
            res = inst.install(targets=["codex", "claude"])
            self.assertTrue(res["codex"].ok)
            self.assertFalse(res["claude"].ok)
            self.assertEqual(bad_claude.read_text(encoding="utf-8"), "[]")
            codex_after = codex_cfg.read_text(encoding="utf-8")
            self.assertIn("# 下面这个注释属于 other 服务器", codex_after)


if __name__ == "__main__":
    unittest.main()
