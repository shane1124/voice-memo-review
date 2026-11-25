# voice-memo-review Phase 1 可插拔产品实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `.` 升级为规则显化、四插槽全可插拔、零外部 Python 依赖的通用语音复盘产品（Phase 1：可插拔内核 + 跨 AI MCP/Skill 一键安装 + 零依赖本地可视化控制台 + 离线免编译分发包与 GitHub Releases 自更新）。

**Architecture:** 采用「统一单守护内核（`vmr serve`）+ 跨 AI 插件与本地 Web 控制台双外壳」设计。底层解耦为「音频输入 → 声纹/切块预处理 → 可插拔转写引擎 → 显化规则库与可插拔分析引擎」四插槽，状态统一落入升级后的 `state.sqlite`；对外同时提供 `127.0.0.1` REST/SSE API（供 `vmr ui` 及 Phase 2 原生 Mac App 复用）和标准 Stdio MCP Server（供 Antigravity / Claude Code / Codex / Cursor 接入）。

**Tech Stack:** Python 3.11+ 纯标准库（`tomllib`、`sqlite3`、`urllib.request`、`http.server`、`concurrent.futures`、`select.kqueue`、`fcntl`、`unittest`）、原生 HTML/CSS/JS 单页控制台（零构建链）、预编译 Apple Silicon `fluidaudiocli`（10 MB）+ Core ML 离线声纹模型（21 MB）+ 静态 arm64 `ffmpeg`/`ffprobe`。

**Spec:** [2026-11-25-pluggable-voice-review-design.md](../../docs/superpowers/specs/2026-11-25-pluggable-voice-review-design.md)

---

## Global Constraints

- 运行时零第三方 Python 依赖（严禁引入 `requests`、`fastapi`、`pydantic`、`mcp`、`pytest` 等外部包），全部测试使用 `python3 -m unittest`。
- 向后兼容 `<downstream-private-console>` 的所有既有导入与调用签名：`FluidAudioDiarizer`、`VoiceprintStore`、`SegmenterConfig`、`prepare_v2_job`、`render_workflow_script`、`sanitize_transcript_field`、`VoiceReviewConfig`、`load_config`。
- 严禁将作者私人业务词表、人物特征、微信推送（`clawbot`/Hermes）或个人上下文采集逻辑引入本仓库。
- 密钥严格隔离存储于 `secrets.json`（文件权限 `0600`），API/日志/配置导出中一律脱敏；`vmr ui` 仅允许绑定 `127.0.0.1`，写请求强校验 `X-VMR-Token` 与本机 `Origin/Host`。
- 遵循简体中文界面、日志与文档规范；程序本体受管目录（`~/.local/share/voice-memo-review/`）与用户数据目录（`VMR_HOME`，默认 `~/.config/voice-memo-review/`）严格分离。
- 默认发布源为公开 GitHub 仓库（通过 `update.repo` 配置，默认 `shane1124/voice-memo-review`，改名后仅需改配置常量即可无缝切换）。

---

## Review Focus

以下 5 类隐性边界与故障场景在真实使用中最易踩坑，每条均已绑定至对应任务的单元测试：

1. **特殊字符路径/词表防注入**：含空格、中文括号、引号、`; & $()` 的音频路径或专名词表进入 `command-asr` 与 `ffmpeg` 时，严格 `shell=False` 逐参数替换，绝不注入或截断（钉于 Task 3、Task 4）。
2. **旧版配置与旧版 SQLite 无损升级**：存在旧版扁平 `config.toml` 或缺列的旧版 `state.sqlite` 时自动迁移加列并保留历史记录，不抛 `OperationalError`（钉于 Task 1、Task 5）。
3. **本地控制台防 CSRF 与密钥防泄露**：缺失/错误 `X-VMR-Token` 或非法跨站 `Origin` 的写请求一律返回 `403`；配置查询接口永远只返回脱敏掩码而非明文 API Key（钉于 Task 6）。
4. **多客户端已有配置无损合并与回滚**：已有包含多组服务器与注释的 `~/.claude.json`、`~/.codex/config.toml`、`~/.cursor/mcp.json` 执行 `vmr install` / `vmr uninstall` 时，必须生成 `.bak` 备份、仅精准增删 `voice-memo-review` 一项并完好保留其他内容；Antigravity 仅写插件目录绝不触碰全局 `config.json`（钉于 Task 8）。
5. **超长录音断点续跑与超长文稿防溢出**：直连转写遇 429/中断时必须复用 `batches_out/` 断点缓存并退避降级；直连 LLM 分析超过 80,000 字符的转录稿时必须自动分块摘要再汇总，不直接触发 context overflow（钉于 Task 4、Task 5）。

---

## File Structure

| 路径 | 职责（新增 / 修改） |
| :--- | :--- |
| `voice_review/secrets.py` | **新增**：`SecretStore`（`0600` 权限管理 `secrets.json`、脱敏掩码、安全读写） |
| `voice_review/config.py` | **修改**：支持 `VMR_HOME`、TOML 读写保存、四插槽配置节、插槽组合兼容性校验 `validate_provider_combo`，兼容旧版扁平 `config.toml` |
| `voice_review/rules.py` | **新增**：`RuleStudio`（管理 `rules/templates/*.md`、`glossary.txt`、`persona.md`、内置 4 套通用模板初始化与重置、透明 Prompt 拼装 `compose_analysis_prompt`） |
| `templates/*.md` | **新增**：4 套干净通用明文内置模板（`general_review`、`meeting_minutes`、`sales_communication`、`solo_memo`） |
| `voice_review/sources.py` | **新增**：统一输入源 `AudioCandidate`、`DropFolderScanner`（防重 SHA256 + 12s 防抖）与多源聚合器 `MultiSourceScanner` |
| `voice_review/diarizer.py` | **修改**：新增 `NoSpeechDetectedError` / `EmptySpeechResult` 处理与 `auto_download_if_missing` 首次模型自动拉取（保留 `offline_mode=True` 向后兼容） |
| `voice_review/segmenter.py` | **修改**：新增 `build_uniform_chunks()` 支持关闭声纹（`preprocessor="none"`）均匀切块产出统一 `Segment` |
| `voice_review/workflow_builder.py` | **修改**：补齐 `0.35s` 全局发射节流锁、`_PARSER_ERR_RE` 脏文本清洗、强化版 `audio_transcriber_lite` 自动定义引导 |
| `voice_review/transcribers.py` | **新增**：`TranscriberProvider` 抽象 + 4 实现（`antigravity-flash`、`gemini-api`、`openai-compatible-asr`、`command-asr` 含 `full_audio` 声纹时间轴对齐与无 shell 安全执行）+ 公共断点缓存与连通测试 |
| `voice_review/analyzers.py` | **新增**：`AnalyzerProvider` 抽象 + 4 实现（`mcp-host`、`antigravity-agentapi`、`openai-compatible-llm` 含 80k 字分块汇总、`gemini-llm`）+ 连通测试 |
| `voice_review/agent_bridge.py` | **修改**：补齐按包数动态超时、RPC 瞬时断连重试与 `audio_transcriber_lite` 提示词兼容 |
| `voice_review/state.py` | **修改**：升级状态机（含 `awaiting_analysis` / `skipped` / 重试退避 / 包级进度字段）、旧库自动 `ALTER TABLE` 迁移 |
| `voice_review/daemon.py` | **新增**：统一单守护 `VmrDaemon`（`daemon.lock` + `daemon.json(0600)` + 后台热启停监听 Worker + 单文件异步任务队列 + `127.0.0.1` REST JSON & SSE API） |
| `voice_review/runner.py` | **修改**：复用新四插槽流水线与 `VmrDaemon`，保留 `VoiceReviewRunner` 兼容入口 |
| `voice_review/web_ui/index.html` | **新增**：零构建链单页控制台（工作台拖拽与阅读、引擎插槽、规则工作室与 Prompt 预览、AI 插件桥接与检查更新） |
| `voice_review/mcp_server.py` | **新增**：零依赖 Stdio JSON-RPC 2.0 MCP Server（支持 8 个 Tools + 3 个 Resources + 长录音后台异步 `job_id` 与进度通知） |
| `skills/voice-memo-review/SKILL.md` | **新增**：跨 Antigravity / Claude Code / Codex 三家通用的标准 Skill |
| `voice_review/installer.py` | **新增**：跨 AI 一键安装与卸载器（Antigravity 插件目录 / Claude Code JSON / Codex TOML / Cursor JSON 备份+幂等读写） |
| `voice_review/updater.py` | **新增**：公开 GitHub Releases 检查更新、`SHA256SUMS` 校验与受管目录原子替换（保护开发仓不自覆盖） |
| `voice_review/cli.py` | **修改**：统一子命令 `run / watch / ui / serve / mcp / install / uninstall / update / enroll / voiceprints / doctor / prep` |
| `scripts/build_release.sh` & `scripts/install.sh` | **新增**：离线免编译分发包构建器（内嵌 `fluidaudiocli` + 21MB 模型 + 静态 `ffmpeg` + 双击 `启动控制台.command`）与用户一键安装脚本 |

---

### Task 1: 初始化 Git 仓、密钥隔离存储与四插槽配置引擎

**Files:**
- Create: `voice_review/secrets.py`
- Modify: `voice_review/config.py`
- Modify: `config.example.toml`
- Test: `tests/test_config_secrets.py`

**Interfaces:**
- Consumes: `voice_review.diarizer.find_fluidaudio_bin`, `voice_review.segmenter.SegmenterConfig`
- Produces:
  - `SecretStore(path: Path)`：`get(key: str) -> str`, `set(key: str, value: str) -> None`, `delete(key: str) -> None`, `masked_all() -> dict[str, str]`
  - `ProviderConfig` / `VoiceReviewConfig`（保留原有全部属性，新增 `vmr_home`, `secrets_path`, `rules_dir`, `voice_memos_enabled`, `drop_folder_enabled`, `drop_folder_dir`, `preprocessor_mode`, `transcriber_provider`, `transcriber_settings`, `analyzer_auto_provider`, `analyzer_settings`, `update_repo`, `ui_port`）
  - `load_config(config_path: Path | None = None, *, vmr_home: Path | None = None) -> VoiceReviewConfig`
  - `save_config_dict(config_path: Path, data: dict) -> None`
  - `validate_provider_combo(transcriber: str, analyzer_auto: str) -> tuple[bool, str]`

- [ ] **Step 1: 初始化本仓库 Git 基线（确保后续按 Task 高频提交）**

在 `.` 初始化 `.gitignore`（忽略 `__pycache__/`、`*.pyc`、`work/`、`state/`、`output/`、`dist/`、`secrets.json`、`.DS_Store`）并做一次初始基线提交。

- [ ] **Step 2: Write the failing test (`tests/test_config_secrets.py`)**

```python
import os, stat, tempfile, unittest
from pathlib import Path
from voice_review.secrets import SecretStore
from voice_review.config import load_config, save_config_dict, validate_provider_combo

class TestConfigAndSecrets(unittest.TestCase):
    def test_secret_store_chmod_0600_and_masking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "secrets.json"
            store = SecretStore(sp)
            store.set("gemini_api_key", "AIzaSySecret12345678")
            mode = stat.S_IMODE(os.stat(sp).st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(store.get("gemini_api_key"), "AIzaSySecret12345678")
            masked = store.masked_all()["gemini_api_key"]
            self.assertNotIn("Secret", masked)
            self.assertTrue(masked.endswith("5678"))

    def test_legacy_flat_config_and_provider_combo_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_p = Path(tmp) / "config.toml"
            cfg_p.write_text('output_dir = "out"\nmodel = "gemini-4-neon-xhigh"\n', encoding="utf-8")
            cfg = load_config(cfg_p)
            self.assertEqual(cfg.output_dir.name, "out")
            self.assertEqual(cfg.transcriber_provider, "antigravity-flash")
            self.assertEqual(cfg.analyzer_auto_provider, "antigravity-agentapi")

            ok1, _ = validate_provider_combo("antigravity-flash", "antigravity-agentapi")
            ok2, msg2 = validate_provider_combo("antigravity-flash", "openai-compatible-llm")
            ok3, _ = validate_provider_combo("gemini-api", "none")
            self.assertTrue(ok1)
            self.assertFalse(ok2)
            self.assertIn("antigravity-flash", msg2)
            self.assertTrue(ok3)

    def test_save_and_reload_pluggable_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_p = Path(tmp) / "config.toml"
            save_config_dict(cfg_p, {
                "sources": {"voice_memos_enabled": False, "drop_folder_enabled": True, "drop_folder_dir": str(Path(tmp) / "drop")},
                "preprocessor": {"mode": "none"},
                "transcriber": {"provider": "openai-compatible-asr", "base_url": "https://api.groq.com/openai/v1", "model": "whisper-large-v3-turbo"},
                "analyzer": {"auto_provider": "openai-compatible-llm", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
                "update": {"repo": "shane1124/voice-memo-review"},
            })
            cfg = load_config(cfg_p)
            self.assertFalse(cfg.voice_memos_enabled)
            self.assertTrue(cfg.drop_folder_enabled)
            self.assertEqual(cfg.preprocessor_mode, "none")
            self.assertEqual(cfg.transcriber_provider, "openai-compatible-asr")
            self.assertEqual(cfg.analyzer_auto_provider, "openai-compatible-llm")
            self.assertEqual(cfg.update_repo, "shane1124/voice-memo-review")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd . && python3 -m unittest tests.test_config_secrets -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'voice_review.secrets'`)

- [ ] **Step 4: Implement `voice_review/secrets.py` & upgrade `voice_review/config.py`**

- 在 `voice_review/secrets.py` 实现 `SecretStore`：原子写临时文件 + `os.chmod(..., 0o600)` + `os.replace`，并支持环境变量兜底（如 `GEMINI_API_KEY`、`OPENAI_API_KEY`）。
- 在 `voice_review/config.py` 扩展 `VoiceReviewConfig`（全部新增字段设合理默认值，保持现有字段与 `load_config(config_path)` 100% 兼容）；内置受管 `bin/` 目录优先探测 `fluidaudiocli`、`ffmpeg`、`ffprobe`；实现纯标准库 TOML 序列化 `save_config_dict` 和 `validate_provider_combo`（硬禁止 `antigravity-flash` 搭配 `openai-compatible-llm` / `gemini-llm`）。
- 同步更新 `config.example.toml` 完整展示四插槽配置注释。

- [ ] **Step 5: Run tests to verify pass & backward compatibility**

Run: `cd . && python3 -m unittest discover -s tests -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add .gitignore voice_review/secrets.py voice_review/config.py config.example.toml tests/test_config_secrets.py
git commit -m "feat: add 0600 SecretStore and pluggable 4-slot config with combo validation"
```

---

### Task 2: 显化规则库（Rule Studio）与 4 套内置通用模板

**Files:**
- Create: `templates/general_review.md`
- Create: `templates/meeting_minutes.md`
- Create: `templates/sales_communication.md`
- Create: `templates/solo_memo.md`
- Create: `voice_review/rules.py`
- Test: `tests/test_rules.py`

**Interfaces:**
- Consumes: `voice_review.agent_bridge.format_duration_cn`
- Produces:
  - `PromptTemplate(id: str, name: str, description: str, body: str, is_default: bool, is_builtin: bool, path: Path)`
  - `RuleStudio(rules_dir: Path, builtin_templates_dir: Path | None = None)`：
    - `ensure_initialized() -> None`（首次将 4 套内置模板拷贝到用户目录，不覆盖已有修改）
    - `list_templates() -> list[PromptTemplate]`
    - `get_template(template_id: str | None = None) -> PromptTemplate`
    - `save_template(template_id: str, *, name: str, description: str, body: str, is_default: bool = False) -> PromptTemplate`
    - `delete_template(template_id: str) -> bool`
    - `reset_builtin_template(template_id: str) -> PromptTemplate`
    - `get_glossary() -> str` / `save_glossary(text: str) -> None`
    - `get_persona() -> str` / `save_persona(text: str) -> None`
    - `compose_analysis_prompt(*, template_id: str | None, title: str, recorded_at: str, duration_sec: float, speakers_summary: str, transcript_text: str | None = None, transcript_path: Path | None = None, analysis_path: Path | None = None) -> str`

- [ ] **Step 1: Write the failing test (`tests/test_rules.py`)**

```python
import tempfile, unittest
from pathlib import Path
from voice_review.rules import RuleStudio

class TestRuleStudio(unittest.TestCase):
    def test_builtin_templates_clean_and_resettable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            studio = RuleStudio(Path(tmp) / "rules")
            studio.ensure_initialized()
            ids = {t.id for t in studio.list_templates()}
            self.assertEqual(ids, {"general_review", "meeting_minutes", "sales_communication", "solo_memo"})
            # 确保零私有业务链路泄露
            for t in studio.list_templates():
                for forbidden in ("Hermes", "clawbot", "微信快报"):
                    self.assertNotIn(forbidden.lower(), t.body.lower())
            # 修改后可重置回内置默认
            studio.save_template("general_review", name="改版", description="desc", body="自定义正文", is_default=True)
            self.assertEqual(studio.get_template("general_review").body, "自定义正文")
            restored = studio.reset_builtin_template("general_review")
            self.assertNotEqual(restored.body, "自定义正文")

    def test_compose_analysis_prompt_transparency_and_persona_optional(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            studio = RuleStudio(Path(tmp) / "rules")
            studio.ensure_initialized()
            p1 = studio.compose_analysis_prompt(
                template_id="meeting_minutes", title="周会", recorded_at="2026-11-25T10:00:00+08:00",
                duration_sec=1800, speakers_summary="SPEAKER_00", transcript_text="[00:00:01] 开始开会"
            )
            self.assertIn("周会", p1)
            self.assertNotIn("用户背景说明", p1)
            studio.save_persona("我是B端产品经理，重点关注交付风险")
            p2 = studio.compose_analysis_prompt(
                template_id="meeting_minutes", title="周会", recorded_at="2026-11-25T10:00:00+08:00",
                duration_sec=1800, speakers_summary="SPEAKER_00", transcript_text="[00:00:01] 开始开会"
            )
            self.assertIn("B端产品经理", p2)
            self.assertIn("[00:00:01] 开始开会", p2)

    def test_template_id_path_traversal_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            studio = RuleStudio(Path(tmp) / "rules")
            with self.assertRaises(ValueError):
                studio.save_template("../evil", name="x", description="x", body="x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd . && python3 -m unittest tests.test_rules -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Create the 4 clean Markdown templates (`templates/*.md`) and implement `voice_review/rules.py`**

- 4 套模板均带标准 YAML Frontmatter（`id` / `name` / `description` / `default`），结构克制清晰、零个人隐私或特定公司术语。
- `RuleStudio` 校验 `template_id` 仅允许 `^[a-z0-9][a-z0-9_-]{0,47}$` 防止路径穿越；默认模板切换时自动清除其它模板的 `default: true`。

- [ ] **Step 4: Run tests to verify pass**

Run: `cd . && python3 -m unittest tests.test_rules -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add templates/ voice_review/rules.py tests/test_rules.py
git commit -m "feat: add explicit RuleStudio with 4 clean builtin prompt templates"
```

---

### Task 3: 多输入源（语音备忘录 + 投放文件夹）与预处理加固（静音跳过 + 无声纹切块）

**Files:**
- Create: `voice_review/sources.py`
- Modify: `voice_review/diarizer.py`
- Modify: `voice_review/segmenter.py`
- Test: `tests/test_sources_preprocess.py`

**Interfaces:**
- Consumes: `voice_review.voice_memos.VoiceMemoScanner`, `probe_duration_sec`, `AUDIO_EXTENSIONS`
- Produces:
  - `AudioCandidate(recording_id: str, filename: str, audio_path: Path, title: str, recorded_at: datetime, recorded_date: str, duration_sec: float, source_kind: str)`
  - `DropFolderScanner(folder_dir: Path, *, min_duration_sec: float = 10.0, debounce_sec: float = 12.0, ffprobe_bin: str = "ffprobe")`
  - `MultiSourceScanner`：`scan_candidates() -> list[AudioCandidate]`, `is_file_stable(cand: AudioCandidate, *, now_mono: float | None = None) -> bool`, `watch_paths() -> list[Path]`
  - `NoSpeechDetectedError(RuntimeError)`（`diarizer.py` 在捕获 `noSpeechDetected` 或 0 段时抛出，供上层精确识别标 `skipped`）
  - `FluidAudioDiarizer(..., auto_download_if_missing: bool = True)`（本地模型目录已存在时严格离线，缺失时首次允许联网下载）
  - `build_uniform_chunks(audio_path: Path, clips_dir: Path, *, chunk_sec: float = 600.0, ffmpeg_bin: str = "ffmpeg", ffprobe_bin: str = "ffprobe", mp3_bitrate: str = "32k") -> tuple[list[Segment], float]`

- [ ] **Step 1: Write the failing test (`tests/test_sources_preprocess.py`)**

```python
import tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from voice_review.diarizer import FluidAudioDiarizer, NoSpeechDetectedError
from voice_review.segmenter import build_uniform_chunks
from voice_review.sources import DropFolderScanner

class TestSourcesAndPreprocess(unittest.TestCase):
    def test_drop_folder_special_chars_and_sha256_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "drop"
            d.mkdir()
            f = d / "客户会议 (第1次); $(whoami).m4a"
            f.write_bytes(b"fake-audio-bytes")
            scanner = DropFolderScanner(d, min_duration_sec=5.0, debounce_sec=0.0)
            with patch("voice_review.sources.probe_duration_sec", return_value=42.0):
                cands = scanner.scan_candidates()
            self.assertEqual(len(cands), 1)
            self.assertEqual(cands[0].source_kind, "drop_folder")
            self.assertTrue(cands[0].recording_id.startswith("drop_"))
            self.assertTrue(scanner.is_file_stable(f))

    def test_diarizer_raises_no_speech_and_auto_download_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "fluidaudiocli"
            fake_bin.write_text("#!/bin/sh\necho 'ERROR: noSpeechDetected'\nexit 1\n")
            fake_bin.chmod(0o755)
            wav = Path(tmp) / "audio_16k.wav"
            wav.write_bytes(b"RIFF")
            d = FluidAudioDiarizer(fluidaudio_bin=fake_bin, offline_mode=True, auto_download_if_missing=True)
            with patch.object(d, "_models_present", return_value=False):
                env_dl = d._build_env()
                self.assertNotIn("REGISTRY_URL", env_dl)
            with patch.object(d, "_models_present", return_value=True):
                env_off = d._build_env()
                self.assertEqual(env_off.get("REGISTRY_URL"), "http://127.0.0.1:9")
            with self.assertRaises(NoSpeechDetectedError):
                d.diarize(wav, work_dir=Path(tmp), reuse_existing=False)

    def test_uniform_chunks_for_no_diarizer_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "talk.m4a"
            src.write_bytes(b"x")
            with patch("voice_review.segmenter.subprocess.run") as mrun, \
                 patch("voice_review.segmenter._probe_duration", return_value=1250.0):
                def _side(cmd, **kw):
                    Path(cmd[-1]).write_bytes(b"mp3")
                    return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()
                mrun.side_effect = _side
                segs, total_dur = build_uniform_chunks(src, Path(tmp) / "clips", chunk_sec=600.0)
            self.assertEqual(len(segs), 3)
            self.assertEqual(segs[0].speaker_label, "说话人")
            self.assertAlmostEqual(total_dur, 1250.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd . && python3 -m unittest tests.test_sources_preprocess -v`
Expected: FAIL

- [ ] **Step 3: Implement `sources.py`, update `diarizer.py` & `segmenter.py`**

- `sources.py`：`DropFolderScanner` 支持 `.m4a/.mp3/.wav/.flac/.ogg/.aac/.mp4`，跳过 `_processed/` 与隐藏文件，使用 `drop_<sha256[:16]>` 防重；`MultiSourceScanner` 聚合 `VoiceMemoScanner` 与 `DropFolderScanner`。
- `diarizer.py`：在 `diarize()` 报错尾部检测到 `noSpeechDetected`（或解析出 0 段有效发言）时抛出 `NoSpeechDetectedError`；新增 `_models_present()` 检查 `~/Library/Application Support/FluidAudio/Models/speaker-diarization` 是否存在非空目录，配合 `auto_download_if_missing` 决定是否放行首次下载。
- `segmenter.py`：新增 `build_uniform_chunks()`（严禁 `shell=True`，用 `argv` 列表安全调用 `ffmpeg`）。

- [ ] **Step 4: Run tests to verify pass**

Run: `cd . && python3 -m unittest discover -s tests -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add voice_review/sources.py voice_review/diarizer.py voice_review/segmenter.py tests/test_sources_preprocess.py
git commit -m "feat: add drop-folder source, graceful noSpeechDetected handling and uniform chunker"
```

---

### Task 4: 可插拔转写引擎（4 大 Transcriber + 工作流稳定性补丁 + 整轨声纹对齐）

**Files:**
- Modify: `voice_review/workflow_builder.py`
- Create: `voice_review/transcribers.py`
- Test: `tests/test_transcribers.py`

**Interfaces:**
- Consumes: `voice_review.config.VoiceReviewConfig`, `voice_review.secrets.SecretStore`, `voice_review.diarizer.FluidAudioDiarizer`, `voice_review.segmenter`
- Produces:
  - `PreparedAudioJob`（封装预处理后的 `job_dir`, `audio_path`, `segments`, `batches`, `speakers_summary`, `diarization_result`, `transcript_path`）
  - `TranscriptOutcome(mode: str, transcript_path: Path, workflow_script_path: Path | None, segment_count: int, batch_count: int, speakers_summary: str)`
  - `align_asr_segments_to_diarization(asr_segments: list[dict], diar_segments: list[tuple[float, float, str]], speaker_labels: dict[str, str]) -> list[dict]`（按最大时间重叠率将整轨 ASR 句子回贴至 `FluidAudio` 说话人）
  - `TranscriberProvider` 抽象基类 + `get_transcriber(cfg: VoiceReviewConfig, secrets: SecretStore) -> TranscriberProvider`：
    - `AntigravityFlashTranscriber` (`antigravity-flash`)
    - `GeminiApiTranscriber` (`gemini-api`，并发 + `batches_out/` 断点缓存 + 429 指数退避 + `[语气]`/`[叠话]` 标注)
    - `OpenAICompatibleAsrTranscriber` (`openai-compatible-asr`，标准库 multipart/form-data 并发 + `batches_out/` 断点缓存)
    - `CommandAsrTranscriber` (`command-asr`，支持 `full_audio` 与 `segment` 双粒度，严格 `argv` + `shell=False`)
  - `test_transcriber_connection(cfg: VoiceReviewConfig, secrets: SecretStore) -> tuple[bool, str]`

- [ ] **Step 1: Write the failing test (`tests/test_transcribers.py`)**

覆盖 5 组核心测试：
1. `test_workflow_builder_has_global_launch_pacing_and_parser_err_cleanup`：验证 `render_workflow_script` 包含 `LAUNCH_PACE_SEC = 0.35` 全局节流锁与 `_PARSER_ERR_RE` 清洗，且 `sanitize_transcript_field` 能清除 `JsonParserError` / `Oops, I forgot` 碎文本。
2. `test_align_asr_segments_to_diarization`：验证整轨 ASR 句子按最大时间重叠精准归属对应说话人，并处理无重叠回退 `"未知说话人"`。
3. `test_gemini_api_transcriber_resume_cache_and_429_backoff`：Mock `urllib.request.urlopen`，第 1 批预先写好 `batches_out/B01.json` 确认跳过不发请求，第 2 批首次返 `429` 后重试成功，最终拼装含 `[语气]` 的标准 Markdown 转录稿。
4. `test_openai_compatible_asr_multipart_and_transcript_output`：Mock HTTP 端点，断言请求头含 `multipart/form-data` 和 `Authorization: Bearer ...`，并输出带时间戳与说话人标签的 Markdown。
5. `test_command_asr_full_audio_no_shell_injection`：传入含 `; echo HACKED` 的文件名和词表，用模拟脚本验证 `shell=False` 未执行注入命令，且正确解析 JSON 并对齐声纹落盘。

- [ ] **Step 2: Run test to verify it fails**

Run: `cd . && python3 -m unittest tests.test_transcribers -v`
Expected: FAIL

- [ ] **Step 3: Upgrade `workflow_builder.py` & implement `voice_review/transcribers.py`**

- 在 `workflow_builder.py`：
  - 给 `_PARSER_ERR_RE` 增补脏文本清洗并合并进 `sanitize_transcript_field`；
  - 在 `AdaptiveLimiter.acquire()` 内增加全局发射节流锁（`LAUNCH_PACE_SEC = max(0.35, STAGGER_SEC)`）确保任意并发/重试间隔平滑；
  - 在 `prepare_v2_job` 保持原签名 100% 兼容。
- 在 `transcribers.py` 实现 4 个 Provider + 公共 Markdown 渲染函数 `render_transcript_markdown(...)` + 进度回调 `on_progress(done_batches, total_batches)` + `test_transcriber_connection(...)`。

- [ ] **Step 4: Run tests to verify pass**

Run: `cd . && python3 -m unittest discover -s tests -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add voice_review/workflow_builder.py voice_review/transcribers.py tests/test_transcribers.py
git commit -m "feat: add 4 pluggable transcribers, full-audio diarization aligner and workflow pacing guard"
```

---

### Task 5: 可插拔分析引擎（含超长稿分块汇总）与升级状态机（含旧库迁移）

**Files:**
- Create: `voice_review/analyzers.py`
- Modify: `voice_review/agent_bridge.py`
- Modify: `voice_review/state.py`
- Test: `tests/test_analyzers_state.py`

**Interfaces:**
- Consumes: `voice_review.config.VoiceReviewConfig`, `voice_review.secrets.SecretStore`, `voice_review.rules.RuleStudio`, `voice_review.transcribers.TranscriptOutcome`
- Produces:
  - `AnalyzerProvider` 抽象 + `get_analyzer(cfg: VoiceReviewConfig, secrets: SecretStore, store: StateStore, rules: RuleStudio) -> AnalyzerProvider`：
    - `McpHostAnalyzer` (`mcp-host` / `none`：只准备提示词不远端调模型)
    - `AntigravityAgentAnalyzer` (`antigravity-agentapi`：自动注入 `define_subagent("audio_transcriber_lite", ...)` 引导、动态超时 `max(analysis_timeout_sec, 600 + batch_count * 45)`、RPC 断连退避重试、超时返回 `False` 绝不误标 `done`)
    - `OpenAICompatibleLlmAnalyzer` (`openai-compatible-llm`：标准库调 `/v1/chat/completions`，转录稿超 `80_000` 字符时自动分块提炼再终审汇总并写入 `analysis_path`)
    - `GeminiLlmAnalyzer` (`gemini-llm`：标准库调 Gemini `generateContent` 并写入 `analysis_path`)
  - `test_analyzer_connection(cfg: VoiceReviewConfig, secrets: SecretStore) -> tuple[bool, str]`
  - 升级版 `StateStore`：自动兼容迁移旧表新增 `source_kind`、`audio_path`、`template_id`、`retry_count`、`next_retry_at`、`progress_done`、`progress_total` 列；新增 `list_recent(limit=50, status=None)`、`record_failure(recording_id, error, *, max_retries=3, retry_stage="discovered", now_ts=None)`、`update_progress(recording_id, done, total)`；`next_pending()` 严禁拉取 `awaiting_analysis` / `skipped` / `done` / 退避期内的 `failed`。

- [ ] **Step 1: Write the failing test (`tests/test_analyzers_state.py`)**

覆盖 4 组核心测试：
1. `test_state_store_migrates_legacy_db_and_respects_awaiting_analysis`：手工建一张旧版 12 列 `recordings` 表插入记录，用新 `StateStore` 打开后验证无报错自动补齐新列，且状态设为 `awaiting_analysis` 或 `skipped` 时 `next_pending()` 返回 `None`；连续失败 3 次后退避并进入终态 `failed`。
2. `test_openai_llm_analyzer_chunks_long_transcript`：构造 170,000 字模拟转录稿，Mock `/v1/chat/completions`，断言触发了 3 次分块提炼 + 1 次最终汇总调用，且成功将带 YAML Frontmatter 的分析稿写入 `analysis_path`。
3. `test_gemini_llm_analyzer_writes_analysis_file`：Mock Gemini `generateContent`，断言成功写出分析稿并包含模板要求内容。
4. `test_antigravity_analyzer_dynamic_timeout_and_honest_failure`：验证 `batch_count=40` 时动态超时 ≥ `2400s`、提示词包含 `audio_transcriber_lite` 定义引导、等待落盘超时时抛出/返回失败状态而非误标 `done`。

- [ ] **Step 2: Run test to verify it fails**

Run: `cd . && python3 -m unittest tests.test_analyzers_state -v`
Expected: FAIL

- [ ] **Step 3: Upgrade `state.py`, `agent_bridge.py` & implement `voice_review/analyzers.py`**

- 在 `state.py` 的 `_init_db()` 查询 `PRAGMA table_info(recordings)` 缺列即自动 `ALTER TABLE ADD COLUMN`。
- 在 `agent_bridge.py` 补全 `deliver()` 瞬时 RPC 断连（`connection refused` / `Unavailable`）退避重试，并在渲染提示词时自动引导宿主定义极简只读 `audio_transcriber_lite`。
- 在 `analyzers.py` 实现 4 个分析器、`chunk_text_for_summary(text, max_chars=80000)` 与 `test_analyzer_connection`。

- [ ] **Step 4: Run tests to verify pass**

Run: `cd . && python3 -m unittest discover -s tests -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add voice_review/state.py voice_review/agent_bridge.py voice_review/analyzers.py tests/test_analyzers_state.py
git commit -m "feat: add pluggable analyzers with long-transcript chunking and upgraded StateStore"
```

---

### Task 6: 统一单守护服务（`vmr serve`）与 `127.0.0.1` REST / SSE API

**Files:**
- Create: `voice_review/daemon.py`
- Modify: `voice_review/runner.py`
- Test: `tests/test_daemon_api.py`

**Interfaces:**
- Consumes: `config.py`, `secrets.py`, `rules.py`, `sources.py`, `transcribers.py`, `analyzers.py`, `state.py`, `diarizer.VoiceprintStore`
- Produces:
  - `DaemonInfo(pid: int, host: str, port: int, token: str)`
  - `discover_running_daemon(vmr_home: Path) -> DaemonInfo | None`（读 `daemon.json` 并探活 `/api/v1/health`）
  - `VmrDaemon(cfg: VoiceReviewConfig, *, host: str = "127.0.0.1", port: int = 0)`：
    - 持有 `fcntl.flock` `daemon.lock`（防双开冲突）并写 `0600` 权限 `daemon.json`
    - 内置核心管线执行器 `PipelineExecutor`（处理单条任务：预处理 → 转写 → 按 `auto_provider` 决定自动分析或落入 `awaiting_analysis` → 实时广播 SSE 事件）
    - 后台热启停监听线程（支持修改配置即时开关 `voice_memos` / `drop_folder` 监听，无需重启进程）
    - REST API 路由（全部只绑定 `127.0.0.1`，写操作强检 `X-VMR-Token` 与本机 `Origin/Host`）：
      - `GET /api/v1/health`（无敏感信息）
      - `GET /api/v1/status`、`GET /api/v1/events`（SSE 实时包级进度流）
      - `GET /api/v1/jobs`、`POST /api/v1/jobs/upload`（multipart 或文件路径提交立即运行）、`POST /api/v1/jobs/<id>/analyze`（对 `awaiting_analysis` 手动选模板触发分析）、`GET /api/v1/jobs/<id>/content`（读转录稿/分析稿正文）
      - `GET/PUT /api/v1/config`（读取脱敏配置与掩码密钥；保存配置、校验插槽组合并热更新监听与 `secrets.json`）
      - `POST /api/v1/providers/test`（测试转写/分析连通性）
      - `GET/POST/DELETE /api/v1/templates[...]`、`POST /api/v1/templates/preview`（返回拼装后完整 Prompt 明文）、`GET/PUT /api/v1/rules/meta`（词表与背景）
      - `GET/POST/DELETE /api/v1/voiceprints[...]`
      - `GET/POST /api/v1/integrations[...]`（调用 Task 8 安装器探测与一键装卸）
      - `GET/POST /api/v1/update[...]`（调用 Task 9 更新器检查与执行更新）
  - `VoiceReviewRunner`（更新 `runner.py` 委托同一 `PipelineExecutor`，保持原有外部接口可用）

- [ ] **Step 1: Write the failing test (`tests/test_daemon_api.py`)**

覆盖 4 组核心测试：
1. `test_daemon_binds_loopback_enforces_token_and_masks_secrets`：起真实临时端口 `VmrDaemon`，断言无 Token 或非法 `Origin: https://evil.example` 发 `PUT /api/v1/config` 返回 `403`；带正确 `X-VMR-Token` 保存密钥后 `GET /api/v1/config` 返回脱敏掩码且不露明文。
2. `test_daemon_lock_prevents_duplicate_and_discover_reuses_port`：启动实例 1 后验证 `discover_running_daemon()` 返回准确端口与 Token，再尝试同目录启动实例 2 抛出单实例冲突错误（`DaemonAlreadyRunningError`）。
3. `test_daemon_submits_single_job_and_transitions_awaiting_analysis`：配置 `auto_provider="none"` + Mock 转写器，通过 `POST /api/v1/jobs/upload` 提交测试音频，轮询 `GET /api/v1/jobs` 断言落入 `awaiting_analysis` 并可读到转录稿正文；再调 `POST /api/v1/jobs/<id>/analyze` 配合 Mock 分析器转入 `done`。
4. `test_daemon_template_preview_endpoint`：调用 `POST /api/v1/templates/preview` 断言返回包含明文规则与占位转录文本的完整拼装 Prompt。

- [ ] **Step 2: Run test to verify it fails**

Run: `cd . && python3 -m unittest tests.test_daemon_api -v`
Expected: FAIL

- [ ] **Step 3: Implement `voice_review/daemon.py` & update `voice_review/runner.py`**

- 使用标准库 `http.server.ThreadingHTTPServer`，内置简单 SSE 广播器（`queue.Queue` 每连接订阅）推送任务状态与 `progress_done/progress_total`。
- 在处理 `POST /api/v1/jobs/upload` 时安全保存上传音频到 `work/uploads/<uuid>_<safe_name>` 并立刻入队唤醒 Worker。

- [ ] **Step 4: Run tests to verify pass**

Run: `cd . && python3 -m unittest discover -s tests -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add voice_review/daemon.py voice_review/runner.py tests/test_daemon_api.py
git commit -m "feat: add unified single-instance VmrDaemon with 127.0.0.1 REST/SSE API"
```

---

### Task 7: 零依赖本地可视化控制台（`voice_review/web_ui/index.html`）

**Files:**
- Create: `voice_review/web_ui/index.html`
- Create: `voice_review/web_ui/app.js`
- Create: `voice_review/web_ui/style.css`
- Modify: `voice_review/daemon.py`（挂载静态资源 `/`、`/assets/*` 并注入 URL `?token=` 引导逻辑）
- Test: `tests/test_web_ui.py`

**Interfaces:**
- Consumes: Task 6 提供的 `/api/v1/*` REST 与 SSE 接口
- Produces:
  - 完整 4 视图本地单页控制台（零 npm/构建依赖、遵循项目反模板化视觉设计规范，清爽克制）：
    1. **工作台**：拖拽/选文件上传、选模板立即跑、语音备忘录与投放文件夹开关热切、实时进度条（SSE）、侧边抽屉阅读渲染后的分析稿与转录稿；
    2. **引擎插槽**：卡片式选择转写器与分析器、不兼容组合实时提示拦截、`base_url / model / api_key` 编辑与「测试连通性」按钮；
    3. **规则工作室**：明文模板列表（新建/复制/编辑/删除/设为默认/重置内置）+ **「预览完整 Prompt」**抽屉 + 专名词表与背景编辑 + 声纹上传录入与删除；
    4. **AI 插件桥接 & 更新**：展示 Antigravity / Claude Code / Codex / Cursor 接入状态与一键安装/卸载按钮、当前版本与 GitHub Releases 检查更新按钮。

- [ ] **Step 1: 先查阅 `frontend-design` 技能确立视觉方向再动手**

读取 `frontend-design/SKILL.md`，为桌面级音频工具确定克制、温润纸感/工作室风格的专属配色与排版方案（杜绝廉价 AI 紫渐变模板）。

- [ ] **Step 2: Write the failing test (`tests/test_web_ui.py`)**

```python
import tempfile, unittest, urllib.request
from pathlib import Path
from voice_review.config import load_config
from voice_review.daemon import VmrDaemon

class TestWebUI(unittest.TestCase):
    def test_ui_serves_html_css_js_with_four_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(vmr_home=Path(tmp))
            daemon = VmrDaemon(cfg, port=0)
            daemon.start_in_thread()
            try:
                base = f"http://127.0.0.1:{daemon.port}"
                html = urllib.request.urlopen(f"{base}/").read().decode("utf-8")
                js = urllib.request.urlopen(f"{base}/assets/app.js").read().decode("utf-8")
                css = urllib.request.urlopen(f"{base}/assets/style.css").read().decode("utf-8")
                for marker in ("工作台", "引擎插槽", "规则工作室", "AI 插件"):
                    self.assertIn(marker, html)
                self.assertIn("X-VMR-Token", js)
                self.assertIn("/api/v1/events", js)
                self.assertGreater(len(css), 200)
            finally:
                daemon.stop()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd . && python3 -m unittest tests.test_web_ui -v`
Expected: FAIL (HTTP 404)

- [ ] **Step 4: Build `index.html`, `style.css`, `app.js` & wire static routes in `daemon.py`**

- URL 携带 `?token=...` 时前端将其存入 `sessionStorage` 并通过 `history.replaceState` 从地址栏清除，避免截屏泄露 Token。
- 所有写请求统一经封装函数 `apiFetch()` 自动携带 `X-VMR-Token`。
- 用极简原生 Markdown 渲染器（纯 JS 防 XSS 转义后渲染标题/列表/引用/加粗/代码块）直观展示分析稿与转录稿。

- [ ] **Step 5: Run tests to verify pass**

Run: `cd . && python3 -m unittest discover -s tests -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add voice_review/web_ui/ voice_review/daemon.py tests/test_web_ui.py
git commit -m "feat: add zero-dependency local Web console (vmr ui) with Rule Studio"
```

---

### Task 8: 跨 AI 标准 Stdio MCP Server、通用 Skill 与四客户端一键安装器

**Files:**
- Create: `voice_review/mcp_server.py`
- Create: `skills/voice-memo-review/SKILL.md`
- Create: `voice_review/installer.py`
- Test: `tests/test_mcp_and_installer.py`

**Interfaces:**
- Consumes: `config.py`, `rules.py`, `state.py`, `daemon.py`（`discover_running_daemon`, `PipelineExecutor`）, `diarizer.FluidAudioDiarizer`
- Produces:
  - `McpStdioServer(cfg: VoiceReviewConfig)`：
    - `handle_message(msg: dict) -> dict | None`（完整实现 MCP `initialize`、`notifications/initialized`、`tools/list`、`tools/call`、`resources/list`、`resources/read`）
    - `serve_stdio(stdin, stdout)`（按换行分隔 JSON-RPC 2.0 帧）
    - 8 个标准工具：`vmr_status`、`vmr_list_recordings`、`vmr_process_audio`（短音频/仅切段同步返回；中长录音直连转写提交到后台守护或独立 detached worker 立即返回 `job_id` 防 60~120s 超时）、`vmr_get_job_status`、`vmr_list_templates`、`vmr_get_template`、`vmr_save_template`、`vmr_save_analysis`、`vmr_manage_voiceprints`
    - 3 个标准资源：`vmr://templates/{id}`、`vmr://glossary`、`vmr://config/summary`
  - `ClientInstaller(*, vmr_bin: Path, skill_source_dir: Path, home_dir: Path | None = None)`：
    - `detect_clients() -> dict[str, ClientStatus]`（检测 `antigravity` / `claude` / `codex` / `cursor`）
    - `install(targets: list[str] | None = None) -> dict[str, InstallResult]`（先写 `.bak` 备份再幂等合并；Antigravity 仅写 `~/.gemini/config/plugins/voice-memo-review/` 绝不改动全局 `config.json`）
    - `uninstall(targets: list[str] | None = None) -> dict[str, InstallResult]`

- [ ] **Step 1: Write the failing test (`tests/test_mcp_and_installer.py`)**

覆盖 4 组核心测试：
1. `test_mcp_handshake_tools_resources_and_template_roundtrip`：向 `McpStdioServer.handle_message` 依次发送 `initialize`、`tools/list`、`resources/list`、`tools/call(vmr_save_template)`、`resources/read(vmr://templates/custom_test)`、`tools/call(vmr_save_analysis)`，断言完全符合 JSON-RPC 2.0 响应格式且分析稿带 Frontmatter 落盘并将状态更新为 `done`。
2. `test_mcp_long_audio_returns_async_job_id_without_blocking`：Mock 一段 1800 秒音频在 `gemini-api` 下调用 `vmr_process_audio`，断言立即返回 `status="running"` 与 `job_id`，不阻塞等待转写完成；随后调用 `vmr_get_job_status(job_id)` 返回进度/结果。
3. `test_installer_preserves_existing_claude_codex_cursor_and_isolates_antigravity`：在临时 `home_dir` 预置带多组 server 的 `~/.claude.json`、带注释与多区块的 `~/.codex/config.toml`、`~/.cursor/mcp.json` 与 `~/.gemini/config/config.json`。执行 `install()` 后断言：
   - Antigravity 在 `~/.gemini/config/plugins/voice-memo-review/` 生成 `plugin.json`、`mcp_config.json`（含 stdio `command` 且无 `variables`）、`skills/voice-memo-review/SKILL.md`，且 `~/.gemini/config/config.json` 字节内容**原封未动**；
   - Claude Code / Cursor 保留原 server 并生成 `.bak` 备份；
   - Codex 保留原注释与其他 `[mcp_servers.*]` 区块并正确追加 `[mcp_servers.voice-memo-review]`；
   - 执行 `uninstall()` 后干净移除 `voice-memo-review` 且原有配置毫发无损。
4. `test_skill_frontmatter_compatible_across_three_clients`：验证 `skills/voice-memo-review/SKILL.md` Frontmatter 同时具备 `name: voice-memo-review` 与非空 `description`。

- [ ] **Step 2: Run test to verify it fails**

Run: `cd . && python3 -m unittest tests.test_mcp_and_installer -v`
Expected: FAIL

- [ ] **Step 3: Implement `mcp_server.py`, `skills/voice-memo-review/SKILL.md` & `installer.py`, and connect `/api/v1/integrations` in `daemon.py`**

- Codex TOML 读写采用独立标记区块（`# >>> voice-memo-review managed >>>` ... `# <<< voice-memo-review managed <<<`）或精确解析 `[mcp_servers.voice-memo-review]` 段落边界，确保增删完全幂等不破坏手写注释。
- `vmr_process_audio` 当检测已有运行中的 `VmrDaemon` 时通过内部 `127.0.0.1` HTTP API 提交任务，否则以 `start_new_session=True` 拉起短生命周期 detached worker 托管，杜绝 MCP 客户端超时强杀丢失进度。

- [ ] **Step 4: Run tests to verify pass**

Run: `cd . && python3 -m unittest discover -s tests -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add voice_review/mcp_server.py skills/ voice_review/installer.py voice_review/daemon.py tests/test_mcp_and_installer.py
git commit -m "feat: add stdio MCP server, cross-client SKILL.md and idempotent 4-client installer"
```

---

### Task 9: CLI 总装、GitHub Releases 自更新器、免编译离线分发打包与全量回归验收

**Files:**
- Create: `voice_review/updater.py`
- Modify: `voice_review/__init__.py`（维护 `__version__ = "1.0.0"`）
- Modify: `voice_review/cli.py`（接齐全部子命令）
- Modify: `daemon.py`（接线 `/api/v1/update`）
- Create: `scripts/build_release.sh`
- Create: `scripts/install.sh`
- Modify: `README.md`
- Test: `tests/test_updater_cli.py`

**Interfaces:**
- Consumes: 全部前置模块
- Produces:
  - `SelfUpdater(repo: str, current_version: str, managed_dir: Path, vmr_home: Path)`：
    - `check_latest() -> UpdateInfo`（请求公开 GitHub Releases API `https://api.github.com/repos/{repo}/releases/latest`，无需 Token）
    - `apply_update(archive_url_or_path: str, expected_sha256: str) -> UpdateResult`（校验 SHA256，检测到 `.git` 开发目录拒绝自覆盖，解压至临时目录后原子替换 `managed_dir`，完全不动 `vmr_home`）
  - 完整 CLI `vmr`：`run / watch / ui / serve / mcp / install / uninstall / update / enroll / voiceprints / doctor / prep`
  - `scripts/build_release.sh`：构建 `dist/vmr-macos-arm64-v<ver>.tar.gz` + `SHA256SUMS`（打包源码 + 4 套模板 + Skill + 预编译 `fluidaudiocli` + 21MB Core ML 离线模型 + 静态 arm64 `ffmpeg`/`ffprobe` + `install.sh` + `启动控制台.command`）

- [ ] **Step 1: Write the failing test (`tests/test_updater_cli.py`)**

覆盖 3 组核心测试：
1. `test_updater_verifies_sha256_refuses_git_dev_dir_and_preserves_user_home`：构造本地 tar.gz 假发版包，断言错误 SHA256 被拒绝；指向含 `.git` 目录被拒绝自覆盖；指向受管目录成功原子替换新版本号文件，且 `vmr_home` 里的 `secrets.json` 与自定义模板完整保留。
2. `test_cli_subcommands_parsing_and_doctor`：通过 `cli.main(["doctor"])` 与参数解析验证 `run / watch / ui / serve / mcp / install / uninstall / update / prep` 全部注册且行为符合预期。
3. `test_install_script_syntax_and_build_script_syntax`：运行 `bash -n scripts/install.sh scripts/build_release.sh` 断言语法无误。

- [ ] **Step 2: Run test to verify it fails**

Run: `cd . && python3 -m unittest tests.test_updater_cli -v`
Expected: FAIL

- [ ] **Step 3: Implement `updater.py`, upgrade `cli.py`, write `scripts/build_release.sh` / `scripts/install.sh` & update `README.md`**

- `vmr ui` 命令：先 `discover_running_daemon()`，若运行中直接用 `webbrowser.open(url_with_token)` 复用打开；否则拉起 `VmrDaemon` 并自动打开浏览器。
- `vmr run <audio> [--template <id>] [--no-analyze]`：单文件直跑并实时打印阶段与包进度。
- `scripts/build_release.sh`：支持环境变量 `FLUIDAUDIO_BIN`、`FLUIDAUDIO_MODELS_DIR`、`STATIC_FFMPEG_BIN`、`STATIC_FFPROBE_BIN` 指定离线打包资产，清晰汇报体积并生成 `SHA256SUMS`。
- `README.md` 重写为面向同事与开源用户的清晰指南（3 分钟零门槛上手、显化规则定制、四插槽切换表、Antigravity/Claude Code/Codex 一键接入）。

- [ ] **Step 4: Run full verification suite (both repos!)**

1. 本仓库全量测试：
   Run: `cd . && python3 -m unittest discover -s tests -v`
   Expected: ALL PASS (0 failures, 0 errors)
2. 向后兼容回归验证（确保私人总控台 0 回归）：
   Run: `cd "<downstream-private-console>" && /opt/homebrew/bin/python3.13 -m unittest discover -s tests -v`
   Expected: 39/39 OK
3. 沙盒端到端实测：
   在临时目录执行 `./scripts/build_release.sh` → 解压 → `./install.sh` → 验证 `vmr doctor`、`vmr install`、`vmr ui` 接口与模拟任务全链路闭环。

- [ ] **Step 5: Commit**

```bash
git add voice_review/__init__.py voice_review/updater.py voice_review/cli.py voice_review/daemon.py scripts/build_release.sh scripts/install.sh README.md tests/test_updater_cli.py
git commit -m "feat: complete Phase 1 CLI, GitHub Releases updater and offline release builder"
```

---

## Self-Review Check Summary

- **Spec Coverage**：设计文档第 1～10 节所有 Phase 1 需求（四插槽、4 转写器、4 分析器、4 套无私货模板、Rule Studio、单守护 `vmr serve`、4 页本地控制台 `vmr ui`、标准 Stdio MCP + 长音频异步 `job_id`、通用 Skill、4 客户端安装卸载、公开 GitHub Releases `SHA256SUMS` 自更新、全离线免编译打包、下游集成项目 向后兼容）均精确落位至 Task 1～9，无遗漏。
- **Interface Consistency**：所有跨任务类名与方法名（`SecretStore`、`RuleStudio`、`AudioCandidate`、`NoSpeechDetectedError`、`TranscriberProvider`、`AnalyzerProvider`、`VmrDaemon`、`McpStdioServer`、`ClientInstaller`、`SelfUpdater`）在 `Consumes` / `Produces` 间一一对齐。
- **Review Focus Pinned**：5 类高风险边界用例全部显式写入对应 Task 的单元测试。
