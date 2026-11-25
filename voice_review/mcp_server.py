"""零第三方依赖的标准 Stdio MCP Server（JSON-RPC 2.0，兼容 Antigravity / Claude Code / Codex / Cursor）。"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, TextIO
import urllib.request

from voice_review import __version__
from voice_review.autostart import AutostartManager, write_restart_token
from voice_review.config import (
    VoiceReviewConfig,
    config_to_dict,
    load_config,
    resolve_claude_binary,
    resolve_codex_binary,
    save_config_dict,
    validate_provider_combo,
)
from voice_review.daemon import PipelineExecutor, discover_running_daemon
from voice_review.diarizer import FluidAudioDiarizer, VoiceprintStore
from voice_review.rules import RuleStudio
from voice_review.secrets import SecretStore
from voice_review.state import StateStore
from voice_review.voice_memos import probe_duration_sec

PROTOCOL_VERSION = "2024-11-05"
ASYNC_DURATION_THRESHOLD_SEC = 20.0


def _tool_defs() -> list[dict[str, Any]]:
    return [
        {
            "name": "vmr_status",
            "description": "查看本地录音自动分析环境状态、当前启用的转写/分析插槽配置与队列概览。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "vmr_list_recordings",
            "description": "列出近期录音记录及其状态（如 awaiting_analysis 待分析、done 已完成、skipped 已跳过）。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "可选过滤状态（awaiting_analysis / done / failed / discovered）"},
                    "limit": {"type": "integer", "default": 20},
                },
            },
        },
        {
            "name": "vmr_process_audio",
            "description": "对指定音频文件执行本地声纹切段与转写。为防止 MCP 超时，中长录音直连转写会自动转入后台托管并立即返回 job_id，请随后调用 vmr_get_job_status 查询进度。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "audio_path": {"type": "string", "description": "本地音频文件绝对路径（m4a/mp3/wav/flac/mp4）"},
                    "title": {"type": "string", "description": "可选：自定义标题"},
                    "template_id": {"type": "string", "description": "可选：指定分析模板 ID（如 general_review / meeting_minutes）"},
                },
                "required": ["audio_path"],
            },
        },
        {
            "name": "vmr_get_job_status",
            "description": "查询异步处理任务的包级进度；当转写完成后返回转录稿路径与拼装好的显化模板分析指令。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "vmr_process_audio 返回的 job_id / recording_id"},
                    "template_id": {"type": "string", "description": "可选：覆盖分析模板 ID"},
                },
                "required": ["job_id"],
            },
        },
        {
            "name": "vmr_list_templates",
            "description": "列出规则工作室中所有显化明文分析模板。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "vmr_get_template",
            "description": "读取指定分析模板的完整明文提示词规则（不传 template_id 时返回默认模板）。",
            "inputSchema": {
                "type": "object",
                "properties": {"template_id": {"type": "string"}},
            },
        },
        {
            "name": "vmr_save_template",
            "description": "新建或修改明文分析提示词模板，透明持久化到用户规则目录。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "template_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "body": {"type": "string"},
                    "is_default": {"type": "boolean", "default": False},
                },
                "required": ["template_id", "name", "body"],
            },
        },
        {
            "name": "vmr_save_analysis",
            "description": "将宿主 AI 按显化规则生成的复盘正文落盘至输出目录，并把对应录音状态更新为 done。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "recording_id": {"type": "string"},
                    "markdown": {"type": "string", "description": "完整 Markdown 复盘正文"},
                    "template_id": {"type": "string"},
                },
                "required": ["recording_id", "markdown"],
            },
        },
        {
            "name": "vmr_manage_voiceprints",
            "description": "查看、注册（从 10~30s 清晰单人样音）或删除本地声纹库成员。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "enroll", "delete"]},
                    "name": {"type": "string"},
                    "audio_path": {"type": "string"},
                },
                "required": ["action"],
            },
        },
        {
            "name": "vmr_get_profile",
            "description": "读取当前用户背景（persona.md）、高频专有名词词表（glossary.txt）、默认模板及脱敏四插槽配置，便于引导个性化定制。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "vmr_update_profile",
            "description": "根据用户口述更新个人角色与关注重点（persona.md）、专有名词词表（glossary.txt）或切换默认分析模板。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "persona": {"type": "string", "description": "可选：完整写入个人背景与复盘关注重点（Markdown）"},
                    "glossary": {"type": "string", "description": "可选：完整写入专有名词校对词表（空格或换行分隔）"},
                    "default_template_id": {"type": "string", "description": "可选：将指定模板 ID 设为默认分析模板"},
                },
            },
        },
        {
            "name": "vmr_update_settings",
            "description": "更新四插槽与常用偏好非敏感配置（转写/分析引擎、监听源开关、macOS 开机自启、对话轮换频率 rotation、输出目录 output_dir、短录音过滤秒数 min_duration_sec），保存后自动通知本机守护热重载。不含 API Key。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "transcriber_provider": {"type": "string", "description": "可选：antigravity-flash / gemini-api / openai-compatible-asr / command-asr"},
                    "transcriber_base_url": {"type": "string"},
                    "transcriber_model": {"type": "string"},
                    "transcriber_command": {"type": "string"},
                    "transcriber_mode": {"type": "string", "enum": ["full_audio", "segment"]},
                    "analyzer_auto_provider": {"type": "string", "description": "可选：antigravity-agentapi / codex-cli / claude-cli / openai-compatible-llm / gemini-llm / none"},
                    "analyzer_base_url": {"type": "string"},
                    "analyzer_model": {"type": "string"},
                    "open_desktop_chat": {"type": "boolean", "description": "可选（codex-cli / claude-cli 生效）：分析完成后自动在 Codex / Claude 桌面版打开带正文的对话窗口（默认 true）"},
                    "codex_open_desktop_chat": {"type": "boolean", "description": "兼容别名，等价于 open_desktop_chat"},
                    "voice_memos_enabled": {"type": "boolean"},
                    "drop_folder_enabled": {"type": "boolean"},
                    "drop_folder_dir": {"type": "string"},
                    "autostart_enabled": {"type": "boolean", "description": "可选：开启或关闭 macOS 开机后台自启（launchd）"},
                    "rotation": {"type": "string", "enum": ["per_recording", "daily", "weekly", "single"], "description": "可选：新录音分析对话归类策略（默认 per_recording 每条新开独立对话 / daily 按天合并 / weekly 按周合并 / single 单窗复用）"},
                    "output_dir": {"type": "string", "description": "可选：复盘分析稿保存目录"},
                    "min_duration_sec": {"type": "number", "description": "可选：自动跳过短于该秒数的误触录音（如 10 或 30）"},
                },
            },
        },
        {
            "name": "vmr_check_update",
            "description": "检查 voice-memo-review 是否有新的 GitHub Release 版本与更新亮点（默认优先读 24 小时本地缓存，传 force_refresh=true 可实时检查）。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "force_refresh": {"type": "boolean", "default": False},
                },
            },
        },
    ]


class McpStdioServer:
    def __init__(self, cfg: VoiceReviewConfig) -> None:
        self.cfg = cfg
        self.store = StateStore(cfg.state_db)
        self.secrets = SecretStore(cfg.secrets_path)
        self.rules = RuleStudio(cfg.rules_dir)
        self.rules.ensure_initialized()
        self.executor = PipelineExecutor(cfg, store=self.store, secrets=self.secrets, rules=self.rules)

    def _dispatch_background_job(self, rec_id: str) -> str:
        """优先唤醒本机已运行的单守护处理（不重置运行中任务），否则拉起透传 --vmr-home 的独立 worker。"""
        info = discover_running_daemon(self.cfg.vmr_home)
        if info is not None:
            try:
                req = urllib.request.Request(
                    f"{info.base_url}/api/v1/jobs/wake",
                    data=b"{}",
                    headers={"Content-Type": "application/json", "X-VMR-Token": info.token},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    if resp.status == 200:
                        return "daemon"
            except Exception:
                pass
        cmd = [sys.executable, "-P", "-m", "voice_review.cli", "--vmr-home", str(self.cfg.vmr_home)]
        if self.cfg.config_path is not None and self.cfg.config_path.is_file():
            cmd += ["--config", str(self.cfg.config_path)]
        cmd += ["_worker-one", rec_id, "--no-analyze"]
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return "detached-worker"

    def _call_tool(self, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        if name == "vmr_status":
            auto_st = AutostartManager.default(self.cfg.vmr_home).status()
            from voice_review.updater import SelfUpdater
            cached_upd = SelfUpdater.default(self.cfg).read_cached_update()
            return {
                "version": __version__,
                "vmr_home": str(self.cfg.vmr_home),
                "preprocessor_mode": self.cfg.preprocessor_mode,
                "transcriber_provider": self.cfg.transcriber_provider,
                "analyzer_auto_provider": self.cfg.analyzer_auto_provider,
                "voice_memos_enabled": self.cfg.voice_memos_enabled,
                "drop_folder_enabled": self.cfg.drop_folder_enabled,
                "autostart_supported": auto_st.supported,
                "autostart_enabled": auto_st.enabled,
                "default_template": self.rules.get_template().id,
                "persona_configured": bool(self.rules.get_persona()),
                "glossary_configured": bool(self.rules.get_glossary()),
                "fluidaudio_ready": self.cfg.fluidaudio_bin is not None,
                "agentapi_ready": self.cfg.agentapi_bin.is_file(),
                "codex_ready": bool(resolve_codex_binary(self.cfg.codex_bin)),
                "claude_ready": bool(resolve_claude_binary(self.cfg.claude_bin)),
                "update_available": bool(cached_upd and cached_upd.has_update),
                "latest_version": cached_upd.latest_version if cached_upd else __version__,
                "release_notes": cached_upd.release_notes if cached_upd else "",
                "recent_count": len(self.store.list_recent(limit=50)),
            }, False

        if name == "vmr_check_update":
            from voice_review.updater import SelfUpdater
            upd = SelfUpdater.default(self.cfg)
            force_refresh = bool(args.get("force_refresh", False))
            info = upd.check_latest(use_cache=not force_refresh)
            return info.to_dict(), (not info.ok)

        if name == "vmr_get_profile":
            return {
                "default_template": self.rules.get_template().to_dict(),
                "persona_effective": self.rules.get_persona(),
                "persona_raw": self.rules.get_persona(raw=True),
                "glossary_effective": self.rules.get_glossary(),
                "glossary_raw": self.rules.get_glossary(raw=True),
                "config": config_to_dict(self.cfg),
                "secrets_masked": self.secrets.masked_all(),
            }, False

        if name == "vmr_update_profile":
            updated_fields: list[str] = []
            if "persona" in args and args["persona"] is not None:
                self.rules.save_persona(str(args["persona"]))
                updated_fields.append("persona")
            if "glossary" in args and args["glossary"] is not None:
                self.rules.save_glossary(str(args["glossary"]))
                updated_fields.append("glossary")
            if args.get("default_template_id"):
                tid = str(args["default_template_id"]).strip()
                tpl = self.rules.get_template(tid)
                self.rules.save_template(
                    tpl.id,
                    name=tpl.name,
                    description=tpl.description,
                    body=tpl.body,
                    is_default=True,
                )
                updated_fields.append("default_template_id")
            return {
                "ok": True,
                "updated": updated_fields,
                "persona_effective": self.rules.get_persona(),
                "glossary_effective": self.rules.get_glossary(),
                "default_template": self.rules.get_template().id,
            }, False

        if name == "vmr_update_settings":
            merged = config_to_dict(self.cfg)
            patch_for_daemon: dict[str, Any] = {}

            if "transcriber_provider" in args or any(
                k in args for k in ("transcriber_base_url", "transcriber_model", "transcriber_command", "transcriber_mode")
            ):
                new_t_prov = str(args.get("transcriber_provider") or self.cfg.transcriber_provider).strip().lower()
                same_t = new_t_prov == self.cfg.transcriber_provider
                t_obj: dict[str, Any] = dict(self.cfg.transcriber_settings) if same_t else {}
                t_obj["provider"] = new_t_prov
                if args.get("transcriber_base_url") is not None:
                    t_obj["base_url"] = str(args["transcriber_base_url"]).strip()
                if args.get("transcriber_model") is not None:
                    t_obj["model"] = str(args["transcriber_model"]).strip()
                if args.get("transcriber_command") is not None:
                    t_obj["command"] = str(args["transcriber_command"]).strip()
                if args.get("transcriber_mode") is not None:
                    t_obj["mode"] = str(args["transcriber_mode"]).strip()
                merged["transcriber"] = t_obj
                patch_for_daemon["transcriber"] = t_obj

            if "analyzer_auto_provider" in args or any(
                k in args for k in ("analyzer_base_url", "analyzer_model", "open_desktop_chat", "codex_open_desktop_chat")
            ):
                new_a_prov = str(args.get("analyzer_auto_provider") or self.cfg.analyzer_auto_provider).strip().lower()
                same_a = new_a_prov == self.cfg.analyzer_auto_provider
                a_obj: dict[str, Any] = dict(self.cfg.analyzer_settings) if same_a else {}
                a_obj["auto_provider"] = new_a_prov
                if args.get("analyzer_base_url") is not None:
                    a_obj["base_url"] = str(args["analyzer_base_url"]).strip()
                if args.get("analyzer_model") is not None:
                    a_obj["model"] = str(args["analyzer_model"]).strip()
                raw_open = (
                    args["open_desktop_chat"]
                    if args.get("open_desktop_chat") is not None
                    else args.get("codex_open_desktop_chat")
                )
                if raw_open is not None and new_a_prov in ("codex-cli", "claude-cli"):
                    a_obj["open_desktop_chat"] = bool(raw_open)
                merged["analyzer"] = a_obj
                patch_for_daemon["analyzer"] = a_obj

            src_patch: dict[str, Any] = {}
            if "voice_memos_enabled" in args and args["voice_memos_enabled"] is not None:
                src_patch["voice_memos_enabled"] = bool(args["voice_memos_enabled"])
            if "drop_folder_enabled" in args and args["drop_folder_enabled"] is not None:
                src_patch["drop_folder_enabled"] = bool(args["drop_folder_enabled"])
            if args.get("drop_folder_dir"):
                src_patch["drop_folder_dir"] = str(args["drop_folder_dir"]).strip()
            if src_patch:
                merged.setdefault("sources", {}).update(src_patch)
                patch_for_daemon["sources"] = src_patch

            if args.get("rotation") in ("daily", "weekly", "per_recording", "single"):
                merged["rotation"] = str(args["rotation"])
                patch_for_daemon["rotation"] = merged["rotation"]
            if args.get("output_dir"):
                out_p = str(Path(str(args["output_dir"])).expanduser().resolve())
                merged["output_dir"] = out_p
                merged["transcripts_dir"] = str(Path(out_p) / "_转录稿")
                patch_for_daemon["output_dir"] = out_p
                patch_for_daemon["transcripts_dir"] = merged["transcripts_dir"]
            if args.get("min_duration_sec") is not None:
                val_min = max(1.0, float(args["min_duration_sec"]))
                merged["min_duration_sec"] = val_min
                patch_for_daemon["min_duration_sec"] = val_min

            t_prov_final = str((merged.get("transcriber") or {}).get("provider") or self.cfg.transcriber_provider)
            a_prov_final = str((merged.get("analyzer") or {}).get("auto_provider") or self.cfg.analyzer_auto_provider)
            ok_combo, combo_msg = validate_provider_combo(t_prov_final, a_prov_final)
            if not ok_combo:
                return {"ok": False, "error": combo_msg}, True

            cfg_target = self.cfg.config_path or (self.cfg.vmr_home / "config.toml")
            save_config_dict(cfg_target, merged)
            self.cfg = load_config(cfg_target, vmr_home=self.cfg.vmr_home)
            self.executor.reload_config(self.cfg)

            daemon_reloaded = False
            info = discover_running_daemon(self.cfg.vmr_home)
            if info is not None and patch_for_daemon:
                try:
                    req = urllib.request.Request(
                        f"{info.base_url}/api/v1/config",
                        data=json.dumps(patch_for_daemon, ensure_ascii=False).encode("utf-8"),
                        headers={"Content-Type": "application/json", "X-VMR-Token": info.token},
                        method="PUT",
                    )
                    with urllib.request.urlopen(req, timeout=3.0) as resp:
                        daemon_reloaded = resp.status == 200
                except Exception:
                    daemon_reloaded = False

            autostart_note = ""
            if "autostart_enabled" in args and args["autostart_enabled"] is not None:
                want_auto = bool(args["autostart_enabled"])
                mgr = AutostartManager.default(self.cfg.vmr_home)
                if info is not None:
                    try:
                        req_a = urllib.request.Request(
                            f"{info.base_url}/api/v1/autostart",
                            data=json.dumps({"enabled": want_auto}).encode("utf-8"),
                            headers={"Content-Type": "application/json", "X-VMR-Token": info.token},
                            method="PUT",
                        )
                        with urllib.request.urlopen(req_a, timeout=5.0) as resp_a:
                            autostart_note = json.loads(resp_a.read().decode("utf-8")).get("message", "")
                    except Exception as exc:
                        autostart_note = f"通过守护切换自启失败：{exc}"
                else:
                    if want_auto:
                        _, autostart_note = mgr.enable(load_now=True)
                    else:
                        _, autostart_note = mgr.disable(unload_now=True)

            return {
                "ok": True,
                "config_path": str(cfg_target),
                "transcriber_provider": self.cfg.transcriber_provider,
                "analyzer_auto_provider": self.cfg.analyzer_auto_provider,
                "voice_memos_enabled": self.cfg.voice_memos_enabled,
                "drop_folder_enabled": self.cfg.drop_folder_enabled,
                "rotation": self.cfg.rotation,
                "output_dir": str(self.cfg.output_dir),
                "min_duration_sec": self.cfg.min_duration_sec,
                "autostart_enabled": AutostartManager.default(self.cfg.vmr_home).status().enabled,
                "autostart_message": autostart_note,
                "daemon_reloaded": daemon_reloaded,
            }, False

        if name == "vmr_list_recordings":
            recs = self.store.list_recent(limit=int(args.get("limit", 20)), status=args.get("status"))
            return {"recordings": [r.to_dict() for r in recs]}, False

        if name == "vmr_list_templates":
            return {"templates": [t.to_dict() for t in self.rules.list_templates()]}, False

        if name == "vmr_get_template":
            tpl = self.rules.get_template(args.get("template_id"))
            return tpl.to_dict(), False

        if name == "vmr_save_template":
            tpl = self.rules.save_template(
                str(args["template_id"]),
                name=str(args["name"]),
                description=str(args.get("description") or ""),
                body=str(args["body"]),
                is_default=bool(args.get("is_default", False)),
            )
            return {"saved": True, "template": tpl.to_dict()}, False

        if name == "vmr_save_analysis":
            rec_id = str(args["recording_id"])
            rec = self.store.get(rec_id)
            if rec is None:
                return {"error": f"未找到录音记录：{rec_id}"}, True
            _, analysis_path, _ = self.executor._build_paths(rec)
            analysis_path.parent.mkdir(parents=True, exist_ok=True)
            analysis_path.write_text(str(args["markdown"]).rstrip() + "\n", encoding="utf-8")
            self.store.update(
                rec_id,
                status="done",
                analysis_path=str(analysis_path),
                template_id=str(args.get("template_id") or rec.template_id or self.cfg.default_template_id),
                last_error="",
                reset_retry=True,
            )
            self.executor.extensions.run_after_analysis(
                {
                    "recording_id": rec_id,
                    "title": rec.title,
                    "recorded_at": rec.recorded_at,
                    "duration_sec": rec.duration_sec,
                    "transcript_path": rec.transcript_path,
                    "analysis_path": str(analysis_path),
                    "template_id": str(args.get("template_id") or rec.template_id or self.cfg.default_template_id),
                    "vmr_home": str(self.cfg.vmr_home),
                    "output_dir": str(self.cfg.output_dir),
                }
            )
            return {"ok": True, "recording_id": rec_id, "analysis_path": str(analysis_path)}, False

        if name == "vmr_manage_voiceprints":
            action = str(args.get("action") or "list")
            vp_store = VoiceprintStore(self.cfg.voiceprints_path)
            if action == "list":
                return {"voiceprints": [{"name": n, "samples": c} for n, c in vp_store.list_summary()]}, False
            if action == "delete":
                ok = vp_store.delete(str(args.get("name") or ""))
                return {"ok": ok}, not ok
            if action == "enroll":
                diarizer = FluidAudioDiarizer(
                    fluidaudio_bin=self.cfg.fluidaudio_bin,
                    ffmpeg_bin=self.cfg.ffmpeg_bin,
                    voiceprints_path=self.cfg.voiceprints_path,
                )
                count, talk_sec = diarizer.enroll_from_audio(str(args["name"]), Path(str(args["audio_path"])))
                return {"ok": True, "name": args["name"], "samples": count, "talk_sec": round(talk_sec, 1)}, False
            return {"error": f"不支持的动作：{action}"}, True

        if name == "vmr_process_audio":
            audio_p = Path(str(args["audio_path"])).expanduser().resolve()
            if not audio_p.is_file():
                return {"error": f"音频文件不存在：{audio_p}"}, True
            tpl_id = str(args.get("template_id") or self.cfg.default_template_id)
            rec = self.executor.submit_single_file(
                audio_p,
                title=str(args.get("title") or ""),
                template_id=tpl_id,
                auto_analyze=False,
            )
            dur = probe_duration_sec(audio_p, self.cfg.ffprobe_bin) or rec.duration_sec

            # antigravity-flash 仅本地切段速度快；或极短音频直接同步完成；中长直连转写异步托管防 MCP 超时
            is_long_direct = self.cfg.transcriber_provider != "antigravity-flash" and dur > ASYNC_DURATION_THRESHOLD_SEC
            if is_long_direct:
                runner_kind = self._dispatch_background_job(rec.recording_id)
                return {
                    "status": "running",
                    "job_id": rec.recording_id,
                    "runner": runner_kind,
                    "duration_sec": round(dur, 1),
                    "message": "长录音已提交后台独立托管转写，请每隔 10~15 秒调用 vmr_get_job_status 查询包级进度。",
                }, False

            final_st = self.executor.process_one(rec, auto_analyze=False)
            return self._build_job_status_payload(rec.recording_id, template_id=tpl_id), (final_st == "failed")

        if name == "vmr_get_job_status":
            job_id = str(args["job_id"])
            payload = self._build_job_status_payload(job_id, template_id=args.get("template_id"))
            return payload, ("error" in payload and not payload.get("recording_id"))

        return {"error": f"未知工具：{name}"}, True

    def _build_job_status_payload(self, job_id: str, *, template_id: str | None = None) -> dict[str, Any]:
        rec = self.store.get(job_id)
        if rec is None:
            return {"error": f"未找到任务：{job_id}"}
        payload: dict[str, Any] = {
            "recording_id": rec.recording_id,
            "title": rec.title,
            "status": rec.status,
            "progress_done": rec.progress_done,
            "progress_total": rec.progress_total,
            "transcript_path": rec.transcript_path,
            "analysis_path": rec.analysis_path,
            "workflow_script_path": rec.workflow_script_path,
            "last_error": rec.last_error,
        }
        if rec.status in ("awaiting_analysis", "transcribed", "prepared", "done"):
            tpl_id = template_id or rec.template_id or self.cfg.default_template_id
            extra_sections = self.executor.extensions.run_before_prompt(
                {
                    "recording_id": rec.recording_id,
                    "title": rec.title,
                    "recorded_at": rec.recorded_at,
                    "duration_sec": rec.duration_sec,
                    "speakers_summary": rec.speakers_summary,
                    "transcript_path": rec.transcript_path,
                    "analysis_path": rec.analysis_path,
                    "template_id": tpl_id,
                    "vmr_home": str(self.cfg.vmr_home),
                    "output_dir": str(self.cfg.output_dir),
                }
            )
            payload["analysis_prompt"] = self.rules.compose_analysis_prompt(
                template_id=tpl_id,
                title=rec.title,
                recorded_at=rec.recorded_at,
                duration_sec=rec.duration_sec,
                speakers_summary=rec.speakers_summary,
                transcript_path=Path(rec.transcript_path) if rec.transcript_path else None,
                analysis_path=Path(rec.analysis_path) if rec.analysis_path else None,
                extra_sections=extra_sections,
            )
            if rec.workflow_script_path:
                payload["next_step"] = (
                    f"若当前宿主为 Antigravity，请先运行 run_workflow(ScriptPath=\"{rec.workflow_script_path}\") 完成精转；"
                    "若当前宿主为 Claude Code / Codex / Cursor，请切换 transcriber.provider 为 gemini-api / openai-compatible-asr / command-asr。"
                )
            else:
                payload["next_step"] = (
                    f"请读取转录稿 {rec.transcript_path}，按 analysis_prompt 中的显化规则完成分析，"
                    f"然后调用 vmr_save_analysis(recording_id=\"{rec.recording_id}\", markdown=...) 落盘。"
                )
        return payload

    def _list_resources(self) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = [
            {"uri": "vmr://glossary", "name": "专有名词校对词表", "mimeType": "text/plain"},
            {"uri": "vmr://config/summary", "name": "当前四插槽脱敏配置", "mimeType": "application/json"},
        ]
        for tpl in self.rules.list_templates():
            resources.append(
                {
                    "uri": f"vmr://templates/{tpl.id}",
                    "name": f"分析模板：{tpl.name}",
                    "description": tpl.description,
                    "mimeType": "text/markdown",
                }
            )
        return resources

    def _read_resource(self, uri: str) -> dict[str, Any]:
        if uri == "vmr://glossary":
            return {"contents": [{"uri": uri, "mimeType": "text/plain", "text": self.rules.get_glossary()}]}
        if uri == "vmr://config/summary":
            summary = {"config": config_to_dict(self.cfg), "secrets": self.secrets.masked_all()}
            return {
                "contents": [
                    {"uri": uri, "mimeType": "application/json", "text": json.dumps(summary, ensure_ascii=False, indent=2)}
                ]
            }
        if uri.startswith("vmr://templates/"):
            tid = uri[len("vmr://templates/") :].strip()
            tpl = self.rules.get_template(tid)
            return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": tpl.path.read_text(encoding="utf-8")}]}
        raise FileNotFoundError(f"未知资源 URI：{uri}")

    def handle_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = str(msg.get("method") or "")
        req_id = msg.get("id")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

        if method == "notifications/initialized" or (req_id is None and method.startswith("notifications/")):
            return None

        try:
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}, "resources": {}},
                        "serverInfo": {"name": "voice-memo-review", "version": __version__},
                    },
                }
            if method == "ping":
                return {"jsonrpc": "2.0", "id": req_id, "result": {}}
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": _tool_defs()}}
            if method == "tools/call":
                tool_name = str(params.get("name") or "")
                arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                data, is_err = self._call_tool(tool_name, arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
                        "isError": is_err,
                    },
                }
            if method == "resources/list":
                return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": self._list_resources()}}
            if method == "resources/read":
                res = self._read_resource(str(params.get("uri") or ""))
                return {"jsonrpc": "2.0", "id": req_id, "result": res}
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    def serve_stdio(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        in_stream = stdin or sys.stdin
        out_stream = stdout or sys.stdout
        for raw_line in in_stream:
            line = raw_line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            resp = self.handle_message(msg)
            if resp is not None:
                out_stream.write(json.dumps(resp, ensure_ascii=False) + "\n")
                out_stream.flush()
