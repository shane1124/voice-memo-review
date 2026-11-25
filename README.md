# voice-memo-review（语音复盘工作台）

> **规则完全显化 · 四插槽全可插拔 · 跨 AI 客户端一键接入 · 零第三方 Python 依赖**

把你的语音备忘录、会议录音或拖入的任意音频，自动完成**本地轻量声纹切段 → 多引擎并发转写 → 按你看得见、改得动的明文提示词模板输出深度复盘**。

- **彻底告别黑盒提示词**：内置 4 套通用明文模板（通用对话复盘 / 标准会议纪要 / 客户销售沟通复盘 / 个人口述灵感整理），随时在控制台「规则工作室」编辑并**一键预览发给 AI 的完整 Prompt**。
- **不绑死任何一家厂商**：自由插拔转写与分析引擎——支持 **Antigravity Flash（零 Key 含语气）**、**Gemini API（直连含语气）**、**OpenAI 兼容 ASR（Whisper / Groq / SiliconFlow / 本地 whisper.cpp）**、**自定义命令行 ASR（整轨 JSON 自动对齐声纹，5 分钟接火山豆包/阿里云/本地 CLI）**，以及 **DeepSeek / OpenAI / Claude 网关 / Gemini / Ollama** 或直接交给你的 AI 客户端分析。
- **跨 AI 原生插件**：一行 `vmr install` 自动接入 **Antigravity（反重力）、Claude Code、Codex、Cursor**（统一 Stdio MCP Server + 通用 `SKILL.md`）。
- **免 Xcode 编译**：Apple Silicon 分发包内置预编译 10 MB `fluidaudiocli` 与 21 MB 离线声纹模型，解压 3 分钟开箱即用。

---

## 3 分钟快速上手

### 方式 A：使用离线免编译分发包（推荐同事使用）
1. 下载最新 [GitHub Release](https://github.com/shane1124/voice-memo-review/releases) 中的 `vmr-macos-arm64-vX.Y.Z.tar.gz` 并解压；
2. 双击 **`启动控制台.command`**（或在终端运行 `./install.sh`）；
3. 浏览器会自动打开本地控制台（仅监听 `127.0.0.1`）：
   - 在 **「引擎插槽」** 选择你的转写与分析引擎（本机装了 Antigravity 直接零配置可用，或填入你自己的 API Key）；
   - 在 **「规则工作室」** 查看/改写明文分析模板、专有名词词表或上传 15 秒样音录入声纹；
   - 在 **「工作台」** 拖入一条录音，或打开「苹果语音备忘录」/「自定义投放文件夹（免 iCloud）」自动监听！

### 方式 B：一键接入你的 AI 客户端（Antigravity / Claude Code / Codex / Cursor）
```bash
vmr install
```
自动检测本机已安装的 AI 客户端并无损写入 MCP Server 与通用 `SKILL.md`（修改既有配置前自动备份 `.bak`；Antigravity 写入独立插件目录，不改动全局 `config.json`）。之后直接对 AI 说：
> “帮我复盘桌面那段会议录音，按标准会议纪要模板输出”
> “把我的销售复盘模板加上竞品对比一节”

---

## 四插槽架构一览

| 插槽 | 可选实现 | 说明 |
| :--- | :--- | :--- |
| **1. 音频输入** | `voice_memos` / `drop_folder` / 单文件拖拽 / MCP | 支持苹果语音备忘录与任意本地投放文件夹（无 iCloud 也能全自动） |
| **2. 本地预处理** | `fluidaudio`（默认）/ `none` | 10MB 本地 Core ML 声纹分离 + 250ms 防咬字切段 + 纯静音自动跳过；也可关闭声纹走均匀切块 |
| **3. 转写引擎** | `antigravity-flash` / `gemini-api` / `openai-compatible-asr` / `command-asr` | 共享断点缓存与 429 退避重试；`command-asr` 支持整轨 JSON 自动按时间轴回贴声纹 |
| **4. 规则与分析** | `antigravity-agentapi` / `openai-compatible-llm` / `gemini-llm` / `none (MCP 接手)` | 超 8 万字长稿自动分块提炼再汇总；全部模板明文存放于 `~/.config/voice-memo-review/rules/templates/` |

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
- 所有音频、声纹向量、词表与分析稿均留在你的本机 `~/.config/voice-memo-review/`；
- API 密钥单独存放在 `secrets.json`（权限强制 `0600`），界面与接口只显示脱敏掩码；
- 本地控制台严格绑定 `127.0.0.1` 并对所有写操作校验随机一次性 `X-VMR-Token` 与本机 `Origin`。
