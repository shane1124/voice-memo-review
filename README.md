# 录音自动分析（voice-memo-review）

> **极简录音自动转写与分析闭环 · 支持 Antigravity / Codex / Claude / WorkBuddy 四大桌面版 · 随录随记零门槛声纹 · 规则全透明可改 · 零第三方 Python 依赖**

把你的苹果语音备忘录、会议录音或拖入的任意音频，自动完成 **本地轻量声纹分人 → 语音转文字 → 按你看得见、改得动的明文模板生成结构化分析**，并直接把分析窗口摆到你的桌面 AI 客户端里随时追问。

---

## 最快上手：直接把这句话发给你的桌面 AI

不管你用的是 **Antigravity（反重力）、Codex、Claude、WorkBuddy 还是 Cursor**，直接复制下面这段话发给它——AI 会全自动装完、体检、接入插件并打开控制台，再用 **4 个大白话问题** 一步帮你配好：

> 请阅读 https://github.com/shane1124/voice-memo-review/blob/main/AI_SETUP.md 并严格按其中的三阶段协议，在当前 Mac 上全自动帮我安装配置录音自动分析工具 `voice-memo-review`（含离线声纹免编译包、桌面版 AI 插件接入与环境体检），装完后先亮出我本机实际检测到的可用桌面版引擎，再按协议里的 4 个问题给我带推荐的选择，并帮我结合我的工作场景定制分析提示词与偏好设置。

---

## 核心特性

- **四大桌面 AI 全部免 Key 直连（零外部 CLI 依赖）**：
  - 自动优先直调桌面 App 安装包自带本体，无需额外安装任何命令行工具：
    - **Antigravity 桌面版**（免费听写 + 自动推送对话）
    - **Codex 桌面版**（复用已登录账号，分析完自动弹出对话窗）
    - **Claude 桌面版**（完成 1 次官方浏览器授权即可免 Key 自动弹出对话窗，同步接入 `claude_desktop_config.json`）
    - **WorkBuddy 桌面版**（直接复用已登录账号，同步接入 `~/.workbuddy/.mcp.json` 与技能，分析完直开桌面端）
  - 也随时可填 Key 使用 **在线语音转写 API**（Gemini、硅基流动、Groq、Whisper 等）、**本机免费离线转写**（Qwen3-ASR / Whisper）或 **在线大模型 API**（DeepSeek / Kimi / 通义 / Gemini / OpenAI）。
- **随录随记零门槛声纹认人**：
  - 不需要专门录自我介绍样音！每次录音识别到尚未登记的主要说话人时，系统会在分析末尾温和问一句 **「识别到 X 位主要说话人，要不要记住备注？」**，你只需顺口回一句（例如「SPEAKER_00 是我，SPEAKER_01 是王总」），或在控制台阅读页填备注点「记住声音」，声音特征立刻存入本机并自动回填真名，**下一次再录到他们自动认人**。
- **日期置前窗口命名 + 清晰透明规则**：
  - 默认每天自动合并成一个清晰日期窗口（形如 **`11-26-2026 录音分析`**），也随时可切为每条新录音单独开窗或按周合并；
  - 内置 4 套通用参考示例模板，更推荐由你的 AI 结合工作场景为你量身定制专属模板与易错词表，所有规则 100% 明文保存在本机「规则工作室」。
- **macOS 开机自启 + 无感安全热更新 + 免 Xcode 编译**：
  - 一键 `vmr autostart on` 或控制台开关开启开机后台常驻；每 24 小时静默检查 GitHub 新版本，确认后校验 SHA-256 原子升级热重启；
  - Apple Silicon 离线包内置 10 MB `fluidaudiocli` 与 21 MB 离线声纹模型，解压即用。

---

## 四步可插拔架构一览

| 步骤 | 可选方式 | 说明 |
| :--- | :--- | :--- |
| **1. 录音来源** | 苹果语音备忘录 / 桌面投放箱 / 网页拖拽 | 支持 iPhone/Mac 录完自动同步（需开 iCloud），也支持免 iCloud 桌面拖入 |
| **2. 离线声纹切段** | FluidAudio 本地声纹（默认）/ 均匀切块 | 10MB 本地 Core ML 声纹分离 + 防咬字切段 + 自动跳过纯静音 + 随录随记建声纹 |
| **3. 语音转文字** | Antigravity 免费听写 / 在线转写 API / 本机离线转写 | 支持 Gemini、Whisper、硅基流动、Groq 或本机 Qwen3-ASR / whisper.cpp，自动回贴声纹 |
| **4. 自动分析与开窗** | Antigravity / Codex / Claude / WorkBuddy 桌面版 / 在线大模型 API / 仅转文字 | 免 Key 直连四大桌面端，默认按天日期置前命名（`MM-DD-YYYY 录音分析`），超长稿自动分段汇总 |

---

## 常用命令

```bash
vmr ui                          # 启动/复用本地单守护并打开可视化控制台
vmr autostart on                # 开启 macOS 开机后台自启（off 关闭 / status 查看）
vmr watch                       # 前台常驻监听语音备忘录与投放文件夹
vmr run 录音.m4a                 # 单文件立刻转写分析
vmr install                     # 一键接入 Antigravity / Claude 桌面版 / Codex / WorkBuddy / Cursor
vmr auth-claude                 # 完成 1 次官方 Claude 浏览器授权（开启 Claude 桌面版自动弹对话）
vmr uninstall                   # 无损卸载 AI 插件接入
vmr update                      # 校验 SHA256 安全升级并自动热重启（保留个人数据）
vmr doctor                      # 一键环境、登录态、权限与开机自启体检
```

## 隐私与安全
- 所有音频、声纹向量、词表、个人背景与分析稿均只保存在你的本机 `~/.config/voice-memo-review/`；
- API 密钥单独存放在 `secrets.json`（权限强制 `0600`），界面、配置文件与对话工具均只展示脱敏掩码；
- 本地控制台严格绑定 `127.0.0.1`，所有写操作校验随机一次性 `X-VMR-Token` 与本机 `Origin`。
