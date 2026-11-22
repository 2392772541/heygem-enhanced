const EMO_LABELS = ["喜", "怒", "哀", "惧", "厌恶", "低落", "惊喜", "平静"];

const state = {
  view: "create",
  createMode: "tts",
  emoMode: "same_as_speaker",
  taskFilter: "all",
  libraryType: "videos",
  selectedCategoryId: "all",
  modelSubTab: "models",
  logSource: "queue",
  selectedModelId: null,
  selectedAudioIds: new Set(),
  lastAuditionAsset: null,
  health: null,
  models: [],
  modelGroups: [],
  voices: [],
  categories: [],
  presets: [],
  jobs: [],
  audios: [],
  ttsPollingTimer: null,
  openTaskErrIds: new Set(),
  openTaskLogIds: new Set(),
  lastHealthFetchAt: 0,
  lastSuccessCount: -1,
  pollInFlight: false,
  backendDisconnected: false,
  disconnectCount: 0,
  lastDisconnectReason: "",
};

// ==================== Helpers ====================

function hasVal(v) {
  return v !== null && v !== undefined;
}

function cleanLocalPath(raw) {
  return String(raw ?? "").trim().replace(/^["']+|["']+$/g, "").trim();
}

function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function toast(msg, type = "info", duration = 3800) {
  const wrap = document.getElementById("toastContainer");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

async function api(path, options = {}) {
  const { timeoutMs, ...fetchOptions } = options;
  let controller = null;
  let timerId = null;
  if (timeoutMs && timeoutMs > 0 && !fetchOptions.signal) {
    controller = new AbortController();
    fetchOptions.signal = controller.signal;
    timerId = setTimeout(() => controller.abort(), timeoutMs);
  }
  try {
    const resp = await fetch(path, fetchOptions);
    const text = await resp.text();
    let data = null;
    try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
    if (!resp.ok) {
      const err = new Error(data?.detail || data?.msg || `HTTP ${resp.status}`);
      err.httpStatus = resp.status;
      err.isNetworkError = resp.status === 502 || resp.status === 503 || resp.status === 504;
      throw err;
    }
    return data;
  } catch (err) {
    if (err?.name === "AbortError") {
      const timeoutErr = new Error("请求超时，Studio 后台服务未响应");
      timeoutErr.httpStatus = 0;
      timeoutErr.isNetworkError = true;
      throw timeoutErr;
    }
    if (!hasVal(err?.httpStatus)) {
      err.httpStatus = 0;
      err.isNetworkError = true;
    }
    throw err;
  } finally {
    if (timerId !== null) clearTimeout(timerId);
  }
}

function fmtTime(ms) {
  if (!ms) return "-";
  const d = new Date(Number(ms));
  if (isNaN(d.getTime())) return "-";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtDuration(sec) {
  if (sec === null || sec === undefined || isNaN(sec) || sec <= 0) return "-";
  const s = Math.round(Number(sec));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return m > 0 ? `${m}分${r}秒` : `${r}秒`;
}

function fmtEta(sec) {
  if (sec === null || sec === undefined || sec < 0) return "估算中";
  if (sec === 0) return "即将完成";
  return fmtDuration(sec);
}

function copyText(text) {
  if (!text) return;
  navigator.clipboard.writeText(text).then(
    () => toast("已复制到剪贴板", "success", 2000),
    () => toast("复制失败，请手动选中复制", "warning")
  );
}

function openModal(id) {
  document.getElementById(id).classList.remove("hidden");
}

function closeModal(id) {
  const el = document.getElementById(id);
  el.classList.add("hidden");
  if (id === "previewModal") {
    const vid = document.getElementById("previewModalVideo");
    vid.pause();
    vid.removeAttribute("src");
  }
}

// ==================== Navigation ====================

function switchView(viewName) {
  state.view = viewName;
  document.querySelectorAll(".nav-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === viewName);
  });
  document.querySelectorAll(".view").forEach((v) => {
    v.classList.toggle("active", v.id === `view-${viewName}`);
  });
  if (viewName === "tasks") renderTasksView();
  if (viewName === "library") renderLibraryView();
  if (viewName === "models") renderModelsManageView();
  if (viewName === "api") renderApiView();
}

// ==================== Init & Polling ====================

function updateEmoSumHint() {
  const hint = document.getElementById("emoSumHint");
  if (!hint) return;
  if (state.emoMode !== "emo_vector") {
    hint.classList.add("hidden");
    return;
  }
  const sum = EMO_LABELS.reduce((acc, _, idx) => acc + Number(document.getElementById(`emoSlider${idx}`)?.value || 0), 0);
  hint.classList.remove("hidden");
  if (sum > 0.8) {
    hint.innerHTML = `<span style="color:#fcd34d;">当前 8 维情感总权重 ${sum.toFixed(2)}（超过 0.80 阈值，合成时将自动柔和归一化防止失真）</span>`;
  } else {
    hint.textContent = `当前 8 维情感总权重：${sum.toFixed(2)} / 0.80`;
  }
}

function initEmoSliders() {
  const box = document.getElementById("emoVectorBox");
  box.innerHTML = EMO_LABELS.map(
    (label, idx) => `
      <div class="emo-slider">
        <span><strong>${label}</strong><em id="emoVal${idx}">0.00</em></span>
        <input type="range" id="emoSlider${idx}" min="0" max="1" step="0.05" value="0"
               oninput="document.getElementById('emoVal${idx}').textContent=Number(this.value).toFixed(2);updateEmoSumHint()" />
      </div>`
  ).join("");
  updateEmoSumHint();
}

function initDropzone() {
  const dz = document.getElementById("audioDropzone");
  if (!dz) return;
  ["dragenter", "dragover"].forEach((evt) =>
    dz.addEventListener(evt, (e) => {
      e.preventDefault();
      dz.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach((evt) =>
    dz.addEventListener(evt, (e) => {
      e.preventDefault();
      dz.classList.remove("dragover");
    })
  );
  dz.addEventListener("drop", (e) => {
    const files = e.dataTransfer?.files;
    if (files && files.length) handleUploadBatchAudios(files);
  });
}

function markBackendDisconnected(err) {
  const wasDisconnected = state.backendDisconnected;
  const isNetwork = err?.isNetworkError !== false;
  const httpStatus = Number(err?.httpStatus || 0);
  state.backendDisconnected = true;
  state.disconnectCount += 1;
  state.lastDisconnectReason = err?.message || "无法连接 Studio 后台服务";

  const strip = document.getElementById("alertStrip");
  strip.className = "alert-strip";
  const hostLabel = esc(window.location.host || window.location.origin);

  if (isNetwork) {
    document.getElementById("dotDocker").className = "dot err";
    document.getElementById("txtDocker").textContent = "渲染引擎 失联";
    document.getElementById("dotTts").className = "dot err";
    document.getElementById("txtTts").textContent = "IndexTTS2 失联";
    document.getElementById("dotQueue").className = "dot err";
    document.getElementById("txtQueue").textContent = "队列 失联";
    document.getElementById("pillGpu").textContent = "🎮 后台失联";
    document.getElementById("pillDisk").textContent = "💾 等待重连";
    document.getElementById("alertList").innerHTML = `
      <div class="alert-item error">
        🔴 与 HeyGem Studio 后台服务 (${hostLabel}) 连接中断：${esc(state.lastDisconnectReason)}。后台可能已停止或正在重启，页面正自动尝试重连（也可双击 Start-Studio.vbs 重新启动）。
      </div>`;
  } else {
    document.getElementById("alertList").innerHTML = `
      <div class="alert-item error">
        🔴 HeyGem Studio 后台接口返回异常 (HTTP ${httpStatus})：${esc(state.lastDisconnectReason)}。请检查后台日志或点击右侧立即重试。
      </div>`;
  }

  document.getElementById("btnAlertReconnect")?.classList.remove("hidden");
  document.getElementById("btnAlertRecover")?.classList.add("hidden");
  document.getElementById("btnAlertModels")?.classList.add("hidden");

  if (!wasDisconnected) {
    toast(
      isNetwork
        ? "与 Studio 后台服务连接中断，已显示告警并正在自动重连..."
        : `Studio 后台接口异常 (HTTP ${httpStatus})：${state.lastDisconnectReason}`,
      "error",
      5000
    );
  }
}

function clearBackendDisconnected() {
  if (!state.backendDisconnected) return false;
  state.backendDisconnected = false;
  state.disconnectCount = 0;
  state.lastDisconnectReason = "";
  toast("已重新连接 HeyGem Studio 后台服务，状态已恢复同步", "success", 3500);
  return true;
}

async function refreshAll(silent = false, forceHealth = false) {
  try {
    const [health, modelsRes, groupsRes, voicesRes, catsRes, presetsRes, jobsRes, audiosRes] = await Promise.all([
      api(`/api/v1/health${forceHealth ? "?force=true" : ""}`, { timeoutMs: 15000 }),
      api("/api/v1/models", { timeoutMs: 15000 }),
      api("/api/v1/model-groups", { timeoutMs: 15000 }),
      api("/api/v1/voices", { timeoutMs: 15000 }),
      api("/api/v1/categories", { timeoutMs: 15000 }),
      api("/api/v1/tts/presets", { timeoutMs: 15000 }),
      api("/api/v1/jobs?limit=200", { timeoutMs: 15000 }),
      api("/api/v1/audios", { timeoutMs: 15000 }),
    ]);

    clearBackendDisconnected();
    state.health = health;
    state.lastHealthFetchAt = Date.now();
    state.models = modelsRes.items || [];
    state.modelGroups = groupsRes.items || [];
    state.voices = voicesRes.items || [];
    state.categories = catsRes.items || [];
    state.presets = presetsRes.items || [];
    state.jobs = jobsRes.items || [];
    state.audios = audiosRes.items || [];
    state.lastSuccessCount = state.jobs.filter((j) => j.status === "success").length;

    // 清理已被删除的音频勾选残留，防止批量入队时引用失效 audio_asset_id
    const validAudioIds = new Set(state.audios.map((a) => a.id));
    for (const sid of Array.from(state.selectedAudioIds)) {
      if (!validAudioIds.has(sid)) state.selectedAudioIds.delete(sid);
    }

    if (!state.models.some((m) => m.id === state.selectedModelId)) {
      state.selectedModelId = null;
    }
    if (!state.selectedModelId && state.models.length > 0) {
      const valid = state.models.find((m) => m.video_exists) || state.models[0];
      state.selectedModelId = valid.id;
    }

    renderHealthBar();
    renderSelectOptions();
    renderCreateModelList();
    updateVoicePreview();
    renderCreateAudioLibrary();
    renderLiveMonitor();
    if (state.view === "tasks") renderTasksView();
    if (state.view === "library") renderLibraryView();
    if (state.view === "models") renderModelsManageView();
    if (state.view === "api") renderApiView();
  } catch (err) {
    markBackendDisconnected(err);
    if (!silent) toast(`刷新数据失败: ${err.message}`, "error");
  }
}

async function pollLiveTick(forceHealth = false) {
  if (state.pollInFlight || document.hidden) return;
  state.pollInFlight = true;
  try {
    const now = Date.now();
    const wasDisconnected = state.backendDisconnected;
    const needHealth = forceHealth || wasDisconnected || (now - state.lastHealthFetchAt >= 10000);
    const jobsPromise = api("/api/v1/jobs?limit=200", { timeoutMs: 12000 });
    const healthPromise = needHealth
      ? api(`/api/v1/health${forceHealth || wasDisconnected ? "?force=true" : ""}`, { timeoutMs: 12000 })
      : Promise.resolve(null);
    const [jobsRes, health] = await Promise.all([jobsPromise, healthPromise]);

    const recovered = clearBackendDisconnected();
    state.jobs = jobsRes.items || [];
    if (health) {
      state.health = health;
      state.lastHealthFetchAt = Date.now();
      renderHealthBar();
    } else if (state.health) {
      // 仅用最新 jobs 刷新侧栏计数器，减少健康探测频率
      const activeCnt = state.jobs.filter((j) => j.status === "pending" || j.status === "waiting").length;
      const successCnt = state.jobs.filter((j) => j.status === "success").length;
      document.getElementById("navActiveCount").textContent = activeCnt;
      document.getElementById("navSuccessCount").textContent = successCnt;
    }

    renderLiveMonitor();
    if (state.view === "tasks") renderTasksView();
    const newSuccessCount = state.jobs.filter((j) => j.status === "success").length;
    if (newSuccessCount !== state.lastSuccessCount) {
      state.lastSuccessCount = newSuccessCount;
      if (state.view === "library") renderLibraryView();
    }
    if (recovered) {
      refreshAll(true, false);
    }
  } catch (err) {
    markBackendDisconnected(err);
  } finally {
    state.pollInFlight = false;
  }
}

function scheduleNextPoll() {
  const hasActive = state.jobs.some((j) => j.status === "pending" || j.status === "waiting");
  const delay = hasActive ? 3000 : 6000;
  setTimeout(async () => {
    await pollLiveTick(false);
    scheduleNextPoll();
  }, delay);
}

// ==================== Health Bar & System Actions ====================

function renderHealthBar() {
  const h = state.health;
  if (!h) return;

  const dockerOk = h.docker?.f2f_api_ok;
  document.getElementById("dotDocker").className = `dot ${dockerOk ? "ok" : "err"}`;
  document.getElementById("txtDocker").textContent = dockerOk ? "渲染引擎 在线" : "渲染引擎 未启动";

  const ttsOk = h.tts?.ok;
  const ttsState = h.tts?.live_status?.state;
  document.getElementById("dotTts").className = `dot ${ttsOk ? (ttsState && ttsState !== "idle" ? "warn" : "ok") : "err"}`;
  document.getElementById("txtTts").textContent = ttsOk
    ? (ttsState && ttsState !== "idle" ? `IndexTTS2 合成中` : `IndexTTS2 就绪`)
    : `IndexTTS2 未启动`;

  const qRun = h.queue?.running;
  const qPause = h.queue?.paused;
  document.getElementById("dotQueue").className = `dot ${qRun ? (qPause ? "warn" : "ok") : "err"}`;
  document.getElementById("txtQueue").textContent = qRun ? (qPause ? "队列 已暂停" : "队列 运行中") : "队列 未启动";

  const btnPause = document.getElementById("btnPauseResume");
  btnPause.textContent = qPause ? "▶ 恢复队列" : "⏸ 暂停队列";
  btnPause.className = `btn btn-sm ${qPause ? "btn-success" : ""}`;

  const g = h.gpu;
  document.getElementById("pillGpu").textContent = g?.available
    ? `🎮 ${g.name.replace("NVIDIA GeForce ", "")} · ${(g.memory_used_mb / 1024).toFixed(1)}/${(g.memory_total_mb / 1024).toFixed(0)}GB · ${g.temperature_c}°C`
    : "🎮 GPU 未检测到";

  const disk = (h.disks || [])[0];
  document.getElementById("pillDisk").textContent = disk
    ? `💾 ${disk.drive} 剩 ${disk.free_gb}GB`
    : "💾 磁盘未知";

  // Sidebar counters
  const activeCnt = (h.job_counts?.pending || 0) + (h.job_counts?.waiting || 0);
  const successCnt = h.job_counts?.success || 0;
  document.getElementById("navActiveCount").textContent = activeCnt;
  document.getElementById("navSuccessCount").textContent = successCnt;
  document.getElementById("navModelCount").textContent = state.models.length;

  // Alert Strip
  document.getElementById("btnAlertReconnect")?.classList.add("hidden");
  document.getElementById("btnAlertRecover")?.classList.remove("hidden");
  document.getElementById("btnAlertModels")?.classList.remove("hidden");

  const alerts = h.alerts || [];
  const strip = document.getElementById("alertStrip");
  if (alerts.length === 0) {
    strip.classList.add("hidden");
  } else {
    const hasErr = alerts.some((a) => a.severity === "error");
    strip.className = `alert-strip ${hasErr ? "" : "warning"}`;
    document.getElementById("alertList").innerHTML = alerts
      .map((a) => `<div class="alert-item ${esc(a.severity)}">${a.severity === "error" ? "🔴" : a.severity === "warning" ? "🟡" : "🔵"} ${esc(a.message)}</div>`)
      .join("");
  }
}

async function handleStartAll() {
  const btn = document.getElementById("btnStartAll");
  btn.disabled = true;
  btn.textContent = "启动中...";
  try {
    const res = await api("/api/v1/system/start", { method: "POST" });
    showRecoverReport(res);
    await refreshAll(true);
  } catch (err) {
    toast(`启动服务失败: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "▶ 一键启动服务";
  }
}

async function handleRecover(forceRestartContainer = false) {
  const btn = document.getElementById("btnRecover");
  btn.disabled = true;
  btn.textContent = "自愈中...";
  try {
    const res = await api("/api/v1/system/recover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ restart_render_container: forceRestartContainer, requeue_stuck: true }),
    });
    showRecoverReport(res);
    await refreshAll(true);
  } catch (err) {
    toast(`自愈失败: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "🩺 自愈恢复";
  }
}

function showRecoverReport(res) {
  const body = document.getElementById("recoverReportBody");
  const steps = res.steps || [];
  body.innerHTML = steps
    .map(
      (s) => `
      <div class="job-card mb-8" style="padding:10px 12px;border-color:${s.ok ? "rgba(16,185,129,0.4)" : "rgba(239,68,68,0.5)"}">
        <div class="flex-between">
          <strong>${s.ok ? "✅" : "❌"} ${esc(s.step)}</strong>
          <span class="badge ${s.ok ? "badge-success" : "badge-danger"}">${s.ok ? "正常" : "异常"}</span>
        </div>
        <div class="section-sub mt-8">${esc(s.detail)}</div>
      </div>`
    )
    .join("");
  openModal("recoverModal");
}

async function handleTogglePause() {
  const paused = Boolean(state.health?.queue?.paused);
  try {
    const res = await api(paused ? "/api/v1/queue/resume" : "/api/v1/queue/pause", { method: "POST" });
    toast(res.message, "success");
    await pollLiveTick(true);
  } catch (err) {
    toast(`操作失败: ${err.message}`, "error");
  }
}

async function handleStopServices() {
  if (!confirm("确认停止后台渲染容器、IndexTTS2 与队列调度器吗？（不会关闭当前网页，随时可一键重新启动）")) return;
  try {
    await api("/api/v1/system/stop", { method: "POST" });
    toast("后台服务已优雅停止", "success");
    await refreshAll(true, true);
  } catch (err) {
    toast(`停止失败: ${err.message}`, "error");
  }
}

function openLogsModal() {
  openModal("logsModal");
  loadLogSource(state.logSource);
}

async function loadLogSource(src) {
  state.logSource = src;
  document.querySelectorAll("#logSourceTabs button").forEach((b) => b.classList.toggle("active", b.dataset.src === src));
  const box = document.getElementById("logsConsole");
  box.textContent = "加载日志中...";
  try {
    const res = await api(`/api/v1/logs?source=${encodeURIComponent(src)}&tail=180`);
    box.textContent = (res.lines || []).join("\n") || "(暂无日志输出)";
    box.scrollTop = box.scrollHeight;
  } catch (err) {
    box.textContent = `获取日志失败: ${err.message}`;
  }
}

// ==================== Create View ====================

function renderSelectOptions() {
  // Model groups filter
  const gf = document.getElementById("createModelGroupFilter");
  const curG = gf.value;
  gf.innerHTML = `<option value="all">全部分组 (${state.models.length})</option><option value="ungrouped">未分组</option>` +
    state.modelGroups.map((g) => `<option value="${g.id}">${esc(g.name)} (${g.model_count})</option>`).join("");
  if (curG && (curG === "all" || curG === "ungrouped" || state.modelGroups.some((g) => String(g.id) === String(curG)))) {
    gf.value = curG;
  } else {
    gf.value = "all";
  }

  // Categories
  const catSel = document.getElementById("jobCategorySelect");
  const curCat = catSel.value;
  catSel.innerHTML = `<option value="">未分类（保存在根目录）</option>` +
    state.categories.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  if (curCat && state.categories.some((c) => String(c.id) === String(curCat))) {
    catSel.value = curCat;
  } else {
    catSel.value = "";
  }

  // Voices
  const vSel = document.getElementById("ttsVoiceSelect");
  const curV = vSel.value;
  vSel.innerHTML = `<option value="">自动使用当前所选数字人的原声</option>` +
    state.voices.map((v) => `<option value="${v.id}">[${v.id}] ${esc(v.display_name)} ${v.exists ? "" : "(缺失)"}</option>`).join("");
  if (curV && state.voices.some((v) => String(v.id) === String(curV))) {
    vSel.value = curV;
  } else {
    vSel.value = "";
  }

  // Presets
  const pSel = document.getElementById("ttsPresetSelect");
  const curP = pSel.value;
  pSel.innerHTML = `<option value="">-- 选择参数预设 --</option>` +
    state.presets.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  if (curP && state.presets.some((p) => String(p.id) === String(curP))) {
    pSel.value = curP;
  } else {
    pSel.value = "";
  }

  // Model modal group select
  const mg = document.getElementById("modelModalGroup");
  mg.innerHTML = `<option value="">不分组</option>` +
    state.modelGroups.map((g) => `<option value="${g.id}">${esc(g.name)}</option>`).join("");
}

function renderCreateModelList() {
  const list = document.getElementById("createModelList");
  const gFilter = document.getElementById("createModelGroupFilter").value;
  const kw = (document.getElementById("createModelSearch").value || "").trim().toLowerCase();

  const filtered = state.models.filter((m) => {
    if (gFilter === "ungrouped" && m.group_id !== null) return false;
    if (gFilter !== "all" && gFilter !== "ungrouped" && String(m.group_id) !== gFilter) return false;
    if (kw && !String(m.name || "").toLowerCase().includes(kw)) return false;
    return true;
  });

  if (!filtered.length) {
    list.innerHTML = `<div class="section-sub" style="padding:20px;text-align:center;">暂无符合条件的数字人模特，请点击上方「+ 导入模特」</div>`;
    return;
  }

  list.innerHTML = filtered
    .map((m) => {
      const sel = m.id === state.selectedModelId;
      return `
        <div class="model-item ${sel ? "selected" : ""} ${m.video_exists ? "" : "missing"}" onclick="selectModel(${m.id})">
          <div style="min-width:0;">
            <div style="font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(m.name)}</div>
            <div class="section-sub" style="font-size:11.5px;">
              ${m.group_name ? `<span class="badge badge-info" style="font-size:10.5px;">${esc(m.group_name)}</span>` : ""}
              ${m.video_exists ? `<span style="color:#6ee7b7;">● 底模就绪 (${m.video_size_mb}MB)</span>` : `<span style="color:#fca5a5;">⚠ 底模视频缺失</span>`}
            </div>
          </div>
          <div class="flex-row" onclick="event.stopPropagation()">
            ${m.video_exists
              ? `<button class="btn btn-sm" title="预览底模" onclick="previewModelVideo(${m.id})">▶</button>`
              : `<button class="btn btn-sm btn-warning" onclick="openRelinkModelModalById(${m.id})">关联</button>`}
          </div>
        </div>`;
    })
    .join("");
}

function selectModel(id) {
  state.selectedModelId = id;
  renderCreateModelList();
  updateVoicePreview();
}

function updateVoicePreview() {
  const player = document.getElementById("voiceRefPreview");
  const vid = document.getElementById("ttsVoiceSelect").value;
  let previewUrl = "";
  if (vid) {
    const v = state.voices.find((x) => String(x.id) === String(vid));
    previewUrl = v?.preview_url || "";
  } else if (state.selectedModelId) {
    const m = state.models.find((x) => x.id === state.selectedModelId);
    previewUrl = m?.voice_preview_url || "";
  }
  if (previewUrl) {
    player.src = previewUrl;
    player.classList.remove("hidden");
  } else {
    player.classList.add("hidden");
  }
}

function setCreateMode(mode) {
  state.createMode = mode;
  document.querySelectorAll("#createModeTabs button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  document.getElementById("createModeTts").classList.toggle("hidden", mode !== "tts");
  document.getElementById("createModeAudio").classList.toggle("hidden", mode !== "audio");
}

function setEmoMode(mode) {
  state.emoMode = mode;
  document.querySelectorAll("#emoModeTabs button").forEach((b) => b.classList.toggle("active", b.dataset.emo === mode));
  document.getElementById("emoAlphaWrap").classList.toggle("hidden", mode === "same_as_speaker");
  document.getElementById("emoVectorBox").classList.toggle("hidden", mode !== "emo_vector");
  document.getElementById("emoAudioBox").classList.toggle("hidden", mode !== "emo_audio");
  document.getElementById("emoTextBox").classList.toggle("hidden", mode !== "emo_text");
  updateEmoSumHint();
}

function onTtsModelVersionChange() {
  const v = document.getElementById("ttsModelVersion").value;
  document.getElementById("ttsLangField").style.opacity = v === "IndexTTS-2.5" ? "1" : "0.5";
}

function syncRangeVal(inputId, spanId, suffix) {
  const v = Number(document.getElementById(inputId).value);
  const text = Number.isInteger(v) && suffix !== "x" ? String(v) : v.toFixed(suffix === "x" || inputId.includes("Temp") || inputId.includes("TopP") || inputId.includes("Alpha") ? 2 : 1);
  document.getElementById(spanId).textContent = `${text}${suffix}`;
}

function updateTextStats() {
  const txt = (document.getElementById("ttsTextInput").value || "").trim();
  const estSec = Math.round(txt.length / 4.2);
  document.getElementById("textStats").textContent = `${txt.length} 字 · 预计约 ${fmtDuration(estSec)}`;
}

function collectTtsParams() {
  const vec = EMO_LABELS.map((_, i) => Number(document.getElementById(`emoSlider${i}`).value || 0));
  return {
    model_version: document.getElementById("ttsModelVersion").value,
    lang: document.getElementById("ttsLang").value,
    speed: Number(document.getElementById("ttsSpeed").value),
    emo_mode: state.emoMode,
    emo_alpha: Number(document.getElementById("ttsEmoAlpha").value),
    emo_vector: vec,
    emo_audio_prompt: cleanLocalPath(document.getElementById("ttsEmoAudioPath").value),
    emo_text: document.getElementById("ttsEmoText").value.trim(),
    diffusion_steps: Number(document.getElementById("ttsSteps").value),
    num_beams: Number(document.getElementById("ttsBeams").value),
    interval_silence: Number(document.getElementById("ttsSilence").value),
    temperature: Number(document.getElementById("ttsTemp").value),
    top_p: Number(document.getElementById("ttsTopP").value),
    top_k: Number(document.getElementById("ttsTopK").value),
    repetition_penalty: Number(document.getElementById("ttsRep").value),
    chunk_size: Number(document.getElementById("ttsChunk").value),
    auto_fast_on_long_text: document.getElementById("ttsAutoFast").checked,
  };
}

function applyTtsParamsToForm(p) {
  if (!p) return;
  if (p.model_version) document.getElementById("ttsModelVersion").value = p.model_version;
  if (p.lang) document.getElementById("ttsLang").value = p.lang;
  if (hasVal(p.speed)) { document.getElementById("ttsSpeed").value = p.speed; syncRangeVal("ttsSpeed", "valSpeed", "x"); }
  if (p.emo_mode) setEmoMode(p.emo_mode);
  if (hasVal(p.emo_alpha)) { document.getElementById("ttsEmoAlpha").value = p.emo_alpha; syncRangeVal("ttsEmoAlpha", "valEmoAlpha", ""); }
  if (Array.isArray(p.emo_vector) && p.emo_vector.length === 8) {
    p.emo_vector.forEach((val, i) => {
      document.getElementById(`emoSlider${i}`).value = val;
      document.getElementById(`emoVal${i}`).textContent = Number(val).toFixed(2);
    });
  }
  if (hasVal(p.emo_audio_prompt)) document.getElementById("ttsEmoAudioPath").value = p.emo_audio_prompt;
  if (hasVal(p.emo_text)) document.getElementById("ttsEmoText").value = p.emo_text;
  if (hasVal(p.diffusion_steps)) { document.getElementById("ttsSteps").value = p.diffusion_steps; syncRangeVal("ttsSteps", "valSteps", ""); }
  if (hasVal(p.num_beams)) { document.getElementById("ttsBeams").value = p.num_beams; syncRangeVal("ttsBeams", "valBeams", ""); }
  if (hasVal(p.interval_silence)) { document.getElementById("ttsSilence").value = p.interval_silence; syncRangeVal("ttsSilence", "valSilence", "ms"); }
  if (hasVal(p.temperature)) { document.getElementById("ttsTemp").value = p.temperature; syncRangeVal("ttsTemp", "valTemp", ""); }
  if (hasVal(p.top_p)) { document.getElementById("ttsTopP").value = p.top_p; syncRangeVal("ttsTopP", "valTopP", ""); }
  if (hasVal(p.top_k)) { document.getElementById("ttsTopK").value = p.top_k; syncRangeVal("ttsTopK", "valTopK", ""); }
  if (hasVal(p.repetition_penalty)) { document.getElementById("ttsRep").value = p.repetition_penalty; syncRangeVal("ttsRep", "valRep", ""); }
  if (hasVal(p.chunk_size)) { document.getElementById("ttsChunk").value = p.chunk_size; syncRangeVal("ttsChunk", "valChunk", ""); }
  if (hasVal(p.auto_fast_on_long_text)) document.getElementById("ttsAutoFast").checked = Boolean(p.auto_fast_on_long_text);
  onTtsModelVersionChange();
  updateEmoSumHint();
}

function applySelectedPreset() {
  const pid = document.getElementById("ttsPresetSelect").value;
  const preset = state.presets.find((x) => String(x.id) === String(pid));
  if (preset) {
    applyTtsParamsToForm(preset.params);
    toast(`已应用参数预设：${preset.name}`, "success", 2200);
  }
}

async function saveCurrentAsPreset() {
  const name = prompt("给这套 IndexTTS2 参数预设起个名字：", "我的专属口播参数");
  if (!name || !name.trim()) return;
  try {
    const res = await api("/api/v1/tts/presets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), params: collectTtsParams() }),
    });
    toast(`已保存预设「${name.trim()}」`, "success");
    await refreshAll(true);
    if (res?.item?.id) {
      document.getElementById("ttsPresetSelect").value = String(res.item.id);
    }
  } catch (err) {
    toast(`保存预设失败: ${err.message}`, "error");
  }
}

async function deleteSelectedPreset() {
  const sel = document.getElementById("ttsPresetSelect");
  const pid = sel?.value;
  if (!pid) return toast("请先在下拉框中选择一个要删除的参数预设", "warning");
  const preset = state.presets.find((x) => String(x.id) === String(pid));
  const label = preset ? `「${preset.name}」` : `#${pid}`;
  if (!confirm(`确定删除参数预设 ${label} 吗？`)) return;
  try {
    await api(`/api/v1/tts/presets/${pid}`, { method: "DELETE" });
    sel.value = "";
    toast(`已删除参数预设 ${label}`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`删除预设失败: ${err.message}`, "error");
  }
}

async function saveAsGlobalTtsConfig() {
  const p = collectTtsParams();
  const mapped = {
    index_tts_model_version: p.model_version,
    index_tts_lang: p.lang,
    index_tts_speed: p.speed,
    index_tts_emo_mode: p.emo_mode,
    index_tts_emo_alpha: p.emo_alpha,
    index_tts_emo_vector: p.emo_vector,
    index_tts_emo_audio_prompt: p.emo_audio_prompt,
    index_tts_emo_text: p.emo_text,
    index_tts_diffusion_steps: p.diffusion_steps,
    index_tts_num_beams: p.num_beams,
    index_tts_interval_silence: p.interval_silence,
    index_tts_temperature: p.temperature,
    index_tts_top_p: p.top_p,
    index_tts_top_k: p.top_k,
    index_tts_repetition_penalty: p.repetition_penalty,
    index_tts_chunk_size: p.chunk_size,
    index_tts_auto_fast_on_long_text: p.auto_fast_on_long_text,
  };
  try {
    await api("/api/v1/tts/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(mapped),
    });
    toast("已保存为全局默认 IndexTTS2 参数（下次自动生效）", "success");
  } catch (err) {
    toast(`保存全局配置失败: ${err.message}`, "error");
  }
}

async function uploadEmoAudio(input) {
  const file = input.files?.[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const res = await api("/api/v1/tts/upload-emo-audio", { method: "POST", body: fd });
    document.getElementById("ttsEmoAudioPath").value = res.path;
    toast(`情感参考音上传成功: ${res.filename}`, "success");
  } catch (err) {
    toast(`上传失败: ${err.message}`, "error");
  }
  input.value = "";
}

function getEffectiveVoiceId() {
  const chosen = document.getElementById("ttsVoiceSelect").value;
  if (chosen) return Number(chosen);
  const model = state.models.find((m) => m.id === state.selectedModelId);
  return model?.voice_id ? Number(model.voice_id) : null;
}

async function handleSynthesizeTts() {
  const text = (document.getElementById("ttsTextInput").value || "").trim();
  if (!text) return toast("请先输入口播文案", "warning");
  const voiceId = getEffectiveVoiceId();
  if (!voiceId) return toast("请先选择数字人模特或手动指定参考音色", "warning");
  const ttsParams = collectTtsParams();
  if (ttsParams.emo_mode === "emo_audio" && !ttsParams.emo_audio_prompt) {
    return toast("已选择「情感参考音频」模式，请先上传情感参考音或填入有效本地路径", "warning");
  }

  const btn = document.getElementById("btnSynthesizeTts");
  const liveBox = document.getElementById("ttsLiveBox");
  const liveMsg = document.getElementById("ttsLiveMsg");
  const liveBar = document.getElementById("ttsLiveBar");

  btn.disabled = true;
  liveBox.classList.remove("hidden");
  liveMsg.textContent = "正在连接 IndexTTS2 并准备模型...";
  liveBar.style.width = "8%";

  clearInterval(state.ttsPollingTimer);
  state.ttsPollingTimer = setInterval(async () => {
    try {
      const st = await api("/api/v1/tts/status");
      const d = st.data || {};
      if (d.message) liveMsg.textContent = `${d.message}${d.elapsed_seconds ? `（已用 ${d.elapsed_seconds}s）` : ""}`;
      if (d.progress) liveBar.style.width = `${Math.max(8, d.progress)}%`;
    } catch {}
  }, 1200);

  try {
    const catVal = document.getElementById("jobCategorySelect").value;
    const res = await api("/api/v1/tts/synthesize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        voice_id: voiceId,
        tts_params: ttsParams,
        name: (document.getElementById("jobNameInput").value || "").trim() || null,
        category_id: catVal ? Number(catVal) : null,
      }),
    });
    const item = res.item;
    state.lastAuditionAsset = item;
    document.getElementById("auditionTitle").textContent = `${item.name} (${fmtDuration(item.duration)})`;
    document.getElementById("auditionPlayer").src = item.stream_url;
    document.getElementById("auditionDownload").href = item.download_url;
    document.getElementById("ttsAuditionResult").classList.remove("hidden");
    toast("试听音频合成完成，可直接播放或一键驱动数字人！", "success");
    await refreshAll(true);
  } catch (err) {
    toast(`语音合成失败: ${err.message}`, "error", 6000);
  } finally {
    clearInterval(state.ttsPollingTimer);
    btn.disabled = false;
    liveBox.classList.add("hidden");
  }
}

async function handleCancelTts() {
  try {
    const res = await api("/api/v1/tts/cancel", { method: "POST" });
    toast(res.msg || "已中断语音合成", "warning");
  } catch (err) {
    toast(`中断失败: ${err.message}`, "error");
  }
}

async function createVideoFromAudition() {
  if (!state.lastAuditionAsset) return toast("暂无可复用的试听音频", "warning");
  if (!state.selectedModelId) return toast("请先在左侧选择数字人模特", "warning");
  const model = state.models.find((m) => m.id === state.selectedModelId);
  if (model && !model.video_exists) {
    return toast(`模特「${model.name}」的底模视频文件缺失，请先点击「关联」补回底模视频`, "error", 5500);
  }
  const btn = document.getElementById("btnAuditionToVideo");
  if (btn) btn.disabled = true;
  const catVal = document.getElementById("jobCategorySelect").value;
  try {
    const res = await api("/api/v1/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model_id: state.selectedModelId,
        name: (document.getElementById("jobNameInput").value || "").trim() || state.lastAuditionAsset.name,
        audio_asset_id: state.lastAuditionAsset.id,
        category_id: catVal ? Number(catVal) : null,
      }),
    });
    toast(`已加入视频生成队列（任务 #${res.job_id}，复用已试听音频免重复配音）`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`创建视频任务失败: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleSubmitTtsVideo() {
  if (!state.selectedModelId) return toast("请先在左侧选择数字人模特", "warning");
  const model = state.models.find((m) => m.id === state.selectedModelId);
  if (model && !model.video_exists) {
    return toast(`模特「${model.name}」的底模视频文件缺失，请先点击「关联」补回底模视频`, "error", 5500);
  }
  const text = (document.getElementById("ttsTextInput").value || "").trim();
  if (!text) return toast("请先输入口播文案", "warning");
  const ttsParams = collectTtsParams();
  if (ttsParams.emo_mode === "emo_audio" && !ttsParams.emo_audio_prompt) {
    return toast("已选择「情感参考音频」模式，请先上传情感参考音或填入有效本地路径", "warning");
  }

  const btn = document.getElementById("btnSubmitTtsVideo");
  if (btn) btn.disabled = true;
  const catVal = document.getElementById("jobCategorySelect").value;
  const voiceVal = document.getElementById("ttsVoiceSelect").value;
  try {
    const res = await api("/api/v1/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model_id: state.selectedModelId,
        name: (document.getElementById("jobNameInput").value || "").trim() || null,
        text,
        voice_id: voiceVal ? Number(voiceVal) : null,
        tts_params: ttsParams,
        category_id: catVal ? Number(catVal) : null,
      }),
    });
    toast(`任务 #${res.job_id} 已加入队列！右侧监视器将实时追踪进度`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`提交任务失败: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ==================== Mode B: Batch Audio Import ====================

async function handleUploadBatchAudios(fileList) {
  if (!fileList || !fileList.length) return;
  const fd = new FormData();
  const catVal = document.getElementById("jobCategorySelect").value;
  if (catVal) fd.append("category_id", catVal);
  for (const f of fileList) fd.append("files", f);

  toast(`正在导入并转码 ${fileList.length} 个音频文件...`, "info");
  try {
    const res = await api("/api/v1/audios/upload", { method: "POST", body: fd });
    (res.items || []).forEach((it) => state.selectedAudioIds.add(it.id));
    toast(`成功导入 ${res.count} 个音频，已自动勾选`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`音频导入失败: ${err.message}`, "error");
  }
  document.getElementById("batchAudioInput").value = "";
}

function formatAudioSourceLabel(sourceType) {
  if (sourceType === "tts") return "IndexTTS2 合成";
  if (sourceType === "local_import") return "本机路径导入";
  return "本地上传";
}

async function handleImportLocalAudioPaths() {
  const input = document.getElementById("localAudioPathInput");
  const raw = input?.value || "";
  const paths = raw
    .split(/\r?\n/)
    .map((ln) => cleanLocalPath(ln))
    .filter(Boolean);
  if (!paths.length) return toast("请先粘贴至少 1 条本机音频文件路径", "warning");

  const btn = document.getElementById("btnImportLocalAudio");
  if (btn) { btn.disabled = true; btn.textContent = "导入中..."; }
  const catVal = document.getElementById("jobCategorySelect").value;
  try {
    const res = await api("/api/v1/audios/import-local", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths, category_id: catVal ? Number(catVal) : null }),
    });
    (res.items || []).forEach((it) => state.selectedAudioIds.add(it.id));
    if (input) input.value = "";
    toast(`已从本机快速导入 ${res.count} 条音频并自动勾选`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`本机路径导入失败: ${err.message}`, "error", 5500);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "📥 导入并勾选"; }
  }
}

function renderCreateAudioLibrary() {
  const wrap = document.getElementById("createAudioLibraryList");
  document.getElementById("selectedAudioCount").textContent = `已选 ${state.selectedAudioIds.size} 条`;
  if (!state.audios.length) {
    wrap.innerHTML = `<div class="section-sub">音频库暂无音频，请拖拽上传、粘贴本机路径或先在左侧合成试听音频。</div>`;
    return;
  }
  wrap.innerHTML = state.audios
    .map((a) => {
      const checked = state.selectedAudioIds.has(a.id);
      return `
        <div class="model-item ${checked ? "selected" : ""}" style="padding:8px 10px;" onclick="toggleSelectAudio(${a.id})">
          <div class="flex-row" style="min-width:0;flex:1;">
            <input type="checkbox" ${checked ? "checked" : ""} onclick="event.stopPropagation();toggleSelectAudio(${a.id})" />
            <div style="min-width:0;">
              <div style="font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(a.name)}</div>
              <div class="section-sub" style="font-size:11.5px;">${formatAudioSourceLabel(a.source_type)} · ${fmtDuration(a.duration)}</div>
            </div>
          </div>
          ${a.stream_url ? `<audio src="${esc(a.stream_url)}" controls style="width:150px;height:28px;" onclick="event.stopPropagation()"></audio>` : ""}
        </div>`;
    })
    .join("");
}

function toggleSelectAudio(id) {
  if (state.selectedAudioIds.has(id)) state.selectedAudioIds.delete(id);
  else state.selectedAudioIds.add(id);
  renderCreateAudioLibrary();
}

async function handleBatchCreateFromAudios() {
  if (!state.selectedModelId) return toast("请先选择数字人模特", "warning");
  const model = state.models.find((m) => m.id === state.selectedModelId);
  if (model && !model.video_exists) return toast(`模特「${model.name}」的底模视频缺失，请先重新关联`, "error");

  const catVal = document.getElementById("jobCategorySelect").value;
  const ids = Array.from(state.selectedAudioIds);
  const pastedInput = document.getElementById("localAudioPathInput");
  const pastedPaths = (pastedInput?.value || "")
    .split(/\r?\n/)
    .map((ln) => cleanLocalPath(ln))
    .filter(Boolean);

  if (!ids.length && !pastedPaths.length) {
    return toast("请先上传、粘贴本机路径或勾选至少 1 条驱动音频", "warning");
  }

  const items = ids.map((aid) => {
    const asset = state.audios.find((x) => x.id === aid);
    return {
      model_id: state.selectedModelId,
      name: asset ? asset.name : `音频任务_${aid}`,
      audio_asset_id: aid,
      category_id: catVal ? Number(catVal) : null,
    };
  });

  pastedPaths.forEach((p) => {
    const baseName = p.split(/[\\/]/).pop()?.replace(/\.[^.]+$/, "") || "本机音频任务";
    items.push({
      model_id: state.selectedModelId,
      name: baseName,
      audio_path: p,
      category_id: catVal ? Number(catVal) : null,
    });
  });

  const btn = document.getElementById("btnBatchCreateAudios");
  if (btn) btn.disabled = true;
  try {
    const res = await api("/api/v1/jobs/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    });
    state.selectedAudioIds.clear();
    if (pastedInput && pastedPaths.length) pastedInput.value = "";
    toast(`已批量提交 ${res.count} 条数字人视频任务进入队列！`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`批量创建失败: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ==================== Live Monitor & Tasks View ====================

function buildPipelineStepsHtml(job) {
  const steps = [
    { key: "queued", label: "1.排队" },
    { key: "tts", label: "2.语音" },
    { key: "render", label: "3.唇形" },
    { key: "mux", label: "4.封装" },
    { key: "done", label: "5.交付" },
  ];
  const phaseIdx = {
    queued: 0,
    tts_loading: 1,
    tts_synthesizing: 1,
    submitting: 2,
    face2face_rendering: 2,
    ffmpeg_muxing: 3,
    delivering: 4,
    success: 5,
    failed: -1,
    cancelled: -2,
  }[job.phase] ?? 0;

  return `<div class="pipeline-steps">${steps
    .map((s, idx) => {
      let cls = "";
      if (job.status === "success" || idx < phaseIdx) cls = "done";
      else if (job.status === "failed" && idx === Math.max(0, phaseIdx)) cls = "failed";
      else if (idx === phaseIdx && job.status !== "cancelled") cls = "active";
      return `<div class="step-node ${cls}">${s.label}</div>`;
    })
    .join("")}</div>`;
}

function statusBadgeHtml(job) {
  const map = {
    pending: ["badge-info", "渲染/处理中"],
    waiting: ["badge-warning", "排队等待"],
    success: ["badge-success", "已完成"],
    failed: ["badge-danger", "报错失败"],
    cancelled: ["badge-muted", "已中断"],
  };
  const [cls, text] = map[job.status] || ["badge-muted", job.status];
  return `<span class="badge ${cls}">${text}</span>`;
}

function renderLiveMonitor() {
  const panel = document.getElementById("liveMonitorPanel");
  const activeJobs = state.jobs.filter((j) => j.status === "pending" || j.status === "waiting");
  const recentFailed = state.jobs.filter((j) => j.status === "failed").slice(0, 2);

  if (!activeJobs.length && !recentFailed.length) {
    panel.innerHTML = `
      <div class="section-sub" style="padding:14px 0;">
        ✅ 当前队列空闲，无报错任务。<br/>选择左侧数字人并输入文案或导入音频后，这里将实时展示帧级渲染进度与一键中断按钮。
      </div>`;
    return;
  }

  let html = "";
  activeJobs.slice(0, 4).forEach((j) => {
    const hbOk = j.heartbeat_age_seconds !== null && j.heartbeat_age_seconds <= 60;
    const hbText = j.heartbeat_age_seconds === null ? "" : (hbOk ? `🟢 ${j.heartbeat_age_seconds}s前活跃` : `🟡 ${j.heartbeat_age_seconds}s无新日志`);
    const frameText = j.total_frames > 0 ? `${j.rendered_frames}/${j.total_frames} 帧` : (j.rendered_frames > 0 ? `已渲染 ${j.rendered_frames} 帧` : "");
    html += `
      <div class="job-card status-${esc(j.status)} mb-8" style="padding:12px;">
        <div class="flex-between">
          <strong style="font-size:13.5px;">#${j.id} ${esc(j.name)}</strong>
          ${statusBadgeHtml(j)}
        </div>
        <div class="section-sub mt-8">${esc(j.stage_label || j.message || "等待执行")}</div>
        ${buildPipelineStepsHtml(j)}
        <div class="progress-track"><div class="progress-fill" style="width:${Math.max(2, j.progress_float)}%;"></div></div>
        <div class="flex-between section-sub mt-8" style="font-size:11.5px;">
          <span>${j.progress_float}% ${frameText ? `· ${frameText}` : ""} ${j.render_fps > 0 ? `· ${j.render_fps}fps` : ""}</span>
          <span>${j.eta_seconds >= 0 ? `剩余 ${fmtEta(j.eta_seconds)}` : hbText}</span>
        </div>
        ${hbText && j.eta_seconds >= 0 ? `<div class="section-sub" style="font-size:11px;">心跳：${hbText}</div>` : ""}
        <div class="flex-row mt-8" style="justify-content:flex-end;">
          <button class="btn btn-sm btn-danger" onclick="handleCancelJob(${j.id})">⏹ 中断任务</button>
        </div>
      </div>`;
  });

  if (recentFailed.length) {
    html += `<div class="section-sub mt-12 mb-8" style="color:#fca5a5;font-weight:600;">⚠ 最近报错任务（需关注）：</div>`;
    recentFailed.forEach((j) => {
      html += `
        <div class="error-box mb-8">
          <div class="flex-between">
            <strong>#${j.id} ${esc(j.name)}</strong>
            <button class="btn btn-sm" onclick="handleRetryJob(${j.id})">🔁 重试</button>
          </div>
          <div class="mt-8">${esc(j.message || j.error_detail)}</div>
        </div>`;
    });
  }

  panel.innerHTML = html;
}

function setTaskFilter(f) {
  state.taskFilter = f;
  document.querySelectorAll("#taskFilterTabs button").forEach((b) => b.classList.toggle("active", b.dataset.status === f));
  renderTasksView();
}

function toggleTaskDetails(kind, jobId, isOpen) {
  const target = kind === "err" ? state.openTaskErrIds : state.openTaskLogIds;
  if (isOpen) target.add(jobId);
  else target.delete(jobId);
}

function previewJobVideo(jobId) {
  const j = state.jobs.find((x) => x.id === jobId);
  if (!j || !j.stream_url) return toast("视频文件不存在或尚未生成", "warning");
  previewVideo(j.stream_url, j.name || `视频 #${j.id}`, j.paths?.playable_path || "");
}

function previewModelVideo(modelId) {
  const m = state.models.find((x) => x.id === modelId);
  if (!m || !m.preview_url) return toast("底模视频文件不存在", "warning");
  previewVideo(m.preview_url, `底模：${m.name}`, m.video_abs_path || "");
}

function openRelinkModelModalById(modelId) {
  const m = state.models.find((x) => x.id === modelId);
  if (!m) return;
  openRelinkModelModal(m.id, m.name || `#${m.id}`);
}

function copyJobDeliveredPath(jobId) {
  const j = state.jobs.find((x) => x.id === jobId);
  const p = j?.paths?.delivered_path || j?.paths?.playable_path || "";
  if (!p) return toast("暂无可复制路径", "warning");
  copyText(p);
}

function openAudioPath(audioId) {
  const a = state.audios.find((x) => x.id === audioId);
  if (!a || !a.file_path) return toast("音频路径不存在", "warning");
  openAnyPath(a.file_path);
}

function deleteCategoryById(catId) {
  const c = state.categories.find((x) => x.id === catId);
  deleteCategory(catId, c?.name || `#${catId}`);
}

function renderTasksView() {
  const wrap = document.getElementById("tasksList");
  const kw = (document.getElementById("taskSearch").value || "").trim().toLowerCase();
  const filtered = state.jobs.filter((j) => {
    if (state.taskFilter === "active" && j.status !== "pending" && j.status !== "waiting") return false;
    if (state.taskFilter !== "all" && state.taskFilter !== "active" && j.status !== state.taskFilter) return false;
    if (kw && !`${j.name} ${j.text_content} ${j.model_name} ${j.message}`.toLowerCase().includes(kw)) return false;
    return true;
  });

  if (!filtered.length) {
    wrap.innerHTML = `<div class="card section-sub" style="text-align:center;padding:30px;">暂无符合筛选条件的任务记录。</div>`;
    return;
  }

  wrap.innerHTML = filtered
    .map((j) => {
      const barCls = j.status === "success" ? "success" : j.status === "failed" ? "failed" : "";
      const framesInfo = j.total_frames > 0
        ? `帧进度 ${j.rendered_frames}/${j.total_frames}${j.render_fps > 0 ? ` (${j.render_fps} 帧/秒)` : ""}`
        : (j.rendered_frames > 0 ? `已写 ${j.rendered_frames} 帧` : "");
      const errOpen = state.openTaskErrIds.has(j.id) ? "open" : "";
      const logOpen = state.openTaskLogIds.has(j.id) ? "open" : "";
      return `
        <div class="job-card status-${esc(j.status)}">
          <div class="flex-between">
            <div class="flex-row">
              <strong>#${j.id} ${esc(j.name)}</strong>
              ${statusBadgeHtml(j)}
              <span class="badge badge-muted">模特: ${esc(j.model_name || "#" + j.model_id)}</span>
              <span class="badge badge-info" style="background:rgba(59,130,246,0.16);">${esc(j.category_name)}</span>
            </div>
            <div class="section-sub">${fmtTime(j.created_at)} ${j.duration ? `· 成片 ${fmtDuration(j.duration)}` : ""}</div>
          </div>

          ${(j.status === "pending" || j.status === "waiting") ? `
            ${buildPipelineStepsHtml(j)}
            <div class="progress-track"><div class="progress-fill ${barCls}" style="width:${Math.max(2, j.progress_float)}%;"></div></div>
            <div class="flex-between section-sub mt-8">
              <span>${esc(j.stage_label || j.message)}</span>
              <span>${j.progress_float}% ${framesInfo ? `· ${framesInfo}` : ""} ${j.eta_seconds >= 0 ? `· 预计剩余 ${fmtEta(j.eta_seconds)}` : ""}</span>
            </div>
          ` : `<div class="section-sub mt-8">${esc(j.stage_label || j.message)}</div>`}

          ${j.status === "failed" ? `
            <div class="error-box">
              <div><strong>❌ 明确报错原因${j.error_code ? ` [${esc(j.error_code)}]` : ""}：</strong>${esc(j.message)}</div>
              ${j.retry_count ? `<div class="mt-8">自动重试次数：${j.retry_count} / ${j.max_retries}</div>` : ""}
              ${j.error_detail && j.error_detail !== j.message ? `<details class="mt-8" ${errOpen} ontoggle="toggleTaskDetails('err',${j.id},this.open)"><summary style="cursor:pointer;">查看原始错误堆栈/日志</summary><pre class="console-box mt-8" style="max-height:150px;">${esc(j.error_detail)}</pre></details>` : ""}
            </div>
          ` : ""}

          ${j.recent_logs?.length && j.status === "pending" ? `
            <details class="mt-8" ${logOpen} ontoggle="toggleTaskDetails('log',${j.id},this.open)"><summary class="section-sub" style="cursor:pointer;">查看该任务实时日志 (${j.recent_logs.length} 条)</summary>
              <pre class="console-box mt-8" style="max-height:140px;">${esc(j.recent_logs.join("\n"))}</pre>
            </details>` : ""}

          <div class="flex-between mt-12">
            <div class="section-sub" style="max-width:58%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
              ${j.paths?.playable_path ? `📄 成品位置: ${esc(j.paths.playable_path)}` : (j.text_content ? `📝 文案: ${esc(j.text_content.slice(0, 60))}` : "")}
            </div>
            <div class="flex-row">
              ${(j.status === "pending" || j.status === "waiting") ? `<button class="btn btn-sm btn-danger" onclick="handleCancelJob(${j.id})">⏹ 中断任务</button>` : ""}
              ${(j.status === "failed" || j.status === "cancelled") ? `<button class="btn btn-sm btn-primary" onclick="handleRetryJob(${j.id})">🔁 重新执行</button>` : ""}
              ${j.file_exists ? `
                <button class="btn btn-sm" onclick="previewJobVideo(${j.id})">▶ 播放</button>
                <a class="btn btn-sm btn-success" href="${esc(j.download_url)}" download>⬇ 下载</a>
                <button class="btn btn-sm" onclick="handleOpenJobFolder(${j.id})">📂 打开目录</button>
              ` : ""}
              <button class="btn btn-sm" title="重命名任务" onclick="renameJob(${j.id})">✏️</button>
              <button class="btn btn-sm btn-danger" onclick="handleDeleteJob(${j.id})">🗑</button>
            </div>
          </div>
        </div>`;
    })
    .join("");
}

async function handleCancelJob(id) {
  if (!confirm(`确定要立即中断任务 #${id} 吗？（若正在渲染，将安全中止后台推理并释放显存，任务记录仍会保留以便后续重试）`)) return;
  try {
    const res = await api(`/api/v1/jobs/${id}/cancel`, { method: "POST" });
    toast(res.message || "已发送中断指令", "warning");
    await refreshAll(true);
  } catch (err) {
    toast(`中断失败: ${err.message}`, "error");
  }
}

async function handleRetryJob(id) {
  try {
    await api(`/api/v1/jobs/${id}/retry`, { method: "POST" });
    toast(`任务 #${id} 已重新加入队列`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`重试失败: ${err.message}`, "error");
  }
}

async function handleDeleteJob(id) {
  if (!confirm(`确定删除任务记录 #${id}？（不会误删任何数字人底模）`)) return;
  try {
    await api(`/api/v1/jobs/${id}`, { method: "DELETE" });
    toast(`已删除任务 #${id}`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`删除失败: ${err.message}`, "error");
  }
}

async function handleOpenJobFolder(id) {
  try {
    const res = await api(`/api/v1/jobs/${id}/open-folder`, { method: "POST" });
    toast(res.message, "success", 2400);
  } catch (err) {
    toast(`打开目录失败: ${err.message}`, "error");
  }
}

async function openDeliveryFolder() {
  const p = state.health?.paths?.delivery_root;
  if (!p) return;
  try {
    await api("/api/v1/system/open-path", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: p }),
    });
  } catch (err) {
    toast(`打开失败: ${err.message}`, "error");
  }
}

// ==================== Library View (Videos & Audios + Categories) ====================

function setLibraryType(t) {
  state.libraryType = t;
  document.querySelectorAll("#libraryTypeTabs button").forEach((b) => b.classList.toggle("active", b.dataset.lib === t));
  renderLibraryView();
}

function selectCategory(catId) {
  state.selectedCategoryId = catId;
  renderLibraryView();
}

async function promptNewCategory() {
  const name = prompt("输入新作品分类名称：", "农资科普");
  if (!name || !name.trim()) return;
  try {
    await api("/api/v1/categories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), color: "#3b82f6" }),
    });
    toast(`已新建分类「${name.trim()}」`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`创建分类失败: ${err.message}`, "error");
  }
}

async function deleteCategory(id, name) {
  if (!confirm(`确定删除分类「${name}」？（组内作品会安全移回「未分类」，不会删除视频文件）`)) return;
  try {
    await api(`/api/v1/categories/${id}`, { method: "DELETE" });
    if (String(state.selectedCategoryId) === String(id)) state.selectedCategoryId = "all";
    toast(`已删除分类「${name}」`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`删除失败: ${err.message}`, "error");
  }
}

function renderLibraryView() {
  // Left category list
  const catBox = document.getElementById("categoryList");
  const isVid = state.libraryType === "videos";
  const allVideos = state.jobs.filter((j) => j.status === "success");
  const totalCount = isVid ? allVideos.length : state.audios.length;
  const uncatCount = isVid
    ? allVideos.filter((j) => !j.category_id).length
    : state.audios.filter((a) => !a.category_id).length;

  let catHtml = `
    <div class="cat-item ${state.selectedCategoryId === "all" ? "active" : ""}" onclick="selectCategory('all')">
      <span>全部</span><span class="nav-count">${totalCount}</span>
    </div>
    <div class="cat-item ${state.selectedCategoryId === "uncategorized" ? "active" : ""}" onclick="selectCategory('uncategorized')">
      <span>未分类</span><span class="nav-count">${uncatCount}</span>
    </div>`;

  state.categories.forEach((c) => {
    const cnt = isVid ? allVideos.filter((j) => j.category_id === c.id).length : state.audios.filter((a) => a.category_id === c.id).length;
    catHtml += `
      <div class="cat-item ${String(state.selectedCategoryId) === String(c.id) ? "active" : ""}" onclick="selectCategory('${c.id}')">
        <span><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${esc(c.color)};margin-right:6px;"></span>${esc(c.name)}</span>
        <span class="flex-row">
          <span class="nav-count">${cnt}</span>
          <button class="btn btn-sm" style="padding:1px 5px;font-size:11px;" onclick="event.stopPropagation();deleteCategoryById(${c.id})">✕</button>
        </span>
      </div>`;
  });
  catBox.innerHTML = catHtml;

  const grid = document.getElementById("libraryGrid");
  const kw = (document.getElementById("libSearch").value || "").trim().toLowerCase();

  if (isVid) {
    const items = allVideos.filter((j) => {
      if (state.selectedCategoryId === "uncategorized" && j.category_id) return false;
      if (state.selectedCategoryId !== "all" && state.selectedCategoryId !== "uncategorized" && String(j.category_id) !== String(state.selectedCategoryId)) return false;
      if (kw && !`${j.name} ${j.text_content} ${j.model_name}`.toLowerCase().includes(kw)) return false;
      return true;
    });

    if (!items.length) {
      grid.innerHTML = `<div class="card section-sub" style="grid-column:1/-1;text-align:center;padding:30px;">该分类下暂无已完成的视频成品。</div>`;
      return;
    }

    grid.innerHTML = items
      .map((j) => `
        <div class="card">
          <div class="flex-between mb-8" style="flex-wrap:nowrap;">
            <strong style="font-size:14px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(j.name)}">#${j.id} ${esc(j.name)}</strong>
            <span class="badge ${j.file_exists ? "badge-success" : "badge-danger"}" style="flex-shrink:0;">${j.file_exists ? `${j.file_size_mb} MB` : "文件已移走"}</span>
          </div>
          <div class="section-sub mb-8">
            模特：${esc(j.model_name || "-")} · 时长：${fmtDuration(j.duration)} · ${fmtTime(j.created_at)}
          </div>
          <div class="flex-row mb-8">
            <span class="section-sub">归类：</span>
            <select class="select" style="flex:1;padding:4px 8px;" onchange="changeJobCategory(${j.id}, this.value)">
              <option value="" ${!j.category_id ? "selected" : ""}>未分类</option>
              ${state.categories.map((c) => `<option value="${c.id}" ${j.category_id === c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}
            </select>
          </div>
          <details class="adv-box" style="margin-top:0;margin-bottom:10px;padding:7px 10px;">
            <summary style="font-size:12px;">📍 源文件位置与配音参数</summary>
            <div class="path-box" style="font-size:11.5px;">
              <div><strong>成片交付：</strong>${esc(j.paths.delivered_path || "(未归档)")} <button class="btn btn-sm" style="padding:1px 6px;" onclick="copyJobDeliveredPath(${j.id})">复制</button></div>
              <div class="mt-8"><strong>渲染源件：</strong>${esc(j.paths.temp_video_path || "(已清理)")}</div>
              <div class="mt-8"><strong>驱动音频：</strong>${esc(j.paths.source_audio_path || "-")}</div>
              <div class="mt-8"><strong>模特底模：</strong>${esc(j.paths.model_video_path || "-")}</div>
              ${j.text_content ? `<div class="mt-8"><strong>口播文案：</strong>${esc(j.text_content)}</div>` : ""}
            </div>
          </details>
          <div class="flex-row">
            ${j.file_exists ? `<button class="btn btn-sm btn-primary" onclick="previewJobVideo(${j.id})">▶ 预览</button>` : ""}
            ${j.file_exists ? `<a class="btn btn-sm btn-success" href="${esc(j.download_url)}" download>⬇ 下载</a>` : ""}
            <button class="btn btn-sm" onclick="handleOpenJobFolder(${j.id})">📂 源文件</button>
            <button class="btn btn-sm" onclick="renameJob(${j.id})">✏️ 重命名</button>
            <button class="btn btn-sm" onclick="cloneJobToCreate(${j.id})">🔁 复用参数</button>
          </div>
        </div>`)
      .join("");
  } else {
    const items = state.audios.filter((a) => {
      if (state.selectedCategoryId === "uncategorized" && a.category_id) return false;
      if (state.selectedCategoryId !== "all" && state.selectedCategoryId !== "uncategorized" && String(a.category_id) !== String(state.selectedCategoryId)) return false;
      if (kw && !`${a.name} ${a.text_content}`.toLowerCase().includes(kw)) return false;
      return true;
    });

    if (!items.length) {
      grid.innerHTML = `<div class="card section-sub" style="grid-column:1/-1;text-align:center;padding:30px;">该分类下暂无音频素材。</div>`;
      return;
    }

    grid.innerHTML = items
      .map((a) => `
        <div class="card">
          <div class="flex-between mb-8">
            <strong>${esc(a.name)}</strong>
            <span class="badge badge-info">${formatAudioSourceLabel(a.source_type)} · ${fmtDuration(a.duration)}</span>
          </div>
          <div class="flex-row mb-8">
            <span class="section-sub">归类：</span>
            <select class="select" style="flex:1;padding:4px 8px;" onchange="changeAudioCategory(${a.id}, this.value)">
              <option value="" ${!a.category_id ? "selected" : ""}>未分类</option>
              ${state.categories.map((c) => `<option value="${c.id}" ${a.category_id === c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}
            </select>
          </div>
          ${a.stream_url ? `<audio src="${esc(a.stream_url)}" controls class="mb-8"></audio>` : `<div class="section-sub mb-8" style="color:#fca5a5;">文件已缺失</div>`}
          <div class="path-box mb-8" style="font-size:11px;">${esc(a.file_path)}</div>
          <div class="flex-row">
            <button class="btn btn-sm btn-primary" onclick="quickVideoFromAudio(${a.id})">🎬 生成数字人视频</button>
            ${a.download_url ? `<a class="btn btn-sm" href="${esc(a.download_url)}" download>⬇ 下载</a>` : ""}
            <button class="btn btn-sm" onclick="renameAudioAsset(${a.id})">✏️ 重命名</button>
            <button class="btn btn-sm" onclick="openAudioPath(${a.id})">📂 定位</button>
            <button class="btn btn-sm btn-danger" onclick="deleteAudioAsset(${a.id})">🗑</button>
          </div>
        </div>`)
      .join("");
  }
}

async function renameJob(jobId) {
  const j = state.jobs.find((x) => x.id === jobId);
  if (!j) return;
  const nextName = prompt("请输入新的作品标题：", j.name || "");
  if (nextName === null) return;
  const trimmed = nextName.trim();
  if (!trimmed) return toast("作品标题不能为空", "warning");
  if (trimmed === j.name) return;
  try {
    await api(`/api/v1/jobs/${jobId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: trimmed }),
    });
    toast(`作品已重命名为「${trimmed}」`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`重命名失败: ${err.message}`, "error");
  }
}

async function renameAudioAsset(audioId) {
  const a = state.audios.find((x) => x.id === audioId);
  if (!a) return;
  const nextName = prompt("请输入新的音频名称：", a.name || "");
  if (nextName === null) return;
  const trimmed = nextName.trim();
  if (!trimmed) return toast("音频名称不能为空", "warning");
  if (trimmed === a.name) return;
  try {
    await api(`/api/v1/audios/${audioId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: trimmed }),
    });
    toast(`音频已重命名为「${trimmed}」`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`音频重命名失败: ${err.message}`, "error");
  }
}

async function changeAudioCategory(audioId, catVal) {
  try {
    await api(`/api/v1/audios/${audioId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category_id: catVal ? Number(catVal) : null }),
    });
    toast("音频分类已更新", "success", 2000);
    await refreshAll(true);
  } catch (err) {
    toast(`修改音频分类失败: ${err.message}`, "error");
  }
}

async function changeJobCategory(jobId, catVal) {
  try {
    await api(`/api/v1/jobs/${jobId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category_id: catVal ? Number(catVal) : null }),
    });
    toast("分类已更新，成片目录已同步归类", "success", 2400);
    await refreshAll(true);
  } catch (err) {
    toast(`修改分类失败: ${err.message}`, "error");
  }
}

function cloneJobToCreate(jobId) {
  const j = state.jobs.find((x) => x.id === jobId);
  if (!j) return;
  if (j.model_id) {
    state.selectedModelId = j.model_id;
    const gf = document.getElementById("createModelGroupFilter");
    if (gf) gf.value = "all";
    const ms = document.getElementById("createModelSearch");
    if (ms) ms.value = "";
  }
  document.getElementById("jobNameInput").value = `${j.name}_复制`;
  document.getElementById("jobCategorySelect").value = j.category_id ? String(j.category_id) : "";

  const voiceSel = document.getElementById("ttsVoiceSelect");
  if (voiceSel) {
    const jobVoiceId = j.voice_id ? String(j.voice_id) : "";
    const hasOpt = jobVoiceId && Array.from(voiceSel.options).some((opt) => opt.value === jobVoiceId);
    voiceSel.value = hasOpt ? jobVoiceId : "";
  }

  let toastMsg = "已复用该作品的模特、音色、文案与 IndexTTS2 参数！";
  if (j.text_content) {
    setCreateMode("tts");
    document.getElementById("ttsTextInput").value = j.text_content;
    updateTextStats();
    // 先复位情感模式与输入，防止残留上一条作品的脏情感配置
    setEmoMode("same_as_speaker");
    document.getElementById("ttsEmoAudioPath").value = "";
    document.getElementById("ttsEmoText").value = "";
    if (j.tts_params && Object.keys(j.tts_params).length) {
      applyTtsParamsToForm(j.tts_params);
    }
  } else if (j.paths?.source_audio_path) {
    setCreateMode("audio");
    const normSrc = String(j.paths.source_audio_path).replace(/\\/g, "/").toLowerCase();
    const matchedAudio = state.audios.find((a) => String(a.file_path || "").replace(/\\/g, "/").toLowerCase() === normSrc);
    const localInput = document.getElementById("localAudioPathInput");
    if (matchedAudio) {
      state.selectedAudioIds.clear();
      state.selectedAudioIds.add(matchedAudio.id);
      if (localInput) localInput.value = "";
      renderCreateAudioLibrary();
      toastMsg = "已复用该音频直驱作品的模特与对应音频库素材！";
    } else {
      state.selectedAudioIds.clear();
      if (localInput) localInput.value = j.paths.source_audio_path;
      renderCreateAudioLibrary();
      toastMsg = "已复用该音频直驱作品的模特与源音频路径！";
    }
  }
  switchView("create");
  renderCreateModelList();
  updateVoicePreview();
  toast(toastMsg, "success");
}

function quickVideoFromAudio(audioId) {
  state.selectedAudioIds.clear();
  state.selectedAudioIds.add(audioId);
  setCreateMode("audio");
  switchView("create");
  renderCreateAudioLibrary();
  toast("已选中该音频，确认左侧数字人后点击批量加入队列即可！", "info");
}

async function deleteAudioAsset(id) {
  if (!confirm("确认删除这条音频素材及文件？")) return;
  try {
    await api(`/api/v1/audios/${id}`, { method: "DELETE" });
    toast("已删除音频素材", "success");
    await refreshAll(true);
  } catch (err) {
    toast(`删除失败: ${err.message}`, "error");
  }
}

async function openAnyPath(path) {
  try {
    await api("/api/v1/system/open-path", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
  } catch (err) {
    toast(`打开路径失败: ${err.message}`, "error");
  }
}

function previewVideo(url, title, pathText) {
  document.getElementById("previewModalTitle").textContent = title;
  document.getElementById("previewModalVideo").src = url;
  document.getElementById("previewModalMeta").textContent = `本地路径：${pathText}`;
  openModal("previewModal");
}

// ==================== Models & Voices Manage View ====================

function setModelSubTab(sub) {
  state.modelSubTab = sub;
  document.querySelectorAll("#modelSubTabs button").forEach((b) => b.classList.toggle("active", b.dataset.sub === sub));
  document.getElementById("modelsSubPanel").classList.toggle("hidden", sub !== "models");
  document.getElementById("voicesSubPanel").classList.toggle("hidden", sub !== "voices");
  renderModelsManageView();
}

function renderModelsManageView() {
  if (state.modelSubTab === "models") {
    const grid = document.getElementById("modelsManageGrid");
    if (!state.models.length) {
      grid.innerHTML = `<div class="card section-sub" style="grid-column:1/-1;text-align:center;">暂无数字人模特，请点击右上角「+ 导入新数字人模特」。</div>`;
      return;
    }
    grid.innerHTML = state.models
      .map((m) => `
        <div class="card" style="border-color:${m.video_exists ? "var(--border)" : "rgba(239,68,68,0.5)"}">
          <div class="flex-between mb-8">
            <strong>#${m.id} ${esc(m.name)}</strong>
            <span class="badge ${m.video_exists ? "badge-success" : "badge-danger"}">${m.video_exists ? `底模正常 ${m.video_size_mb}MB` : "⚠ 底模视频缺失"}</span>
          </div>
          <div class="flex-row mb-8">
            <span class="section-sub">分组：</span>
            <select class="select" style="flex:1;padding:4px 8px;" onchange="changeModelGroup(${m.id}, this.value)">
              <option value="" ${!m.group_id ? "selected" : ""}>未分组</option>
              ${state.modelGroups.map((g) => `<option value="${g.id}" ${m.group_id === g.id ? "selected" : ""}>${esc(g.name)}</option>`).join("")}
            </select>
          </div>
          <div class="path-box mb-8" style="font-size:11px;">
            <div>底模视频：${esc(m.video_abs_path)}</div>
            <div>原声参考：${m.audio_exists ? "✅" : "⚠"} ${esc(m.audio_abs_path)}</div>
          </div>
          ${m.voice_preview_url ? `<audio src="${esc(m.voice_preview_url)}" controls class="mb-8"></audio>` : ""}
          <div class="flex-row">
            ${m.video_exists
              ? `<button class="btn btn-sm btn-primary" onclick="previewModelVideo(${m.id})">▶ 预览底模</button>`
              : ""}
            <button class="btn btn-sm ${m.video_exists ? "" : "btn-warning"}" onclick="openRelinkModelModalById(${m.id})">🔗 ${m.video_exists ? "替换底模" : "重新关联底模"}</button>
            <button class="btn btn-sm" onclick="renameModel(${m.id})">✏️ 重命名</button>
            <button class="btn btn-sm" onclick="selectModel(${m.id});switchView('create')">🎬 去创作</button>
            <button class="btn btn-sm btn-danger" title="删除模特记录" onclick="deleteModelById(${m.id})">🗑</button>
          </div>
        </div>`)
      .join("");
  } else {
    const grid = document.getElementById("voicesManageGrid");
    grid.innerHTML = state.voices
      .map((v) => `
        <div class="card">
          <div class="flex-between mb-8">
            <strong>#${v.id} ${esc(v.display_name)}</strong>
            <span class="badge ${v.exists ? "badge-success" : "badge-danger"}">${v.exists ? "可用" : "文件缺失"}</span>
          </div>
          ${v.preview_url ? `<audio src="${esc(v.preview_url)}" controls class="mb-8"></audio>` : ""}
          <div class="path-box" style="font-size:11px;">${esc(v.abs_path)}</div>
        </div>`)
      .join("");
  }
}

async function promptNewModelGroup() {
  const name = prompt("输入新模特分组名称：", "主力出镜");
  if (!name || !name.trim()) return;
  try {
    await api("/api/v1/model-groups", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    toast(`已新建分组「${name.trim()}」`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`创建失败: ${err.message}`, "error");
  }
}

async function renameModel(modelId) {
  const m = state.models.find((x) => x.id === modelId);
  if (!m) return;
  const nextName = prompt("请输入新的数字人模特名称：", m.name || "");
  if (nextName === null) return;
  const trimmed = nextName.trim();
  if (!trimmed) return toast("模特名称不能为空", "warning");
  if (trimmed === m.name) return;
  try {
    await api(`/api/v1/models/${modelId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: trimmed }),
    });
    toast(`模特已重命名为「${trimmed}」`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`模特重命名失败: ${err.message}`, "error");
  }
}

async function changeModelGroup(modelId, groupVal) {
  try {
    await api(`/api/v1/models/${modelId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ group_id: groupVal ? Number(groupVal) : null }),
    });
    toast("模特分组已更新", "success", 2000);
    await refreshAll(true);
  } catch (err) {
    toast(`更新失败: ${err.message}`, "error");
  }
}

async function deleteModelById(modelId) {
  const m = state.models.find((x) => x.id === modelId);
  const label = m ? `「${m.name}」(#${m.id})` : `#${modelId}`;
  if (!confirm(`确定删除数字人模特记录 ${label} 吗？（仅移除列表记录，不删除历史已完成视频）`)) return;
  try {
    await api(`/api/v1/models/${modelId}`, { method: "DELETE" });
    if (state.selectedModelId === modelId) state.selectedModelId = null;
    toast(`已删除模特 ${label}`, "success");
    await refreshAll(true, true);
  } catch (err) {
    toast(`删除模特失败: ${err.message}`, "error");
  }
}

function openAddModelModal() {
  document.getElementById("modelModalTitle").textContent = "导入新数字人模特";
  document.getElementById("modelModalRelinkId").value = "";
  document.getElementById("modelModalNameField").classList.remove("hidden");
  document.getElementById("modelModalGroupField").classList.remove("hidden");
  document.getElementById("modelModalName").value = "";
  document.getElementById("modelModalFile").value = "";
  document.getElementById("modelModalLocalPath").value = "";
  openModal("modelModal");
}

function openRelinkModelModal(modelId, modelName) {
  document.getElementById("modelModalTitle").textContent = `重新关联底模视频：${modelName}`;
  document.getElementById("modelModalRelinkId").value = String(modelId);
  document.getElementById("modelModalNameField").classList.add("hidden");
  document.getElementById("modelModalGroupField").classList.add("hidden");
  document.getElementById("modelModalFile").value = "";
  document.getElementById("modelModalLocalPath").value = "";
  openModal("modelModal");
}

async function submitModelModal() {
  const relinkId = document.getElementById("modelModalRelinkId").value;
  const file = document.getElementById("modelModalFile").files?.[0];
  const localPath = cleanLocalPath(document.getElementById("modelModalLocalPath").value);
  if (!file && !localPath) return toast("请选择要上传的视频文件，或填写本机视频绝对路径", "warning");

  const btn = document.getElementById("btnSubmitModelModal");
  btn.disabled = true;
  btn.textContent = "导入处理中...";

  try {
    const fd = new FormData();
    if (file) fd.append("file", file);
    if (localPath) fd.append("local_video_path", localPath);

    if (relinkId) {
      await api(`/api/v1/models/${relinkId}/relink`, { method: "POST", body: fd });
      toast("底模视频重新关联成功！", "success");
    } else {
      const name = document.getElementById("modelModalName").value.trim();
      if (!name) { btn.disabled = false; btn.textContent = "确认导入"; return toast("请填写模特名称", "warning"); }
      fd.append("name", name);
      const gid = document.getElementById("modelModalGroup").value;
      if (gid) fd.append("group_id", gid);
      const res = await api("/api/v1/models", { method: "POST", body: fd });
      state.selectedModelId = res.item.id;
      toast(`新模特「${name}」导入成功，已自动提取参考音色！`, "success");
    }
    closeModal("modelModal");
    await refreshAll(true);
  } catch (err) {
    toast(`操作失败: ${err.message}`, "error", 6000);
  } finally {
    btn.disabled = false;
    btn.textContent = "确认导入";
  }
}

function openUploadVoiceModal() {
  document.getElementById("voiceModalName").value = "";
  document.getElementById("voiceModalFile").value = "";
  openModal("voiceModal");
}

async function submitVoiceModal() {
  const file = document.getElementById("voiceModalFile").files?.[0];
  if (!file) return toast("请选择参考音频文件", "warning");
  const btn = document.getElementById("btnSubmitVoiceModal");
  if (btn) { btn.disabled = true; btn.textContent = "上传转码中..."; }
  const fd = new FormData();
  fd.append("name", document.getElementById("voiceModalName").value.trim());
  fd.append("file", file);
  try {
    const res = await api("/api/v1/voices", { method: "POST", body: fd });
    toast(`新音色「${res.item.display_name}」上传成功！`, "success");
    closeModal("voiceModal");
    await refreshAll(true);
    document.getElementById("ttsVoiceSelect").value = String(res.item.id);
    updateVoicePreview();
  } catch (err) {
    toast(`上传音色失败: ${err.message}`, "error");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "确认上传"; }
  }
}

// ==================== Automation API View ====================

function renderApiView() {
  const mid = state.selectedModelId || (state.models[0]?.id ?? 48);
  const apiBase = `${window.location.origin}/api/v1`;
  const pyCode = `import requests, time

BASE = "${apiBase}"

# 1) 创建数字人视频任务（融合 IndexTTS2 参数 + 自动分类）
resp = requests.post(f"{BASE}/jobs", json={
    "model_id": ${mid},
    "name": "AI自动生成口播示例",
    "category_name": "日常口播",
    "text": "大家好，这是通过自动化 AI 接口一键生成的数字人口播视频。",
    "tts_params": {
        "model_version": "IndexTTS-2",
        "speed": 1.02,
        "diffusion_steps": 25,
        "num_beams": 2,
        "emo_mode": "emo_vector",
        "emo_alpha": 0.65,
        "emo_vector": [0.4, 0, 0, 0, 0, 0, 0.2, 0]
    }
}).json()
job_id = resp["job_id"]
print("已提交任务:", job_id)

# 2) 轮询实时进度（帧级进度 + 明确报错）
while True:
    st = requests.get(f"{BASE}/jobs/{job_id}").json()["item"]
    print(f"[{st['status']}] {st['progress_float']}% | {st['stage_label']}")
    if st["status"] in ("success", "failed", "cancelled"):
        break
    time.sleep(5)

if st["status"] == "success":
    print("成片位置:", st["paths"]["delivered_path"])
    print("下载链接:", f"{BASE}/jobs/{job_id}/download")`;

  document.getElementById("apiPythonSnippet").textContent = pyCode;
  if (!document.getElementById("apiTestBody").value.trim()) {
    fillApiDemo("health");
  }
}

function fillApiDemo(kind) {
  const mid = state.selectedModelId || (state.models[0]?.id ?? 48);
  if (kind === "health") {
    document.getElementById("apiTestMethod").value = "GET";
    document.getElementById("apiTestPath").value = "/api/v1/health";
    document.getElementById("apiTestBody").value = "";
  } else if (kind === "text_job") {
    document.getElementById("apiTestMethod").value = "POST";
    document.getElementById("apiTestPath").value = "/api/v1/jobs";
    document.getElementById("apiTestBody").value = JSON.stringify({
      model_id: mid,
      name: "API联调测试任务",
      category_name: "测试样片",
      text: "你好，这是一条自动化接口联调测试文案。",
      tts_params: { model_version: "IndexTTS-2", diffusion_steps: 15, num_beams: 1, speed: 1.0 },
    }, null, 2);
  } else {
    const sampleAudio = (state.audios.find((a) => a.exists)?.file_path || state.voices.find((v) => v.exists)?.abs_path || "D:/duix_avatar_data/voice/data/origin_audio/example.wav").replace(/\\/g, "/");
    document.getElementById("apiTestMethod").value = "POST";
    document.getElementById("apiTestPath").value = "/api/v1/jobs";
    document.getElementById("apiTestBody").value = JSON.stringify({
      model_id: mid,
      name: "本地音频直驱测试",
      category_name: "测试样片",
      audio_path: sampleAudio,
    }, null, 2);
  }
}

async function runApiTest() {
  const method = document.getElementById("apiTestMethod").value;
  const path = document.getElementById("apiTestPath").value.trim();
  const body = document.getElementById("apiTestBody").value.trim();
  const out = document.getElementById("apiTestResponse");
  out.textContent = "请求发送中...";
  try {
    const opts = { method };
    if (method !== "GET" && body) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = body;
    }
    const res = await api(path, opts);
    out.textContent = JSON.stringify(res, null, 2);
    if (method === "POST") await refreshAll(true);
  } catch (err) {
    out.textContent = `错误: ${err.message}`;
  }
}

// ==================== Bootstrap ====================

window.addEventListener("DOMContentLoaded", async () => {
  const hostEl = document.getElementById("studioEndpointHost");
  if (hostEl) hostEl.textContent = window.location.host || window.location.origin;
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollLiveTick(false);
  });
  initEmoSliders();
  initDropzone();
  await refreshAll();
  // 拉取当前全局 IndexTTS2 参数初始化表单
  try {
    const cfgRes = await api("/api/v1/tts/config");
    const c = cfgRes.config || {};
    applyTtsParamsToForm({
      model_version: c.index_tts_model_version,
      lang: c.index_tts_lang,
      speed: c.index_tts_speed,
      emo_mode: c.index_tts_emo_mode,
      emo_alpha: c.index_tts_emo_alpha,
      emo_vector: c.index_tts_emo_vector,
      emo_audio_prompt: c.index_tts_emo_audio_prompt,
      emo_text: c.index_tts_emo_text,
      diffusion_steps: c.index_tts_diffusion_steps,
      num_beams: c.index_tts_num_beams,
      interval_silence: c.index_tts_interval_silence,
      temperature: c.index_tts_temperature,
      top_p: c.index_tts_top_p,
      top_k: c.index_tts_top_k,
      repetition_penalty: c.index_tts_repetition_penalty,
      chunk_size: c.index_tts_chunk_size,
      auto_fast_on_long_text: c.index_tts_auto_fast_on_long_text,
    });
  } catch {}
  scheduleNextPoll();
});
