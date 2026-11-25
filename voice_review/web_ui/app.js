(() => {
  "use strict";

  // 从 URL 读取一次性会话令牌并立即清除地址栏参数，防止截屏外泄
  const params = new URLSearchParams(window.location.search);
  const urlToken = params.get("token");
  if (urlToken) {
    sessionStorage.setItem("vmr_token", urlToken);
    params.delete("token");
    const cleanUrl = window.location.pathname + (params.toString() ? "?" + params.toString() : "");
    window.history.replaceState({}, document.title, cleanUrl);
  }
  const TOKEN = sessionStorage.getItem("vmr_token") || "";

  const state = {
    config: null,
    secrets: {},
    templates: [],
    activeTplId: null,
    jobs: [],
    drawerJobId: null,
    drawerData: { analysis: "", transcript: "", rawPrompt: "" },
    drawerMode: "job",
    drawerTab: "analysis",
  };

  const $ = (sel) => document.querySelector(sel);

  function showToast(msg, isErr = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.style.background = isErr ? "#a82c20" : "#141413";
    el.classList.add("show");
    clearTimeout(showToast._timer);
    showToast._timer = setTimeout(() => el.classList.remove("show"), 3200);
  }

  async function apiFetch(path, opts = {}) {
    const headers = Object.assign({}, opts.headers || {});
    if (TOKEN) headers["X-VMR-Token"] = TOKEN;
    if (opts.body && !(opts.body instanceof ArrayBuffer) && !(opts.body instanceof Blob) && typeof opts.body !== "string") {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const resp = await fetch(path, Object.assign({}, opts, { headers }));
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.error || `请求失败（HTTP ${resp.status}）`);
    }
    return data;
  }

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function inlineMd(escaped) {
    return escaped
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
  }

  function renderMarkdown(md) {
    const lines = String(md || "").replace(/\r\n/g, "\n").split("\n");
    const out = [];
    let inList = false;
    let inCode = false;
    let codeLines = [];
    let tableRows = [];

    const flushList = () => {
      if (inList) { out.push("</ul>"); inList = false; }
    };
    const flushTable = () => {
      if (!tableRows.length) return;
      const headCells = tableRows[0];
      const bodyRows = tableRows.slice(2);
      const thead = `<thead><tr>${headCells.map((c) => `<th>${inlineMd(escapeHtml(c))}</th>`).join("")}</tr></thead>`;
      const tbody = bodyRows.length
        ? `<tbody>${bodyRows.map((r) => `<tr>${r.map((c) => `<td>${inlineMd(escapeHtml(c))}</td>`).join("")}</tr>`).join("")}</tbody>`
        : "";
      out.push(`<table>${thead}${tbody}</table>`);
      tableRows = [];
    };

    for (const raw of lines) {
      if (raw.trim().startsWith("```")) {
        flushList();
        flushTable();
        if (!inCode) {
          inCode = true;
          codeLines = [];
        } else {
          out.push(`<pre>${escapeHtml(codeLines.join("\n"))}</pre>`);
          inCode = false;
        }
        continue;
      }
      if (inCode) {
        codeLines.push(raw);
        continue;
      }
      if (raw.trim().startsWith("|") && raw.trim().endsWith("|")) {
        flushList();
        const cells = raw.trim().slice(1, -1).split("|").map((c) => c.trim());
        tableRows.push(cells);
        continue;
      }
      flushTable();

      const line = escapeHtml(raw);
      if (/^###\s+/.test(line)) {
        flushList();
        out.push(`<h3>${inlineMd(line.replace(/^###\s+/, ""))}</h3>`);
      } else if (/^##\s+/.test(line)) {
        flushList();
        out.push(`<h2>${inlineMd(line.replace(/^##\s+/, ""))}</h2>`);
      } else if (/^#\s+/.test(line)) {
        flushList();
        out.push(`<h1>${inlineMd(line.replace(/^#\s+/, ""))}</h1>`);
      } else if (/^&gt;\s?/.test(line)) {
        flushList();
        out.push(`<blockquote>${inlineMd(line.replace(/^&gt;\s?/, ""))}</blockquote>`);
      } else if (/^[-*]\s+/.test(line)) {
        if (!inList) { out.push("<ul>"); inList = true; }
        out.push(`<li>${inlineMd(line.replace(/^[-*]\s+/, ""))}</li>`);
      } else if (line.trim() === "---") {
        flushList();
        out.push("<hr />");
      } else if (line.trim() === "") {
        flushList();
      } else {
        flushList();
        out.push(`<p>${inlineMd(line)}</p>`);
      }
    }
    flushList();
    flushTable();
    if (inCode) out.push(`<pre>${escapeHtml(codeLines.join("\n"))}</pre>`);
    return out.join("\n") || '<div class="empty-state">暂无正文内容</div>';
  }

  // 主导航切换
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
      btn.classList.add("active");
      $(`#view-${btn.dataset.view}`).classList.add("active");
      if (btn.dataset.view === "integrations") {
        Promise.all([loadIntegrations(), loadLocalExtensions()]).catch((e) => showToast(e.message, true));
      }
      if (btn.dataset.view === "rules") {
        Promise.all([loadTemplates(), loadRuleMeta(), loadVoiceprints()]).catch((e) => showToast(e.message, true));
      }
    });
  });

  // 规则工作室子 Tab 切换
  document.querySelectorAll("[data-rules-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-rules-tab]").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".rules-pane").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $(`#rules-pane-${btn.dataset.rulesTab}`).classList.add("active");
    });
  });

  // 状态加载
  async function loadStatus() {
    if (!TOKEN) {
      $("#tokenBanner").hidden = false;
      $("#daemonDot").className = "status-dot error";
      $("#daemonLabel").textContent = "缺少会话令牌";
      return;
    }
    try {
      const st = await apiFetch("/api/v1/status");
      state.daemonPid = st.pid;
      $("#tokenBanner").hidden = true;
      $("#appVersion").textContent = `v${st.version}`;
      $("#updateCurrentVer").textContent = `v${st.version}`;
      $("#daemonDot").className = "status-dot online";
      $("#daemonLabel").textContent = st.watching ? "监听运行中" : "本地守护已就绪";
      $("#engineMini").textContent = `${st.transcriber_provider} → ${st.analyzer_auto_provider}`;
      $("#toggleVoiceMemos").checked = Boolean(st.voice_memos_enabled);
      $("#toggleDropFolder").checked = Boolean(st.drop_folder_enabled);
      const autoPill = $("#autostartPill");
      if (autoPill) {
        autoPill.hidden = !st.autostart_supported;
        $("#toggleAutostart").checked = Boolean(st.autostart_enabled);
      }
      if (st.update_available && !state.updateBannerDismissed) {
        showUpdateBanner(st.latest_version, st.release_notes || "");
      }
    } catch (err) {
      $("#daemonDot").className = "status-dot error";
      $("#daemonLabel").textContent = "守护未连接或令牌失效";
    }
  }

  // 任务列表
  const STATUS_TEXT = {
    discovered: ["排队中", "running"],
    preparing: ["本地声纹切段中", "running"],
    prepared: ["待转写", "awaiting"],
    transcribing: ["转写中", "running"],
    transcribed: ["已转写", "running"],
    analyzing: ["分析中", "running"],
    awaiting_analysis: ["待分析", "awaiting"],
    done: ["已完成", "done"],
    skipped: ["静音已跳过", ""],
    failed: ["处理失败", "failed"],
  };

  function fmtDur(sec) {
    const total = Math.max(0, Math.round(sec || 0));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return m > 0 ? `${m}分${String(s).padStart(2, "0")}秒` : `${s}秒`;
  }

  async function loadJobs() {
    const data = await apiFetch("/api/v1/jobs?limit=60");
    state.jobs = data.jobs || [];
    const box = $("#jobsList");
    if (!state.jobs.length) {
      box.innerHTML = '<div class="empty-state">还没有录音记录，拖入一段音频开始吧。</div>';
      return;
    }
    box.innerHTML = state.jobs.map((j) => {
      const [label, cls] = STATUS_TEXT[j.status] || [j.status, ""];
      const prog = j.status === "transcribing" && j.progress_total > 0
        ? ` ${j.progress_done}/${j.progress_total}` : "";
      const errNote = j.last_error ? ` · ${escapeHtml(j.last_error.slice(0, 60))}` : "";
      const canRead = Boolean(j.transcript_path || j.analysis_path);
      return `
        <div class="job-row">
          <div class="job-info">
            <div class="job-title">${escapeHtml(j.title)}</div>
            <div class="job-meta">${escapeHtml(j.recorded_at.slice(0, 16).replace("T", " "))} · ${fmtDur(j.duration_sec)} · ${escapeHtml(j.source_kind)}${errNote}</div>
          </div>
          <div class="job-right">
            <span class="badge ${cls}">${label}${prog}</span>
            ${canRead ? `<button class="btn subtle" data-open-job="${escapeHtml(j.recording_id)}" type="button">阅读</button>` : ""}
            ${j.status === "failed" ? `<button class="btn subtle" data-retry-job="${escapeHtml(j.recording_id)}" type="button">重试</button>` : ""}
          </div>
        </div>
      `;
    }).join("");

    box.querySelectorAll("[data-open-job]").forEach((b) => {
      b.addEventListener("click", () => openJobDrawer(b.dataset.openJob).catch((e) => showToast(e.message, true)));
    });
    box.querySelectorAll("[data-retry-job]").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          await apiFetch(`/api/v1/jobs/${encodeURIComponent(b.dataset.retryJob)}/retry`, { method: "POST", body: {} });
          showToast("已重新加入队列");
          await loadJobs();
        } catch (e) { showToast(e.message, true); }
      });
    });
  }

  // 上传录音
  async function uploadAudioFile(file) {
    const tplId = $("#uploadTemplateSelect").value || "";
    const qs = new URLSearchParams({ filename: file.name, template_id: tplId });
    showToast(`正在上传：${file.name}`);
    await apiFetch(`/api/v1/jobs/upload?${qs.toString()}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
    showToast(`已开始处理：${file.name}`);
    await loadJobs();
  }

  const dropZone = $("#dropZone");
  const fileInput = $("#fileInput");
  $("#pickFileBtn").addEventListener("click", () => fileInput.click());
  $("#dropClickArea").addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    if (fileInput.files && fileInput.files[0]) uploadAudioFile(fileInput.files[0]).catch((e) => showToast(e.message, true));
    fileInput.value = "";
  });
  ["dragenter", "dragover"].forEach((ev) => dropZone.addEventListener(ev, (e) => { e.preventDefault(); dropZone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((ev) => dropZone.addEventListener(ev, (e) => { e.preventDefault(); dropZone.classList.remove("dragover"); }));
  dropZone.addEventListener("drop", (e) => {
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) uploadAudioFile(f).catch((err) => showToast(err.message, true));
  });

  // 快捷开关监听源
  async function updateSourceToggle() {
    try {
      await apiFetch("/api/v1/config", {
        method: "PUT",
        body: {
          sources: {
            voice_memos_enabled: $("#toggleVoiceMemos").checked,
            drop_folder_enabled: $("#toggleDropFolder").checked,
          },
        },
      });
      showToast("监听开关已更新");
      await loadStatus();
    } catch (e) {
      showToast(e.message, true);
    }
  }
  $("#toggleVoiceMemos").addEventListener("change", updateSourceToggle);
  $("#toggleDropFolder").addEventListener("change", updateSourceToggle);
  $("#toggleAutostart").addEventListener("change", async () => {
    const want = $("#toggleAutostart").checked;
    const prevPid = state.daemonPid;
    try {
      const res = await apiFetch("/api/v1/autostart", { method: "PUT", body: { enabled: want } });
      showToast(res.message || (want ? "已开启开机自启" : "已关闭开机自启"));
      if (res.restarting) {
        await waitForDaemonReconnect(prevPid, false);
      }
      await loadStatus();
    } catch (e) {
      $("#toggleAutostart").checked = !want;
      showToast(e.message, true);
    }
  });
  $("#refreshJobsBtn").addEventListener("click", () => loadJobs().catch((e) => showToast(e.message, true)));

  // 引擎插槽配置
  function refreshProviderFieldVisibility() {
    const tr = $("#cfgTranscriber").value;
    const an = $("#cfgAnalyzer").value;
    const trShow = {
      "antigravity-flash": [],
      "gemini-api": ["base_url", "model", "api_key"],
      "openai-compatible-asr": ["base_url", "model", "api_key"],
      "command-asr": ["command", "mode"],
    }[tr] || [];
    document.querySelectorAll("[data-tr-field]").forEach((el) => {
      el.hidden = !trShow.includes(el.dataset.trField);
    });
    const anShow = {
      "antigravity-agentapi": [],
      "codex-cli": ["model"],
      "claude-cli": ["model"],
      "none": [],
      "openai-compatible-llm": ["base_url", "model", "api_key"],
      "gemini-llm": ["base_url", "model", "api_key"],
    }[an] || [];
    document.querySelectorAll("[data-an-field]").forEach((el) => {
      el.hidden = !anShow.includes(el.dataset.anField);
    });
    if (tr === "gemini-api") {
      $("#cfgTrBaseUrl").placeholder = "https://generativelanguage.googleapis.com/v1beta";
      $("#cfgTrModel").placeholder = "gemini-2.5-flash";
    } else if (tr === "openai-compatible-asr") {
      $("#cfgTrBaseUrl").placeholder = "https://api.openai.com/v1";
      $("#cfgTrModel").placeholder = "whisper-1";
    }
    if (an === "gemini-llm") {
      $("#cfgAnBaseUrl").placeholder = "https://generativelanguage.googleapis.com/v1beta";
      $("#cfgAnModel").placeholder = "gemini-2.5-pro";
    } else if (an === "openai-compatible-llm") {
      $("#cfgAnBaseUrl").placeholder = "https://api.deepseek.com/v1";
      $("#cfgAnModel").placeholder = "deepseek-chat";
    } else if (an === "codex-cli") {
      $("#cfgAnModel").placeholder = "留空使用本机 Codex 默认模型";
    } else if (an === "claude-cli") {
      $("#cfgAnModel").placeholder = "留空使用本机 Claude Code 默认模型";
    }
    const warn = $("#comboWarning");
    if (tr === "antigravity-flash" && ["openai-compatible-llm", "gemini-llm", "codex-cli", "claude-cli"].includes(an)) {
      warn.hidden = false;
      warn.textContent = "组合不兼容：antigravity-flash 依赖 Antigravity 一体化工作流，不能直接搭配独立分析引擎（Codex / Claude / 外部 API）。若想用 Codex 或 Claude 自动分析，请把转写引擎切为 Gemini API、OpenAI 兼容 ASR 或自定义本地命令行。";
    } else {
      warn.hidden = true;
    }
  }

  function syncSecretInputsForSelectedProviders() {
    const tr = $("#cfgTranscriber").value;
    const an = $("#cfgAnalyzer").value;
    $("#cfgTrApiKey").value = tr === "gemini-api"
      ? (state.secrets.gemini_api_key || "")
      : (tr === "openai-compatible-asr" ? (state.secrets.openai_asr_api_key || "") : "");
    $("#cfgAnApiKey").value = an === "gemini-llm"
      ? (state.secrets.gemini_api_key || "")
      : (an === "openai-compatible-llm" ? (state.secrets.openai_llm_api_key || "") : "");
  }

  function buildProvidersPayloadFromForm() {
    const tr = $("#cfgTranscriber").value;
    const an = $("#cfgAnalyzer").value;
    const secretsPatch = {};
    const putSecretIfNew = (key, rawVal) => {
      const val = String(rawVal || "").trim();
      if (val && !val.includes("****")) secretsPatch[key] = val;
    };
    if (tr === "gemini-api") putSecretIfNew("gemini_api_key", $("#cfgTrApiKey").value);
    if (tr === "openai-compatible-asr") putSecretIfNew("openai_asr_api_key", $("#cfgTrApiKey").value);
    if (an === "gemini-llm") putSecretIfNew("gemini_api_key", $("#cfgAnApiKey").value);
    if (an === "openai-compatible-llm") putSecretIfNew("openai_llm_api_key", $("#cfgAnApiKey").value);

    const trObj = { provider: tr };
    if (tr === "gemini-api" || tr === "openai-compatible-asr") {
      if ($("#cfgTrBaseUrl").value.trim()) trObj.base_url = $("#cfgTrBaseUrl").value.trim();
      if ($("#cfgTrModel").value.trim()) trObj.model = $("#cfgTrModel").value.trim();
    } else if (tr === "command-asr") {
      trObj.mode = $("#cfgTrMode").value;
      if ($("#cfgTrCommand").value.trim()) trObj.command = $("#cfgTrCommand").value.trim();
    }

    const anObj = { auto_provider: an };
    if (an === "openai-compatible-llm" || an === "gemini-llm") {
      if ($("#cfgAnBaseUrl").value.trim()) anObj.base_url = $("#cfgAnBaseUrl").value.trim();
      if ($("#cfgAnModel").value.trim()) anObj.model = $("#cfgAnModel").value.trim();
    } else if (an === "codex-cli" || an === "claude-cli") {
      if ($("#cfgAnModel").value.trim()) anObj.model = $("#cfgAnModel").value.trim();
    }
    return { trObj, anObj, secretsPatch };
  }

  async function loadConfig() {
    const data = await apiFetch("/api/v1/config");
    state.config = data.config;
    state.secrets = data.secrets || {};
    const c = state.config;
    $("#cfgOutputDir").value = c.output_dir || "";
    $("#cfgDropDir").value = (c.sources && c.sources.drop_folder_dir) || "";
    $("#cfgPreprocessor").value = (c.preprocessor && c.preprocessor.mode) || "fluidaudio";
    $("#cfgTranscriber").value = (c.transcriber && c.transcriber.provider) || "antigravity-flash";
    $("#cfgTrBaseUrl").value = (c.transcriber && c.transcriber.base_url) || "";
    $("#cfgTrModel").value = (c.transcriber && c.transcriber.model) || "";
    const trCmd = c.transcriber && c.transcriber.command;
    $("#cfgTrCommand").value = Array.isArray(trCmd) ? trCmd.join(" ") : (trCmd || "");
    $("#cfgTrMode").value = (c.transcriber && c.transcriber.mode) || "full_audio";
    $("#cfgAnalyzer").value = (c.analyzer && c.analyzer.auto_provider) || "antigravity-agentapi";
    $("#cfgAnBaseUrl").value = (c.analyzer && c.analyzer.base_url) || "";
    $("#cfgAnModel").value = (c.analyzer && c.analyzer.model) || "";
    $("#updateRepoLabel").textContent = (c.update && c.update.repo) || "shane1124/voice-memo-review";
    refreshProviderFieldVisibility();
    syncSecretInputsForSelectedProviders();
  }

  $("#cfgTranscriber").addEventListener("change", () => {
    refreshProviderFieldVisibility();
    syncSecretInputsForSelectedProviders();
  });
  $("#cfgAnalyzer").addEventListener("change", () => {
    refreshProviderFieldVisibility();
    syncSecretInputsForSelectedProviders();
  });

  $("#saveProvidersBtn").addEventListener("click", async () => {
    const { trObj, anObj, secretsPatch } = buildProvidersPayloadFromForm();
    try {
      await apiFetch("/api/v1/config", {
        method: "PUT",
        body: {
          output_dir: $("#cfgOutputDir").value.trim(),
          sources: { drop_folder_dir: $("#cfgDropDir").value.trim() },
          preprocessor: { mode: $("#cfgPreprocessor").value },
          transcriber: trObj,
          analyzer: anObj,
          secrets: secretsPatch,
        },
      });
      showToast("配置已保存");
      await loadConfig();
      await loadStatus();
    } catch (e) {
      showToast(e.message, true);
    }
  });

  $("#testTranscriberBtn").addEventListener("click", async () => {
    const { trObj, secretsPatch } = buildProvidersPayloadFromForm();
    try {
      const res = await apiFetch("/api/v1/providers/test", {
        method: "POST",
        body: { kind: "transcriber", transcriber: trObj, secrets: secretsPatch },
      });
      showToast(res.message, !res.ok);
    } catch (e) { showToast(e.message, true); }
  });

  $("#testAnalyzerBtn").addEventListener("click", async () => {
    const { anObj, secretsPatch } = buildProvidersPayloadFromForm();
    try {
      const res = await apiFetch("/api/v1/providers/test", {
        method: "POST",
        body: { kind: "analyzer", analyzer: anObj, secrets: secretsPatch },
      });
      showToast(res.message, !res.ok);
    } catch (e) { showToast(e.message, true); }
  });

  // 规则工作室
  function fillTemplateEditor(tpl) {
    state.activeTplId = tpl ? tpl.id : null;
    $("#tplId").value = tpl ? tpl.id : "";
    $("#tplName").value = tpl ? tpl.name : "";
    $("#tplDesc").value = tpl ? tpl.description : "";
    $("#tplBody").value = tpl ? tpl.body : "";
    $("#tplDefault").checked = Boolean(tpl && tpl.is_default);
    $("#resetTemplateBtn").hidden = !(tpl && tpl.is_builtin);
    const diffHint = $("#tplBuiltinDiffHint");
    if (diffHint) diffHint.hidden = !(tpl && tpl.is_builtin && tpl.differs_from_builtin);
    renderTemplateList();
  }

  function renderTemplateList() {
    const box = $("#templateList");
    box.innerHTML = state.templates.map((t) => `
      <button type="button" class="tpl-item ${t.id === state.activeTplId ? "active" : ""}" data-tid="${escapeHtml(t.id)}">
        <div class="tpl-item-name">
          <span>${escapeHtml(t.name)}</span>
          ${t.is_default ? '<span class="badge done">默认</span>' : ""}
        </div>
        <div class="tpl-item-desc">${escapeHtml(t.description || t.id)}</div>
      </button>
    `).join("");
    box.querySelectorAll("[data-tid]").forEach((b) => {
      b.addEventListener("click", () => {
        const found = state.templates.find((x) => x.id === b.dataset.tid);
        if (found) fillTemplateEditor(found);
      });
    });

    const sel = $("#uploadTemplateSelect");
    const prev = sel.value;
    sel.innerHTML = state.templates.map((t) => `<option value="${escapeHtml(t.id)}">${escapeHtml(t.name)}${t.is_default ? "（默认）" : ""}</option>`).join("");
    if (prev && state.templates.some((t) => t.id === prev)) sel.value = prev;
  }

  async function loadTemplates() {
    const data = await apiFetch("/api/v1/templates");
    state.templates = data.templates || [];
    const current = state.templates.find((t) => t.id === state.activeTplId) || state.templates[0] || null;
    fillTemplateEditor(current);
  }

  async function loadRuleMeta() {
    const data = await apiFetch("/api/v1/rules/meta");
    $("#rulesGlossary").value = data.glossary || "";
    $("#rulesPersona").value = data.persona || "";
  }

  async function loadVoiceprints() {
    const data = await apiFetch("/api/v1/voiceprints");
    const items = data.voiceprints || [];
    const box = $("#voiceprintList");
    if (!items.length) {
      box.innerHTML = '<div class="empty-state">尚未录入声纹，上传 10～30 秒清晰单人样音即可自动认人。</div>';
      return;
    }
    box.innerHTML = items.map((v) => `
      <div class="vp-row">
        <div><strong>${escapeHtml(v.name)}</strong> <span class="job-meta">· ${v.samples} 条样本</span></div>
        <button class="btn danger-text" data-del-vp="${escapeHtml(v.name)}" type="button">删除</button>
      </div>
    `).join("");
    box.querySelectorAll("[data-del-vp]").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          await apiFetch(`/api/v1/voiceprints/${encodeURIComponent(b.dataset.delVp)}`, { method: "DELETE" });
          showToast(`已删除声纹：${b.dataset.delVp}`);
          await loadVoiceprints();
        } catch (e) { showToast(e.message, true); }
      });
    });
  }

  $("#newTemplateBtn").addEventListener("click", () => {
    fillTemplateEditor({ id: "", name: "", description: "", body: "", is_default: false, is_builtin: false });
    $("#tplName").focus();
  });

  $("#saveTemplateBtn").addEventListener("click", async () => {
    try {
      const res = await apiFetch("/api/v1/templates", {
        method: "POST",
        body: {
          id: $("#tplId").value.trim(),
          name: $("#tplName").value.trim(),
          description: $("#tplDesc").value.trim(),
          body: $("#tplBody").value,
          is_default: $("#tplDefault").checked,
        },
      });
      state.activeTplId = res.template.id;
      showToast(`已保存模板「${res.template.name}」`);
      await loadTemplates();
    } catch (e) { showToast(e.message, true); }
  });

  $("#resetTemplateBtn").addEventListener("click", async () => {
    if (!state.activeTplId) return;
    try {
      await apiFetch(`/api/v1/templates/${encodeURIComponent(state.activeTplId)}/reset`, { method: "POST", body: {} });
      showToast("已恢复内置默认正文");
      await loadTemplates();
    } catch (e) { showToast(e.message, true); }
  });

  $("#deleteTemplateBtn").addEventListener("click", async () => {
    if (!state.activeTplId) return;
    try {
      await apiFetch(`/api/v1/templates/${encodeURIComponent(state.activeTplId)}`, { method: "DELETE" });
      state.activeTplId = null;
      showToast("模板已删除");
      await loadTemplates();
    } catch (e) { showToast(e.message, true); }
  });

  $("#saveMetaRulesBtn").addEventListener("click", async () => {
    try {
      await apiFetch("/api/v1/rules/meta", {
        method: "PUT",
        body: { glossary: $("#rulesGlossary").value, persona: $("#rulesPersona").value },
      });
      showToast("词表与个人背景已保存");
    } catch (e) { showToast(e.message, true); }
  });

  $("#previewPromptBtn").addEventListener("click", async () => {
    try {
      const res = await apiFetch("/api/v1/templates/preview", {
        method: "POST",
        body: {
          template_id: state.activeTplId || undefined,
          name: $("#tplName").value.trim() || undefined,
          body: $("#tplBody").value,
          title: "示例项目周会",
        },
      });
      state.drawerMode = "prompt";
      state.drawerData.rawPrompt = res.prompt;
      $("#drawerTitle").textContent = "完整 Prompt 预览";
      $("#drawerTabs").hidden = true;
      $("#drawerReanalyzeBtn").hidden = true;
      $("#drawerContent").innerHTML = `<pre>${escapeHtml(res.prompt)}</pre>`;
      openDrawerUI();
    } catch (e) { showToast(e.message, true); }
  });

  $("#vpUploadBtn").addEventListener("click", () => {
    if (!$("#vpNameInput").value.trim()) { showToast("请先输入说话人姓名", true); return; }
    $("#vpFileInput").click();
  });

  $("#vpFileInput").addEventListener("change", async () => {
    const file = $("#vpFileInput").files && $("#vpFileInput").files[0];
    if (!file) return;
    const qs = new URLSearchParams({ name: $("#vpNameInput").value.trim(), filename: file.name });
    try {
      showToast("正在提取声纹特征…");
      const res = await apiFetch(`/api/v1/voiceprints/upload?${qs.toString()}`, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: file,
      });
      showToast(`录入成功：${res.name}（有效人声 ${res.talk_sec}s）`);
      $("#vpNameInput").value = "";
      await loadVoiceprints();
    } catch (e) { showToast(e.message, true); }
    finally { $("#vpFileInput").value = ""; }
  });

  // 侧边抽屉
  function openDrawerUI() {
    $("#drawerBackdrop").hidden = false;
    $("#readerDrawer").hidden = false;
  }
  function closeDrawerUI() {
    $("#drawerBackdrop").hidden = true;
    $("#readerDrawer").hidden = true;
  }
  $("#drawerBackdrop").addEventListener("click", closeDrawerUI);
  $("#drawerCloseBtn").addEventListener("click", closeDrawerUI);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#readerDrawer").hidden) closeDrawerUI();
  });

  function renderDrawerTab() {
    document.querySelectorAll("#drawerTabs .seg-btn").forEach((t) => {
      t.classList.toggle("active", t.dataset.tab === state.drawerTab);
    });
    const text = state.drawerTab === "analysis" ? state.drawerData.analysis : state.drawerData.transcript;
    $("#drawerContent").innerHTML = renderMarkdown(text);
  }

  document.querySelectorAll("#drawerTabs .seg-btn").forEach((t) => {
    t.addEventListener("click", () => {
      state.drawerTab = t.dataset.tab;
      renderDrawerTab();
    });
  });

  async function openJobDrawer(recId) {
    const data = await apiFetch(`/api/v1/jobs/${encodeURIComponent(recId)}/content`);
    state.drawerMode = "job";
    state.drawerJobId = recId;
    state.drawerData.analysis = data.analysis || "";
    state.drawerData.transcript = data.transcript || "";
    state.drawerTab = data.analysis ? "analysis" : "transcript";
    $("#drawerTitle").textContent = data.job.title;
    $("#drawerTabs").hidden = false;
    $("#drawerReanalyzeBtn").hidden = !data.transcript;
    renderDrawerTab();
    openDrawerUI();
  }

  $("#drawerReanalyzeBtn").addEventListener("click", async () => {
    if (!state.drawerJobId) return;
    const tplId = $("#uploadTemplateSelect").value || undefined;
    try {
      showToast("正在重新分析…");
      await apiFetch(`/api/v1/jobs/${encodeURIComponent(state.drawerJobId)}/analyze`, {
        method: "POST",
        body: { template_id: tplId },
      });
      showToast("分析已完成");
      await openJobDrawer(state.drawerJobId);
      await loadJobs();
    } catch (e) { showToast(e.message, true); }
  });

  $("#drawerCopyBtn").addEventListener("click", async () => {
    const text = state.drawerMode === "prompt"
      ? state.drawerData.rawPrompt
      : (state.drawerTab === "analysis" ? state.drawerData.analysis : state.drawerData.transcript);
    try {
      await navigator.clipboard.writeText(text || "");
      showToast("已复制到剪贴板");
    } catch (e) { showToast("复制失败，请手动选中复制", true); }
  });

  // AI 插件桥接与更新（简洁设置列表行风格）
  async function loadIntegrations() {
    const data = await apiFetch("/api/v1/integrations");
    const clients = data.clients || {};
    const box = $("#clientsGrid");
    const keys = Object.keys(clients);
    if (!keys.length) {
      box.innerHTML = '<div class="empty-state">暂未扫描到 AI 客户端</div>';
      return;
    }
    box.innerHTML = keys.map((k) => {
      const c = clients[k];
      const badge = c.installed
        ? '<span class="badge done">已接入</span>'
        : (c.detected ? '<span class="badge awaiting">未接入</span>' : '<span class="badge">未安装</span>');
      return `
        <div class="client-row">
          <div class="client-info">
            <div class="client-name">${escapeHtml(c.display_name || k)}</div>
            <div class="client-path">${escapeHtml(c.config_path || "")}</div>
          </div>
          <div class="client-right">
            ${badge}
            <button class="btn subtle" data-inst="${escapeHtml(k)}" type="button">${c.installed ? "同步" : "接入"}</button>
            ${c.installed ? `<button class="btn danger-text" data-uninst="${escapeHtml(k)}" type="button">卸载</button>` : ""}
          </div>
        </div>
      `;
    }).join("");

    box.querySelectorAll("[data-inst]").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          await apiFetch("/api/v1/integrations/install", { method: "POST", body: { targets: [b.dataset.inst] } });
          showToast(`已同步接入 ${b.dataset.inst}（请新开对话或重启客户端生效）`);
          await loadIntegrations();
        } catch (e) { showToast(e.message, true); }
      });
    });
    box.querySelectorAll("[data-uninst]").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          await apiFetch("/api/v1/integrations/uninstall", { method: "POST", body: { targets: [b.dataset.uninst] } });
          showToast(`已卸载 ${b.dataset.uninst}`);
          await loadIntegrations();
        } catch (e) { showToast(e.message, true); }
      });
    });
  }

  $("#refreshIntegrationsBtn").addEventListener("click", () => {
    Promise.all([loadIntegrations(), loadLocalExtensions()]).catch((e) => showToast(e.message, true));
  });

  async function loadLocalExtensions() {
    const grp = $("#localExtensionsGroup");
    const box = $("#extensionsGrid");
    if (!grp || !box) return;
    try {
      const data = await apiFetch("/api/v1/extensions");
      const exts = data.extensions || [];
      if (!exts.length) {
        grp.hidden = true;
        return;
      }
      grp.hidden = false;
      box.innerHTML = exts.map((e) => {
        const hooksTxt = (e.hooks || []).join(" · ");
        const errTxt = e.error ? ` · 异常：${escapeHtml(e.error)}` : "";
        return `
          <div class="client-row">
            <div class="client-info">
              <div class="client-name">${escapeHtml(e.name || e.id)}</div>
              <div class="client-path">${escapeHtml(e.description || e.id)}${hooksTxt ? `（${escapeHtml(hooksTxt)}）` : ""}${errTxt}</div>
            </div>
            <div class="client-right">
              <label class="switch-pill">
                <input type="checkbox" data-ext-toggle="${escapeHtml(e.id)}" ${e.enabled ? "checked" : ""} />
                <span>${e.enabled ? "已启用" : "已停用"}</span>
              </label>
            </div>
          </div>
        `;
      }).join("");
      box.querySelectorAll("[data-ext-toggle]").forEach((cb) => {
        cb.addEventListener("change", async () => {
          const id = cb.dataset.extToggle;
          const want = cb.checked;
          try {
            await apiFetch(`/api/v1/extensions/${encodeURIComponent(id)}`, {
              method: "PUT",
              body: { enabled: want },
            });
            showToast(`本机扩展 ${id} 已${want ? "启用" : "停用"}`);
            await loadLocalExtensions();
          } catch (err) {
            cb.checked = !want;
            showToast(err.message, true);
          }
        });
      });
    } catch (_) {
      grp.hidden = true;
    }
  }
  $("#installAllClientsBtn").addEventListener("click", async () => {
    try {
      await apiFetch("/api/v1/integrations/install", { method: "POST", body: {} });
      showToast("已为所有已检测 AI 客户端同步 MCP 与 Skill（请新开对话或重启生效）");
      await loadIntegrations();
    } catch (e) { showToast(e.message, true); }
  });

  function showUpdateBanner(latestVer, notes) {
    if (!latestVer) return;
    $("#updateBannerTitle").textContent = `发现新版本 v${latestVer}`;
    $("#updateBannerNotes").textContent = notes || "包含最新功能优化与问题修复，点击即可安全校验升级并自动重启。";
    $("#updateBanner").hidden = false;
  }

  async function waitForDaemonReconnect(prevPid, reloadPage) {
    const deadline = Date.now() + 25000;
    await new Promise((r) => setTimeout(r, 800));
    while (Date.now() < deadline) {
      try {
        const resp = await fetch("/api/v1/health", { cache: "no-store" });
        if (resp.ok) {
          const data = await resp.json();
          if (data.ok && (!prevPid || data.pid !== prevPid)) {
            if (reloadPage) {
              showToast(`已重启至 v${data.version}，正在刷新页面…`);
              setTimeout(() => window.location.reload(), 500);
            }
            return true;
          }
        }
      } catch (_) {}
      await new Promise((r) => setTimeout(r, 600));
    }
    return false;
  }

  async function performUpdateAndRestart() {
    const prevPid = state.daemonPid;
    try {
      showToast("正在校验 SHA256 并安全更新…");
      const res = await apiFetch("/api/v1/update/apply", { method: "POST", body: { auto_restart: true } });
      showToast(res.message || "更新成功，正在自动重启…");
      if (res.restarting) {
        const ok = await waitForDaemonReconnect(prevPid, true);
        if (!ok) showToast("更新已写入磁盘，请手动刷新页面或重新运行 vmr ui");
      }
    } catch (e) { showToast(e.message, true); }
  }

  $("#bannerDismissUpdateBtn").addEventListener("click", () => {
    state.updateBannerDismissed = true;
    $("#updateBanner").hidden = true;
  });
  $("#bannerApplyUpdateBtn").addEventListener("click", performUpdateAndRestart);

  $("#checkUpdateBtn").addEventListener("click", async () => {
    try {
      const info = await apiFetch("/api/v1/update/check");
      const box = $("#updateStatus");
      if (!info.ok) {
        box.textContent = `检查更新失败：${info.error || "网络不可达"}`;
        $("#applyUpdateBtn").hidden = true;
        return;
      }
      if (info.has_update) {
        const notesHtml = info.release_notes ? ` · ${escapeHtml(info.release_notes)}` : "";
        box.innerHTML = `发现新版本 <strong>v${escapeHtml(info.latest_version)}</strong>（当前 v${escapeHtml(info.current_version)}）${notesHtml}`;
        $("#applyUpdateBtn").hidden = false;
        state.updateBannerDismissed = false;
        showUpdateBanner(info.latest_version, info.release_notes || "");
      } else {
        box.innerHTML = `当前已是最新版本 <strong>v${escapeHtml(info.current_version)}</strong>。`;
        $("#applyUpdateBtn").hidden = true;
        $("#updateBanner").hidden = true;
      }
    } catch (e) { showToast(e.message, true); }
  });

  $("#applyUpdateBtn").addEventListener("click", performUpdateAndRestart);

  // SSE 实时进度与静默更新订阅
  function connectEvents() {
    if (!TOKEN) return;
    const es = new EventSource(`/api/v1/events?token=${encodeURIComponent(TOKEN)}`);
    es.addEventListener("job", () => loadJobs().catch(() => {}));
    es.addEventListener("progress", () => loadJobs().catch(() => {}));
    es.addEventListener("update", (ev) => {
      try {
        const info = JSON.parse(ev.data || "{}");
        if (info.has_update && !state.updateBannerDismissed) {
          showUpdateBanner(info.latest_version, info.release_notes || "");
        }
      } catch (_) {}
    });
    es.onerror = () => {
      es.close();
      setTimeout(connectEvents, 4000);
    };
  }

  if (TOKEN) {
    Promise.all([loadStatus(), loadConfig(), loadTemplates(), loadJobs()])
      .then(() => connectEvents())
      .catch((e) => showToast(e.message, true));
  } else {
    loadStatus();
  }
})();
