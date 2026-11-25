# voice-memo-review（语音复盘工作台）

> **极简录音复盘闭环 · 规则完全显化 · 四插槽全可插拔 · 开机自启与无感热更新 · 跨 AI 客户端一键接入 · 零第三方 Python 依赖**

把你的语音备忘录、会议录音或拖入的任意音频，自动完成**本地轻量声纹切段 → 多引擎并发转写 → 按你看得见、改得动的明文提示词模板输出结构化复盘**。

---

## 最快上手方式：直接把这句话发给你的 AI

不管你用的是 **Codex、Claude Code、Antigravity（反重力）还是 Cursor**，直接复制下面这段话发给它——你的 AI 会全自动帮你装完、体检、接入插件、打开控制台，**再根据你这台 Mac 实际装的软件，按「来源 → 转写模型 → 分析 Agent → 量身定制你的专属复盘规则与偏好」4 个清晰问题一步配好**：

> 请阅读 https://github.com/shane1124/voice-memo-review/blob/main/AI_SETUP.md 并严格按其中的三阶段协议，在当前 Mac 上全自动帮我安装配置 `voice-memo-review`（含离线声纹免编译包、AI 客户端 MCP/Skill 接入与环境体检），装完后先亮出我本机实际检测到的可用引擎，再按协议里的 4 个问题给我带推荐的选择，并帮我结合我的工作场景定制提示词与偏好设置。

---

## 核心特性

- **通用开箱示例 + AI 结合你的工作量身定制规则**：
  - 内置 4 套干净通用的参考模板：**通用对话复盘（默认示例）/ 标准会议纪要 / 客户拜访与商务沟通 / 个人复盘与口述整理**，外加带示例指引的专有名词词表与个人背景说明；
  - 更推荐安装完直接让你的 AI 结合你平时的工作场景（口述或经你授权扫描工作目录）为你量身生成一套专属复盘模板与易错词表，所有规则 100% 明文存放在本机「规则工作室」，随时可改可预览。
- **四插槽全可插拔，支持免 Key 调用本机 AI 客户端**：
  - **转写插槽**：Antigravity Flash（零 Key 含语气标注）/ Gemini API（直连含语气）/ OpenAI 兼容 ASR（Whisper / Groq / SiliconFlow / 本地 Whisper 服务）/ 自定义命令行 ASR（支持整轨或逐段，5 分钟接本地 Qwen3-ASR / whisper.cpp / 火山豆包 / 阿里云）。
  - **后台自动分析插槽**：**Antigravity（免 Key 一体化）**、**Codex CLI（`codex-cli`，免 Key 直接用本机 Codex 账号）**、**Claude Code CLI（`claude-cli`，免 Key 直接用本机 Claude 账号）**、DeepSeek / OpenAI / Ollama / Gemini API，或留待 MCP 对话接手。
- **macOS 开机后台自启 + 无感安全热更新**：
  - 一键 `vmr autostart on` 或控制台开关开启标准 `launchd` 后台常驻（开机自启、关终端不掉线、崩溃自动拉起）。
  - 守护进程每 24 小时带 `ETag` 静默检查一次 GitHub 新版本；有新版时在控制台顶部横幅与 AI 对话里温和提醒；点击「一键更新并重启」或运行 `vmr update` 自动避开正在转写的录音，校验 `SHA256SUMS` 原子替换并自动无感重启刷新页面。
- **跨 AI 原生 MCP（13 个工具）+ 通用 Skill**：
  - 一行 `vmr install` 自动无损接入 **Antigravity、Claude Code、Codex、Cursor**；首次接入后请**新开一条对话或重启客户端**刷新工具列表。
- **免 Xcode 编译**：Apple Silicon 离线分发包内置 10 MB `fluidaudiocli` 与 21 MB 离线声纹模型，解压 3 分钟开箱即用。

---

## 四插槽架构一览

| 插槽 | 可选实现 | 说明 |
| :--- | :--- | :--- |
| **1. 音频输入** | `voice_memos` / `drop_folder` / 单文件拖拽 / MCP | 支持苹果语音备忘录（需 iCloud 同步）与任意本地投放文件夹（免 iCloud） |
| **2. 本地预处理** | `fluidaudio`（默认）/ `none` | 10MB 本地 Core ML 声纹分离 + 250ms 防咬字切段 + 纯静音自动跳过；也可关闭声纹走均匀切块 |
| **3. 转写引擎** | `antigravity-flash` / `gemini-api` / `openai-compatible-asr` / `command-asr` | 共享断点缓存与 429 退避重试；`command-asr` 支持整轨 JSON 自动按时间轴回贴声纹 |
| **4. 规则与分析** | `antigravity-agentapi` / `codex-cli` / `claude-cli` / `openai-compatible-llm` / `gemini-llm` / `none` | 支持免 Key 直调本机 Codex / Claude / Antigravity；超 8 万字长稿自动分段提炼再汇总；规则全明文可改 |

---

## 常用命令

```bash
vmr ui                          # 启动/复用本地单守护并打开可视化控制台
vmr autostart on                # 开启 macOS 开机后台自启（off 关闭 / status 查看）
vmr watch                       # 前台无头常驻监听语音备忘录与投放文件夹
vmr run 录音.m4a --template meeting_minutes  # 单文件立刻转写分析
vmr enroll "我" 清晰自我介绍15秒.m4a           # 注册本地声纹
vmr install                     # 一键接入 Antigravity / Claude Code / Codex / Cursor
vmr uninstall                   # 无损卸载 AI 插件接入
vmr update                      # 校验 SHA256 安全升级并自动热重启（保留个人数据）
vmr doctor                      # 一键环境、开机自启与插槽组合体检
```

## 隐私与安全
- 所有音频、声纹向量、词表、个人背景与分析稿均留在你的本机 `~/.config/voice-memo-review/`；
- API 密钥单独存放在 `secrets.json`（权限强制 `0600`），界面、配置文件与 MCP 工具均只显示脱敏掩码；
- 本地控制台严格绑定 `127.0.0.1` 并对所有写操作校验随机一次性 `X-VMR-Token` 与本机 `Origin`。
