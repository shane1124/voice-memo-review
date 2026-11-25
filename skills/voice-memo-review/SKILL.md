---
name: voice-memo-review
description: 跨 AI 可插拔语音复盘技能。用于处理本地录音文件或苹果语音备忘录、结合用户工作场景量身定制显化分析规则模板、更新个人背景与词表、切换转写/分析插槽与开机自启（支持 Antigravity/Codex CLI/Claude CLI/API）、检查安全更新、管理本地声纹名册，并按透明模板产出结构化深度复盘。兼容 Antigravity、Claude Code、Codex 与 Cursor。
---

# 语音复盘工作流（voice-memo-review）

当用户要求你「安装/配置语音复盘」「转写或复盘某条录音」「查看近期录音」「结合我的工作量身定制或修改复盘模板、个人背景或词表」「切换转写/分析引擎、对话频率或开机自启」「检查更新」「录入声纹」时，优先通过已接入的 `voice-memo-review` MCP 工具完成全流程。

## 装后引导、量身定制与版本提醒
- **4 问流水线引导**：先调用 `vmr_status` 亮出本机实际检测到的可用引擎（Antigravity / Codex CLI / Claude Code CLI，**不要拿苹果语音备忘录目录存在当成已可同步打勾**），再按 **① 录音来源（优先推荐「苹果语音备忘录」，可实现结束录音后全自动转写分析，同时提醒需开启 iCloud 同步与足够空间；另提供桌面投放箱与手动拖入；自动监听默认连带开开机自启）→ ② 转写模型（按本机推荐）→ ③ 分析 Agent（按本机推荐并点明联动规则）→ ④ 量身定制专属模板与运行偏好（对话频率 `rotation` / 保存目录 `output_dir` / 短录音过滤 `min_duration_sec`）** 引导配置。
- **优先量身定制模板**：内置的 4 套模板（通用对话复盘 / 会议讨论 / 客户拜访 / 个人复盘）只是参考示例；当用户同意时，可结合其口述或经授权扫描其常用工作目录/项目结构，为其量身生成专属复盘模板、个人背景（`persona.md`）与易错词表（`glossary.txt`），并通过 `vmr_save_template` 与 `vmr_update_profile` 仅保存在本机。
- **温和版本提醒**：每次调用 `vmr_status` 若看到 `update_available == true`，在完成手头任务后顺口提醒一句：「顺便一提，`voice-memo-review` 发布了新版 `v<latest_version>`（`<release_notes>`），需要我帮你运行 `vmr update` 安全升级并自动重启吗？」（未获确认不擅自升级）。
- **如实说明跨客户端边界**：MCP 是对话内按需调用工具，不会把后台完成的复盘强行弹进已打开的旧聊天窗；后台自动复盘成品统一落盘到本地 Markdown 并在 `vmr ui` 控制台实时呈现，随时在 AI 新对话里一句话即可调 MCP 读取、追问或重分析。

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
     2. 若需处理新音频文件，先用 `vmr_status` 确认当前 `transcriber_provider`：若仍是 `antigravity-flash`（仅 Antigravity 宿主可执行 `run_workflow`），明确告知可在后台由 Antigravity 转写后由本端读取复盘，或用 `vmr_update_settings` 切为独立转写引擎搭配 `codex-cli` / `claude-cli` / `none`；
     3. 拿到 `transcript_path` 与 `analysis_prompt` 后写出完整复盘，调用 `vmr_save_analysis(recording_id, markdown)` 落盘并把正文直接展示给用户。

## 显化规则、画像、配置、更新与声纹管理
- **看/改/定制复盘模板**：调用 `vmr_list_templates` / `vmr_get_template` / `vmr_save_template`，把改动说明清楚并告知文件保存在 `~/.config/voice-memo-review/rules/templates/`。
- **看/改个人背景与专名词表**：调用 `vmr_get_profile` / `vmr_update_profile` 透明写入 `persona.md` 与 `glossary.txt`。
- **切换引擎、监听源、开机自启与运行偏好**：调用 `vmr_update_settings`（支持 `autostart_enabled`、`rotation`、`output_dir`、`min_duration_sec`；切勿让用户在聊天里发明文 API Key，如需填 Key 引导在 `vmr ui` 控制台「引擎插槽」填写）。
- **检查新版本**：调用 `vmr_check_update(force_refresh=true)` 汇报最新版本与亮点；用户确认更新后执行 `vmr update`（自动校验 SHA256、保护正在进行的转写任务，并无感热重启后台守护）。
- **声纹注册与管理**：引导用户提供 10～30 秒清晰单人样音，调用 `vmr_manage_voiceprints(action="enroll", name=..., audio_path=...)`。
