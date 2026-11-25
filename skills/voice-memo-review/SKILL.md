---
name: voice-memo-review
description: 跨 AI 可插拔语音复盘技能。用于处理本地录音文件或苹果语音备忘录、查看/修改显化分析规则模板、更新个人背景与词表、切换转写与分析插槽（支持 Antigravity/Codex CLI/Claude CLI/API）、管理本地声纹名册，并按透明模板产出结构化深度复盘。兼容 Antigravity、Claude Code、Codex 与 Cursor。
---

# 语音复盘工作流（voice-memo-review）

当用户要求你「安装/配置语音复盘」「转写或复盘某条录音」「查看近期录音」「查看或修改分析提示词模板、个人背景或专名词表」「切换转写/分析引擎」「录入声纹」时，优先通过已接入的 `voice-memo-review` MCP 工具完成全流程。

## 首次使用 / 装后自动引导（Onboarding）
- 当用户刚完成安装，或调用 `vmr_status` 发现 `persona_configured == false` 且用户正在询问「怎么用 / 怎么配置 / 提示词在哪」时，请主动用简洁人话告知：
  1. 已内置开箱可用的 **「深度多镜头实战复盘」** 默认模板（自动区分客户谈单、内部讨论、熟人茶局、短测试，并输出 30 秒速览、事实锚点、没接稳的话术与替代示范、经营/杠杆视角），另附会议纪要、销售沟通、个人灵感 3 套模板；
  2. 询问用户是否需要顺手配置三件事（用户回简短几句，你直接调 `vmr_update_settings` / `vmr_update_profile` / `vmr_save_template` 一次性改好）：
     - **引擎组合**：本机免 Key 可选 `antigravity-flash + antigravity-agentapi`，或任意独立转写引擎（Gemini API / Whisper / 本地命令行）搭配 `codex-cli` / `claude-cli` 免 Key 自动分析；
     - **录音来源**：苹果语音备忘录自动监听 / 桌面投放文件夹 / 手动拖拽；
     - **个人定制**：你的职业角色与重点关注项（写入 `persona.md`）、易错人名/术语词表（写入 `glossary.txt`）以及模板微调需求。
  3. **如实说明跨客户端边界**：MCP 是对话内按需调用工具，不会把后台完成的复盘强行弹进已打开的旧聊天窗；后台自动复盘成品统一落盘到本地 Markdown 并在 `vmr ui` 控制台实时呈现，随时在 AI 新对话里一句话即可调 MCP 读取、追问或重分析。

## 核心工作流原则
1. **规则完全显化**：严禁凭空猜测分析结构，必须先通过 `vmr_get_template` 或 `vmr_list_templates`（以及 `vmr_get_profile`）读取用户当前激活的明文模板与背景再进行分析。
2. **绝不超时阻塞**：处理中长录音时，`vmr_process_audio` 会把直连转写交给后台独立进程托管并立即返回 `job_id`。请每隔 10～15 秒调用 `vmr_get_job_status(job_id)` 获取包级进度，直至进入 `awaiting_analysis` 或 `done`。
3. **跨宿主自适应**：
   - 若你在 **Antigravity** 中运行且 `vmr_process_audio` 返回了 `workflow_script_path`：
     1. 首次对话先调用 `define_subagent(Name="audio_transcriber_lite", Description="只读音频听写专员", EnableWriteTools=false, EnableMCPTools=false, EnableSubagentTools=false, SystemPrompt="仅调用 read_file 听音频并返回严格 JSON。")`；
     2. 调用 `run_workflow(ScriptPath=workflow_script_path)` 并发精转落盘转录稿；
     3. 读取落盘转录稿并按 `analysis_prompt` 中的显化规则完成分析，最后调 `vmr_save_analysis` 落盘。
   - 若你在 **Claude Code / Codex / Cursor** 中运行：
     1. 若录音已在后台完成转写（`vmr_list_recordings` 显示 `awaiting_analysis` 或 `done`），直接读取 `transcript_path` 与 `vmr_get_template` / `analysis_prompt`，按显化规则输出深度复盘并按需调 `vmr_save_analysis` 落盘；
     2. 若需处理新音频文件，先用 `vmr_status` 确认当前 `transcriber_provider`：若仍是 `antigravity-flash`（仅 Antigravity 宿主可执行 `run_workflow`），明确提示用户可在后台由 Antigravity 转写后由本端读取复盘，或使用 `vmr_update_settings` 切为独立转写引擎（如 `gemini-api` / `openai-compatible-asr` / `command-asr`）搭配 `codex-cli` / `claude-cli` / `none`；
     3. 拿到 `transcript_path` 与 `analysis_prompt` 后写出完整复盘，调用 `vmr_save_analysis(recording_id, markdown)` 落盘并把正文直接展示给用户。

## 显化规则、画像、配置与声纹管理
- **看/改复盘模板**：调用 `vmr_list_templates` / `vmr_get_template` / `vmr_save_template`，把改动说明清楚并告知文件保存在 `~/.config/voice-memo-review/rules/templates/`。
- **看/改个人背景与专名词表**：调用 `vmr_get_profile` / `vmr_update_profile` 透明写入 `persona.md` 与 `glossary.txt`。
- **切换引擎与监听源**：调用 `vmr_update_settings`（切勿让用户在聊天里发明文 API Key，如需填 Key 引导在 `vmr ui` 控制台「引擎插槽」填写）。
- **声纹注册与管理**：引导用户提供 10～30 秒清晰单人样音，调用 `vmr_manage_voiceprints(action="enroll", name=..., audio_path=...)`。
