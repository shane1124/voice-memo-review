# AI 代理全自动安装与装后引导协议（AI_SETUP）

> **给正在阅读本文件的 AI（Codex / Claude Code / Antigravity / Cursor）**：
> 当用户把本仓库链接或安装指令交给你时，请严格按下方 **三阶段闭环** 执行——先全自动完成本机安装与体检，再根据**这台 Mac 实际检测到的环境**，按「来源 → 转写模型 → 分析 Agent → 量身定制专属规则与运行偏好」4 个清晰问题发问；其中第 4 题**优先推荐结合用户平时的工作场景为他量身设计一套专属复盘提示词**（内置 4 套仅作参考示例），收到回复后直接落盘并配好开机自启。

---

## 第一阶段：全自动安装与环境体检（尽量零打断执行）

1. **检查本机环境**
   - 确认 macOS 架构与 Python 3.11+、`ffmpeg`（macOS 自带 `/usr/bin/python3` 通常是 3.9，优先检查 `/opt/homebrew/bin/python3.13` / `python3.12` / `python3.11` 与 `ffmpeg`）。
   - 若缺失 Python 3.11+ 或 `ffmpeg`，优先通过 Homebrew 安装或给出单行安装命令。

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
   - 如不是 Apple Silicon Mac，可改为克隆仓库后执行 `./install.sh`。

3. **一键接入 AI 客户端、环境体检与启动控制台**
   - 执行 `~/.local/bin/vmr install`，自动为本机检测到的 Antigravity / Claude Code / Codex / Cursor 写入 MCP Server（13 个工具）与通用 `SKILL.md`（自动备份既有配置为 `.bak`）。
   - 检查 `~/.local/bin` 是否在 shell `PATH` 中，缺失则补入 `~/.zshrc`。
   - 执行 `~/.local/bin/vmr doctor`，记下本机实际检出的引擎环境：`Antigravity agentapi`、`Codex CLI`、`Claude Code CLI`、`FluidAudio`。
   - **注意**：不要把「苹果语音备忘录目录存在」当成“已可自动同步”去打勾推荐——因为无法判断用户是否开启了 iCloud 语音备忘录同步以及 iCloud 额度是否充足，应在第 1 题里如实提醒前提条件。
   - 执行 `~/.local/bin/vmr ui` 打开本机极简控制台（仅监听 `127.0.0.1`）。
   - **暂不开启自动监听与开机自启**，等用户在第二阶段选定录音来源后再按选择启用。

---

## 第二阶段：亮出本机工具清单 + 按 4 个问题逐项引导（带【本机推荐】与量身定制提示词）

完成安装后，请用简洁、直白的中文按以下结构回复用户（**举例请保持通用，不要擅自套用个人记忆中的私人姓名或敏感项目名**）：

### 1. 一句话汇报安装状态与本机工具清单
- 汇报已安装的版本号、已接入的 AI 客户端列表、已打开本地控制台（以后运行 `vmr ui` 随时重新打开）。
- **用一行亮出这台 Mac 实际检测到的可用引擎**：
  > 本机已检测到：Antigravity（✅/❌） · Codex CLI（✅/❌） · Claude Code CLI（✅/❌） · 离线声纹切段（✅）
- **补两句重要提醒**：
  - Codex / Claude Code / Cursor 首次接入 MCP 后，**请新开一条对话或重启客户端**以刷新 13 个 `vmr_*` 工具；
  - 后台守护每 24 小时静默检查一次 GitHub 新版本，有新版会在控制台顶部横幅和对话里温和提醒，一键校验 SHA-256 升级并自动重启。

### 2. 按音频流水线顺序抛出以下 4 个问题（前 3 题选核心链路，第 4 题合入量身定制规则与运行偏好）

> **请直接回一个简短组合（例如「① + 1 + 1 + 帮我结合我的工作量身定做一套」），我立刻帮你一次配好：**
>
> **1. 录音来源怎么来？**
> - **① 苹果语音备忘录自动监听【推荐 · 最省心】**（用 iPhone / Apple Watch / Mac 录完一按停止，即可自动同步并完成转写与复盘，全程零手动操作；*提醒：需先配置开启 iCloud 语音备忘录同步且确保 iCloud 空间充足，首次读取可能需授予完全磁盘访问权限*）
> - **② 桌面「语音复盘投放箱」**（`~/Desktop/语音复盘投放箱`，适合没开 iCloud、用其他手机/录音笔/会议软件导出音频的场景，拖进去就自动跑）
> - **③ 仅在网页控制台手动拖入**（关闭后台监听）
> - *（选 ① 或 ② 时，我会默认顺手开启 **macOS 开机后台自启**，重启电脑不掉线、关终端也能默默跑；不需要可顺口说一声。）*
>
> **2. 用什么模型转写？** *(按本机实际检测结果标出【本机推荐】)*
> - *(本机已装 Antigravity 时优先标【本机推荐】)* **1. Antigravity Flash**（免 API Key，原生听音并标注语气与叠话情绪）
> - *(本机未装 Antigravity 时按需推荐 2 或 3)* **2. Gemini API**（填自己的 Gemini Key，直连多模态含语气标注）
> - **3. OpenAI 兼容 ASR**（Whisper / Groq / SiliconFlow / 本地 Whisper 服务）
> - **4. 自定义本地命令行**（例如本机离线免费的 `whisper-cli` / `mlx-qwen3-asr`，支持声纹对齐）
>
> **3. 用什么 Agent / 模型分析？** *(按本机已装 AI 动态推荐，提前点破联动规则)*
> - *(若第 2 题选 1 `Antigravity Flash`，主动提示两者一体化免费联动，标为【本机推荐】)* **1. Antigravity 后台自动分析**（与 `Antigravity Flash` 免 Key 联动；平时仍可在 Codex / Claude 新对话用 MCP 读稿深挖或重分析）
> - *(若第 2 题选 2/3/4 独立转写，且本机已装 Codex CLI 或 Claude Code CLI，优先标【本机推荐】)* **2. 本机 Codex CLI（`codex-cli`）或 Claude Code CLI（`claude-cli`）**（免 API Key，直接复用你本机已登录账号完成后台自动复盘）
> - **3. 外部大模型 API**（DeepSeek / OpenAI / Gemini / 本地 Ollama）
> - **4. 仅自动转写，需要复盘时再到聊天里叫我分析**
>
> **4.（推荐定制）你的专属复盘提示词 + 个人常用设置偏好**
> - **先说明**：软件里内置的 4 套模板（通用对话复盘 / 会议讨论 / 客户拜访 / 个人复盘）**只是通用参考示例**，随时可以直接开跑；
> - **【更推荐的做法】让我结合你平时的真实工作场景，为你量身设计一套专属复盘模板、个人背景和易错词表**：
>   - 你可以直接用两三句话告诉我你做什么工作、最常录什么场景、有哪些高频易错词；
>   - **或者直接授权我快速扫一眼你常用的工作目录/笔记结构**（仅在你明确同意后扫描，生成的模板和背景只保存在你本机 `~/.config/voice-memo-review/rules/`，绝不外传），我来替你起草一套最趁手的专属模板供你确认！
>   - 想省事也可以直接回 **「先用内置示例」**，以后随时一句口述就能改。
> - **运行小偏好（顺口提一句就行，不提则走括号内默认）**：
>   - **新开分析对话频率**：默认**每周**新开一个（可改为**每天** / **每条录音独立开窗** / **一直复用同一窗**）；
>   - **复盘稿保存目录**：默认 `~/Documents/VoiceMemoReview`（也可改到你的笔记库目录）；
>   - **短录音过滤门槛**：默认跳过 **10 秒**以下误触（可改如 30 秒）；也可以顺手提供一段 15 秒清晰人声让我帮你录入本地声纹。

---

## 第三阶段：收到用户回答后的自动落盘动作

1. **校验引擎组合并写入全量配置**：
   - 若用户组合存在冲突（如 `antigravity-flash` 转写搭配 `codex-cli` 自动分析），用 1 句人话解释原因并给出两个可行切换方案让用户拍板。
   - 调用 `vmr_update_settings(...)`（支持 `transcriber_*`、`analyzer_*`、`voice_memos_enabled`、`drop_folder_enabled`、`autostart_enabled`、`rotation`、`output_dir`、`min_duration_sec`）；若仍在安装旧对话尚未加载 MCP，则直接用本机 Python 写入 `~/.config/voice-memo-review/config.toml` 并按需运行 `~/.local/bin/vmr autostart on`（选 ① 或 ② 默认开启，选 ③ 默认关闭）。
   - 如选了需要 API Key 的引擎，**绝不让用户把明文 Key 发进聊天窗口**，引导用户在控制台「引擎插槽」填写（自动加密隔离存进 `0600` 权限 `secrets.json`）。
2. **量身定制并落盘专属模板、背景与词表**：
   - 若用户授权扫描其常用工作目录/项目背景：先轻量浏览目录名与 README/个人说明（不乱翻无关隐私文件），总结出该用户的核心工作场景、关注指标与高频专有名词，生成 1 套清晰简洁的专属模板草稿展示给用户确认（或直接写入 `~/.config/voice-memo-review/rules/templates/custom_default.md` 并设为默认，同时写入 `persona.md` 与 `glossary.txt`）；
   - 若用户直接口述了偏好，调用 `vmr_update_profile` 与 `vmr_save_template` 立即落盘；若用户回「先用内置示例」，则保留默认通用示例。
3. **收尾核验并给出 3 句开箱指令**：
   - 运行 `~/.local/bin/vmr doctor` 确认全绿；
   - 提醒用户新开一条对话即可调用 13 个 MCP 工具，并给出 3 句顺手示例：
     - 「看看我今天最新那条录音，按我的专属复盘模板整理重点」
     - 「帮我把默认复盘模板里加上『风险与待确认』一节，并把新项目词加进词表」
     - 「帮我检查一下语音复盘有没有新版本」
