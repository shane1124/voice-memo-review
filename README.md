# 录音自动分析（voice-memo-review）

> **极简录音自动转写与分析闭环 · 规则完全显化 · 四插槽全可插拔 · 开机自启与无感热更新 · 跨 AI 客户端一键接入 · 零第三方 Python 依赖**

把你的语音备忘录、会议录音或拖入的任意音频，自动完成**本地轻量声纹切段 → 多引擎并发转写 → 按你看得见、改得动的明文提示词模板输出结构化分析**。

---

## 最快上手方式：直接把这句话发给你的 AI

不管你用的是 **Codex、Claude Code、Antigravity（反重力）还是 Cursor**，直接复制下面这段话发给它——你的 AI 会全自动帮你装完、体检、接入插件、打开控制台，**再根据你这台 Mac 实际装的软件，按「来源 → 转写模型 → 分析 Agent → 量身定制你的专属分析规则与偏好」4 个清晰问题一步配好**：

> 请阅读 https://github.com/shane1124/voice-memo-review/blob/main/AI_SETUP.md 并严格按其中的三阶段协议，在当前 Mac 上全自动帮我安装配置录音自动分析工具 `voice-memo-review`（含离线声纹免编译包、AI 客户端 MCP/Skill 接入与环境体检），装完后先亮出我本机实际检测到的可用引擎，再按协议里的 4 个问题给我带推荐的选择，并帮我结合我的工作场景定制分析提示词与偏好设置。

---

## 核心特性

- **通用开箱示例 + AI 结合你的工作量身定制规则**：
  - 内置 4 套干净通用的参考模板：**通用录音分析（默认示例）/ 标准会议纪要 / 客户拜访与销售沟通 / 个人复盘与口述整理**，外加带示例指引的专有名词词表与个人背景说明；
  - 更推荐安装完直接让你的 AI 结合你平时的工作场景（口述或经你授权扫描工作目录）为你量身生成一套专属分析模板与易错词表，所有规则 100% 明文存放在本机「规则工作室」，随时可改可预览。
- **四插槽全可插拔，支持免 Key 调用本机 AI 客户端**：
  - **转写插槽**：Antigravity Flash（零 Key 含语气标注）/ Gemini API（直连含语气）/ OpenAI 兼容 ASR（Whisper / Groq / SiliconFlow / 本地 Whisper 服务）/ 自定义命令行 ASR（支持整轨或逐段，5 分钟接本地 Qwen3-ASR / whisper.cpp / 火山豆包 / 阿里云）。
  - **后台自动分析插槽**：Antigravity、Codex CLI、Claude CLI、DeepSeek / OpenAI / Ollama / Gemini API，或只转写、留待 MCP 对话接手。`claude-cli` 是独立后台进程，须登录 CLI；`--desktop --resume` 只负责在成功后打开对话，不提供桌面登录。若想用已登录的 Claude 桌面版且不另登 CLI，使用下方的「Claude 桌面接手」。
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
| **4. 规则与分析** | `antigravity-agentapi` / `codex-cli` / `claude-cli` / `openai-compatible-llm` / `gemini-llm` / `none` | 后台引擎可独立分析；`none` 保留待分析转录稿，供已登录的客户端接手 |

---

## Claude 桌面接手（不用登录独立 CLI）

VMR 继续负责监听与转写，**Claude 桌面版的 Code 会话**读取待分析转录稿，使用当前 App 账号分析，并通过 MCP 把 Markdown 存回 VMR。支持本地 Qwen/Whisper 等独立转写；不适用于依赖 Antigravity 工作流的 `antigravity-flash`。

1. VMR 控制台 → **引擎插槽**：保留独立转写，在后台分析选择 **仅自动转写／由 MCP 会话接手**（`none`）。不用改变录音来源、声纹、模板或自启。确保 `vmr install` 已为 Claude Code 接入 `voice-memo-review` MCP。
2. **手动接手**：新开一个已登录的本机 Claude 桌面 Code 会话，说「按 VMR 的模板整理最新待分析录音并保存」。如果只是旧的 CLI 分析失败，已有转录稿也可以在该会话中手动指定，不用重录。
3. **自动接手（可选）**：在支持 Mod 的 Claude 桌面 Code 中，通过输入框旁的 **+ → 插件 → 添加市场**，添加本仓库并安装 `voice-memo-desktop`。测试分支可以先下载到本机，再添加该分支 checkout 的文件夹（含 `.claude-plugin/marketplace.json`）作为本地市场。安装不会自动提交分析；在一个专用本机 Code 会话里主动输入 `/vmr-desktop-start` 才开始，输入 `/vmr-desktop-stop` 停止接手新录音。MCP 工具仍遵循会话的权限提示。

插件约每 15 秒检查一次「待分析」，在该会话空闲时处理一条。不重复提交同一条失败的任务，可以手动处理或停止后再启动。只在**桌面 App 和接手会话仍运行**时分析；关闭后 VMR 可继续转写排队，重开后需重新启动接手。结果留在指定会话，不自动切换按日窗口。转录文本和分析规则会发送给你选用的 Claude 服务；原音不由此插件上传。

**同步测试分支不等于发布安装包。** 默认安装协议下载 GitHub 最新 Release。新插件可从该分支单独装入，兼容已安装的 VMR v1.5.1 MCP；若要让默认下载也带上新版控制台和文档，需另行构建并发布 Release。测试无需删除用户数据目录。

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
