---
name: voice-memo-review
description: 跨 AI 可插拔录音自动分析技能。用于处理本地录音文件或苹果语音备忘录、结合用户工作场景量身定制显化分析模板、识别未登记主要说话人并一句话记住声纹、切换转写/桌面分析引擎与开机自启（支持 Antigravity / Codex / Claude / WorkBuddy 桌面版与在线 API）、检查安全更新，并按透明模板产出结构化录音分析。兼容 Antigravity、Claude、Codex、WorkBuddy 与 Cursor。
---

# 录音自动分析工作流（voice-memo-review）

当用户要求你「安装/配置录音自动分析」「转写或分析某条录音」「查看近期录音」「记住某位说话人的声纹备注」「定制或修改分析模板、个人背景或词表」「切换转写/分析引擎、对话窗口归类方式或开机自启」「检查更新」时，优先通过已接入的 `voice-memo-review` MCP 工具完成全流程。

## 装后引导、大白话原则与版本提醒
- **说人话，不抛技术黑话**：面向用户介绍选项时，避免抛出 `openai-compatible-asr`、`command-asr`、`codex-cli`、`claude-cli`、`antigravity-agentapi`、`CLI` 等技术代号；统一使用直白中文：**Antigravity 免费听写 / 在线语音转写 API（Gemini、硅基流动、Groq、Whisper 等）/ 本机离线免费转写**，以及 **Antigravity 桌面版 / Codex 桌面版 / Claude 桌面版 / WorkBuddy 桌面版 / 在线大模型 API**。
- **4 问流水线引导**：先调用 `vmr_status` 亮出本机实际检测到的桌面端（Antigravity / Codex / Claude / WorkBuddy，**不要拿苹果语音备忘录目录存在当成已可同步打勾**），再按 **① 录音来源（优先推荐苹果语音备忘录，提醒需开启 iCloud 且空间充足；另提供桌面投放箱与手动拖入）→ ② 怎么转成文字 → ③ 交给谁分析 → ④ 专属模板与日常偏好（默认每天合并一个日期置前窗口如 `11-26-2026 录音分析`、自动弹出桌面端、文末自动询问是否记住新说话人声纹）** 引导配置。
- **优先量身定制模板**：内置 4 套模板只是通用参考示例；当用户同意时，可结合其口述或经授权扫描其常用工作目录，量身生成专属模板、个人背景（`persona.md`）与易错词表（`glossary.txt`），并通过 `vmr_save_template` 与 `vmr_update_profile` 仅保存在本机。
- **温和版本提醒**：每次调用 `vmr_status` 若看到 `update_available == true`，完成手头任务后顺口提醒一句：「顺便一提，录音自动分析发布了新版 `v<latest_version>`（`<release_notes>`），需要我帮你安全升级并自动重启吗？」（未获确认不擅自升级）。
- **权限与授权避坑**：
  - 若选用 **Claude 桌面版** 且 `vmr_status` 显示 `claude_logged_in == false`，提醒并引导运行 `vmr auth-claude`（或在控制台点「一键登录官方 Claude 账号」）完成 1 次官方浏览器授权，之后即可免 Key 自动弹出桌面对话；
  - 若开启苹果语音备忘录监听且 `vmr doctor` 报无读取权限，务必引导用户给 **`vmr doctor` 打印出的精确 `Python 真实路径`**（而非仅 `Python.app`）授予 macOS「完全磁盘访问权限」。

## 核心工作流原则
1. **规则完全显化**：严禁凭空猜测分析结构，必须先通过 `vmr_get_template` / `vmr_list_templates` 与 `vmr_get_profile` 读取用户当前激活的明文模板与背景再进行分析。
2. **随录随记声纹轻提问（核心体验）**：
   - 分析录音时，如果 `analysis_prompt` 或 `vmr_get_job_status` 中包含未登记的主要说话人（`unmatched_speakers`），务必在正式分析正文最末尾附一小段友好的 **「🎙️ 是否记录说话人声纹？」** 提问：列出识别到的主要未登记说话人人数、代号、大致发言时长与原话示例，并明确说明 **「记录备注后，下次录音会自动认出并标出姓名，不记可直接忽略」**；
   - 一旦用户回复备注（如「SPEAKER_00 是我，SPEAKER_01 是王总」），立刻调用 `vmr_manage_voiceprints(action="confirm_from_recording", recording_id=..., mappings={...}, rewrite_docs=true)` 直接零样音入库本机声纹库并顺手替换当前稿件里的代号！
3. **绝不超时阻塞**：处理中长录音时，`vmr_process_audio` 会把直连转写交给后台独立进程托管并立即返回 `job_id`。每隔 10～15 秒调用 `vmr_get_job_status(job_id)` 获取进度，直至进入 `awaiting_analysis` 或 `done`。
4. **跨宿主自适应**：
   - 若在 **Antigravity** 中运行且 `vmr_process_audio` 返回了 `workflow_script_path`：
     1. 首次对话先调用 `define_subagent(Name="audio_transcriber_lite", Description="只读音频听写专员", EnableWriteTools=false, EnableMCPTools=false, EnableSubagentTools=false, SystemPrompt="仅调用 read_file 听音频并返回严格 JSON。")`；
     2. 调用 `run_workflow(ScriptPath=workflow_script_path)` 并发精转落盘转录稿；
     3. 读取转录稿并按 `analysis_prompt` 完成分析（保留末尾声纹提问），最后调 `vmr_save_analysis` 落盘。
   - 若在 **Codex / Claude / WorkBuddy / Cursor** 中运行：
     1. 若录音已在后台完成转写，直接读取 `transcript_path` 与 `analysis_prompt`，按显化规则输出完整分析（保留末尾声纹提问）并调 `vmr_save_analysis` 落盘；
     2. 若需处理新音频且当前转写引擎仍是 `antigravity-flash`，温和说明可切为在线语音转写 API 或本机离线免费转写。

## 规则、声纹、配置与更新管理
- **看/改/定制模板**：`vmr_list_templates` / `vmr_get_template` / `vmr_save_template`。
- **看/改个人背景与词表**：`vmr_get_profile` / `vmr_update_profile`。
- **一键记住声纹或管理声纹名册**：优先用 `vmr_manage_voiceprints(action="confirm_from_recording", mappings=...)` 直接从录音零样音入库；也支持 `action="enroll"` 上传 10～30 秒清晰样音或 `list` / `delete`。
- **切换配置**：`vmr_update_settings`（切勿让用户在聊天里发明文 API Key，如需填 Key 引导在 `vmr ui` 控制台「引擎插槽」填写）。
- **检查与升级版本**：`vmr_check_update(force_refresh=true)`；用户确认后运行 `vmr update`。
