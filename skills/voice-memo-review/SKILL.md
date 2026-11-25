---
name: voice-memo-review
description: 跨 AI 可插拔语音复盘技能。用于处理本地录音文件或苹果语音备忘录、查看/修改显化分析规则模板、管理本地声纹名册，并按透明模板产出结构化复盘分析稿。兼容 Antigravity、Claude Code 与 Codex。
---

# 语音复盘工作流（voice-memo-review）

当用户要求你「转写或复盘某条录音」「查看近期录音」「查看或修改分析提示词模板」「录入声纹」时，优先通过已接入的 `voice-memo-review` MCP 工具完成全流程。

## 核心原则
1. **规则完全显化**：严禁凭空猜测分析结构，必须先通过 `vmr_get_template` 或 `vmr_list_templates` 读取用户当前激活的明文模板再进行分析。
2. **绝不超时阻塞**：处理中长录音时，`vmr_process_audio` 会把直连转写交给后台独立进程托管并立即返回 `job_id`。请每隔 10～15 秒调用 `vmr_get_job_status(job_id)` 获取包级进度，直至进入 `awaiting_analysis` 或 `done`。
3. **跨宿主自适应**：
   - 若你在 **Antigravity** 中运行且 `vmr_process_audio` 返回了 `workflow_script_path`：
     1. 首次对话先调用 `define_subagent(Name="audio_transcriber_lite", Description="只读音频听写专员", EnableWriteTools=false, EnableMCPTools=false, EnableSubagentTools=false, SystemPrompt="仅调用 read_file 听音频并返回严格 JSON。")`；
     2. 调用 `run_workflow(ScriptPath=workflow_script_path)` 并发精转落盘转录稿；
     3. 读取落盘转录稿并按 `analysis_prompt` 中的显化规则完成分析，最后调 `vmr_save_analysis` 落盘。
   - 若你在 **Claude Code / Codex / Cursor** 中运行：
     1. 调用 `vmr_process_audio`（必要时配合 `vmr_get_job_status` 轮询）拿到已转写的 `transcript_path` 和 `analysis_prompt`；
     2. 读取转录稿并按显化模板写出高质量复盘；
     3. 调用 `vmr_save_analysis(recording_id, markdown)` 落盘并把结果展示给用户。

## 显化规则与声纹管理
- 用户说「看看我现在的复盘规则 / 把销售复盘模板加上竞品一节」：直接调用 `vmr_list_templates` / `vmr_get_template` / `vmr_save_template` 透明修改并告知保存位置。
- 用户说「帮我录一下我的声纹」：引导提供 10～30 秒清晰单人样音，调用 `vmr_manage_voiceprints(action="enroll", name=..., audio_path=...)`。
