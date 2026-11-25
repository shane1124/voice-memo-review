"""voice-memo-review 统一命令行入口（vmr）。"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import webbrowser

from voice_review import __version__
from voice_review.autostart import AutostartManager, run_restart_helper, spawn_restart_helper
from voice_review.config import VoiceReviewConfig, load_config, validate_provider_combo
from voice_review.daemon import (
    DaemonAlreadyRunningError,
    PipelineExecutor,
    VmrDaemon,
    discover_running_daemon,
)
from voice_review.diarizer import FluidAudioDiarizer, VoiceprintStore
from voice_review.extensions import ExtensionManager
from voice_review.installer import ClientInstaller
from voice_review.mcp_server import McpStdioServer
from voice_review.rules import RuleStudio
from voice_review.updater import SelfUpdater
from voice_review.voice_memos import SHANGHAI_TZ
from voice_review.workflow_builder import prepare_v2_job


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def _probe_voice_memos_access(recordings_dir: Path) -> tuple[bool, str]:
    if not recordings_dir.is_dir():
        return False, f"目录不存在：{recordings_dir}"
    try:
        os.listdir(recordings_dir)
        return True, f"可读：{recordings_dir}（自动同步需已开启 iCloud 且空间充足）"
    except PermissionError:
        real_py = os.path.realpath(sys.executable)
        return (
            False,
            f"无读取权限（系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 点＋按 ⌘⇧G 粘贴并勾选：{real_py}）",
        )
    except OSError as exc:
        return False, f"访问异常：{exc}"


def _detect_local_asr_tools() -> list[str]:
    found: list[str] = []
    for name in ("whisper-cli", "whisper", "mlx-qwen3-asr"):
        hit = shutil.which(name)
        if hit:
            found.append(f"{name} ({hit})")
    return found


def _cmd_doctor(cfg: VoiceReviewConfig) -> int:
    ok_combo, combo_msg = validate_provider_combo(cfg.transcriber_provider, cfg.analyzer_auto_provider)
    autostart_st = AutostartManager.default(cfg.vmr_home).status()
    vm_ok, vm_detail = _probe_voice_memos_access(cfg.recordings_dir)
    local_asrs = _detect_local_asr_tools()
    checks = [
        ("ffmpeg", shutil.which(cfg.ffmpeg_bin) is not None or Path(cfg.ffmpeg_bin).is_file(), cfg.ffmpeg_bin),
        ("ffprobe", shutil.which(cfg.ffprobe_bin) is not None or Path(cfg.ffprobe_bin).is_file(), cfg.ffprobe_bin),
        ("FluidAudio (fluidaudiocli)", cfg.fluidaudio_bin is not None, str(cfg.fluidaudio_bin or "未找到（可设 preprocessor=none）")),
        ("Antigravity agentapi", cfg.agentapi_bin.is_file(), str(cfg.agentapi_bin)),
        ("Codex CLI（免 Key 分析）", shutil.which(cfg.codex_bin) is not None or Path(cfg.codex_bin).is_file(), cfg.codex_bin),
        ("Claude Code CLI（免 Key 分析）", shutil.which(cfg.claude_bin) is not None or Path(cfg.claude_bin).is_file(), cfg.claude_bin),
        ("苹果语音备忘录读取权限", vm_ok, vm_detail),
        ("开机后台自启（launchd）", autostart_st.enabled, ("已开启" + (" · 运行托管中" if autostart_st.loaded else " · 下次开机生效")) if autostart_st.enabled else "未开启（可用 vmr autostart on 开启）"),
        ("插槽组合校验", ok_combo, f"{cfg.transcriber_provider} + {cfg.analyzer_auto_provider} ({combo_msg})"),
    ]
    all_ok = True
    print(f"=== voice-memo-review v{__version__} 环境体检 ===")
    print(f"  数据目录：{cfg.vmr_home}")
    print(f"  Python 真实路径（完全磁盘访问权限授权对象）：{os.path.realpath(sys.executable)}")
    if local_asrs:
        print(f"  本机已检出离线转写 CLI：{', '.join(local_asrs)}")
    for name, ok, detail in checks:
        mark = "✅" if ok else "❌"
        print(f"  {mark} {name:<28} {detail}")
        if not ok and name in ("ffmpeg", "插槽组合校验"):
            all_ok = False
    store = VoiceprintStore(cfg.voiceprints_path)
    vps = store.list_summary()
    print(
        f"  🎙️  本地声纹库 ({cfg.voiceprints_path}): {len(vps)} 人"
        + (f" -> {', '.join(f'{n}({c})' for n, c in vps)}" if vps else "（尚未注册，可用 vmr enroll 或控制台录入）")
    )
    rules = RuleStudio(cfg.rules_dir)
    rules.ensure_initialized()
    print(f"  📋 显化规则模板 ({cfg.rules_dir}): {len(rules.list_templates())} 套（默认：{rules.get_template().id}）")
    exts = ExtensionManager.from_vmr_home(cfg.vmr_home).list_extensions()
    if exts:
        enabled_cnt = sum(1 for e in exts if e.enabled)
        names_txt = ", ".join(f"{e.id}({'开' if e.enabled else '关'})" for e in exts)
        print(f"  🧩 本机私有扩展 ({cfg.vmr_home / 'extensions'}): {enabled_cnt}/{len(exts)} 已启用 -> {names_txt}")
    return 0 if all_ok else 1


def _cmd_enroll(cfg: VoiceReviewConfig, name: str, audio: Path) -> int:
    diarizer = FluidAudioDiarizer(
        fluidaudio_bin=cfg.fluidaudio_bin,
        ffmpeg_bin=cfg.ffmpeg_bin,
        voiceprints_path=cfg.voiceprints_path,
    )
    count, talk_sec = diarizer.enroll_from_audio(name, audio)
    print(f"✅ 已录入声纹：{name}（有效人声 {talk_sec:.1f}s，当前累计样本 {count} 条）-> {cfg.voiceprints_path}")
    return 0


def _cmd_voiceprints(cfg: VoiceReviewConfig, delete_name: str | None) -> int:
    store = VoiceprintStore(cfg.voiceprints_path)
    if delete_name:
        if store.delete(delete_name):
            print(f"✅ 已从声纹库删除：{delete_name}")
            return 0
        print(f"⚠️ 声纹库中未找到：{delete_name}")
        return 1
    items = store.list_summary()
    if not items:
        print(f"声纹库为空（{cfg.voiceprints_path}）。示例：vmr enroll \"我\" 自我介绍15秒.m4a")
        return 0
    print(f"当前声纹库（{cfg.voiceprints_path}）：")
    for name, cnt in items:
        print(f"  - {name}：{cnt} 条样本")
    return 0


def _cmd_prep(cfg: VoiceReviewConfig, audio: Path, out_dir: Path | None, context: str, num_speakers: int | None) -> int:
    src = audio.expanduser().resolve()
    target_out = out_dir.expanduser().resolve() if out_dir else cfg.output_dir
    target_out.mkdir(parents=True, exist_ok=True)
    job_dir = cfg.work_dir / "v2_jobs" / src.stem
    transcript_path = target_out / f"{src.stem}｜转录稿.md"
    rules = RuleStudio(cfg.rules_dir)
    rules.ensure_initialized()

    diarizer = FluidAudioDiarizer(
        fluidaudio_bin=cfg.fluidaudio_bin,
        ffmpeg_bin=cfg.ffmpeg_bin,
        voiceprints_path=cfg.voiceprints_path,
    )
    job = prepare_v2_job(
        src,
        job_dir=job_dir,
        transcript_path=transcript_path,
        diarizer=diarizer,
        seg_config=cfg.segmenter,
        title=src.stem,
        recorded_at=datetime.now(SHANGHAI_TZ).replace(microsecond=0),
        asr_context=context or rules.get_glossary() or cfg.asr_context,
        num_speakers=num_speakers,
        cleanup_clips=True,
    )
    print(f"\n✅ V2 本地物理切段完成（耗时 {job.prep_sec}s）！")
    print(f"   - 物理发言段：{job.segment_count} 段 → 打包为 {job.batch_count} 个并发小包（裂变核对 {job.split_check_count} 组）")
    print(f"   - 说话人概况：\n{job.speakers_summary}")
    print(f"   - Antigravity 工作流脚本：{job.workflow_script_path}")
    print(f"   - 输出转录稿目标路径：{job.transcript_path}")
    return 0


def _cmd_run(cfg: VoiceReviewConfig, audio: Path, template_id: str, no_analyze: bool) -> int:
    def _on_ev(ev: str, data: dict) -> None:
        if ev == "progress":
            print(f"  [进度] {data.get('done', 0)}/{data.get('total', 0)} 包已完成", flush=True)
        elif ev == "job":
            print(f"  [状态] {data.get('status')} {data.get('error', '')}".strip(), flush=True)

    executor = PipelineExecutor(cfg, on_event=_on_ev)
    rec = executor.submit_single_file(audio, template_id=template_id)
    print(f"▶ 开始处理：{rec.title}（ID={rec.recording_id}，转写={cfg.transcriber_provider}，分析={cfg.analyzer_auto_provider}）")
    final_st = executor.process_one(rec, auto_analyze=not no_analyze)
    updated = executor.store.get(rec.recording_id)
    if final_st == "done" and updated:
        print(f"✅ 处理完成！\n   - 转录稿：{updated.transcript_path}\n   - 分析稿：{updated.analysis_path}")
        return 0
    if final_st == "awaiting_analysis" and updated:
        print(f"✅ 转写已完成（等待手动或 MCP 接手分析）：\n   - 转录稿：{updated.transcript_path}")
        return 0
    if final_st == "skipped":
        print("⏭️  未检测到有效人声（纯静音/环境音），已优雅跳过。")
        return 0
    err = updated.last_error if updated else ""
    print(f"❌ 处理未完成（status={final_st}）：{err}")
    return 1


def _cmd_ui(cfg: VoiceReviewConfig, port: int | None, no_open: bool) -> int:
    running = discover_running_daemon(cfg.vmr_home)
    if running is not None:
        print(f"✅ 检测到本地单守护已在运行（PID={running.pid}），直接复用控制台：{running.base_url}")
        if not no_open:
            webbrowser.open(running.ui_url)
        return 0
    daemon = VmrDaemon(cfg, port=port, start_watcher=True)
    try:
        info = daemon.start_in_thread()
    except DaemonAlreadyRunningError as exc:
        print(f"⚠️ {exc}")
        return 1
    print(f"🚀 voice-memo-review 控制台已启动：{info.base_url}")
    if not no_open:
        webbrowser.open(info.ui_url)
    print("按 Ctrl+C 停止服务。")
    try:
        daemon._stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
    return 0


def _cmd_install(targets: list[str] | None, uninstall: bool) -> int:
    inst = ClientInstaller.default()
    res = inst.uninstall(targets=targets) if uninstall else inst.install(targets=targets)
    action = "卸载" if uninstall else "接入"
    all_ok = True
    for client, item in res.items():
        mark = "✅" if item.ok else "❌"
        print(f"  {mark} [{client}] {item.message} -> {item.config_path}")
        if not item.ok:
            all_ok = False
    if not res:
        print(f"未匹配到需要{action}的客户端。")
    return 0 if all_ok else 1


def _cmd_extensions(cfg: VoiceReviewConfig, action: str, ext_id: str | None) -> int:
    mgr = ExtensionManager.from_vmr_home(cfg.vmr_home)
    if action == "list":
        exts = mgr.list_extensions()
        if not exts:
            print(f"当前无本机私有扩展（目录：{mgr.extensions_dir}）。")
            return 0
        print(f"本机私有扩展（{mgr.extensions_dir}）：")
        for e in exts:
            mark = "✅ 启用" if e.enabled else "⏸️ 停用"
            err = f" [错误：{e.error}]" if e.error else ""
            print(f"  - {e.id:<20} {mark}  {e.name} — {e.description}{err}")
        return 0
    if action in ("enable", "disable"):
        if not ext_id:
            print("❌ 请指定扩展 ID，例如：vmr extensions enable my_ext")
            return 1
        try:
            updated = mgr.set_enabled(ext_id, action == "enable")
            print(f"✅ 已{'启用' if updated.enabled else '停用'}本机扩展：{updated.id}（{updated.name}）")
            return 0
        except Exception as exc:
            print(f"❌ {exc}")
            return 1
    print(f"❌ 未知动作：{action}")
    return 1


def _cmd_autostart(cfg: VoiceReviewConfig, action: str) -> int:
    mgr = AutostartManager.default(cfg.vmr_home)
    if action == "status":
        st = mgr.status()
        if not st.supported:
            print("⚠️ 开机自启目前仅支持 macOS（launchd）。")
            return 0
        if st.enabled:
            state_txt = "已开启且正在由 launchd 后台托管" if st.loaded else "已写入配置（待加载或下次开机生效）"
            print(f"✅ 开机后台自启：{state_txt}\n   - 配置文件：{st.plist_path}")
        else:
            print("ℹ️  开机后台自启：未开启。运行 `vmr autostart on` 即可开启。")
        return 0
    if action == "on":
        running = discover_running_daemon(cfg.vmr_home)
        if running is not None:
            # 保留既有控制台令牌，无缝交给 launchd 接管
            from voice_review.autostart import write_restart_token
            write_restart_token(cfg.vmr_home, running.token)
            ok, msg = mgr.enable(load_now=True, stop_existing_pid=running.pid)
        else:
            ok, msg = mgr.enable(load_now=True)
        print(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1
    if action == "off":
        ok, msg = mgr.disable(unload_now=True)
        print(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1
    print(f"❌ 未知动作：{action}")
    return 1


def _cmd_update(cfg: VoiceReviewConfig, check_only: bool, archive: str | None, sha256: str, force: bool = False) -> int:
    upd = SelfUpdater.default(cfg)
    if archive:
        res = upd.apply_update(archive, expected_sha256=sha256)
        print(("✅ " if res.ok else "❌ ") + res.message)
        return 0 if res.ok else 1
    info = upd.check_latest(use_cache=False)
    if not info.ok:
        print(f"❌ {info.error}")
        return 1
    if not info.has_update:
        print(f"✅ 当前已是最新版本 v{info.current_version}（发布源：{cfg.update_repo}）。")
        return 0
    print(f"✨ 发现新版本 v{info.latest_version}（当前 v{info.current_version}）：{info.release_url}")
    if info.release_notes:
        print(f"   更新亮点：{info.release_notes}")
    if check_only:
        return 0
    if not info.download_url:
        print("⚠️ 最新 Release 未附带 macOS 离线压缩包资产。")
        return 1

    from voice_review.state import StateStore
    st_store = StateStore(cfg.state_db)
    active = [r for r in st_store.list_recent(limit=50) if r.status in ("preparing", "transcribing", "analyzing")]
    if active and not force:
        print(f"⚠️ 当前有 {len(active)} 条录音正在后台处理中，为避免中断已暂停升级；请等待完成或加 --force 强制升级。")
        return 1

    res = upd.apply_update(info.download_url, expected_sha256=info.sha256)
    print(("✅ " if res.ok else "❌ ") + res.message)
    if not res.ok:
        return 1

    # 如本机已有守护进程在运行，自动安全热重启并保留会话令牌
    running = discover_running_daemon(cfg.vmr_home)
    mgr = AutostartManager.default(cfg.vmr_home)
    if running is not None:
        spawn_restart_helper(
            vmr_bin=mgr.vmr_bin,
            vmr_home=cfg.vmr_home,
            old_pid=running.pid,
            token=running.token,
            use_launchd=mgr.status().enabled,
        )
        print("🔄 已触发后台单守护自动热重启，控制台刷新后即切换到新版本。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vmr",
        description=f"voice-memo-review v{__version__}：规则显化、四插槽可插拔录音自动分析引擎与跨 AI 插件",
    )
    parser.add_argument("--config", type=Path, default=None, help="配置文件路径（默认读取 VMR_HOME/config.toml）")
    parser.add_argument("--vmr-home", type=Path, default=None, help="用户数据目录（默认 ~/.config/voice-memo-review）")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="检查环境、插槽组合、显化模板与声纹库")

    p_ui = sub.add_parser("ui", help="启动或复用本地单守护并打开可视化控制台")
    p_ui.add_argument("--port", type=int, default=None, help="指定本地回环端口")
    p_ui.add_argument("--no-open", action="store_true", help="仅启动服务不自动打开浏览器")

    p_serve = sub.add_parser("serve", help="前台运行统一单守护服务（HTTP/SSE + 监听）")
    p_serve.add_argument("--port", type=int, default=None)

    sub.add_parser("watch", help="无头常驻监听语音备忘录与投放文件夹（复用单守护）")
    sub.add_parser("mcp", help="启动标准 Stdio MCP Server（供 Antigravity / Claude Code / Codex / Cursor 调用）")

    p_run = sub.add_parser("run", help="单文件一键完成预处理、转写与分析")
    p_run.add_argument("audio", type=Path, help="本地音频文件路径")
    p_run.add_argument("--template", default="", help="指定显化分析模板 ID")
    p_run.add_argument("--no-analyze", action="store_true", help="仅转写落盘，留待手动或 MCP 分析")

    p_inst = sub.add_parser("install", help="一键把 MCP 与通用 Skill 接入本机已安装的 AI 客户端")
    p_inst.add_argument("--target", action="append", choices=("antigravity", "claude", "codex", "cursor"), help="指定目标客户端（可重复，不填自动探测）")

    p_uninst = sub.add_parser("uninstall", help="从 AI 客户端无损移除 MCP 与 Skill")
    p_uninst.add_argument("--target", action="append", choices=("antigravity", "claude", "codex", "cursor"))

    p_upd = sub.add_parser("update", help="从公开 GitHub Releases 检查并安全更新程序本体")
    p_upd.add_argument("--check", action="store_true", help="仅检查是否有新版本")
    p_upd.add_argument("--archive", default=None, help="使用本地离线更新包路径更新")
    p_upd.add_argument("--sha256", default="", help="配合 --archive 的 SHA256 校验值")
    p_upd.add_argument("--force", action="store_true", help="即使有任务正在处理中也强制更新")

    p_auto = sub.add_parser("autostart", help="管理 macOS 开机后台自启（launchd LaunchAgent）")
    p_auto.add_argument("action", nargs="?", default="status", choices=("on", "off", "status"), help="on 开启 / off 关闭 / status 查看状态")

    p_ext = sub.add_parser("extensions", help="查看或启停本机私有扩展模块（位于 VMR_HOME/extensions）")
    p_ext.add_argument("action", nargs="?", default="list", choices=("list", "enable", "disable"))
    p_ext.add_argument("ext_id", nargs="?", default=None)

    p_enroll = sub.add_parser("enroll", help="从 10~30 秒清晰单人样音注册声纹")
    p_enroll.add_argument("name", help="说话人名称")
    p_enroll.add_argument("audio", type=Path, help="单人清晰样音文件")

    p_vp = sub.add_parser("voiceprints", help="查看或删除已注册声纹")
    p_vp.add_argument("--delete", dest="delete_name", default=None)

    p_prep = sub.add_parser("prep", help="单文件仅跑本地声纹切段并生成 Antigravity 并发工作流")
    p_prep.add_argument("audio", type=Path)
    p_prep.add_argument("--out", type=Path, default=None)
    p_prep.add_argument("--context", default="")
    p_prep.add_argument("--num-speakers", type=int, default=None)

    # 供 MCP 长音频异步托管拉起的内部单任务执行器
    p_wone = sub.add_parser("_worker-one", help=argparse.SUPPRESS)
    p_wone.add_argument("recording_id")
    p_wone.add_argument("--no-analyze", action="store_true")

    # 供热更新与自启切换使用的内部重启接力器
    p_rst = sub.add_parser("_restart-helper", help=argparse.SUPPRESS)
    p_rst.add_argument("--old-pid", type=int, default=0)
    p_rst.add_argument("--use-launchd", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd != "mcp":
        _setup_logging(args.verbose)

    cfg_path = args.config
    if cfg_path is None and args.vmr_home is None:
        local_cfg = Path.cwd() / "config.toml"
        if local_cfg.is_file():
            cfg_path = local_cfg
    cfg = load_config(cfg_path, vmr_home=args.vmr_home)

    if args.cmd == "doctor":
        return _cmd_doctor(cfg)
    if args.cmd == "enroll":
        return _cmd_enroll(cfg, args.name, args.audio)
    if args.cmd == "voiceprints":
        return _cmd_voiceprints(cfg, args.delete_name)
    if args.cmd == "prep":
        return _cmd_prep(cfg, args.audio, args.out, args.context, args.num_speakers)
    if args.cmd == "run":
        return _cmd_run(cfg, args.audio, args.template, args.no_analyze)
    if args.cmd == "ui":
        return _cmd_ui(cfg, args.port, args.no_open)
    if args.cmd in ("serve", "watch"):
        port = getattr(args, "port", None)
        VmrDaemon(cfg, port=port, start_watcher=True).serve_forever()
        return 0
    if args.cmd == "mcp":
        McpStdioServer(cfg).serve_stdio()
        return 0
    if args.cmd == "install":
        return _cmd_install(args.target, uninstall=False)
    if args.cmd == "uninstall":
        return _cmd_install(args.target, uninstall=True)
    if args.cmd == "update":
        return _cmd_update(cfg, args.check, args.archive, args.sha256, force=args.force)
    if args.cmd == "autostart":
        return _cmd_autostart(cfg, args.action)
    if args.cmd == "extensions":
        return _cmd_extensions(cfg, args.action, args.ext_id)
    if args.cmd == "_restart-helper":
        mgr = AutostartManager.default(cfg.vmr_home)
        return run_restart_helper(
            vmr_bin=mgr.vmr_bin,
            vmr_home=cfg.vmr_home,
            old_pid=args.old_pid,
            use_launchd=args.use_launchd,
        )
    if args.cmd == "_worker-one":
        executor = PipelineExecutor(cfg)
        rec = executor.store.get(args.recording_id)
        if rec is None:
            return 1
        executor.process_one(rec, auto_analyze=not args.no_analyze)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
