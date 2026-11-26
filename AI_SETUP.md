# AI 代理全自动安装与装后引导协议（AI_SETUP）

> **给正在阅读本文件的 AI（Antigravity / Codex / Claude / WorkBuddy / Cursor）**：
> 当用户把本仓库链接或安装指令交给你时，请严格按下方 **三阶段闭环** 执行——先全自动完成本机安装与体检，再用**极简大白话（避免抛出一堆代码键名或晦涩技术词）**亮出这台 Mac 实际检测到的桌面端环境，按「录音怎么进 → 怎么转成文字 → 交给谁分析 → 你的专属模板与日常偏好」4 个清晰问题一步配好，收到回复后立刻落盘生效。

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

3. **一键接入 AI 桌面端、环境体检与启动控制台**
   - 执行 `~/.local/bin/vmr install`，自动为本机检测到的 **Antigravity / Codex / Claude 桌面版 / Claude Code / WorkBuddy / Cursor** 无损接入 MCP（13 个工具）与通用 `SKILL.md`（自动备份既有配置为 `.bak`）。
   - 检查 `~/.local/bin` 是否在 shell `PATH` 中，缺失则补入 `~/.zshrc`。
   - 执行 `~/.local/bin/vmr doctor`，记下本机实际检出的桌面端与本地能力：`Antigravity 桌面版`、`Codex 桌面版`、`Claude 桌面版`（含登录状态）、`WorkBuddy 桌面版`、`FluidAudio 离线声纹` 以及是否已装本机离线转写工具（如 `mlx-qwen3-asr` / `whisper-cli`）。
   - **重要**：
     - 所有桌面版均优先直连桌面 App 安装包内置本体，**无需向用户提及或安装任何外部 CLI 工具**；
     - 不要把「苹果语音备忘录目录存在」当成“已可自动同步”去打勾推荐——因为无法判断是否开启了 iCloud 语音备忘录同步以及空间是否充足，应在第 1 题里如实提醒前提条件。
   - 执行 `~/.local/bin/vmr ui` 打开本机极简控制台（仅监听 `127.0.0.1`）。
   - **暂不开启自动监听与开机自启**，等用户在第二阶段选定录音来源后再按选择启用。

---

## 第二阶段：一句话汇报 + 4 个大白话选择（直接让人看懂、3 秒回完）

完成安装后，请用**自然流畅、无技术黑话的简体中文**按以下格式回复用户（**举例保持通用，不要擅自套用个人记忆中的私人姓名或敏感项目名**）：

### 1. 简短汇报安装成果与本机可用桌面端
- 一句话汇报已装版本、已接入的桌面 AI 列表、以及已打开本地控制台（之后随时运行 `vmr ui` 即可重开）。
- **一行亮出这台 Mac 实际检测到的桌面端**：
  > 本机已检测到：Antigravity 桌面版（✅/❌） · Codex 桌面版（✅/❌） · Claude 桌面版（✅/⚠️未授权/❌） · WorkBuddy 桌面版（✅/❌） · 离线声纹分人（✅）
- **两句轻提醒**：
  - 首次接入后，请**新开一条对话或重启桌面客户端**，让新增的录音工具生效；
  - 后台每天会自动检查一次 GitHub 新版本，有新版时会温和提醒，确认后一键升级。

### 2. 按顺手流程抛出 4 个清晰大白话问题

> **直接回复你的选择就行（例如「① + 1 + 1 + 帮我结合我的工作定制一套模板」），我立刻帮你一次配好：**
>
> **1. 录音怎么进来？**
> - **① 苹果语音备忘录自动同步【推荐 · 最省心】**：iPhone / Apple Watch / Mac 录完一按停止，自动同步到电脑并完成转写分析，全程不用动手（*需开启 iCloud 语音备忘录且 iCloud 空间充足；首次读取可能需要授一次磁盘权限*）
> - **② 桌面投放箱**：在桌面建一个「录音自动分析投放箱」，适合不用 iCloud、或从录音笔/微信/会议软件导出音频拖进去自动处理
> - **③ 只在网页控制台手动拖入**：平时不后台监听，想分析哪条拖哪条
> - *（选 ① 或 ② 时，我会默认顺手开启开机后台自启，重启电脑也不掉线；不需要说一声即可。）*
>
> **2. 怎么把录音转成文字？** *(按本机实际环境标出【推荐】)*
> - *(已装 Antigravity 时标【推荐】)* **1. Antigravity 免费听写**：免 API Key，原生听音还能标出说话人的语气和情绪
> - **2. 在线语音转写 API**：填自己的接口 Key 就能用，支持 **Gemini**（含语气标注）、**硅基流动 / Groq / Whisper 等常见语音接口**，速度稳定
> - *(已检出本机 `mlx-qwen3-asr` / `whisper-cli` 时可标【推荐】)* **3. 本机离线免费转写**：完全不上云、不花钱，用你电脑本地的离线语音模型转写并自动对齐说话人
>
> **3. 转成文字后，交给谁自动分析？** *(按本机已装桌面 App 动态推荐)*
> - **1. 本机桌面 AI 自动分析【推荐 · 免 API Key】**：直接联动你电脑上已装好的 **Antigravity 桌面版 / Codex 桌面版 / Claude 桌面版 / WorkBuddy 桌面版**——分析写完不仅保存文档，还会**直接在桌面版窗口里把分析摆出来**，看完随时接着追问！
>   - *(联动提醒：若第 2 题选了 1「Antigravity 免费听写」，这里需搭配 Antigravity 桌面版；若想用 Codex / Claude / WorkBuddy 桌面版分析，第 2 题选 2「在线 API」或 3「本机离线转写」即可。)*
> - **2. 在线大模型 API**：填自己的 Key（支持 DeepSeek / Kimi / 通义 / Gemini / OpenAI 等），自动生成分析稿保存到文件夹
> - **3. 只自动转成文字**：先帮我把录音转成带说话人的文字稿，需要分析时我再发给你聊
>
> **4. 你的专属分析模板 + 日常小偏好（已配好默认，想改顺嘴说一句）**
> - **分析模板**：内置 4 套通用参考示例（通用录音分析 / 会议纪要 / 客户拜访 / 个人复盘）随时能用；**更推荐告诉我你做什么工作、最常录什么场景**（或授权我快速扫一眼你常用的工作目录，仅在本机生成、绝不外传），我替你量身写一套最趁手的专属模板和易错词表；想省事也可以直接回 **「先用内置示例」**。
> - **日常默认设置（不提就直接按下面生效）**：
>   - **分析窗口怎么分**：默认**每天合并成一个清晰日期窗口**（如 `11-26-2026 录音分析`）并自动弹到桌面端给你看；想改成**每条新录音单独开一个窗口**、**按周合并**，或**只静默存文件不弹窗**，说一声就行；
>   - **怎么认出每个说话人**：以后录音里识别到**尚未登记的主要说话人**时，我会在分析最末尾顺嘴问你一句「识别到 X 位主要说话人，要不要记住备注？」——你回一句（例如「SPEAKER_00 是我，SPEAKER_01 是王总」），声音特征就存在你本机，下次再录自动认人；
>   - **保存位置与过滤**：分析稿默认存 `~/Documents/VoiceMemoReview`，自动跳过 **10 秒**以下误触（都可随时改）。

---

## 第三阶段：收到用户回答后的自动落盘动作

1. **内部映射配置并保存**（不要把内部 provider 代号抛给用户看，AI 自己在底层自动映射）：
   - 转写映射：Antigravity 免费听写 → `antigravity-flash`；Gemini 在线听写 → `gemini-api`；硅基流动 / Groq / Whisper 等在线语音 API → `openai-compatible-asr`；本机离线转写（如 `mlx-qwen3-asr` / `whisper-cli`）→ `command-asr`。
   - 分析映射：Antigravity 桌面版 → `antigravity-agentapi`；Codex 桌面版 → `codex-cli`；Claude 桌面版 → `claude-cli`；WorkBuddy 桌面版 → `workbuddy-desktop`；DeepSeek / Kimi / 通义 / OpenAI 等在线大模型 → `openai-compatible-llm`；Gemini 大模型 → `gemini-llm`；仅转文字 → `none`。
   - 调用 `vmr_update_settings(...)`（或在尚未加载 MCP 的旧对话里直接写 `~/.config/voice-memo-review/config.toml` 并按需运行 `~/.local/bin/vmr autostart on`）。
   - 如选了需要 API Key 的在线服务，**绝不让用户把明文 Key 发进聊天窗口**，引导用户在控制台「引擎插槽」里填写（自动以 `0600` 权限独立存进 `secrets.json`）。
2. **量身定制并落盘专属模板、背景与词表**：
   - 若用户口述了工作场景或授权扫描常用工作目录：整理出专属模板、个人背景与高频易错词，写入 `~/.config/voice-memo-review/rules/` 并设为默认；若用户回「先用内置示例」，保留默认通用示例。
3. **收尾核验（一步扫清两个常见权限/授权卡点）**：
   - 运行 `~/.local/bin/vmr doctor` 确认全绿；
   - **官方 Claude 桌面版 1 次授权收口**：若用户选了 **Claude 桌面版** 且 `vmr doctor` 显示「Claude 桌面版（尚未授权登录）」，说明官方 Claude 桌面版不向后台进程共享登录态——告知用户「只需在浏览器点 1 次官方授权，之后新录音即可免 Key 自动弹出 Claude 桌面版对话」，并帮其执行 `~/.local/bin/vmr auth-claude`（或引导在控制台点 **「一键登录官方 Claude 账号」**），完成后再跑一次 `vmr doctor` 确认变绿；
   - **macOS 苹果语音备忘录权限指引**：若用户选了 ①「苹果语音备忘录」且 `vmr doctor` 报「苹果语音备忘录读取权限 ❌」，**直接把 `vmr doctor` 输出的精确 `Python 真实路径`**（如 `/opt/homebrew/Cellar/python@3.13/.../bin/python3.13`，切勿误导用户只加 `Python.app`）发给用户，引导他去「系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 点＋按 ⌘⇧G 粘贴路径 → 开启开关」，完成后重启后台守护再核验变绿；
   - 提醒用户新开一条对话即可调用全部录音工具，并附上 3 句开箱示例：
     - 「看看我今天最新那条录音，按我的模板整理重点」
     - 「刚才那条录音里 SPEAKER_00 是我，SPEAKER_01 是王总，帮我记住声纹」
     - 「帮我检查一下录音自动分析有没有新版本」
