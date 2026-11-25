"""零第三方依赖的标准 Stdio MCP Server（JSON-RPC 2.0，兼容 Antigravity / Claude Code / Codex / Cursor）。"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any, TextIO
import urllib.request

from voice_review import __version__
from voice_review.config import VoiceReviewConfig, config_to_dict
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
            "description": "查看本地语音复盘环境状态、当前启用的转写/分析插槽配置与队列概览。",
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
            return {
                "version": __version__,
                "vmr_home": str(self.cfg.vmr_home),
                "preprocessor_mode": self.cfg.preprocessor_mode,
                "transcriber_provider": self.cfg.transcriber_provider,
                "analyzer_auto_provider": self.cfg.analyzer_auto_provider,
                "default_template": self.rules.get_template().id,
                "fluidaudio_ready": self.cfg.fluidaudio_bin is not None,
                "recent_count": len(self.store.list_recent(limit=50)),
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
            payload["analysis_prompt"] = self.rules.compose_analysis_prompt(
                template_id=tpl_id,
                title=rec.title,
                recorded_at=rec.recorded_at,
                duration_sec=rec.duration_sec,
                speakers_summary=rec.speakers_summary,
                transcript_path=Path(rec.transcript_path) if rec.transcript_path else None,
                analysis_path=Path(rec.analysis_path) if rec.analysis_path else None,
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
