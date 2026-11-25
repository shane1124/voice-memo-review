# voice-memo-review（语音复盘工作台）

> **开箱即用的实战深度复盘 · 规则完全显化 · 四插槽全可插拔 · 跨 AI 客户端一键接入 · 零第三方 Python 依赖**

把你的语音备忘录、会议录音或拖入的任意音频，自动完成**本地轻量声纹切段 → 多引擎并发转写 → 按你看得见、改得动的明文实战提示词模板输出深度复盘**。

---

## 最快上手方式：直接把这句话发给你的 AI

不管你用的是 **Codex、Claude Code、Antigravity（反重力）还是 Cursor**，直接复制下面这段话发给它——你的 AI 会全自动帮你装完、接好插件、打开控制台，**并主动问你 3 个问题把引擎、个人背景、词表和复盘提示词一次调好**：

> 请阅读 https://github.com/shane1124/voice-memo-review/blob/main/AI_SETUP.md 并严格按其中的两阶段协议，在当前 Mac 上全自动帮我安装配置 `voice-memo-review`（含离线声纹免编译包、AI 客户端 MCP/Skill 接入与环境体检），装完后直接按协议问我那 3 个定制问题并帮我配好。

---

## 核心特性

- **默认就是能直接打仗的实战深度模板（告别空壳）**：
  - 默认启用 **「深度多镜头实战复盘」**（自动定级 `S 级客户谈单/核心汇报`、`A 级内部讨论/深度思考`、`A-人情级熟人茶局饭局`、`C 级短测试极简归档`），自动产出 **30 秒速览快报 + 核心事实锚点 + 没接稳的话术与 `[语气]` 潜台词拆解及自然口语替代 + 动态经营/杠杆视角**，严守六层事实口径不瞎编。
  - 同时内置 **标准会议纪要 / 客户销售沟通 / 个人口述灵感** 3 套模板，外加带示例指引的专有名词词表与个人背景说明；全部明文存放在「规则工作室」，可随时手动改，也可以在 AI 对话里一句话让它帮你改。
- **四插槽全可插拔，支持免 Key 调用本机 AI 客户端**：
  - **转写插槽**：Antigravity Flash（零 Key 含语气标注）/ Gemini API（直连含语气）/ OpenAI 兼容 ASR（Whisper / Groq / SiliconFlow / 本地 Whisper 服务）/ 自定义命令行 ASR（整轨 JSON 自动回贴声纹，5 分钟接本地 Qwen3-ASR / whisper.cpp / 火山豆包 / 阿里云）。
  - **后台自动分析插槽**：**Antigravity（免 Key 一体化）**、**Codex CLI（`codex-cli`，免 Key 直接用本机 Codex 账号）**、**Claude Code CLI（`claude-cli`，免 Key 直接用本机 Claude 账号）**、DeepSeek / OpenAI / Ollama / Gemini API，或留待 MCP 对话接手。
- **跨 AI 原生 MCP + 通用 Skill**：
  - 一行 `vmr install` 自动无损接入 **Antigravity、Claude Code、Codex、Cursor**（共 12 个 MCP 工具，支持查录音、转写、改模板、写个人背景与词表、切引擎、录声纹）。
  - **重要提示**：首次接入或更新 MCP 后，请**新开一条对话或重启 Codex / Claude Code / Cursor** 以刷新工具列表。
- **免 Xcode 编译**：Apple Silicon 离线分发包内置 10 MB `fluidaudiocli` 与 21 MB 离线声纹模型，解压 3 分钟开箱即用。

---

## 手动安装方式（不想用 AI 代装时）

### 方式 A：下载离线免编译分发包（推荐 Apple Silicon Mac）
1. 打开 [GitHub Releases](https://github.com/shane1124/voice-memo-review/releases/latest) 下载 `vmr-macos-arm64-vX.Y.Z.tar.gz` 并解压；
2. 双击 **`启动控制台.command`**（或在终端运行 `./install.sh`）；
3. 浏览器会自动打开本地极简控制台（仅监听 `127.0.0.1`）：
   - 在 **「引擎插槽」** 选择转写与分析引擎；
   - 在 **「规则工作室」** 查看/改写明文模板、词表背景或录入 15 秒声纹；
   - 在 **「工作台」** 拖入音频，或打开「苹果语音备忘录」/「自定义投放文件夹（免 iCloud）」自动监听。

### 方式 B：一键接入本机 AI 客户端
```bash
vmr install
```
接入完成后，**新开一条对话或重启 Codex / Claude Code / Cursor**，直接对 AI 说：
- “看看我今天最新那条录音，按深度复盘模板整理重点”
- “我是做 B 端 SaaS 的，平时常聊 ARR、交付验收，帮我把个人背景和词表写进去”
- “把默认复盘模板加上竞品对比一节”

---

## 四插槽架构一览

| 插槽 | 可选实现 | 说明 |
| :--- | :--- | :--- |
| **1. 音频输入** | `voice_memos` / `drop_folder` / 单文件拖拽 / MCP | 支持苹果语音备忘录与任意本地投放文件夹（无 iCloud 也能全自动） |
| **2. 本地预处理** | `fluidaudio`（默认）/ `none` | 10MB 本地 Core ML 声纹分离 + 250ms 防咬字切段 + 纯静音自动跳过；也可关闭声纹走均匀切块 |
| **3. 转写引擎** | `antigravity-flash` / `gemini-api` / `openai-compatible-asr` / `command-asr` | 共享断点缓存与 429 退避重试；`command-asr` 支持整轨 JSON 自动按时间轴回贴声纹 |
| **4. 规则与分析** | `antigravity-agentapi` / `codex-cli` / `claude-cli` / `openai-compatible-llm` / `gemini-llm` / `none` | 支持免 Key 直调本机 Codex / Claude / Antigravity；超 8 万字长稿自动分段提炼再汇总；规则全明文可改 |

---

## 常用命令

```bash
vmr ui                          # 启动/复用本地单守护并打开可视化控制台
vmr watch                       # 无头后台监听语音备忘录与投放文件夹
vmr run 录音.m4a --template meeting_minutes  # 单文件立刻转写分析
vmr enroll "我" 清晰自我介绍15秒.m4a           # 注册本地声纹
vmr install                     # 一键接入 Antigravity / Claude Code / Codex / Cursor
vmr uninstall                   # 无损卸载 AI 插件接入
vmr update                      # 从 GitHub Releases 校验 SHA256 安全升级（保留个人数据）
vmr doctor                      # 一键环境与插槽组合体检
```

## 隐私与安全
- 所有音频、声纹向量、词表、个人背景与分析稿均留在你的本机 `~/.config/voice-memo-review/`；
- API 密钥单独存放在 `secrets.json`（权限强制 `0600`），界面、配置文件与 MCP 工具均只显示脱敏掩码；
- 本地控制台严格绑定 `127.0.0.1` 并对所有写操作校验随机一次性 `X-VMR-Token` 与本机 `Origin`。
