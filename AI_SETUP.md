# AI 代理全自动安装与装后引导协议（AI_SETUP）

> **给正在阅读本文件的 AI（Codex / Claude Code / Antigravity / Cursor）**：
> 当用户把本仓库链接或安装指令交给你时，请严格按下方 **三阶段闭环** 执行——先全自动完成本机安装与体检，再根据**这台 Mac 实际检测到的环境**，按「来源 → 转写模型 → 分析 Agent → 可选规则微调」4 个独立问题给出带推荐的选择题，收到回复后直接落盘并配好开机自启。

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
   - 执行 `~/.local/bin/vmr doctor`，**准确记下这台 Mac 实际检出的每一项**：`Antigravity agentapi`、`Codex CLI`、`Claude Code CLI`、`FluidAudio`、`苹果语音备忘录目录`、`开机后台自启`。
   - 执行 `~/.local/bin/vmr ui` 打开本机极简控制台（仅监听 `127.0.0.1`）。
   - **暂不开启自动监听与开机自启**，等用户在第二阶段选定录音来源后再按选择启用。

---

## 第二阶段：先亮本机体检清单，再按 4 个流水线问题逐项引导（带【本机推荐】）

完成安装后，请用简洁、直白的中文按以下结构回复用户（**举例子请保持通用，不要擅自套用个人记忆中的私人姓名或敏感项目名**）：

### 1. 一句话汇报安装状态与本机体检清单
- 汇报已安装的版本号、已接入的 AI 客户端列表、已打开本地控制台（以后随时可运行 `vmr ui` 重新打开）。
- **用一行亮出这台 Mac 实际检测到了什么**，例如：
  > 本机已检测到：Antigravity（✅/❌） · Codex CLI（✅/❌） · Claude Code CLI（✅/❌） · 苹果语音备忘录（✅/❌） · 离线声纹切段（✅）
- **补两句重要说明（防踩坑）**：
  - Codex / Claude Code / Cursor 首次接入 MCP 后，**请新开一条对话或重启客户端**以刷新 13 个 `vmr_*` 工具；
  - 后台守护每 24 小时会自动静默检查一次 GitHub 新版本，有新版会在控制台顶部横幅和 AI 对话中温和提示，点一下「一键更新并重启」或运行 `vmr update` 即可校验 SHA-256 并无感重启生效。

### 2. 严格按音频流水线顺序抛出以下 4 个独立问题（每题根据上方体检结果标出【本机推荐】）

> **请按下面 4 项直接回一个简短组合（例如「① + 1 + 1 + 先用默认」），我立刻帮你一次配好：**
>
> **1. 录音来源怎么来？**
> - **① 苹果语音备忘录自动监听**（适合 iPhone/Mac 录音，需 iCloud 同步；首次读取可能需授予完全磁盘访问权限）
> - **② 桌面「语音复盘投放箱」**（`~/Desktop/语音复盘投放箱`，不依赖 iCloud，拖进去自动跑）
> - **③ 仅在网页控制台手动拖入**（关闭后台监听）
> - *（说明：若选 ① 或 ②，我会自动帮你开启 **macOS 开机后台自启**，重启电脑不掉线、关终端也能默默跑，之后随时可在控制台关闭。）*
>
> **2. 用什么模型转写？（按本机已装环境动态推荐）**
> - *(若本机已装 Antigravity，优先标为【本机推荐】)* **1. Antigravity Flash**（免 API Key，原生听音并标注语气与叠话情绪）
> - *(若本机未装 Antigravity，根据实际推荐 2 或 3)* **2. Gemini API**（填自己的 Gemini Key，直连多模态含语气标注）
> - **3. OpenAI 兼容 ASR**（Whisper / Groq / SiliconFlow / 本地 Whisper 服务）
> - **4. 自定义本地命令行**（例如本机免费离线的 `whisper-cli` / `mlx_qwen3_asr`，整轨 JSON 自动对齐声纹）
>
> **3. 用什么 Agent / 模型分析？（按本机已装 AI 动态推荐，提前点破联动规则）**
> - *(若第 2 题选 1 `Antigravity Flash`，主动提示它和 Antigravity 是一体化工作流，标此项为【本机推荐】)* **1. Antigravity 后台自动分析**（与 `Antigravity Flash` 免 Key 联动；同时随时可以在 Codex / Claude 新对话里用 MCP 读取全稿追问或重分析）
> - *(若第 2 题选 2/3/4 独立转写，且本机检测到 Codex CLI 或 Claude Code CLI，优先标此项为【本机推荐】)* **2. 本机 Codex CLI（`codex-cli`）或 Claude Code CLI（`claude-cli`）**（免 API Key，直接复用你本机已登录账号完成后台自动复盘）
> - **3. 外部大模型 API**（DeepSeek / OpenAI / Gemini / 本地 Ollama）
> - **4. 仅自动转写，留待聊天里按需复盘**（后台转完即止，随时在 AI 对话里说一句按模板分析）
>
> **4.（可选）复盘提示词与背景要不要现在微调？**
> - 默认已内置开箱即用的 **「深度多镜头实战复盘」**（自动区分 S 级客户谈单/核心汇报、A 级内部讨论/深度思考、A-人情级熟人茶局饭局、C 级短测试极简归档，输出 30 秒速览快报、关键事实锚点、没接稳的话术与口语替代示范及经营/杠杆视角），另附会议纪要、销售沟通、口述灵感 3 套明文模板。
> - 直接回 **「先用默认」** 就能开跑；或者随便告诉我两三句话（你的职业角色、常录场景、容易转错的词、或模板想增删的板块），我立刻写入个人背景、词表和模板里。

---

## 第三阶段：收到用户回答后的自动落盘动作

1. **校验组合并写入配置**：
   - 若用户组合存在冲突（例如选了 `antigravity-flash` 转写 + `codex-cli` 自动分析），用 1 句人话解释原因并给出两个可行切换方案让用户确认，绝不静默写错配置。
   - 若在新对话且已加载 MCP，直接调用 `vmr_update_settings(...)`（含 `autostart_enabled`）；若仍在安装旧对话，用本机 Python 写入 `~/.config/voice-memo-review/config.toml`，并按需执行 `~/.local/bin/vmr autostart on`（选 ① 或 ② 时自动开启，选 ③ 时保持关闭）。
   - 如选了需要 API Key 的引擎，**绝不让用户把明文 Key 发进聊天窗口**，请引导用户在控制台「引擎插槽」填入（独立存入 `0600` 权限 `secrets.json`）。
2. **写入个人背景、词表与模板**：
   - 若用户提供了背景、易错词或模板修改要求，调用 `vmr_update_profile` / `vmr_save_template`（或写入 `~/.config/voice-memo-review/rules/`）。
3. **收尾核验并给出 3 句开箱指令**：
   - 运行 `~/.local/bin/vmr doctor` 确认全绿（包括开机自启与插槽组合校验）；
   - 提醒用户新开一条对话即可调用 13 个 MCP 工具，并给 3 句顺手示例：
     - 「看看我今天最新那条录音，按深度复盘模板整理重点」
     - 「帮我把默认复盘模板加上竞品对比一节，并更新我的易错词表」
     - 「帮我检查一下语音复盘有没有新版本」
