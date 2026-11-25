# voice-memo-review 可插拔语音复盘产品设计文档

- **日期**：2026-11-25
- **所属仓库**：`.`
- **产品形态**：方案三「一核两壳」分阶段落地（Phase 1：可插拔内核 + 跨 AI MCP/Skill 一键插件 + 本地可视化控制台；Phase 2：原生 SwiftUI 菜单栏 Mac App + Sparkle 自动更新）
- **状态**：已批准定稿（Approved · 2026-11-25）
- **发布源**：公开 GitHub 仓库（GitHub Releases + `SHA256SUMS`，目标账号 `shane1124` 相关新用户名，仓库路径通过配置 `update.repo` 解耦支持无缝切换）

---

## 1. 产品意图与背景（Intent & Why）

当前项目已经验证了一套极高质量的音频处理核心：**本地轻量声纹分离 + 物理气口切段 + 并发精转 + 深度复盘**（实测日常 30～60 分钟录音约 4～6 分钟出稿，2～12 小时超长录音约 9～10 分钟出稿）。

但现有版本存在 4 个阻碍对外交付给同事使用的结构性问题：
1. **绑死单一生态**：转写与分析强绑定 Google / Antigravity（`agentapi` + `run_workflow`），使用 Claude Code、Codex、OpenAI、国内大模型或其他转写服务的同事无法开箱使用。
2. **规则隐性化与私货耦合**：分析维度与提示词偏个人定制且隐蔽在配置文件/单文件中，他人看不懂分析依据，也难以改成适合自己岗位的复盘框架。
3. **输入强绑 iCloud 语音备忘录**：没有开启 iCloud 同步的同事，或想处理微信导出/录音笔/会议录屏音频时，缺少自动投放文件夹和一键运行入口。
4. **装机与运维门槛高**：文档要求自行用 Xcode/Swift 编译 `FluidAudio`；现网也暴露出纯静音录音报错（`noSpeechDetected`）、超长录音超时误标 `done`、缺单实例锁、缺 0.35s 全局发射节流锁等问题。

本设计的目标是把 `voice-memo-review` 重塑为一个**干净、规则显化、全链路可插拔、可持续自动更新的通用产品**，第一阶段先让同事零门槛用上，第二阶段无痛套壳演进为正式原生 Mac App。

---

## 2. 目标、非目标与反目标

### 2.1 核心目标（Goals）
1. **闭环聚焦「自动分析产出」**：输入录音 → 自动（可选）声纹切段 → 转录稿落盘 → 按用户可见可改的规则生成结构化分析稿并落盘。
2. **规则完全显化（Rule Studio）**：所有分析提示词模板、专有名词词表、个人角色背景（默认空）、声纹名册均以明文文件存储并可在界面/AI 中直接查看、预览最终拼装 Prompt、复制与编辑。
3. **核心四段全可插拔**：音频输入源、本地声纹切段、转写引擎（Transcriber）、分析引擎（Analyzer）全部抽象为标准接口，允许任意组合，不绑死任何一家模型厂商。
4. **跨 AI 原生友好**：提供标准 MCP Server + 通用 `SKILL.md` + 一键安装器，自动适配 **Antigravity（反重力）、Claude Code、Codex、Cursor** 各自的配置文件路径与格式。
5. **非技术同事零命令行可用**：Phase 1 即内置零依赖本地可视化控制台（`vmr ui`），支持拖拽录音、点选引擎、可视化编辑规则模板、录声纹、一键安装 AI 插件与查看队列进度。
6. **免编译分发与可更新**：随分发包内置 10MB 预编译 `fluidaudiocli`（零外部动态库依赖），彻底告别 Xcode 编译；提供清晰的升级路径（Phase 1 `vmr update` → Phase 2 Sparkle 自动更新）。

### 2.2 明确非目标（Non-Goals）
- **不包含任何微信机器人 / `clawbot` / Hermes 通知推送链路**。
- **不包含个人微信聊天采集、电脑/浏览器行为轨迹采集、日终多维汇总**（这些继续留在作者私人总控台 `下游私有集成项目` 中独立维护）。
- **不包含 Windows / Linux 原生桌面 App 打包**（核心 Python CLI/MCP 在非 macOS 可通过关闭 `FluidAudio` 预处理运行，但首期分发与桌面壳以 macOS Apple Silicon 为主）。
- **不包含云端 SaaS 账号体系或服务器端存储**：所有录音、声纹、词表、API Key、转录稿与分析稿均留在用户本机。

### 2.3 反目标（Anti-Goals：即使技术跑通也算失败）
- **绝不把作者私人复盘视角/业务专词/人物名带入默认模板或代码**。
- **绝不让黑盒 Prompt 控制分析结果**：发给模型的完整系统/用户提示词必须能让用户一眼看到并修改。
- **绝不要求同事安装 Xcode 或手动编辑 JSON/TOML 才能跑通第一条录音**。
- **绝不破坏现有私人总控台对本仓库的 Python 导入兼容**：`voice_review.diarizer`（`FluidAudioDiarizer`、`VoiceprintStore`）、`voice_review.segmenter`（`SegmenterConfig`）、`voice_review.workflow_builder`（`prepare_v2_job`、`render_workflow_script`、`sanitize_transcript_field`）的既有公开签名必须保持向后兼容。

---

## 3. 方案对比与已选路线（Approaches）

在设计讨论阶段对比了 3 套产品形态：

| 方案 | 形态 | 主要优势 | 主要代价 | 结论 |
| :--- | :--- | :--- | :--- | :--- |
| 方案一 | 纯跨 AI 插件包（MCP + Skill + 文件目录） | 开发最快、无签名拦截、极轻 | 非技术同事改模板/填 Key/拖录音门槛高，缺可视进度 | 放弃（覆盖面过窄） |
| 方案二 | 一步到位原生 SwiftUI Mac App + Sparkle | 原生体验一步到位 | 前期工期长；未公证前首次打开有 Gatekeeper 弹窗 | 放弃（首版验证太慢） |
| **方案三（已选）** | **「一核两壳」分阶段落地**：统一可插拔内核 → Phase 1 跨 AI 插件 + 内置零依赖本地控制台（`vmr ui`） → Phase 2 套原生 SwiftUI 菜单栏壳 + Sparkle | 第一阶段几天内交付可用产品（兼顾技术与非技术同事，零公证弹窗）；第二阶段 100% 复用内核与本地 API 无缝升级为正式 Mac App | 需在 Phase 1 就严格定义核心边界、配置目录规范与本地 HTTP API 契约 | **采用** |

---

## 4. 技术约束与安全边界

1. **核心零第三方 Python 运行时依赖**：继续坚持 Python 3.11+ 纯标准库（`tomllib`、`sqlite3`、`urllib.request`、`http.server`、`concurrent.futures`、`select.kqueue`、`fcntl`、`json`、`re`），杜绝虚拟环境/依赖地狱，且极大减轻后续打入 `.app` 的体积。
2. **原生二进制与离线模型策略（架构裁决：真正零依赖、免 Xcode）**：
   - `fluidaudiocli`（Apple Silicon 预编译二进制仅 **10 MB**，`otool -L` 已验证 100% 链接 macOS 系统框架、零 Homebrew 动态库依赖）：直接随发布包内置于受管目录 `bin/fluidaudiocli`。
   - **Core ML 声纹模型（21 MB）双保险策略**：
     1. Phase 1 离线分发包**直接内置 21 MB 声纹模型**，`install.sh` 首次安装时自动预置到 `~/Library/Application Support/FluidAudio/Models/speaker-diarization`（若该目录尚不存在），同事彻底免科学上网、零首次下载失败；
     2. 同步升级 `voice_review/diarizer.py`：保留 `offline_mode: bool = True` 默认签名兼容 `下游私有集成项目`，新增参数 `auto_download_if_missing: bool = True`——仅当本地模型目录不存在时自动放行首次联网下载（保留代理环境变量），模型一旦就位即切回严格离线模式零网络请求。
   - **`ffmpeg` / `ffprobe` 静态内嵌策略**：Phase 1 分发包与 Phase 2 Mac App **均随包内置静态编译 arm64 `ffmpeg` / `ffprobe`**（与 `fluidaudiocli` + 21MB 模型合计整包约 **55～65 MB**），彻底避免在无 Homebrew 的全新 Mac 上触发 Xcode Command Line Tools 安装弹窗；运行时按「内置 `bin/` → 配置指定路径 → 系统 `PATH`」顺序探测。
3. **程序受管目录与用户数据彻底分离（升级不丢数据、不破坏 MCP 路径）**：
   - **受管程序目录**：`~/.local/share/voice-memo-review/`（`install.sh` 与 `vmr update` 原子替换该目录，并通过稳定软链/启动器 `~/.local/bin/vmr` 暴露命令，因此写入 Antigravity / Claude Code / Codex / Cursor 的 MCP 启动命令永远固定不变；若检测到从 `.git` 开发仓源码直接运行，`vmr update` 拒绝自覆盖）。
   - **用户数据目录**：`VMR_HOME`（默认 `~/.config/voice-memo-review/`，兼容 `--config` 覆盖），更新程序绝不触碰用户数据：
     ```text
     ~/.config/voice-memo-review/
     ├── config.toml            # 主配置（不含敏感密钥）
     ├── secrets.json           # API Key 专用文件，权限强制 0600，绝不导出/同步
     ├── state.sqlite           # 队列与处理状态库
     ├── daemon.lock            # 单实例 fcntl 进程锁（统一守护进程持有）
     ├── daemon.json            # 当前运行中守护进程的 port / pid / token 信息（权限 0600）
     ├── voiceprints.json       # 本地声纹向量库
     ├── rules/
     │   ├── templates/         # 明文分析提示词模板（含内置拷贝与用户自建）
     │   ├── glossary.txt       # 专有名词校对词表
     │   └── persona.md         # 可选：用户自定义角色/关注点（默认空）
     ├── work/                  # 临时切段与断点缓存（完成后自动清理）
     └── logs/                  # 轮转日志
     ```
4. **本地控制台安全边界**：
   - `vmr ui` **严禁监听 `0.0.0.0`**，只绑定 `127.0.0.1`（默认随机或可配空闲端口如 `18765`）；
   - 启动时生成随机 32 字节一次性会话 Token，写入本地打开链接 `?token=...` 并在写操作（保存配置/密钥、上传音频、触发安装、编辑模板）强制校验 `X-VMR-Token` 与 `Origin/Host` 为 `127.0.0.1` / `localhost`，防恶意网页跨站攻击；
   - API 返回的配置对象默认对 `secrets.json` 中的密钥做脱敏掩码（如 `sk-****1234`），前端仅在用户主动输入新值时覆盖。

---

## 5. 核心架构设计：四插槽可插拔管线（`vmr-core`）

```text
[插槽1: 输入源]           [插槽2: 本地预处理]        [插槽3: 转写引擎]           [插槽4: 显化规则 + 分析器]
┌──────────────────┐     ┌───────────────────┐     ┌──────────────────────┐     ┌─────────────────────────┐
│• Apple 语音备忘录│     │• FluidAudio 声纹  │     │• antigravity-flash   │     │ 显化规则库 (rules/):    │
│  (kqueue 秒唤醒) │     │  + 250ms 物理切段 │     │  (并发直听+语气)     │     │ • templates/*.md        │
│• 自定义投放目录  │ ──▶ │  + noSpeech 跳过  │ ──▶ │• gemini-api          │ ──▶ │ • glossary.txt          │
│  (免 iCloud)     │     │• none (纯切块模式 │     │  (直连多模态+语气)   │     │ • persona.md            │
│• 单文件 / 拖拽   │     │  免声纹/跨平台)   │     │• openai-compat-asr   │     │                         │
│• MCP 工具调用    │     └───────────────────┘     │  (Whisper/Groq/本地) │     │ 分析引擎 (Analyzer):    │
└──────────────────┘                               │• command-asr         │     │ • mcp-host (AI客户端)   │
                                                   │  (任意CLI/国内ASR)   │     │ • antigravity-agentapi  │
                                                   └──────────────────────┘     │ • openai-compat-llm     │
                                                                                │ • gemini-llm            │
                                                                                └─────────────────────────┘
```

### 5.1 插槽 1：音频输入（Input Sources）
- **多源统一发现抽象** `AudioSource`：每个输入源产出标准化 `AudioCandidate(recording_id, audio_path, title, recorded_at, duration_sec, source_kind)`。
- **支持 3 种入口（可独立开关或同时启用）**：
  1. `voice_memos`：复用现有 `VoiceMemoScanner`（监听 `CloudRecordings.db` + WAL + 目录 `kqueue`）。
  2. `drop_folder`：监听用户配置的任意本地文件夹（如 `~/Desktop/语音复盘投放箱`），扫描 `.m4a / .mp3 / .wav / .flac / .ogg / .aac / .mp4`，以文件 `sha256` 前 16 位 + 文件大小作为 `recording_id` 防重，复用 12 秒写稳防抖；支持配置完成后是否自动归档原音频到 `_processed/` 子目录（默认不移动原文件，只记状态库防重）。
  3. `single_file`：`vmr run <音频文件> [--template <id>]`、控制台上传接口或 MCP 工具直接触发，绕过防抖立即进入队列。

### 5.2 插槽 2：本地预处理与声纹切段（Preprocessor）
- **模式 A：`fluidaudio`（默认）**
  - 复用 `FluidAudioDiarizer` + `segmenter.py`（250ms 前气口 + 150ms 后余韵 + 裂变声纹核对组）。
  - **关键缺陷修复**：捕获 `FluidAudio` 输出中的 `noSpeechDetected`（或 0 有效发言段），不再抛异常重试，而是返回标准化 `EmptySpeechResult`，状态机将其标记为 `skipped`（可选写出极简空录说明，不消耗任何转写/分析 API 费用）。
- **模式 B：`none`（关闭声纹 / 通用切块兜底）**
  - 适用于未安装 `FluidAudio`、非 Apple Silicon 机器、或单人口述极速场景。
  - 直接用 `ffmpeg` 按时长（默认每段 ≤ 10 分钟、32kbps 单声道 MP3，确保远低于 Whisper 25MB 单文件上限）做均匀切块，产出统一 `PreparedAudioJob` 结构供下游所有转写插槽无差别消费。

### 5.3 插槽 3：可插拔转写引擎（Transcriber Providers）
定义统一接口 `TranscriberProvider`：
- **输入**：`PreparedAudioJob`（包含切段清单 `segments`、批次 `batches`、声纹摘要、专有名词词表 `glossary`）+ `RuntimeContext`（断点缓存目录 `batches_out/`、进度回调函数 `on_progress`）。
- **输出**：`TranscriptOutcome`（`mode="direct"` 表示已完成转写并落盘 `transcript_path`；`mode="delegated_workflow"` 表示生成了交由宿主 Antigravity 执行的 `workflow_script_path`，用于 `antigravity-flash` 零费用路径）。

**Phase 1 首发内置 4 个 Provider**：

| Provider ID | 适用人群与工作原理 | 语气/叠话标注 | 所需凭证 |
| :--- | :--- | :---: | :--- |
| `antigravity-flash` | 复用强化版 `workflow_builder.py`（**必须补齐 0.35s 全局发射节流锁 + `_PARSER_ERR_RE` 清洗 + 120s 自动对半拆包 + 自动定义 `audio_transcriber_lite` 引导**），由 Antigravity 宿主执行并发精转 | 支持 | 零 API Key（需本机 Antigravity） |
| `gemini-api` | 纯标准库并发调用 Gemini `generateContent`（inline base64 audio，支持自定义 `base_url` 与模型名如 `gemini-2.5-flash`），按批次发切段并解析 JSON 输出，共享 `batches_out/` 断点缓存与 429 退避降级 | 支持 | `GEMINI_API_KEY` |
| `openai-compatible-asr` | 纯标准库 multipart/form-data 并发调用 `/v1/audio/transcriptions`，按切段/合并短段调用；兼容 **OpenAI Whisper / gpt-4o-transcribe、Groq、SiliconFlow（SenseVoice/FunASR）、以及本地 whisper.cpp / mlx-whisper / faster-whisper HTTP 服务** | 不含（纯精准文字+声纹标签） | `base_url` + 可选 `api_key` + `model` |
| `command-asr` | 通用命令行适配器：支持两种调用粒度——① **`full_audio`（默认，推荐国内云厂与本地 CLI）**：对整条原音频仅调用一次，要求 stdout 输出标准 JSON `{"segments": [{"start": 0.0, "end": 4.2, "text": "..."}]}`，由内核自动按「最大时间重叠算法」对齐回贴 `FluidAudio` 声纹说话人；② **`segment`（逐段并发）**：按切段逐条调用并读取纯文本 stdout。**安全边界**：严格使用 `argv` 数组 + `shell=False` 逐参数替换 `{audio}`/`{lang}`/`{glossary}`，禁止 `shell=True`，彻底杜绝命令注入 | 视命令输出而定 | 用户自定义命令与环境变量 |

> **关键组合兼容性与双通道语义（架构裁决）**：
> 1. **后台自动分析器 vs MCP 交互分析分离**：配置中明确定义 `analyzer.auto_provider`（可选：`antigravity-agentapi` / `openai-compatible-llm` / `gemini-llm` / `none`）。
>    - 设为前 3 项时：后台监听、UI 拖拽或 `vmr run` 均自动完成「转写 → 分析 → `done`」全闭环；
>    - 设为 `none`（仅自动转写，交由 AI 客户端接手分析）时：后台转写完成后合法停留在 **`awaiting_analysis`** 状态（不再反复拉取重试），用户可在 Claude Code / Codex / Antigravity 中通过 MCP 拉起分析，或在 `vmr ui` 手动选模板触发分析并转为 `done`；
>    - 无论后台 `auto_provider` 设为哪项，从 MCP 入口发起的分析请求始终默认由当前宿主 AI（`mcp-host`）消费显化模板并落盘。
> 2. **`antigravity-flash` 硬约束规则**：`antigravity-flash` 与 `antigravity-agentapi` 属于绑定在一起的「Antigravity 一体化免费模式」（单次工作流完成精转+分析）。若用户选择外部直连分析器（`openai-compatible-llm` / `gemini-llm`）或在 Claude Code / Codex 下经 MCP 转写，配置校验与 UI **明确硬禁止**同时选择 `antigravity-flash` 并直接提示切换为 `gemini-api` / `openai-compatible-asr` / `command-asr`，避免脆弱的跨宿主半截回调链。

### 5.4 插槽 4：显化规则库与可插拔分析器（Rule Studio & Analyzer Providers）

#### A. 显化规则体系（Rule Studio）
每个模板都是 `rules/templates/<id>.md` 的标准明文 Markdown 文件，采用**头部元数据 + 正文规则**结构，彻底消灭黑盒：

```markdown
---
id: general_review
name: 通用对话复盘
description: 适用于日常工作讨论、合作沟通与项目同步，提炼结论、关键数字、沟通改进点与下一步行动。
default: true
---
请基于下方转录稿完成一份精炼、客观的中文复盘文档：

## 输出结构要求
1. **核心摘要与关键事实**：3～5 句交代背景、结论与关键数字/承诺锚点。
2. **关键卡点与情绪洞察**：仅列有原文 `[HH:MM:SS]` 依据的真实沟通卡点；若转录稿包含 `[语气]` 标注，结合语气说明对方真实关注点，并给出更自然的替代话术。
3. **下一步行动建议**：1～3 条可执行动作（含建议负责人或时间节点）。

## 纪律约束
- 只依据转录稿事实，严禁编造未出现的数字或承诺；
- 简洁直接，不写空泛客套话。
```

**Prompt 透明拼装规则（所有引擎统一复用同一个渲染函数 `compose_analysis_prompt`）**：
1. 固定元数据头（录音时间、标题、时长、说话人声纹摘要、输出要求）；
2. 用户可选的 `rules/persona.md`（若非空才注入，默认空）；
3. 选中的 `rules/templates/<id>.md` 完整正文；
4. 转录稿正文（或路径引用，视宿主模式而定）。
- 控制台提供**「查看完整 Prompt」**抽屉，用户随时能核对系统到底发了什么给模型。

**首发内置 4 套通用明文模板（首次初始化复制到用户目录，用户可任意改写或重置为内置默认）**：
1. `general_review`：通用对话复盘（默认）
2. `meeting_minutes`：标准会议纪要（决议、分歧、Action Items）
3. `sales_communication`：客户 / 销售沟通复盘（需求、异议、承诺、推进话术）
4. `solo_memo`：个人口述灵感整理（提炼核心观点、结构化大纲、待验证问题）

#### B. 可插拔分析器（Analyzer Providers）
定义统一接口 `AnalyzerProvider`：

| Provider ID | 工作原理 | 典型场景 |
| :--- | :--- | :--- |
| `mcp-host` | 不在后台主动调远端 LLM，而是把落盘的转录稿路径 + 拼装好的显化规则模板返回给当前正在调用 MCP 的宿主 AI（Antigravity / Claude Code / Codex / Cursor），由宿主 AI 读稿分析后调用 `save_analysis` 落盘 | 在自己常用的 AI 客户端里交互式复盘、随时追问 |
| `antigravity-agentapi` | 复用并加固现有 `AgentBridge`：后台守护自动投递到 Antigravity 对话完成精转+分析落盘；**修复动态超时（按包数自动放宽至 1800s+）、超时标 `failed` 而非 `done`、RPC 瞬时断连退避重试、提示词内置首次定义 `audio_transcriber_lite` 引导** | 装有 Antigravity 的用户实现全自动后台无人值守复盘 |
| `openai-compatible-llm` | 纯标准库调用 `/v1/chat/completions`（支持自定义 `base_url`、`model`、`api_key`），兼容 OpenAI、Claude 兼容网关、DeepSeek、Kimi、通义千问、豆包、Ollama 等；超长转录稿自动启用分块摘要再汇总，直接将分析稿写入 `output_dir` | **不装 Antigravity 的同事实现 100% 后台全自动出分析稿** |
| `gemini-llm` | 纯标准库调用 Gemini `generateContent`（利用百万级上下文一次读完超长转录稿），直接将分析稿写入 `output_dir` | 只配一个 Gemini Key 跑通“转写 + 分析”全自动闭环 |

---

## 6. 进程拓扑、状态机与稳定性加固（含现有线上缺陷根治）

### 6.1 统一单守护服务拓扑（架构裁决：`vmr serve` 一体化模型）
彻底消除 `vmr watch` 与 `vmr ui` 双进程互抢文件锁、配置不同步、进度无法实时推送的复杂性：
- **唯一常驻核心服务（`vmr serve`）**：单进程内同时持有 `daemon.lock`（`fcntl.flock`）、本地 `127.0.0.1` HTTP/SSE 服务（信息写入 `0600` 权限的 `daemon.json`）、以及可动态热启停的后台监听 Worker（语音备忘录 + 投放文件夹）和单文件任务队列；
- **`vmr watch`**：以无头常驻模式启动同一单实例守护（不自动弹浏览器；支持配套生成可选 macOS `launchd` LaunchAgent 实现开机常驻、关终端不掉线）；
- **`vmr ui`（及双击 `启动控制台.command`）**：先检测 `daemon.lock` 与 `daemon.json` 探活——**若单实例守护已在运行，直接复用其端口与 Token 在默认浏览器打开控制台；若未运行，则拉起该守护并打开控制台**；
- 监听开关切换、引擎插槽修改、模板编辑、拖拽上传入队与包级实时进度（如 `8/12 包`）全部在同一进程内闭环生效，且 **Phase 2 原生 Mac App 直接托管这同一个本地守护服务**。

### 6.2 升级状态机
`state.sqlite` 状态流转统一升级为：

```text
discovered ─▶ preparing ─▶ prepared ─▶ transcribing ─▶ transcribed ─┬─(auto_provider != none)─▶ analyzing ─▶ done
                  │                         │                        └─(auto_provider == none)─▶ awaiting_analysis ─(MCP/UI触发)─▶ done
                  ├─(noSpeechDetected)─▶ skipped
                  └─────────────────────────┴──────────────────────────────────────────────────┴─▶ failed (带 retry_count + 退避重试)
```

同时落地 5 项经现网验证的稳定性护栏：
1. **单实例文件锁（`daemon.lock`）**：彻底杜绝双开抢占临时 `job_dir` 导致 `No such file or directory` 切段报错。
2. **0.35s 全局发射节流锁 + 限流自适应降级**：合并进 `workflow_builder.py` 与直连 Provider 并发池，防止瞬时高并发打崩 Antigravity `language_server` 或触发 API 429 风控。
3. **脏输出清洗（`_SCHEMA_LEAK_RE` + `_DANGLING_BRACE_RE` + `_PARSER_ERR_RE`）**：统一在转录组装层执行。
4. **纯静音优雅跳过**：`FluidAudio` 报 `noSpeechDetected` 或切出 0 段时标记 `skipped`，零报错、零 API 消耗。
5. **动态超时与诚实状态**：等待分析落盘超时按 `max(analysis_timeout_sec, 600 + batch_count * 45)` 动态放宽，超时标记为 `failed`（保留断点缓存供下次重试），绝不误标 `done`。

---

## 7. Phase 1 外壳设计：跨 AI 插件 + 本地可视化控制台

### 7.1 跨 AI 标准 MCP Server + 通用 Skill + 一键安装器

#### A. 零依赖 Stdio MCP Server（`vmr mcp`）
采用标准 MCP JSON-RPC 2.0 over stdio 协议实现，纯 Python 标准库编写，零 `mcp`/`fastmcp` 外部安装依赖。暴露以下工具与资源：

- **Tools（含防长音频 MCP 60～120s 硬超时异步契约）**：
  - `vmr_status`：查看环境健康度、当前激活的转写/分析插槽、默认模板与队列概况。
  - `vmr_list_recordings`：列出近期已发现/已处理录音及文件路径（支持按状态/日期过滤，含 `awaiting_analysis` 待分析清单）。
  - `vmr_process_audio`：对指定音频路径（或最新录音）执行预处理切段与转写提交：
    - 若为短音频（预计 ≤ 20 秒内完成）或 `antigravity-flash` 仅切段模式：同步返回结果（转录稿路径或 `workflow_script_path`）；
    - 若为需耗时直连转写的中长录音：**立即把任务提交给后台单实例守护（若未运行则拉起独立 detached worker 托管，防止 MCP stdio 子进程超时强杀浪费 API 费用）**，瞬时返回 `{"status": "running", "job_id": "...", "batch_count": N}`；
  - `vmr_get_job_status`：按 `job_id` 查询精确包级进度（如 `completed_batches / total_batches`）；当进入 `awaiting_analysis` / `done` 时直接返回 `transcript_path`、声纹摘要与拼装好的显化模板分析指令（同时在支持进度通知的客户端发送 `notifications/progress`）。
  - `vmr_list_templates` / `vmr_get_template` / `vmr_save_template`：列出、读取、创建/修改明文分析规则模板（AI 对话里也能直接帮用户调规则）。
  - `vmr_save_analysis`：将宿主 AI 生成的复盘正文按规范文件名与 YAML Frontmatter 写入 `output_dir`，并更新 `state.sqlite` 为 `done`。
  - `vmr_manage_voiceprints`：查看、注册（从样音路径）或删除本地声纹。
- **Resources**：
  - `vmr://templates/{template_id}`：只读暴露明文模板内容；
  - `vmr://glossary`：暴露当前专有名词词表；
  - `vmr://config/summary`：暴露脱敏后的当前插槽配置。

#### B. 跨客户端通用 Skill（`skills/voice-memo-review/SKILL.md`）
一份符合 Antigravity、Claude Code、Codex 三家共同 Frontmatter 规范（`name` + `description`）的 `SKILL.md`：
- 指导 AI 优先通过 MCP 工具发现音频、读取显化模板与词表；
- 区分执行路径：在 Antigravity 下可调用 `define_subagent("audio_transcriber_lite", ...)` + `run_workflow` 零成本精转；在 Claude Code / Codex / Cursor 下调用 `vmr_process_audio`（长录音返回 `job_id` 时配合 `vmr_get_job_status` 轮询至转写落盘），随后按显化模板分析并调 `vmr_save_analysis` 落盘；
- 支持用户用自然语言说「帮我看看现在的复盘规则」「把销售复盘模板加上竞品对比一节」，直接调用模板工具可视化落盘。

#### C. 一键安装与卸载器（`vmr install` / `vmr uninstall`）
`vmr install [--target auto|antigravity|claude|codex|cursor]` 执行幂等写入（所有客户端的 MCP 启动命令统一指向稳定的 `~/.local/bin/vmr mcp`，修改任何已有配置文件前先备份 `.bak`）：
- **Antigravity**：在 `~/.gemini/config/plugins/voice-memo-review/` 生成独立插件包（`plugin.json` 不设 `disabled`、`mcp_config.json` 采用标准 Stdio `command`/`args` 且不声明 `variables`、`skills/voice-memo-review/SKILL.md`）。*已核实 Antigravity 官方插件与 MCP 规范（`agy-customizations/docs/plugins.md` & `mcp_servers.md`）：该目录下的标准插件与 Stdio MCP 默认自动发现并启用，**无需且严禁手改 `~/.gemini/config/config.json` 或全局 `mcp_config.json`**；若用户曾手动禁用，安装器仅提示在插件页开启。*
- **Claude Code**：以 JSON 安全合并写入 `~/.claude.json` 的 `mcpServers["voice-memo-review"]`，并复制 Skill 到 `~/.claude/skills/voice-memo-review/SKILL.md`；
- **Codex**：安全增删 `~/.codex/config.toml` 中独立区块 `[mcp_servers.voice-memo-review]`（仅做段落级精确替换，保留用户原有注释与其它配置），并复制 Skill 到 `~/.codex/skills/voice-memo-review/SKILL.md`；
- **Cursor**：安全合并写入 `~/.cursor/mcp.json`。

### 7.2 零依赖本地可视化控制台（`vmr ui`）
启动方式：`vmr ui`（自动拉起 `http://127.0.0.1:<port>/?token=<token>` 并在浏览器打开；也可通过 `--daemon` 与后台监听同进程运行，方便实时推送进度）。

**界面布局草图（4 个主导航页，清爽克制、单页即开即用）**：

```text
┌─────────────────────────────────────────────────────────────────────────┐
│  Voice Memo Review  v1.0     ● 监听运行中 (语音备忘录 + 投放箱)   [检查更新] │
├──────────────┬──────────────────────────────────────────────────────────┤
│              │  [ 拖拽音频到这里，或点击选择文件立刻转写复盘 ]              │
│  ● 工作台     │    模板: [通用对话复盘 ▾]   转写: Gemini API   [开始处理]   │
│    引擎插槽   │──────────────────────────────────────────────────────────│
│    规则工作室 │  最近任务                                                 │
│    AI 插件    │  • 11-25 14:20 项目周会.m4a     [已完成 4m12s] [看分析][看转录] │
│              │  • 11-25 11:05 客户沟通.m4a     [转写中 8/12包 ▓▓▓▓░░]     │
│              │  • 11-25 09:30 空白误触.m4a     [已跳过 · 无有效人声]        │
└──────────────┴──────────────────────────────────────────────────────────┘
```

各页面核心能力：
1. **工作台**：拖拽/上传单文件立即运行；开关「苹果语音备忘录监听」和「自定义投放文件夹路径」；实时进度列表（轮询/SSE）；点击条目右侧抽屉直接阅读渲染后的分析稿与转录稿，支持一键复制或打开所在文件夹。
2. **引擎插槽**：卡片式切换**转写引擎**与**分析引擎**（含不兼容组合实时提醒）；编辑 `base_url`、`model`、`api_key`（密码框掩码存储进 `secrets.json`）；内置**「测试连通性」**按钮，第一时间暴露 Key/网络错误。
3. **规则工作室（核心亮点）**：
   - 模板管理：左侧列表切换/新建/复制/删除/设为默认/重置内置模板；右侧明文 Markdown 编辑器 + **「预览完整 Prompt」**按钮；
   - 词表与背景：专有名词校对词表编辑框 + 可选个人背景编辑框；
   - 声纹管理：展示已注册说话人列表，支持上传 10～30 秒样音一键录入声纹与删除。
4. **AI 插件桥接 & 更新**：自动扫描本机 Antigravity / Claude Code / Codex / Cursor 的安装与接入状态，提供「一键安装 / 重新同步 / 卸载」按钮；展示当前版本与 GitHub Releases 最新版本对比（支持一键触发更新或复制 `vmr update` 命令）。

> **Phase 1 → Phase 2 复用契约**：`vmr ui` 的前后端交互全部基于清晰的 REST JSON 接口（`/api/v1/status`、`/api/v1/jobs`、`/api/v1/providers`、`/api/v1/templates`、`/api/v1/voiceprints`、`/api/v1/integrations`、`/api/v1/config`）。这意味着 **Phase 2 的原生 Mac App 可以 100% 复用这套后端 API 与核心服务，零逻辑重复编写**。

---

## 8. 分发包与 Phase 2 原生 Mac App 演进路线

### 8.1 Phase 1 同事试用分发包（立即可发）
- **发布物**：打包产出 `vmr-macos-arm64-vX.Y.Z.tar.gz` + `SHA256SUMS`（整包约 **55～65 MB**，内嵌源码 + 10MB 预编译 `fluidaudiocli` + 21MB Core ML 离线声纹模型 + 静态 arm64 `ffmpeg`/`ffprobe` + 4 套通用明文模板 + 一键安装脚本 `install.sh` + 双击入口 `启动控制台.command`）。
- **同事首次体验路径（目标 ≤ 3 分钟，零 Xcode / 零 Homebrew / 零科学上网）**：
  1. 解压后运行 `./install.sh`（将程序安装至受管目录 `~/.local/share/voice-memo-review/`、创建 `~/.local/bin/vmr`、离线预置 21MB 声纹模型、复制内置模板到 `~/.config/voice-memo-review/rules/templates/`）；
  2. 自动打开 `vmr ui` 本地控制台：选好自己的转写/分析方式（如一键连本机 Antigravity，或填入自己的 API Key），可选点一下「一键安装到 Claude Code / Codex / Antigravity」；
  3. 拖一条测试录音进去，或打开投放文件夹/语音备忘录监听，3～5 分钟看到第一份按明文规则生成的复盘稿。
- **Phase 1 更新方式**：运行 `vmr update`（或在控制台点击更新），默认从**公开 GitHub Releases**（`update.repo` 配置项指向目标公开仓，零 Token 即可匿名检查并拉取，同时支持自定义静态 Feed URL / 本地离线包路径）校验 `SHA256SUMS` 后原子替换 `~/.local/share/voice-memo-review/`，**完全不动 `~/.config/voice-memo-review/` 里的用户数据**，并自动重新同步 MCP/Skill。

### 8.2 Phase 2 原生 Mac App 升级路线（后续迭代）
待 Phase 1 经同事真实使用收敛反馈后，在仓库新增 `macos/` 目录构建原生外壳：
1. **形态**：原生 SwiftUI **菜单栏常驻图标 + 主窗口**（`VoiceMemoReview.app`），直接内嵌：
   - 静态 arm64 `ffmpeg` / `ffprobe` + `fluidaudiocli`（彻底零系统依赖）；
   - Python 独立运行环境（`python-build-standalone` 轻量嵌入包）+ `voice_review` 核心服务；
   - 原生菜单栏状态（拖拽音频到菜单栏图标即刻触发、实时进度、开机自启 `SMAppService`、原生引导授予「完全磁盘访问权限」）；主窗口初期可直接加载本地 `vmr ui` API 或逐步替换为纯 SwiftUI 视图。
2. **自动更新**：集成 **Sparkle 2**，每次 GitHub Actions 打 Tag 发布新 `.dmg`/`.zip` 时自动生成 `appcast.xml`，同事菜单栏收到更新提示一键静默升级重启。
3. **签名公证**：预留 `Developer ID Application` 签名与 `notarytool` 公证脚本；无开发者账号阶段提供内部试用说明（右键打开 / 去隔离属性）。

---

## 9. 决策归属清晰清单

### 已由用户确认的决策
- 面向他人的独立产品：**不包含微信机器人（`clawbot`/Hermes）和个人数字上下文采集**，闭环终点为自动产出结构化分析稿。
- **规则完全显化**：内置通用模板，提示词对用户透明可读可改。
- **全链路可插拔**：不绑死 Google/Antigravity 转写与分析，支持多转写引擎与跨 AI 客户端（Antigravity / Claude Code / Codex）。
- **落地路线**：采用方案三「一核两壳」分阶段实施（Phase 1：统一可插拔内核 + 跨 AI MCP/Skill + 本地可视化控制台 `vmr ui`；Phase 2：原生 SwiftUI Mac App + Sparkle 自动更新）。

- **发布与更新源**：已确认使用**公开 GitHub 仓库**（免 Token 匿名检查更新，账号将更改为 `shane1124` 相关用户名，默认仓库 slug 抽成配置常量方便改名后一键同步）。

### 已通过工程审查并拍板写入的架构默认裁决（已批准）
1. **统一单守护进程（`vmr serve`）**：`vmr watch` 与 `vmr ui` 共享同一单实例本地守护与 HTTP/SSE 服务，二次打开控制台自动复用已有实例端口，消灭双进程锁冲突并 100% 为 Phase 2 Mac App 复用。
2. **后台分析与 MCP 交互解耦**：后台 `analyzer.auto_provider` 设为 `none` 时，转写完优雅停在 `awaiting_analysis` 待人工/MCP 接手；`antigravity-flash` 与 `antigravity-agentapi` 绑定为一体化免费模式，不混接外部 LLM 分析器。
3. **MCP 长音频防超时异步契约**：超过短录音阈值时 `vmr_process_audio` 交给后台托管并立即返回 `job_id`，通过 `vmr_get_job_status` 查询进度，彻底避免 Claude Code / Codex / Cursor 的 60～120 秒工具超时强杀。
4. **全离线免环境依赖分发包（约 55～65 MB）**：Phase 1 直接内置 10MB `fluidaudiocli` + 21MB Core ML 声纹模型 + 静态 arm64 `ffmpeg`/`ffprobe`，同时保留 `diarizer.py` 对本地模型缺失时才放行联网下载的兼容策略，真正做到零 Homebrew / 零 Xcode / 免翻墙开箱即用。
5. **Antigravity 原生插件目录自动加载**：仅写入 `~/.gemini/config/plugins/voice-memo-review/`（含 Stdio `mcp_config.json` 与 `SKILL.md`），遵循官方规范不触碰全局 `config.json`。
6. **`command-asr` 默认整轨对齐 + 安全无 shell 执行**：支持整条原音频一次调用并按时间轴自动回贴声纹，严格用 `argv` 数组 `shell=False` 执行防注入；程序安装至受管目录 `~/.local/share/voice-memo-review/` 并做 `SHA256SUMS` 校验。

### 留给实现阶段自行决策的工程细节（Builder's Call）
- `vmr ui` 具体前端视觉配色、字体与组件样式（遵循项目视觉设计规范，保持极简清晰、无第三方前端框架构建链）。
- `openai-compatible-asr` 单段最大时长/并发数默认调优值，以及超长转录稿在低上下文窗口模型下的分块汇总阈值（默认建议按 80,000 字符切分）。
- SQLite 表迁移脚本细节（自动兼容旧版 `state.sqlite` 升级加列）。
- 单元测试组织方式（所有 Provider、MCP JSON-RPC 协议、跨客户端配置读写器、静音跳过与单实例锁均需用 Mock 覆盖纯离线单元测试）。

---

## 10. 验收标准（Phase 1 Definition of Done）

1. **向后兼容**：`下游私有集成项目` 现有 39 项测试与导入路径全部保持绿色通过。
2. **插槽全通**：
   - 4 个转写 Provider（`antigravity-flash`、`gemini-api`、`openai-compatible-asr`、`command-asr`）与 4 个分析 Provider（`mcp-host`、`antigravity-agentapi`、`openai-compatible-llm`、`gemini-llm`）均完成接口实现与 Mock 单元测试，且至少拿真实录音实测跑通完整端到端链路。
   - 关闭声纹（`none` 模式）与开启声纹（`fluidaudio` 模式）均可走通；纯静音 `noSpeechDetected` 正确落为 `skipped` 零报错。
3. **规则显化**：4 套通用模板均为干净无私货明文 Markdown，支持在 `vmr ui` 和 MCP 工具中查看、编辑、预览完整 Prompt 与生效。
4. **跨 AI 接入**：`vmr mcp` 通过标准 MCP 协议握手与工具调用测试；`vmr install` 在临时沙盒目录验证能幂等、无损地写入并卸载 Antigravity 插件、Claude Code JSON、Codex TOML、Cursor JSON。
5. **免编译分发**：产出带预编译 10MB `fluidaudiocli` 的完整同事试用包，在全新目录执行安装、启动 `vmr ui`、拖入音频出分析稿全程无需 Xcode 编译、无需手改 JSON/TOML。
