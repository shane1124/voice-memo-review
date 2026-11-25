# AI 代理全自动安装与装后引导协议（AI_SETUP）

> **给正在阅读本文件的 AI（Codex / Claude Code / Antigravity / Cursor）**：
> 当用户把本仓库链接或安装指令交给你时，请严格按下方 **两阶段闭环** 执行——**先全自动完成本机安装与体检，再立刻主动向用户发起 3 个简洁问答并帮他把配置、背景、词表和模板一次配好**。不要只装完软件就把用户晾在原地！

---

## 第一阶段：全自动安装与客户端接入（尽量零打断执行）

1. **检查本机环境**
   - 确认 macOS 架构与 Python 3.11+、ffmpeg（注意 macOS 自带 `/usr/bin/python3` 往往是 3.9，优先检查 `/opt/homebrew/bin/python3.13` / `python3.12` / `python3.11` 与 `ffmpeg`）。
   - 若缺失 Python 3.11+ 或 `ffmpeg`，优先尝试通过 Homebrew 安装，或给出清晰的单行命令提示用户安装。

2. **下载最新版离线免编译包并校验 SHA-256**（无需 Xcode 编译，内置 10MB `fluidaudiocli` 与 21MB 离线声纹模型）：
   ```bash
   mkdir -p /tmp/vmr-setup && cd /tmp/vmr-setup
   TAG=$(curl -fsSL https://api.github.com/repos/shane1124/voice-memo-review/releases/latest | grep '"tag_name"' | head -1 | sed -E 's/.*"([^"]+)".*/\1/')
   curl -fsSL -O "https://github.com/shane1124/voice-memo-review/releases/download/${TAG}/vmr-macos-arm64-${TAG}.tar.gz"
   curl -fsSL -O "https://github.com/shane1124/voice-memo-review/releases/download/${TAG}/SHA256SUMS"
   shasum -a 256 -c SHA256SUMS
   tar -xzf "vmr-macos-arm64-${TAG}.tar.gz"
   VMR_NO_OPEN_UI=1 "./voice-memo-review-${TAG}/install.sh"
   rm -rf /tmp/vmr-setup
   ```
   - 如用户的 Mac 不是 Apple Silicon 或更希望直接从源码安装，可克隆仓库并执行 `./install.sh`。

3. **一键接入 AI 客户端 & 环境体检**
   - 执行 `~/.local/bin/vmr install`，自动为本机检测到的 Antigravity / Claude Code / Codex / Cursor 写入 MCP Server 与通用 `SKILL.md`（均自动备份原配置为 `.bak`）。
   - 检查 `~/.local/bin` 是否已在用户的 shell `PATH`（如 `~/.zshrc`）中，缺失则补入。
   - 执行 `~/.local/bin/vmr doctor`，记下本机检测到的可用引擎（`Antigravity agentapi`、`Codex CLI`、`Claude Code CLI`、`FluidAudio`、`苹果语音备忘录目录`）。
   - 执行 `~/.local/bin/vmr ui` 打开本机可视化控制台（仅监听 `127.0.0.1`）。

---

## 第二阶段：装完必须自动向用户汇报并主动发问（核心体验！）

完成安装后，请用自然、简洁的中文按以下结构回复用户：

### 1. 简明汇报安装状态 & 一句重要提醒（防踩坑！）
- 告知已安装的版本号、已打开本地控制台（之后随时可运行 `vmr ui` 重新打开）、以及本机已接入哪些 AI 客户端。
- **特别提醒 Codex / Claude Code / Cursor 用户**：
  - 本次新写入的 MCP 工具（12 个 `vmr_*` 工具）需要**新开一条对话或重启客户端**才会挂载到工具栏里（当前正在安装的这条旧对话还没加载新工具属于正常现象）。
  - 同时用一句大白话说清两种体验的区别：
    - **后台自动分析**：录音一进来，后台自动转写+复盘并保存到本地 Markdown 和控制台（免 Key 可选 Antigravity 一体化，或选独立转写 + `codex-cli` / `claude-cli` 免 Key 自动分析）；
    - **聊天内主动调用（MCP）**：MCP 是按需拉取工具，录音完不会自己强行弹进已打开的旧聊天框；但新开对话后随时说一句「看看我刚录的音 / 帮我深挖一下 / 帮我改下复盘提示词」，AI 就能直接读取完整转录稿与显化模板为你服务。

### 2. 说明已预置开箱即用的深度实战模板（默认不用自己从零写）
- 明确告诉用户：软件**不是空壳**，已经内置好一套开箱即用的默认模板——**「深度多镜头实战复盘」**（自动识别 S 级客户谈单/核心汇报、A 级内部讨论/深度思考、A-人情级熟人茶局饭局、C 级短测试极简归档，自动输出 30 秒速览快报、关键事实锚点、没接稳的话术与口语替代示范，以及经营/杠杆视角），外加「标准会议纪要 / 客户销售沟通 / 个人口述灵感」3 套场景模板，全部明文存放在控制台「规则工作室」，随时看得见、改得动。

### 3. 主动向用户抛出以下 3 个简洁问题（回复即可一键配好）

> **请告诉我下面 3 件事（随便回简短几句，我直接帮你把配置和提示词一次改好）：**
>
> 1. **转写 + 后台自动分析想用哪套组合？**（根据刚才 `vmr doctor` 体检结果优先推荐本机免 Key 方案）
>    - **方案 A（本机装了 Antigravity，最省心免 Key）**：Antigravity Flash 转写（支持语气/情绪标注）+ Antigravity 自动分析
>    - **方案 B（纯 Codex / Claude 用户，分析免 Key）**：用 Gemini API / OpenAI 兼容 ASR（Whisper/Groq/SiliconFlow）或本地免费命令行转写 + **`codex-cli` 或 `claude-cli`** 免 Key 自动分析
>    - **方案 C（纯 API 组合）**：Gemini API 或 OpenAI 兼容 ASR 转写 + DeepSeek / OpenAI / Gemini API 自动分析
>    - **方案 D（仅自动转写）**：后台只自动转写，需要复盘时直接在 AI 新对话里一句话调用 MCP 分析
> 2. **录音平时从哪来？**
>    - ① 自动监听「苹果语音备忘录」（需开启 iCloud 同步，首次读取需在系统设置授予终端/控制台完全磁盘访问权限）
>    - ② 使用「桌面投放文件夹」（`~/Desktop/语音复盘投放箱`，不依赖 iCloud，拖进去就自动跑）
>    - ③ 只在浏览器控制台手动拖拽文件（关闭后台自动监听）
> 3. **（可选）要不要顺手定制你的专属背景、易错词表或复盘模板？**
>    - 告诉我两三句话就行：**你做什么工作/平时最常录哪类对话？有哪些容易转错的人名/公司/行业缩写？默认复盘模板里有没有想增删的板块？**
>    - 不填也完全可以先按默认版直接开跑，以后随时一句话让我帮你改！

---

## 第三阶段：收到用户回答后的自动落盘动作

1. **写入引擎与监听源配置**：
   - 若在新对话且已加载 MCP 工具，直接调用 `vmr_update_settings(...)`；
   - 若仍在安装时的同一条旧对话（尚未加载 MCP 工具），直接用本机 Python 调用 `voice_review.config` 或通过本地控制台 API 写入 `~/.config/voice-memo-review/config.toml` 并重启/刷新守护进程，切勿让用户手动去改 TOML 文件！
   - 若用户选了需要 API Key 的引擎，**绝不要让用户把明文 Key 贴进聊天记录**，请引导用户打开控制台「引擎插槽」填入（自动落入 `0600` 权限的 `secrets.json`）。
2. **写入个人背景、词表与定制模板**：
   - 调用 `vmr_update_profile(persona=..., glossary=..., default_template_id=...)`（或直接写入 `~/.config/voice-memo-review/rules/persona.md` 与 `glossary.txt`）；
   - 若用户想改模板内容，调用 `vmr_save_template(...)` 保存并告知具体改动。
3. **收尾给出 3 句开箱即用示范**（并提醒新开一条对话即可用 MCP）：
   - 「看看我今天最新那条录音，按深度复盘模板整理重点」
   - 「把默认复盘模板里加上『竞品对比』这一节，并更新我的个人背景」
   - 「帮我用桌面这段 15 秒自我介绍录音录一下我的声纹」
