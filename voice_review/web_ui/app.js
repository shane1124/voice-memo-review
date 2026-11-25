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
    drawerMode: "job", // "job" | "prompt"
    drawerTab: "analysis",
  };

  const $ = (sel) => document.querySelector(sel);

  function showToast(msg, isErr = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.style.background = isErr ? "#b42318" : "#162624";
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

  function renderMarkdown(md) {
    const lines = String(md || "").replace(/\r\n/g, "\n").split("\n");
    const out = [];
    let inList = false;
    for (const raw of lines) {
      const line = escapeHtml(raw);
      if (/^###\s+/.test(line)) {
        if (inList) { out.push("</ul>"); inList = false; }
        out.push(`<h3>${line.replace(/^###\s+/, "")}</h3>`);
      } else if (/^##\s+/.test(line)) {
        if (inList) { out.push("</ul>"); inList = false; }
        out.push(`<h2>${line.replace(/^##\s+/, "")}</h2>`);
      } else if (/^#\s+/.test(line)) {
        if (inList) { out.push("</ul>"); inList = false; }
        out.push(`<h1>${line.replace(/^#\s+/, "")}</h1>`);
      } else if (/^&gt;\s?/.test(line)) {
        if (inList) { out.push("</ul>"); inList = false; }
        out.push(`<blockquote>${line.replace(/^&gt;\s?/, "")}</blockquote>`);
      } else if (/^[-*]\s+/.test(line)) {
        if (!inList) { out.push("<ul>"); inList = true; }
        out.push(`<li>${line.replace(/^[-*]\s+/, "").replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")}</li>`);
      } else if (line.trim() === "---") {
        if (inList) { out.push("</ul>"); inList = false; }
        out.push("<hr />");
      } else if (line.trim() === "") {
        if (inList) { out.push("</ul>"); inList = false; }
      } else {
        if (inList) { out.push("</ul>"); inList = false; }
        out.push(`<p>${line.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>")}</p>`);
      }
    }
    if (inList) out.push("</ul>");
    return out.join("\n") || '<div class="empty-state">暂无正文内容</div>';
  }

  // 导航切换
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
      btn.classList.add("active");
      const target = `#view-${btn.dataset.view}`;
      $(target).classList.add("active");
      if (btn.dataset.view === "integrations") loadIntegrations();
      if (btn.dataset.view === "rules") { loadTemplates(); loadRuleMeta(); loadVoiceprints(); }
    });
  });

  // 状态加载
  async function loadStatus() {
    try {
      const st = await apiFetch("/api/v1/status");
      $("#appVersion").textContent = `v${st.version}`;
      $("#updateCurrentVer").textContent = `v${st.version}`;
      $("#daemonDot").className = "dot online";
      $("#daemonLabel").textContent = st.watching ? "后台监听运行中" : "单守护已就绪（手动模式）";
      $("#engineMini").textContent = `转写: ${st.transcriber_provider} | 分析: ${st.analyzer_auto_provider}`;
      $("#toggleVoiceMemos").checked = Boolean(st.voice_memos_enabled);
      $("#toggleDropFolder").checked = Boolean(st.drop_folder_enabled);
    } catch (err) {
      $("#daemonDot").className = "dot error";
      $("#daemonLabel").textContent = "守护未连接或令牌失效";
    }
  }

  // 任务列表
  const STATUS_TEXT = {
    discovered: ["待处理", "running"],
    preparing: ["本地声纹切段中", "running"],
    prepared: ["待转写", "running"],
    transcribing: ["转写中", "running"],
    transcribed: ["已转写", "running"],
    analyzing: ["分析生成中", "running"],
    awaiting_analysis: ["待分析（可手动/MCP接手）", "awaiting"],
    done: ["已完成", "done"],
    skipped: ["已跳过（无有效人声）", ""],
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
      box.innerHTML = '<div class="empty-state">暂无处理记录，拖入一条录音试试看。</div>';
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
          <div>
            <div class="job-title">${escapeHtml(j.title)}</div>
            <div class="job-meta">${escapeHtml(j.recorded_at.slice(0, 16).replace("T", " "))} · ${fmtDur(j.duration_sec)} · ${escapeHtml(j.source_kind)}${errNote}</div>
          </div>
          <span class="badge ${cls}">${label}${prog}</span>
          <div class="head-actions">
            ${canRead ? `<button class="btn ghost" data-open-job="${escapeHtml(j.recording_id)}">查看结果</button>` : ""}
            ${j.status === "failed" ? `<button class="btn ghost" data-retry-job="${escapeHtml(j.recording_id)}">重试</button>` : ""}
          </div>
        </div>
      `;
    }).join("");

    box.querySelectorAll("[data-open-job]").forEach((b) => {
      b.addEventListener("click", () => openJobDrawer(b.dataset.openJob));
    });
    box.querySelectorAll("[data-retry-job]").forEach((b) => {
      b.addEventListener("click", async () => {
        await apiFetch(`/api/v1/jobs/${encodeURIComponent(b.dataset.retryJob)}/retry`, { method: "POST", body: {} });
        showToast("已重新加入队列");
        loadJobs();
      });
    });
  }

  // 上传录音
  async function uploadAudioFile(file) {
    const tplId = $("#uploadTemplateSelect").value || "";
    const qs = new URLSearchParams({ filename: file.name, template_id: tplId });
    showToast(`正在上传并入队：${file.name}`);
    const buf = await file.arrayBuffer();
    await apiFetch(`/api/v1/jobs/upload?${qs.toString()}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: buf,
    });
    showToast(`已入队开始处理：${file.name}`);
    loadJobs();
  }

  const dropZone = $("#dropZone");
  const fileInput = $("#fileInput");
  $("#pickFileBtn").addEventListener("click", () => fileInput.click());
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
      showToast("监听源开关已热更新");
      loadStatus();
    } catch (e) {
      showToast(e.message, true);
    }
  }
  $("#toggleVoiceMemos").addEventListener("change", updateSourceToggle);
  $("#toggleDropFolder").addEventListener("change", updateSourceToggle);
  $("#refreshJobsBtn").addEventListener("click", () => loadJobs());

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
      "none": [],
      "openai-compatible-llm": ["base_url", "model", "api_key"],
      "gemini-llm": ["base_url", "model", "api_key"],
    }[an] || [];
    document.querySelectorAll("[data-an-field]").forEach((el) => {
      el.hidden = !anShow.includes(el.dataset.anField);
    });
    const warn = $("#comboWarning");
    if (tr === "antigravity-flash" && (an === "openai-compatible-llm" || an === "gemini-llm")) {
      warn.hidden = false;
      warn.textContent = "组合不兼容：antigravity-flash 依赖 Antigravity 一体化工作流，不能直接搭配外部大模型分析器。请改用 antigravity-agentapi，或切换转写引擎。";
    } else {
      warn.hidden = true;
    }
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
    $("#cfgTrApiKey").value = $("#cfgTranscriber").value === "gemini-api"
      ? (state.secrets.gemini_api_key || "")
      : (state.secrets.openai_asr_api_key || "");

    $("#cfgAnalyzer").value = (c.analyzer && c.analyzer.auto_provider) || "antigravity-agentapi";
    $("#cfgAnBaseUrl").value = (c.analyzer && c.analyzer.base_url) || "";
    $("#cfgAnModel").value = (c.analyzer && c.analyzer.model) || "";
    $("#cfgAnApiKey").value = $("#cfgAnalyzer").value === "gemini-llm"
      ? (state.secrets.gemini_api_key || "")
      : (state.secrets.openai_llm_api_key || "");

    $("#updateRepoLabel").textContent = (c.update && c.update.repo) || "shane1124/voice-memo-review";
    refreshProviderFieldVisibility();
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
      if (val && !val.includes("****")) {
        secretsPatch[key] = val;
      }
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
      // 直接传递原始命令字符串交由后端 shlex.split 安全解析，完整保留含空格引号路径
      if ($("#cfgTrCommand").value.trim()) trObj.command = $("#cfgTrCommand").value.trim();
    }

    const anObj = { auto_provider: an };
    if (an === "openai-compatible-llm" || an === "gemini-llm") {
      if ($("#cfgAnBaseUrl").value.trim()) anObj.base_url = $("#cfgAnBaseUrl").value.trim();
      if ($("#cfgAnModel").value.trim()) anObj.model = $("#cfgAnModel").value.trim();
    }
    return { trObj, anObj, secretsPatch };
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
      showToast("引擎插槽与密钥已安全保存");
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
    } catch (e) {
      showToast(e.message, true);
    }
  });
  $("#testAnalyzerBtn").addEventListener("click", async () => {
    const { anObj, secretsPatch } = buildProvidersPayloadFromForm();
    try {
      const res = await apiFetch("/api/v1/providers/test", {
        method: "POST",
        body: { kind: "analyzer", analyzer: anObj, secrets: secretsPatch },
      });
      showToast(res.message, !res.ok);
    } catch (e) {
      showToast(e.message, true);
    }
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
      box.innerHTML = '<div class="empty-state">尚未录入声纹，上传一段 10～30 秒清晰单人样音即可自动认人。</div>';
      return;
    }
    box.innerHTML = items.map((v) => `
      <div class="vp-item">
        <span><strong>${escapeHtml(v.name)}</strong>（${v.samples} 条样本）</span>
        <button class="btn danger-ghost" data-del-vp="${escapeHtml(v.name)}">删除</button>
      </div>
    `).join("");
    box.querySelectorAll("[data-del-vp]").forEach((b) => {
      b.addEventListener("click", async () => {
        await apiFetch(`/api/v1/voiceprints/${encodeURIComponent(b.dataset.delVp)}`, { method: "DELETE" });
        showToast(`已删除声纹：${b.dataset.delVp}`);
        loadVoiceprints();
      });
    });
  }

  $("#newTemplateBtn").addEventListener("click", () => {
    fillTemplateEditor({ id: "", name: "", description: "", body: "", is_default: false, is_builtin: false });
    $("#tplId").focus();
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
      showToast(`模板「${res.template.name}」已保存`);
      await loadTemplates();
    } catch (e) {
      showToast(e.message, true);
    }
  });

  $("#resetTemplateBtn").addEventListener("click", async () => {
    if (!state.activeTplId) return;
    try {
      await apiFetch(`/api/v1/templates/${encodeURIComponent(state.activeTplId)}/reset`, { method: "POST", body: {} });
      showToast("已恢复内置默认正文");
      await loadTemplates();
    } catch (e) {
      showToast(e.message, true);
    }
  });

  $("#deleteTemplateBtn").addEventListener("click", async () => {
    if (!state.activeTplId) return;
    try {
      await apiFetch(`/api/v1/templates/${encodeURIComponent(state.activeTplId)}`, { method: "DELETE" });
      state.activeTplId = null;
      showToast("模板已删除");
      await loadTemplates();
    } catch (e) {
      showToast(e.message, true);
    }
  });

  $("#saveMetaRulesBtn").addEventListener("click", async () => {
    await apiFetch("/api/v1/rules/meta", {
      method: "PUT",
      body: { glossary: $("#rulesGlossary").value, persona: $("#rulesPersona").value },
    });
    showToast("专名词表与个人背景已保存");
  });

  $("#previewPromptBtn").addEventListener("click", async () => {
    try {
      const res = await apiFetch("/api/v1/templates/preview", {
        method: "POST",
        body: { template_id: state.activeTplId || undefined, title: "示例项目周会" },
      });
      state.drawerMode = "prompt";
      state.drawerData.rawPrompt = res.prompt;
      $("#drawerTitle").textContent = "完整拼装 Prompt 预览（所见即所发）";
      $("#drawerTabs").hidden = true;
      $("#drawerReanalyzeBtn").hidden = true;
      $("#drawerContent").innerHTML = `<pre>${escapeHtml(res.prompt)}</pre>`;
      openDrawerUI();
    } catch (e) {
      showToast(e.message, true);
    }
  });

  $("#vpUploadBtn").addEventListener("click", () => {
    if (!$("#vpNameInput").value.trim()) { showToast("请先填写说话人姓名", true); return; }
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
        body: await file.arrayBuffer(),
      });
      showToast(`声纹录入成功：${res.name}（有效人声 ${res.talk_sec}s）`);
      $("#vpNameInput").value = "";
      loadVoiceprints();
    } catch (e) {
      showToast(e.message, true);
    } finally {
      $("#vpFileInput").value = "";
    }
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

  function renderDrawerTab() {
    document.querySelectorAll("#drawerTabs .tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.tab === state.drawerTab);
    });
    const text = state.drawerTab === "analysis" ? state.drawerData.analysis : state.drawerData.transcript;
    $("#drawerContent").innerHTML = renderMarkdown(text);
  }

  document.querySelectorAll("#drawerTabs .tab").forEach((t) => {
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
      showToast("正在按选定模板重新分析…");
      await apiFetch(`/api/v1/jobs/${encodeURIComponent(state.drawerJobId)}/analyze`, {
        method: "POST",
        body: { template_id: tplId },
      });
      showToast("分析已完成");
      await openJobDrawer(state.drawerJobId);
      loadJobs();
    } catch (e) {
      showToast(e.message, true);
    }
  });

  $("#drawerCopyBtn").addEventListener("click", async () => {
    const text = state.drawerMode === "prompt"
      ? state.drawerData.rawPrompt
      : (state.drawerTab === "analysis" ? state.drawerData.analysis : state.drawerData.transcript);
    await navigator.clipboard.writeText(text || "");
    showToast("已复制到剪贴板");
  });

  // AI 插件桥接与更新
  async function loadIntegrations() {
    const data = await apiFetch("/api/v1/integrations");
    const clients = data.clients || {};
    const grid = $("#clientsGrid");
    const keys = Object.keys(clients);
    if (!keys.length) {
      grid.innerHTML = '<div class="empty-state">暂未扫描到客户端信息</div>';
      return;
    }
    grid.innerHTML = keys.map((k) => {
      const c = clients[k];
      const badge = c.installed
        ? '<span class="badge done">已接入</span>'
        : (c.detected ? '<span class="badge awaiting">已检测到 · 未接入</span>' : '<span class="badge">未检测到客户端</span>');
      return `
        <div class="client-card">
          <div class="client-top">
            <strong>${escapeHtml(c.display_name || k)}</strong>
            ${badge}
          </div>
          <div class="client-path">${escapeHtml(c.config_path || "")}</div>
          <div class="head-actions">
            <button class="btn primary" data-inst="${escapeHtml(k)}">${c.installed ? "重新同步" : "一键接入"}</button>
            ${c.installed ? `<button class="btn danger-ghost" data-uninst="${escapeHtml(k)}">卸载</button>` : ""}
          </div>
        </div>
      `;
    }).join("");

    grid.querySelectorAll("[data-inst]").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          await apiFetch("/api/v1/integrations/install", { method: "POST", body: { targets: [b.dataset.inst] } });
          showToast(`已接入 ${b.dataset.inst}`);
          loadIntegrations();
        } catch (e) { showToast(e.message, true); }
      });
    });
    grid.querySelectorAll("[data-uninst]").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          await apiFetch("/api/v1/integrations/uninstall", { method: "POST", body: { targets: [b.dataset.uninst] } });
          showToast(`已卸载 ${b.dataset.uninst}`);
          loadIntegrations();
        } catch (e) { showToast(e.message, true); }
      });
    });
  }

  $("#refreshIntegrationsBtn").addEventListener("click", () => loadIntegrations());
  $("#installAllClientsBtn").addEventListener("click", async () => {
    try {
      await apiFetch("/api/v1/integrations/install", { method: "POST", body: {} });
      showToast("已为所有已检测 AI 客户端写入 MCP 与 Skill");
      loadIntegrations();
    } catch (e) { showToast(e.message, true); }
  });

  $("#checkUpdateBtn").addEventListener("click", async () => {
    const info = await apiFetch("/api/v1/update/check");
    const box = $("#updateStatus");
    if (!info.ok) {
      box.textContent = `检查更新失败：${info.error || "网络不可达"}`;
      $("#applyUpdateBtn").hidden = true;
      return;
    }
    if (info.has_update) {
      box.innerHTML = `发现新版本 <strong>v${escapeHtml(info.latest_version)}</strong>（当前 v${escapeHtml(info.current_version)}）。点击右侧「立即更新」即可安全升级，不会覆盖你的模板与密钥。`;
      $("#applyUpdateBtn").hidden = false;
    } else {
      box.innerHTML = `当前已是最新版本 <strong>v${escapeHtml(info.current_version)}</strong>。`;
      $("#applyUpdateBtn").hidden = true;
    }
  });

  $("#applyUpdateBtn").addEventListener("click", async () => {
    try {
      showToast("正在校验 SHA256 并更新程序目录…");
      const res = await apiFetch("/api/v1/update/apply", { method: "POST", body: {} });
      showToast(res.message || "更新成功，请重启控制台");
    } catch (e) { showToast(e.message, true); }
  });

  // SSE 实时进度订阅
  function connectEvents() {
    if (!TOKEN) return;
    const es = new EventSource(`/api/v1/events?token=${encodeURIComponent(TOKEN)}`);
    es.addEventListener("job", () => loadJobs());
    es.addEventListener("progress", () => loadJobs());
    es.onerror = () => {
      es.close();
      setTimeout(connectEvents, 4000);
    };
  }

  // 初始化
  Promise.all([loadStatus(), loadConfig(), loadTemplates(), loadJobs()])
    .then(() => connectEvents())
    .catch((e) => showToast(e.message, true));
})();
