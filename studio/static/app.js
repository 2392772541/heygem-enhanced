const EMO_LABELS = ["喜", "怒", "哀", "惧", "厌恶", "低落", "惊喜", "平静"];
const PREFS_KEY = "heygem_local_studio_prefs_v2";

const VIEW_TITLES = {
  create: "做视频",
  voice: "配声音",
  tasks: "生成进度",
  library: "我的作品",
  models: "形象与音色",
  system: "设置",
};

const SVG_PLAY = `<svg class="ico ico-sm" viewBox="0 0 24 24"><polygon points="7 4 20 12 7 20 7 4"></polygon></svg>`;
const SVG_CHECK = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>`;
const SVG_EDIT = `<svg class="ico ico-sm" viewBox="0 0 24 24"><path d="M12 20h9"></path><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"></path></svg>`;
const SVG_CLOSE = `<svg class="ico ico-sm" viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>`;

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
  discoveredVideos: [],
  modelGroups: [],
  voices: [],
  categories: [],
  presets: [],
  jobs: [],
  audios: [],
  ttsPollingTimer: null,
  openTaskErrIds: new Set(),
  openTaskLogIds: new Set(),
  dismissedBannerJobIds: new Set(),
  sessionSubmittedJobIds: loadSessionSubmittedIds(),
  previewCurrentPath: "",
  pickingFile: false,
  importingBusy: false,
  importingLabel: "",
  lastHealthFetchAt: 0,
  lastSuccessCount: -1,
  pollInFlight: false,
  backendDisconnected: false,
  disconnectCount: 0,
  lastDisconnectReason: "",
  cloudSyncActive: false,
  cloudSyncDrawerOpen: false,
  avatarGalleryExpanded: false,
  prefsLoaded: false,
  preferredVoiceId: "",
};

// ==================== 本地记忆与基础工具 ====================

function loadPrefs() {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function savePrefs(patch = {}) {
  try {
    const cur = loadPrefs();
    localStorage.setItem(PREFS_KEY, JSON.stringify({ ...cur, ...patch }));
  } catch {}
}

function loadSessionSubmittedIds() {
  try {
    const raw = sessionStorage.getItem("heygem_session_job_ids");
    const arr = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(arr) ? arr.map(Number).filter((n) => Number.isFinite(n)) : []);
  } catch {
    return new Set();
  }
}

function trackSessionJobId(jobId) {
  const num = Number(jobId);
  if (!Number.isFinite(num)) return;
  if (!state.sessionSubmittedJobIds.has(num)) {
    state.sessionSubmittedJobIds.add(num);
    try {
      const recent = Array.from(state.sessionSubmittedJobIds).slice(-40);
      sessionStorage.setItem("heygem_session_job_ids", JSON.stringify(recent));
    } catch {}
  }
}

function isLocalBrowser() {
  if (state.health?.access) {
    if (state.health.access.desktop_tools_supported === false) return false;
    if (typeof state.health.access.client_is_local === "boolean") {
      return state.health.access.client_is_local;
    }
  }
  const h = (window.location.hostname || "").toLowerCase();
  return h === "127.0.0.1" || h === "localhost" || h === "::1" || h === "[::1]";
}

function formatApiError(data, status) {
  if (typeof data?.detail === "string" && data.detail.trim()) return data.detail.trim();
  if (Array.isArray(data?.detail) && data.detail.length) {
    return data.detail
      .map((item) => {
        if (typeof item === "string") return item;
        const field = Array.isArray(item?.loc) ? item.loc[item.loc.length - 1] : "";
        const msg = item?.msg || JSON.stringify(item);
        return field ? `${field}: ${msg}` : msg;
      })
      .join("；");
  }
  if (typeof data?.msg === "string" && data.msg.trim()) return data.msg.trim();
  return `请求出错 (${status})`;
}

function fmtSizeMb(mb) {
  const n = Number(mb);
  if (!Number.isFinite(n) || n <= 0) return "< 0.1 MB";
  if (n < 0.1) return "< 0.1 MB";
  return `${n >= 100 ? n.toFixed(1) : n} MB`;
}

function hasVal(v) {
  return v !== null && v !== undefined;
}

function cleanLocalPath(raw) {
  return String(raw ?? "").trim().replace(/^["']+|["']+$/g, "").trim();
}

function fileStemFromPath(p) {
  const base = String(p || "").split(/[\\/]/).pop() || "";
  return base.replace(/\.[^.]+$/, "").trim();
}

function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function toast(msg, type = "info", duration = 3600) {
  const wrap = document.getElementById("toastContainer");
  if (!wrap) return;
  while (wrap.children.length >= 3) wrap.firstElementChild?.remove();
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
      const err = new Error(formatApiError(data, resp.status));
      err.httpStatus = resp.status;
      err.isNetworkError = resp.status === 502 || resp.status === 503 || resp.status === 504;
      throw err;
    }
    return data;
  } catch (err) {
    if (err?.name === "AbortError") {
      const timeoutErr = new Error("请求超时，后台服务暂未响应");
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
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(
      () => toast("已复制到剪贴板", "success", 2000),
      () => fallbackCopyText(text)
    );
  } else {
    fallbackCopyText(text);
  }
}

function fallbackCopyText(text) {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    toast("已复制到剪贴板", "success", 2000);
  } catch {
    toast("复制失败，请手动选中复制", "warning");
  }
}

function openModal(id) {
  document.getElementById(id)?.classList.remove("hidden");
}

function closeModal(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.add("hidden");
  if (id === "previewModal") {
    state.previewCurrentPath = "";
    const vid = document.getElementById("previewModalVideo");
    if (vid) {
      vid.onloadedmetadata = null;
      vid.onerror = null;
      vid.pause();
      vid.removeAttribute("src");
      vid.removeAttribute("poster");
      vid.classList.remove("audio-only", "hidden");
    }
    document.getElementById("previewModalFallback")?.classList.add("hidden");
    document.getElementById("btnPreviewExternal")?.classList.add("hidden");
  }
}

function getValidModels() {
  return state.models.filter((m) => m.video_exists);
}

function getMissingModels() {
  return state.models.filter((m) => !m.video_exists);
}

function getSelectedValidModel() {
  if (!state.selectedModelId) return null;
  const m = state.models.find((x) => x.id === state.selectedModelId);
  return m && m.video_exists ? m : null;
}

function applyClientModeVisibility() {
  const local = isLocalBrowser();
  const sideMode = document.getElementById("sideClientMode");
  if (sideMode) sideMode.textContent = local ? "本机工作台" : "局域网远程访问";

  document.getElementById("btnOpenDeliveryFolder")?.classList.toggle("hidden", !local);
  document.getElementById("btnPickLocalAudios")?.classList.toggle("hidden", !local);
  document.getElementById("btnPickEmoAudioLocal")?.classList.toggle("hidden", !local);
  document.getElementById("btnModalPickVideo")?.classList.toggle("hidden", !local);

  const sumAudioPath = document.getElementById("summaryLocalAudioPaths");
  if (sumAudioPath) {
    sumAudioPath.textContent = local ? "粘贴本机音频路径批量导入（可选）" : "粘贴服务器端音频路径批量导入（可选）";
  }
  const sumVideoPath = document.getElementById("summaryModalLocalVideoPath");
  if (sumVideoPath) {
    sumVideoPath.textContent = local ? "手动粘贴本机视频路径（可选）" : "手动粘贴服务器端视频路径（可选）";
  }
}

function toggleMobileSidebar() {
  const sb = document.getElementById("appSidebar");
  const bd = document.getElementById("sidebarBackdrop");
  if (!sb) return;
  const open = !sb.classList.contains("open");
  sb.classList.toggle("open", open);
  if (bd) bd.classList.toggle("hidden", !open);
}

function closeMobileSidebar() {
  document.getElementById("appSidebar")?.classList.remove("open");
  document.getElementById("sidebarBackdrop")?.classList.add("hidden");
}

// ==================== 视图导航 ====================

function switchView(viewName) {
  if (!VIEW_TITLES[viewName]) viewName = "create";
  closeMobileSidebar();
  state.view = viewName;

  document.querySelectorAll(".rail-item, .rail-settings-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === viewName);
  });
  document.querySelectorAll(".view").forEach((v) => {
    v.classList.toggle("active", v.id === `view-${viewName}`);
  });

  const titleEl = document.getElementById("pageTitle");
  if (titleEl) titleEl.textContent = VIEW_TITLES[viewName];

  const mainEl = document.getElementById("mainContent");
  if (mainEl) mainEl.scrollTop = 0;

  if (viewName === "voice") {
    syncTextBetweenViews("create");
    renderVoiceHistoryList();
    updateVoiceStudioRefPreview();
  }
  if (viewName === "tasks") renderTasksView();
  if (viewName === "library") {
    renderLibraryView();
    refreshCloudSyncStatus(false);
  }
  if (viewName === "models") renderModelsManageView();
  if (viewName === "system") {
    renderHealthBar();
    loadLogSource(state.logSource);
    if (document.getElementById("devApiDrawer")?.open) renderApiView();
  }
}

function openVoiceStudioFromCreate() {
  syncTextBetweenViews("create");
  switchView("voice");
}

function handleTopLiveChipClick() {
  const activeCnt = state.jobs.filter((j) => j.status === "pending" || j.status === "waiting").length;
  if (activeCnt > 0) switchView("tasks");
  else switchView("system");
}

// ==================== 初始化与轮询 ====================

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
    hint.textContent = `当前 8 维情绪总强度 ${sum.toFixed(2)}（超过 0.80 时后台会自动柔和平衡）`;
  } else {
    hint.textContent = `当前 8 维情绪总强度：${sum.toFixed(2)} / 0.80`;
  }
}

function initEmoSliders() {
  const box = document.getElementById("emoVectorBox");
  if (!box) return;
  box.innerHTML = EMO_LABELS.map(
    (label, idx) => `
      <div class="emo-item">
        <div class="field-label-row" style="margin-bottom:4px;">
          <span>${label}</span>
          <span class="meta-num" id="emoVal${idx}">0.00</span>
        </div>
        <input type="range" id="emoSlider${idx}" min="0" max="1" step="0.05" value="0"
               oninput="document.getElementById('emoVal${idx}').textContent=Number(this.value).toFixed(2);updateEmoSumHint();syncTuneSummaryAll()" />
      </div>`
  ).join("");
  updateEmoSumHint();
}

function initDropzone() {
  const dz = document.getElementById("audioDropzone");
  if (!dz) return;
  ["dragenter", "dragover"].forEach((evt) =>
    dz.addEventListener(evt, (e) => { e.preventDefault(); dz.classList.add("dragover"); })
  );
  ["dragleave", "drop"].forEach((evt) =>
    dz.addEventListener(evt, (e) => { e.preventDefault(); dz.classList.remove("dragover"); })
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
  state.lastDisconnectReason = err?.message || "无法连接工作台后台";

  const sDot = document.getElementById("sideDotOverall");
  const sLab = document.getElementById("sideStatusLabel");
  if (sDot) sDot.className = "status-dot dot-err";
  if (sLab) sLab.textContent = "已断连";

  const chip = document.getElementById("topLiveChip");
  const dot = document.getElementById("topLiveDot");
  const txt = document.getElementById("topLiveText");
  if (chip && dot && txt) {
    chip.className = "live-chip err";
    dot.className = "status-dot dot-err";
    txt.textContent = isNetwork ? "后台未连接" : `后台异常 (${httpStatus})`;
  }

  const strip = document.getElementById("alertStrip");
  if (strip) {
    strip.className = "alert-strip";
    document.getElementById("alertText").textContent = isNetwork
      ? "与工作台后台断开连接，页面正在自动重连。"
      : `后台接口返回异常 (${httpStatus})：${state.lastDisconnectReason}`;
  }
  document.getElementById("btnAlertReconnect")?.classList.remove("hidden");
  document.getElementById("btnAlertStart")?.classList.add("hidden");
  document.getElementById("btnAlertResume")?.classList.add("hidden");
  document.getElementById("btnAlertRecover")?.classList.add("hidden");

  if (!wasDisconnected) {
    toast(isNetwork ? "与后台服务连接中断，正在自动重连..." : `后台异常：${state.lastDisconnectReason}`, "error", 5000);
  }
}

function clearBackendDisconnected() {
  if (!state.backendDisconnected) return false;
  state.backendDisconnected = false;
  state.disconnectCount = 0;
  state.lastDisconnectReason = "";
  toast("已重新连接后台服务", "success", 2800);
  return true;
}

function updateNavCounters() {
  const activeCnt = state.jobs.filter((j) => j.status === "pending" || j.status === "waiting").length;
  const successCnt = state.jobs.filter((j) => j.status === "success").length;
  const navAct = document.getElementById("navActiveCount");
  const navSuc = document.getElementById("navSuccessCount");
  const navMod = document.getElementById("navModelCount");
  if (navAct) {
    navAct.textContent = activeCnt;
    navAct.classList.toggle("hidden", activeCnt === 0);
  }
  if (navSuc) navSuc.textContent = successCnt;
  if (navMod) navMod.textContent = getValidModels().length;
  updateTopLiveChip();
}

function updateTopLiveChip() {
  if (state.backendDisconnected) return;
  const chip = document.getElementById("topLiveChip");
  const dot = document.getElementById("topLiveDot");
  const txt = document.getElementById("topLiveText");
  if (!chip || !dot || !txt) return;

  const activeJobs = state.jobs.filter((j) => j.status === "pending" || j.status === "waiting");
  if (activeJobs.length > 0) {
    const first = activeJobs[0];
    chip.className = "live-chip";
    dot.className = "status-dot dot-accent pulse";
    txt.textContent = activeJobs.length > 1
      ? `正在生成 ${first.progress_float || 0}% · 共 ${activeJobs.length} 条`
      : `正在生成 ${first.progress_float || 0}%`;
    return;
  }

  const h = state.health;
  if (h) {
    const dockerOk = Boolean(h.docker?.f2f_api_ok);
    const ttsNeeded = h.tts?.mode === "index-tts2";
    const ttsOk = !ttsNeeded || Boolean(h.tts?.ok);
    const qRun = Boolean(h.queue?.running);
    const qPause = Boolean(h.queue?.paused);
    if (!dockerOk || !ttsOk || !qRun) {
      chip.className = "live-chip err";
      dot.className = "status-dot dot-err";
      txt.textContent = "本机引擎未启动";
      return;
    }
    if (qPause) {
      chip.className = "live-chip warn";
      dot.className = "status-dot dot-warn";
      txt.textContent = "队列已暂停";
      return;
    }
  }
  chip.className = "live-chip hidden";
}

async function refreshAll(silent = false, forceHealth = false) {
  try {
    const healthPromise = api(`/api/v1/health${forceHealth ? "?force=true" : ""}`, { timeoutMs: 15000 })
      .then((health) => {
        state.health = health;
        state.lastHealthFetchAt = Date.now();
        applyClientModeVisibility();
        renderHealthBar();
        updateTopLiveChip();
      })
      .catch(() => {});

    const [modelsRes, groupsRes, voicesRes, catsRes, presetsRes, jobsRes, audiosRes, discoverRes] = await Promise.all([
      api("/api/v1/models", { timeoutMs: 15000 }),
      api("/api/v1/model-groups", { timeoutMs: 15000 }),
      api("/api/v1/voices", { timeoutMs: 15000 }),
      api("/api/v1/categories", { timeoutMs: 15000 }),
      api("/api/v1/tts/presets", { timeoutMs: 15000 }),
      api("/api/v1/jobs?limit=200", { timeoutMs: 15000 }),
      api("/api/v1/audios", { timeoutMs: 15000 }),
      api("/api/v1/models/discover-local", { timeoutMs: 10000 }).catch(() => ({ items: [] })),
    ]);

    clearBackendDisconnected();
    state.models = modelsRes.items || [];
    state.modelGroups = groupsRes.items || [];
    state.voices = voicesRes.items || [];
    state.categories = catsRes.items || [];
    state.presets = presetsRes.items || [];
    state.jobs = jobsRes.items || [];
    state.audios = audiosRes.items || [];
    state.discoveredVideos = discoverRes?.items || [];
    state.lastSuccessCount = state.jobs.filter((j) => j.status === "success").length;
    state.jobs.forEach((j) => {
      if (j.status === "pending" || j.status === "waiting") trackSessionJobId(j.id);
    });

    if (!state.prefsLoaded) {
      const prefs = loadPrefs();
      state.prefsLoaded = true;
      if (prefs.selectedModelId) state.selectedModelId = Number(prefs.selectedModelId);
      if (prefs.selectedVoiceId !== undefined) state.preferredVoiceId = String(prefs.selectedVoiceId);
      if (prefs.createMode === "audio" || prefs.createMode === "tts") {
        setCreateMode(prefs.createMode, false);
      }
    }

    const validAudioIds = new Set(state.audios.map((a) => a.id));
    for (const sid of Array.from(state.selectedAudioIds)) {
      if (!validAudioIds.has(sid)) state.selectedAudioIds.delete(sid);
    }

    const curModel = state.models.find((m) => m.id === state.selectedModelId);
    if (!curModel || !curModel.video_exists) {
      const firstValid = getValidModels()[0];
      state.selectedModelId = firstValid ? firstValid.id : null;
    }

    updateNavCounters();
    applyClientModeVisibility();
    renderSelectOptions();
    renderCreateModelList();
    renderSelectedAvatarBar();
    updateVoicePreview();
    updateVoiceStudioRefPreview();
    renderCreateAudioLibrary();
    renderVoiceHistoryList();
    renderLiveMonitor();
    updateWizardIndicators();
    syncTuneSummaryAll();

    if (state.view === "tasks") renderTasksView();
    if (state.view === "library") renderLibraryView();
    if (state.view === "models") renderModelsManageView();
    if (state.view === "system" && document.getElementById("devApiDrawer")?.open) renderApiView();

    if (!silent || !state.health) await healthPromise;
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
    const [jobsRes, health] = await Promise.all([
      api("/api/v1/jobs?limit=200", { timeoutMs: 12000 }),
      needHealth ? api(`/api/v1/health${forceHealth || wasDisconnected ? "?force=true" : ""}`, { timeoutMs: 12000 }) : Promise.resolve(null),
    ]);

    clearBackendDisconnected();
    state.jobs = jobsRes.items || [];
    state.jobs.forEach((j) => {
      if (j.status === "pending" || j.status === "waiting") trackSessionJobId(j.id);
    });
    if (health) {
      state.health = health;
      state.lastHealthFetchAt = Date.now();
      applyClientModeVisibility();
      renderHealthBar();
    }

    const successCnt = state.jobs.filter((j) => j.status === "success").length;
    updateNavCounters();
    renderLiveMonitor();
    if (state.view === "tasks") renderTasksView();
    if (state.view === "library" && successCnt !== state.lastSuccessCount) renderLibraryView();
    if (state.view === "library" || state.cloudSyncActive) refreshCloudSyncStatus(false);

    state.lastSuccessCount = successCnt;
    if (wasDisconnected) refreshAll(true, false);
  } catch (err) {
    markBackendDisconnected(err);
  } finally {
    state.pollInFlight = false;
  }
}

function scheduleNextPoll() {
  const hasActive = state.jobs.some((j) => j.status === "pending" || j.status === "waiting") || state.cloudSyncActive;
  const delay = state.backendDisconnected ? 3000 : (hasActive ? 2500 : 6000);
  setTimeout(async () => {
    await pollLiveTick(false);
    scheduleNextPoll();
  }, delay);
}

// ==================== 系统与引擎状态 ====================

function isEngineReady() {
  const h = state.health;
  if (!h) return true;
  const dockerOk = Boolean(h.docker?.f2f_api_ok);
  const ttsNeeded = h.tts?.mode === "index-tts2";
  const ttsOk = !ttsNeeded || Boolean(h.tts?.ok);
  const qOk = Boolean(h.queue?.running);
  return dockerOk && ttsOk && qOk;
}

function renderHealthBar() {
  const h = state.health;
  if (!h) return;

  const dockerOk = Boolean(h.docker?.f2f_api_ok);
  const ttsNeeded = h.tts?.mode === "index-tts2";
  const ttsOk = !ttsNeeded || Boolean(h.tts?.ok);
  const ttsState = h.tts?.live_status?.state;
  const qRun = Boolean(h.queue?.running);
  const qPause = Boolean(h.queue?.paused);

  const setDotTxt = (dotId, txtId, cls, text) => {
    const d = document.getElementById(dotId);
    const t = document.getElementById(txtId);
    if (d) d.className = `status-dot ${cls}`;
    if (t) t.textContent = text;
  };

  setDotTxt("dotDocker", "txtDocker", dockerOk ? "dot-ok" : "dot-err", dockerOk ? "已就绪" : "未启动");
  setDotTxt("dotTts", "txtTts", ttsOk ? (ttsState && ttsState !== "idle" ? "dot-warn" : "dot-ok") : "dot-err", ttsOk ? (ttsState && ttsState !== "idle" ? "正在配音" : "已就绪") : "未启动");
  setDotTxt("dotQueue", "txtQueue", qRun ? (qPause ? "dot-warn" : "dot-ok") : "dot-err", qRun ? (qPause ? "已暂停" : "正常运行") : "未启动");

  [document.getElementById("btnPauseResume"), document.getElementById("btnTasksPauseResume")]
    .filter(Boolean)
    .forEach((btn) => {
      btn.textContent = qPause ? "恢复队列" : "暂停队列";
      btn.classList.toggle("btn-success", qPause);
    });

  const g = h.gpu;
  const pillGpu = document.getElementById("pillGpu");
  if (pillGpu) {
    pillGpu.textContent = g?.available
      ? `${g.name.replace("NVIDIA GeForce ", "")} · ${(g.memory_used_mb / 1024).toFixed(1)}/${(g.memory_total_mb / 1024).toFixed(0)} GB · ${g.temperature_c}°C`
      : "未检测到独立显卡";
  }

  const disks = Array.isArray(h.disks) ? h.disks : [];
  const pillDisk = document.getElementById("pillDisk");
  if (pillDisk) {
    pillDisk.textContent = disks.length
      ? disks.map((d) => `${d.label} 剩 ${d.free_gb} GB`).join(" · ")
      : "磁盘空间未知";
  }

  const access = h.access || {};
  const localUrl = access.local_url || `${window.location.protocol}//127.0.0.1:${window.location.port || 8390}`;
  const sysLocalUrl = document.getElementById("sysLocalUrl");
  if (sysLocalUrl) sysLocalUrl.textContent = localUrl;

  const clientLocal = isLocalBrowser();
  const modeBadge = document.getElementById("sysClientModeBadge");
  if (modeBadge) {
    modeBadge.className = `state-tag ${clientLocal ? "st-success" : "st-pending"}`;
    modeBadge.textContent = clientLocal ? "本机直接访问" : "局域网远程访问";
  }

  const lanWrap = document.getElementById("sysLanUrlsWrap");
  if (lanWrap) {
    const entries = Array.isArray(access.lan_entries) && access.lan_entries.length
      ? access.lan_entries
      : (Array.isArray(access.lan_urls) ? access.lan_urls.map((u) => ({ url: u, label: "局域网" })) : []);
    if (!access.lan_enabled) {
      lanWrap.innerHTML = `<span class="muted-text">当前仅监听本机 (${esc(access.bind_host || "127.0.0.1")})</span>`;
    } else if (!entries.length) {
      lanWrap.innerHTML = `<span class="muted-text">暂未检测到可用局域网地址</span>`;
    } else {
      lanWrap.innerHTML = entries
        .map(
          (e) => `
          <div class="access-row">
            <div class="inline-row" style="gap:6px;min-width:0;">
              <span class="state-tag st-muted">${esc(e.label || "局域网")}</span>
              <code>${esc(e.url)}</code>
            </div>
            <button class="btn btn-xs btn-secondary" data-copy="${esc(e.url)}" onclick="copyText(this.dataset.copy)">复制</button>
          </div>`
        )
        .join("");
    }
  }

  const downParts = [];
  if (!dockerOk) downParts.push("口型渲染");
  if (!ttsOk) downParts.push("语音合成");
  if (!qRun) downParts.push("后台队列");

  const sideDot = document.getElementById("sideDotOverall");
  const sideLab = document.getElementById("sideStatusLabel");
  if (downParts.length === 0) {
    if (sideDot) sideDot.className = `status-dot ${qPause ? "dot-warn" : "dot-ok"}`;
    if (sideLab) sideLab.textContent = qPause ? "队列暂停" : "本机就绪";
  } else {
    if (sideDot) sideDot.className = "status-dot dot-err";
    if (sideLab) sideLab.textContent = "未启动";
  }

  const strip = document.getElementById("alertStrip");
  const alertText = document.getElementById("alertText");
  document.getElementById("btnAlertReconnect")?.classList.add("hidden");
  document.getElementById("btnAlertStart")?.classList.add("hidden");
  document.getElementById("btnAlertResume")?.classList.add("hidden");
  document.getElementById("btnAlertRecover")?.classList.add("hidden");

  if (downParts.length > 0) {
    strip.className = "alert-strip";
    alertText.textContent = `本机引擎尚未启动（${downParts.join("、")}），点右侧一键启动即可开始制作。`;
    document.getElementById("btnAlertStart")?.classList.remove("hidden");
  } else if (qPause) {
    strip.className = "alert-strip warning";
    alertText.textContent = "后台制作队列已暂停，新提交的任务会排队等待恢复。";
    document.getElementById("btnAlertResume")?.classList.remove("hidden");
  } else {
    const lowDisk = (h.alerts || []).find((a) => a.code === "LOW_DISK");
    if (lowDisk) {
      strip.className = "alert-strip warning";
      alertText.textContent = lowDisk.message;
    } else {
      strip.classList.add("hidden");
    }
  }

  const sysAlertsWrap = document.getElementById("sysAlertsWrap");
  const alerts = (h.alerts || []).filter((a) => a.code !== "MODEL_FILES_MISSING");
  if (sysAlertsWrap) {
    if (!alerts.length) {
      sysAlertsWrap.classList.add("hidden");
      sysAlertsWrap.innerHTML = "";
    } else {
      sysAlertsWrap.classList.remove("hidden");
      sysAlertsWrap.innerHTML = alerts.map((a) => `<div class="error-box" style="margin-bottom:6px;">${esc(a.message)}</div>`).join("");
    }
  }
  updateTopLiveChip();
}

async function handleStartAll() {
  const btns = [document.getElementById("btnStartAll"), document.getElementById("btnAlertStart")].filter(Boolean);
  btns.forEach((b) => { b.disabled = true; b.dataset.origText = b.textContent; b.textContent = "启动中..."; });
  toast("正在后台拉起本机渲染与配音服务，请稍候...", "info", 5000);
  try {
    const res = await api("/api/v1/system/start", { method: "POST", timeoutMs: 180000 });
    showRecoverReport(res);
    await refreshAll(true, true);
  } catch (err) {
    toast(`启动服务失败: ${err.message}`, "error", 6000);
  } finally {
    btns.forEach((b) => { b.disabled = false; b.textContent = b.dataset.origText || "一键启动服务"; });
  }
}

async function handleRecover(forceRestartContainer = false) {
  const btn = document.getElementById("btnRecover");
  if (btn) { btn.disabled = true; btn.dataset.origText = btn.textContent; btn.textContent = "修复中..."; }
  try {
    const res = await api("/api/v1/system/recover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ restart_render_container: forceRestartContainer, requeue_stuck: true }),
      timeoutMs: 180000,
    });
    showRecoverReport(res);
    await refreshAll(true, true);
  } catch (err) {
    toast(`修复失败: ${err.message}`, "error");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.origText || "自愈修复"; }
  }
}

function showRecoverReport(res) {
  const body = document.getElementById("recoverReportBody");
  const steps = res.steps || [];
  body.innerHTML = steps
    .map(
      (s) => `
      <div class="inline-status-box ${s.ok ? "ok" : "err"}" style="margin-bottom:8px;">
        <div class="inline-row-between">
          <strong>${esc(s.step)}</strong>
          <span class="state-tag ${s.ok ? "st-success" : "st-failed"}">${s.ok ? "正常" : "异常"}</span>
        </div>
        <div class="muted-text" style="font-size:12px;">${esc(s.detail)}</div>
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
  if (!confirm("确认停止后台渲染与配音服务吗？（不会关闭当前网页，随时可点「一键启动服务」恢复）")) return;
  try {
    await api("/api/v1/system/stop", { method: "POST", timeoutMs: 90000 });
    toast("后台服务已停止", "success");
    await refreshAll(true, true);
  } catch (err) {
    toast(`停止失败: ${err.message}`, "error");
  }
}

async function loadLogSource(src) {
  state.logSource = src;
  document.querySelectorAll("#logSourceTabs button").forEach((b) => b.classList.toggle("active", b.dataset.src === src));
  const box = document.getElementById("logsConsole");
  if (!box) return;
  box.textContent = "加载日志中...";
  try {
    const res = await api(`/api/v1/logs?source=${encodeURIComponent(src)}&tail=180`);
    box.textContent = (res.lines || []).join("\n") || "(暂无日志输出)";
    box.scrollTop = box.scrollHeight;
  } catch (err) {
    box.textContent = `获取日志失败: ${err.message}`;
  }
}

// ==================== 做视频：形象选择与就绪检查 ====================

function handleQuickAddModelClick() {
  if (isLocalBrowser()) quickPickAndImportModel();
  else openAddModelModal();
}

function updateWizardIndicators() {
  const selModel = getSelectedValidModel();
  const hasModel = Boolean(selModel);
  const text = (document.getElementById("ttsTextInput")?.value || "").trim();
  const pastedAudioLines = (document.getElementById("localAudioPathInput")?.value || "")
    .split(/\r?\n/)
    .map((ln) => cleanLocalPath(ln))
    .filter(Boolean);
  const audioCount = state.selectedAudioIds.size + pastedAudioLines.length;
  const hasContent = state.createMode === "tts" ? text.length > 0 : audioCount > 0;
  const effectiveVoice = state.createMode === "tts" ? getEffectiveVoiceId() : true;
  const usableVoiceCount = state.voices.filter((v) => v.exists).length;

  const hint = document.getElementById("wizardReadyHint");
  if (hint) {
    if (state.importingBusy) {
      hint.className = "ready-summary";
      hint.textContent = state.importingLabel || "正在导入形象视频并提取声音...";
    } else if (!hasModel) {
      hint.className = "ready-summary";
      hint.textContent = "请先在左侧选择或添加一位出镜形象";
    } else if (!hasContent) {
      hint.className = "ready-summary";
      hint.textContent = state.createMode === "tts"
        ? `已选形象「${selModel.name}」 · 请输入口播文案`
        : `已选形象「${selModel.name}」 · 请选择至少一条录音`;
    } else if (state.createMode === "tts" && !effectiveVoice) {
      hint.className = "ready-summary";
      hint.textContent = usableVoiceCount > 0
        ? `「${selModel.name}」无原声，请先在配音音色里选一个声音`
        : `「${selModel.name}」无原声，请先上传一段参考音色`;
    } else {
      hint.className = "ready-summary ready";
      if (state.createMode === "tts") {
        hint.textContent = `已就绪：${selModel.name} · ${text.length} 字（约 ${fmtDuration(Math.round(text.length / 4.2))}）`;
      } else {
        hint.textContent = `已就绪：${selModel.name} · ${audioCount} 条录音`;
      }
    }
  }

  const batchBtn = document.getElementById("btnBatchCreateAudios");
  if (batchBtn) {
    batchBtn.textContent = audioCount > 1 ? `批量生成 ${audioCount} 条视频` : "开始生成视频";
  }
}

function renderSelectOptions() {
  const validModels = getValidModels();
  const gf = document.getElementById("createModelGroupFilter");
  if (gf) {
    const curG = gf.value;
    gf.innerHTML = `<option value="all">全部分组</option><option value="ungrouped">未分组</option>` +
      state.modelGroups.map((g) => `<option value="${g.id}">${esc(g.name)} (${g.model_count})</option>`).join("");
    if (curG && (curG === "all" || curG === "ungrouped" || state.modelGroups.some((g) => String(g.id) === String(curG)))) {
      gf.value = curG;
    } else {
      gf.value = "all";
    }
  }

  [document.getElementById("jobCategorySelect"), document.getElementById("audioJobCategorySelect"), document.getElementById("voiceCategorySelect")]
    .filter(Boolean)
    .forEach((catSel) => {
      const curCat = catSel.value;
      catSel.innerHTML = `<option value="">不分类（默认目录）</option>` +
        state.categories.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
      if (curCat && state.categories.some((c) => String(c.id) === String(curCat))) catSel.value = curCat;
      else catSel.value = "";
    });

  const selModel = getSelectedValidModel();
  const usableVoices = state.voices.filter((v) => v.exists);
  let defaultLabel = "自动跟随形象原声";
  if (selModel) {
    if (selModel.audio_exists) defaultLabel = `跟随形象原声（${selModel.name}）`;
    else if (usableVoices.length > 0) defaultLabel = "该形象无原声，请选择音色";
    else defaultLabel = "该形象无原声，请先上传音色";
  }
  const voiceOpts = `<option value="">${esc(defaultLabel)}</option>` +
    usableVoices.map((v) => `<option value="${v.id}">${esc(v.display_name)}</option>`).join("");

  [document.getElementById("ttsVoiceSelect"), document.getElementById("voiceStudioVoiceSelect")]
    .filter(Boolean)
    .forEach((vSel) => {
      const curV = vSel.value || state.preferredVoiceId;
      vSel.innerHTML = voiceOpts;
      if (curV && usableVoices.some((v) => String(v.id) === String(curV))) {
        vSel.value = curV;
      } else {
        vSel.value = "";
      }
    });

  const presetOpts = `<option value="">默认风格预设</option>` +
    state.presets.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  [document.getElementById("ttsPresetSelect"), document.getElementById("createPresetSelect")]
    .filter(Boolean)
    .forEach((pSel) => {
      const curP = pSel.value;
      pSel.innerHTML = presetOpts;
      if (curP && state.presets.some((p) => String(p.id) === String(curP))) pSel.value = curP;
      else pSel.value = "";
    });

  const mg = document.getElementById("modelModalGroup");
  if (mg) {
    mg.innerHTML = `<option value="">不分组</option>` +
      state.modelGroups.map((g) => `<option value="${g.id}">${esc(g.name)}</option>`).join("");
  }

  const badge = document.getElementById("createModelCountBadge");
  if (badge) badge.textContent = `${validModels.length} 个`;
}

function handleThumbFallback(imgEl, previewUrl) {
  if (!imgEl || imgEl.dataset.fallbackDone === "1") return;
  imgEl.dataset.fallbackDone = "1";
  if (previewUrl) {
    const vid = document.createElement("video");
    vid.src = `${previewUrl}#t=0.1`;
    vid.preload = "metadata";
    vid.muted = true;
    vid.playsInline = true;
    imgEl.replaceWith(vid);
  } else {
    const ph = document.createElement("div");
    ph.className = "media-cover-placeholder";
    ph.textContent = "暂无封面";
    imgEl.replaceWith(ph);
  }
}

function handleCoverImgError(imgEl) {
  if (!imgEl || imgEl.dataset.fallbackDone === "1") return;
  imgEl.dataset.fallbackDone = "1";
  const ph = document.createElement("div");
  ph.className = "media-cover-placeholder";
  ph.textContent = "暂无封面";
  imgEl.replaceWith(ph);
}

function buildDiscoverHtml(unimportedList, compact = false) {
  if (!unimportedList.length || !isLocalBrowser()) return "";
  if (compact) {
    return `
      <details class="fold-panel" style="margin-top:12px;margin-bottom:0;">
        <summary>发现电脑里有 ${unimportedList.length} 个未导入拍摄视频</summary>
        <div class="fold-body" style="display:flex;flex-direction:column;gap:6px;">
          ${unimportedList.map((v) => `
            <div class="discover-row">
              <div style="min-width:0;flex:1;">
                <div class="avatar-name" title="${esc(v.filename)}">${esc(v.name)}</div>
                <div class="avatar-sub">${fmtSizeMb(v.size_mb)}</div>
              </div>
              <button class="btn btn-xs btn-primary" ${state.importingBusy ? "disabled" : ""} data-path="${esc(v.path)}" onclick="importDiscoveredVideoByPath(this.dataset.path)">导入</button>
            </div>`).join("")}
        </div>
      </details>`;
  }
  return `
    <div class="inline-status-box">
      <div style="font-weight:500;font-size:13px;">发现电脑里还有 ${unimportedList.length} 个现成拍摄视频，点一下即可导入</div>
      <div style="display:flex;flex-direction:column;gap:6px;">
        ${unimportedList.map((v) => `
          <div class="discover-row">
            <div style="min-width:0;flex:1;">
              <div class="avatar-name" title="${esc(v.filename)}">${esc(v.name)}</div>
              <div class="avatar-sub">${esc(v.filename)} · ${fmtSizeMb(v.size_mb)}</div>
            </div>
            <button class="btn btn-xs btn-primary" ${state.importingBusy ? "disabled" : ""} data-path="${esc(v.path)}" onclick="importDiscoveredVideoByPath(this.dataset.path)">导入</button>
          </div>`).join("")}
      </div>
    </div>`;
}

function toggleAvatarGalleryExpand() {
  state.avatarGalleryExpanded = !state.avatarGalleryExpanded;
  renderCreateModelList();
}

function renderSelectedAvatarBar() {
  const bar = document.getElementById("selectedAvatarBar");
  if (!bar) return;
  const sel = getSelectedValidModel();
  if (!sel) {
    bar.innerHTML = "";
    return;
  }
  bar.innerHTML = `
    <div class="selected-avatar-pill">
      <span>已选：${esc(sel.name)}</span>
      <button class="btn-link" onclick="previewModelVideo(${sel.id})">预览</button>
      ${sel.audio_exists ? `<button class="btn-link" onclick="playSelectedModelVoice()">听原声</button>` : ""}
    </div>`;
}

function playSelectedModelVoice() {
  const m = getSelectedValidModel();
  if (!m?.voice_preview_url) return toast("当前形象没有可试听的原声", "warning");
  const wrap = document.getElementById("stageModelVoiceWrap");
  const player = document.getElementById("stageModelVoicePlayer");
  if (wrap && player) {
    const isHidden = wrap.classList.contains("hidden");
    if (isHidden) {
      if (player.getAttribute("src") !== m.voice_preview_url) player.src = m.voice_preview_url;
      wrap.classList.remove("hidden");
      player.play().catch(() => {});
    } else {
      player.pause();
      wrap.classList.add("hidden");
    }
  }
}

function renderCreateModelList() {
  const list = document.getElementById("createModelList");
  const filterBar = document.getElementById("createModelFilterBar");
  const discoverWrap = document.getElementById("createDiscoverCompact");
  const missingWrap = document.getElementById("createMissingBanner");
  const importingBanner = document.getElementById("step1ImportingBanner");
  const validModels = getValidModels();
  const missingModels = getMissingModels();
  const unimportedDiscovered = state.discoveredVideos.filter((v) => !v.already_imported);

  if (importingBanner) {
    if (state.importingBusy) {
      importingBanner.classList.remove("hidden");
      importingBanner.textContent = state.importingLabel || "正在导入形象视频并提取原声...";
    } else {
      importingBanner.classList.add("hidden");
      importingBanner.textContent = "";
    }
  }

  if (filterBar) filterBar.classList.toggle("hidden", validModels.length <= 4);

  if (validModels.length === 0) {
    const local = isLocalBrowser();
    list.innerHTML = `
      <div class="empty-box" style="border:1px dashed var(--border-strong);border-radius:var(--radius-md);padding:24px 14px;">
        <div style="margin-bottom:10px;color:var(--text-primary);font-weight:500;">还没有可用的出镜形象</div>
        <div class="inline-row" style="justify-content:center;">
          ${local ? `<button class="btn btn-sm btn-primary" ${state.importingBusy ? "disabled" : ""} onclick="quickPickAndImportModel()">从电脑选视频</button>` : ""}
          <button class="btn btn-sm btn-secondary" ${state.importingBusy ? "disabled" : ""} onclick="document.getElementById('quickUploadVideoInput').click()">网页上传</button>
        </div>
      </div>`;
    if (discoverWrap) discoverWrap.innerHTML = buildDiscoverHtml(unimportedDiscovered, true);
  } else {
    const gf = document.getElementById("createModelGroupFilter")?.value || "all";
    const kw = (document.getElementById("createModelSearch")?.value || "").trim().toLowerCase();
    const filtered = validModels.filter((m) => {
      if (gf === "ungrouped" && m.group_id) return false;
      if (gf !== "all" && gf !== "ungrouped" && String(m.group_id) !== String(gf)) return false;
      if (kw && !(m.name || "").toLowerCase().includes(kw)) return false;
      return true;
    });

    const expBtn = document.getElementById("btnToggleAvatarExpand");
    if (expBtn) {
      const needToggle = filtered.length > 6;
      expBtn.classList.toggle("hidden", !needToggle);
      expBtn.textContent = state.avatarGalleryExpanded ? "收起为单排" : `展开全部 (${filtered.length})`;
    }

    if (!filtered.length) {
      list.innerHTML = `<div class="empty-box">没有匹配的出镜形象</div>`;
    } else {
      list.innerHTML = `<div class="avatar-gallery ${state.avatarGalleryExpanded ? "expanded" : ""}">
        ${filtered.map((m) => {
          const sel = m.id === state.selectedModelId;
          return `
            <div class="avatar-tile ${sel ? "selected" : ""}" onclick="selectModel(${m.id})">
              <div class="avatar-thumb">
                <img src="${esc(m.thumb_url)}" alt="${esc(m.name)}" loading="lazy" data-preview="${esc(m.preview_url)}" onerror="handleThumbFallback(this, this.dataset.preview)" />
                <span class="avatar-badge-selected" aria-hidden="true">${SVG_CHECK}</span>
                <button class="avatar-play-mini" title="预览视频" onclick="event.stopPropagation();previewModelVideo(${m.id})">${SVG_PLAY}</button>
              </div>
              <div class="avatar-info">
                <div class="avatar-name" title="${esc(m.name)}">${esc(m.name)}</div>
                <div class="avatar-sub">${fmtSizeMb(m.video_size_mb)} · ${m.audio_exists ? "有原声" : "无原声"}</div>
              </div>
            </div>`;
        }).join("")}
      </div>`;
    }
    if (discoverWrap) discoverWrap.innerHTML = buildDiscoverHtml(unimportedDiscovered, true);
  }

  if (missingModels.length > 0 && missingWrap) {
    missingWrap.classList.remove("hidden");
    missingWrap.innerHTML = `
      <div class="inline-row-between" style="margin-top:10px;padding:8px 12px;border-radius:var(--radius-sm);background:var(--warn-tint);font-size:12.5px;">
        <span style="color:var(--warn-text);">有 ${missingModels.length} 条历史形象视频缺失</span>
        <button class="btn-link" onclick="switchView('models')">去处理</button>
      </div>`;
  } else if (missingWrap) {
    missingWrap.classList.add("hidden");
    missingWrap.innerHTML = "";
  }
}

function selectModel(id) {
  state.selectedModelId = id;
  savePrefs({ selectedModelId: id });
  renderCreateModelList();
  renderSelectedAvatarBar();
  renderSelectOptions();
  updateVoicePreview();
  updateWizardIndicators();
}

function setImportingState(busy, label = "") {
  state.importingBusy = busy;
  state.importingLabel = label;
  const pickBtn = document.getElementById("btnQuickPickVideo");
  if (pickBtn) pickBtn.disabled = busy;
  renderCreateModelList();
  renderSelectedAvatarBar();
  updateWizardIndicators();
}

async function pickLocalFileSafe(kind = "video", multi = false) {
  if (!isLocalBrowser()) return { remote: true, cancelled: true, paths: [] };
  if (state.pickingFile) {
    toast("文件选择窗口已经打开，请在桌面或任务栏查看", "info", 2800);
    return { busy: true, cancelled: true, paths: [] };
  }
  state.pickingFile = true;
  try {
    const res = await api("/api/v1/system/pick-file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, multi }),
      timeoutMs: 120000,
    });
    return { cancelled: Boolean(res.cancelled), paths: res.paths || [] };
  } catch (err) {
    if (err.httpStatus === 409) {
      toast(err.message || "文件选择窗口已打开", "info", 2800);
      return { busy: true, cancelled: true, paths: [] };
    }
    if (err.httpStatus === 403) return { remote: true, cancelled: true, paths: [] };
    throw err;
  } finally {
    state.pickingFile = false;
  }
}

async function quickPickAndImportModel() {
  if (state.importingBusy) return toast("正在导入视频，请稍候...", "info");
  try {
    const pick = await pickLocalFileSafe("video", false);
    if (pick.remote) { openAddModelModal(); return; }
    if (pick.cancelled || !pick.paths?.length) return;
    await importModelByLocalPath(pick.paths[0], fileStemFromPath(pick.paths[0]));
  } catch (err) {
    toast(`无法打开选文件窗口，已切换为添加弹窗：${err.message}`, "warning", 4200);
    openAddModelModal();
  }
}

async function importDiscoveredVideoByPath(targetPath) {
  if (!targetPath || state.importingBusy) return;
  await importModelByLocalPath(targetPath, fileStemFromPath(targetPath));
}

async function importModelByLocalPath(localPath, autoName) {
  const name = autoName || fileStemFromPath(localPath) || "新形象";
  setImportingState(true, `正在导入「${name}」并提取声音...`);
  toast(`正在导入「${name}」并提取原声...`, "info", 4000);
  try {
    const fd = new FormData();
    fd.append("name", name);
    fd.append("local_video_path", localPath);
    const res = await api("/api/v1/models", { method: "POST", body: fd, timeoutMs: 300000 });
    if (res?.item?.id) {
      state.selectedModelId = res.item.id;
      savePrefs({ selectedModelId: res.item.id });
    }
    toast(`形象「${res?.item?.name || name}」添加完成`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`导入失败: ${err.message}`, "error", 6000);
  } finally {
    setImportingState(false);
  }
}

async function handleQuickUploadModelFile(file) {
  if (!file) return;
  const inputEl = document.getElementById("quickUploadVideoInput");
  if (inputEl) inputEl.value = "";
  if (state.importingBusy) return toast("正在导入视频，请稍候...", "info");
  const name = file.name.replace(/\.[^.]+$/, "").trim() || "新形象";
  setImportingState(true, `正在上传「${name}」并提取声音...`);
  toast(`正在上传「${name}」，请勿关闭页面...`, "info", 4200);
  try {
    const fd = new FormData();
    fd.append("name", name);
    fd.append("file", file);
    const res = await api("/api/v1/models", { method: "POST", body: fd, timeoutMs: 300000 });
    if (res?.item?.id) {
      state.selectedModelId = res.item.id;
      savePrefs({ selectedModelId: res.item.id });
    }
    toast(`形象「${res?.item?.name || name}」上传完成`, "success");
    await refreshAll(true, true);
  } catch (err) {
    toast(`上传失败: ${err.message}`, "error", 6000);
  } finally {
    setImportingState(false);
  }
}

async function quickRelinkModelFromDisk(modelId) {
  const m = state.models.find((x) => x.id === modelId);
  if (!m) return;
  if (!isLocalBrowser()) { openRelinkModelModal(m.id, m.name || `#${m.id}`); return; }
  try {
    const pick = await pickLocalFileSafe("video", false);
    if (pick.remote) { openRelinkModelModal(m.id, m.name || `#${m.id}`); return; }
    if (pick.cancelled || !pick.paths?.length) return;
    toast(`正在为「${m.name}」补回原视频...`, "info", 3600);
    const fd = new FormData();
    fd.append("local_video_path", pick.paths[0]);
    const res = await api(`/api/v1/models/${modelId}/relink`, { method: "POST", body: fd, timeoutMs: 300000 });
    if (res?.item?.id) state.selectedModelId = res.item.id;
    toast(`形象「${m.name}」原视频已补回`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`补回失败: ${err.message}`, "error", 5500);
  }
}

async function handleCleanupMissingModels() {
  const missing = getMissingModels();
  if (!missing.length) return toast("当前没有缺失形象记录", "info");
  if (!confirm(`确定清理这 ${missing.length} 条原视频已缺失的历史形象记录吗？（不会删除已有成片）`)) return;
  try {
    const res = await api("/api/v1/models/cleanup-missing", { method: "POST" });
    toast(`已清理 ${Number(res.deleted_count ?? 0)} 条失效记录`, "success", 4000);
    await refreshAll(true, true);
  } catch (err) {
    toast(`清理失败: ${err.message}`, "error");
  }
}

// ==================== 做视频 & 配声音：文案、音色与调音双向同步 ====================

function clearScriptInput() {
  const a = document.getElementById("ttsTextInput");
  const b = document.getElementById("voiceTextInput");
  if (a) a.value = "";
  if (b) b.value = "";
  updateTextStats();
  updateWizardIndicators();
}

function syncTextBetweenViews(source) {
  const createEl = document.getElementById("ttsTextInput");
  const voiceEl = document.getElementById("voiceTextInput");
  if (!createEl || !voiceEl) return;
  if (source === "create" && voiceEl.value !== createEl.value) voiceEl.value = createEl.value;
  else if (source === "voice" && createEl.value !== voiceEl.value) createEl.value = voiceEl.value;
  updateTextStats();
}

function onScriptInputChanged() {
  syncTextBetweenViews("create");
  updateWizardIndicators();
}

function onVoiceTextInputChanged() {
  syncTextBetweenViews("voice");
  updateWizardIndicators();
}

function updateTextStats() {
  const txt = (document.getElementById("ttsTextInput")?.value || "").trim();
  const estSec = Math.round(txt.length / 4.2);
  const label = `${txt.length} 字 · 约 ${txt.length === 0 ? "0 秒" : fmtDuration(Math.max(1, estSec))}`;
  const s1 = document.getElementById("textStats");
  const s2 = document.getElementById("voiceTextStats");
  if (s1) s1.textContent = label;
  if (s2) s2.textContent = label;
}

function onCreateVoiceSelectChanged() {
  const val = document.getElementById("ttsVoiceSelect")?.value || "";
  state.preferredVoiceId = val;
  savePrefs({ selectedVoiceId: val });
  const v2 = document.getElementById("voiceStudioVoiceSelect");
  if (v2 && v2.value !== val) v2.value = val;
  updateVoicePreview();
  updateVoiceStudioRefPreview();
}

function onVoiceStudioVoiceChanged() {
  const val = document.getElementById("voiceStudioVoiceSelect")?.value || "";
  state.preferredVoiceId = val;
  savePrefs({ selectedVoiceId: val });
  const v1 = document.getElementById("ttsVoiceSelect");
  if (v1 && v1.value !== val) v1.value = val;
  updateVoicePreview();
  updateVoiceStudioRefPreview();
}

function resolveRefVoicePreviewUrl(selectId) {
  const vid = document.getElementById(selectId)?.value;
  if (vid) {
    const v = state.voices.find((x) => String(x.id) === String(vid));
    return v?.preview_url || "";
  }
  return getSelectedValidModel()?.voice_preview_url || "";
}

function updateVoicePreview() {
  const player = document.getElementById("voiceRefPreview");
  const btn = document.getElementById("btnToggleVoicePreview");
  const wrap = document.getElementById("voicePreviewWrap");
  if (!player) return;
  const previewUrl = resolveRefVoicePreviewUrl("ttsVoiceSelect");
  if (previewUrl) {
    if (player.getAttribute("src") !== previewUrl) player.src = previewUrl;
    if (btn) btn.disabled = false;
  } else {
    player.pause();
    player.removeAttribute("src");
    wrap?.classList.add("hidden");
    if (btn) btn.disabled = true;
  }
  updateWizardIndicators();
}

function updateVoiceStudioRefPreview() {
  const player = document.getElementById("voiceStudioRefPlayer");
  const btn = document.getElementById("btnVoiceStudioPlayRef");
  const wrap = document.getElementById("voiceStudioRefWrap");
  if (!player) return;
  const previewUrl = resolveRefVoicePreviewUrl("voiceStudioVoiceSelect");
  if (previewUrl) {
    if (player.getAttribute("src") !== previewUrl) player.src = previewUrl;
    if (btn) btn.disabled = false;
  } else {
    player.pause();
    player.removeAttribute("src");
    wrap?.classList.add("hidden");
    if (btn) btn.disabled = true;
  }
}

function toggleVoicePreviewPlayer() {
  const wrap = document.getElementById("voicePreviewWrap");
  const player = document.getElementById("voiceRefPreview");
  if (!wrap || !player) return;
  if (!player.getAttribute("src")) return toast("当前没有可试听的参考原声", "warning");
  const isHidden = wrap.classList.contains("hidden");
  wrap.classList.toggle("hidden", !isHidden);
  if (isHidden) player.play().catch(() => {});
  else player.pause();
}

function toggleVoiceStudioRefPlayer() {
  const wrap = document.getElementById("voiceStudioRefWrap");
  const player = document.getElementById("voiceStudioRefPlayer");
  if (!wrap || !player) return;
  if (!player.getAttribute("src")) return toast("当前没有可试听的参考原声", "warning");
  const isHidden = wrap.classList.contains("hidden");
  wrap.classList.toggle("hidden", !isHidden);
  if (isHidden) player.play().catch(() => {});
  else player.pause();
}

function setCreateMode(mode, persist = true) {
  state.createMode = mode;
  if (persist) savePrefs({ createMode: mode });
  document.querySelectorAll("#createModeTabs button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  document.getElementById("createModeTts")?.classList.toggle("hidden", mode !== "tts");
  document.getElementById("createModeAudio")?.classList.toggle("hidden", mode !== "audio");
  document.getElementById("actionBtnsTts")?.classList.toggle("hidden", mode !== "tts");
  document.getElementById("actionBtnsAudio")?.classList.toggle("hidden", mode !== "audio");
  updateWizardIndicators();
}

function setEmoMode(mode) {
  state.emoMode = mode;
  document.querySelectorAll("#emoModeTabs button, #createEmoModeTabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.emo === mode);
  });
  document.getElementById("emoAlphaWrap")?.classList.toggle("hidden", mode === "same_as_speaker");
  document.getElementById("emoVectorBox")?.classList.toggle("hidden", mode !== "emo_vector");
  document.getElementById("emoAudioBox")?.classList.toggle("hidden", mode !== "emo_audio");
  document.getElementById("emoTextBox")?.classList.toggle("hidden", mode !== "emo_text");
  updateEmoSumHint();
  syncTuneSummaryAll();
}

function syncSpeedFromCreate(val) {
  const num = Number(val);
  const cEl = document.getElementById("createSpeedRange");
  const vEl = document.getElementById("ttsSpeed");
  if (cEl && String(cEl.value) !== String(num)) cEl.value = String(num);
  if (vEl && String(vEl.value) !== String(num)) vEl.value = String(num);
  const s1 = document.getElementById("createValSpeed");
  const s2 = document.getElementById("valSpeed");
  if (s1) s1.textContent = `${num.toFixed(2)}x`;
  if (s2) s2.textContent = `${num.toFixed(2)}x`;
  syncTuneSummaryAll();
}

function syncSpeedFromVoiceStudio(val) {
  syncSpeedFromCreate(val);
}

function onTtsModelVersionChange() {
  const v = document.getElementById("ttsModelVersion")?.value || "IndexTTS-2";
  const langField = document.getElementById("ttsLangField");
  if (langField) langField.style.opacity = v === "IndexTTS-2.5" ? "1" : "0.5";
  syncTuneSummaryAll();
}

function syncRangeVal(inputId, spanId, suffix) {
  const el = document.getElementById(inputId);
  const sp = document.getElementById(spanId);
  if (!el || !sp) return;
  const v = Number(el.value);
  const text = Number.isInteger(v) && suffix !== "x"
    ? String(v)
    : v.toFixed(suffix === "x" || inputId.includes("Temp") || inputId.includes("TopP") || inputId.includes("Alpha") ? 2 : 1);
  sp.textContent = `${text}${suffix}`;
}

function syncTuneSummaryAll() {
  const speed = Number(document.getElementById("ttsSpeed")?.value || 1.0).toFixed(2);
  const emoNames = {
    same_as_speaker: "跟随原声",
    emo_vector: "8维情绪",
    emo_audio: "参考音频",
    emo_text: "情绪描述",
  };
  const presetSel = document.getElementById("ttsPresetSelect");
  const presetText = presetSel && presetSel.value ? presetSel.options[presetSel.selectedIndex]?.text : "";
  const emoLabel = emoNames[state.emoMode] || "跟随原声";

  const tuneSum = document.getElementById("ttsTuneSummary");
  if (tuneSum) {
    const parts = [];
    if (presetText) parts.push(presetText);
    parts.push(`语速 ${speed}x`);
    parts.push(emoLabel);
    tuneSum.textContent = parts.join(" · ");
  }

  const badge = document.getElementById("createTuneBadge");
  if (badge) badge.textContent = `${emoLabel} · ${speed}x`;

  const emoHint = document.getElementById("createEmoHint");
  if (emoHint) {
    if (state.emoMode === "same_as_speaker") {
      emoHint.textContent = "自动保持参考人声的自然语气。";
    } else if (state.emoMode === "emo_vector") {
      const vec = EMO_LABELS.map((lb, i) => ({ lb, v: Number(document.getElementById(`emoSlider${i}`)?.value || 0) }))
        .filter((x) => x.v > 0)
        .map((x) => `${x.lb} ${x.v.toFixed(2)}`);
      emoHint.textContent = vec.length ? `已调情绪：${vec.join("、")}` : "尚未调整 8 维情绪滑块，可点上方「去配声音精细调音」。";
    } else if (state.emoMode === "emo_audio") {
      const p = cleanLocalPath(document.getElementById("ttsEmoAudioPath")?.value);
      emoHint.textContent = p ? `已选情感音频：${fileStemFromPath(p)}` : "还未选择情感参考音频，请去「配声音」选择。";
    } else if (state.emoMode === "emo_text") {
      const t = (document.getElementById("ttsEmoText")?.value || "").trim();
      emoHint.textContent = t ? `情绪描述：「${t.slice(0, 26)}」` : "还未输入情绪描述词，请去「配声音」填写。";
    }
  }
}

function syncPostprocessFromCreate(val) {
  const mode = val || "fixB";
  const a = document.getElementById("createPostprocessMode");
  const b = document.getElementById("ttsPostprocessMode");
  if (a) a.value = mode;
  if (b) b.value = mode;
  syncTuneSummaryAll();
}

function syncPostprocessFromStudio(val) {
  syncPostprocessFromCreate(val);
}

function syncPersonalVoiceFromCreate(val) {
  const v = val || "";
  const a = document.getElementById("createPersonalVoiceSelect");
  const b = document.getElementById("ttsPersonalVoiceSelect");
  if (a) a.value = v;
  if (b) b.value = v;
  syncTuneSummaryAll();
}

function syncPersonalVoiceFromStudio(val) {
  syncPersonalVoiceFromCreate(val);
}

function populateRegisteredPersonalVoices(list) {
  const items = Array.isArray(list) ? list : [];
  state.registeredVoices = items;
  const html = `<option value="">按数字人绑定自动匹配</option>` +
    items.map((v) => `<option value="${esc(v.cfg_path || "")}">${esc(v.label || v.display_name)}（已就绪）</option>`).join("");
  ["createPersonalVoiceSelect", "ttsPersonalVoiceSelect"].forEach((id) => {
    const sel = document.getElementById(id);
    if (!sel) return;
    const prev = sel.value || "";
    sel.innerHTML = html;
    if (prev && items.some((x) => String(x.cfg_path || "") === prev)) sel.value = prev;
    else sel.value = "";
  });
}

function collectTtsParams() {
  const vec = EMO_LABELS.map((_, i) => Number(document.getElementById(`emoSlider${i}`)?.value || 0));
  const ppMode = document.getElementById("ttsPostprocessMode")?.value || document.getElementById("createPostprocessMode")?.value || "fixB";
  const chosenCfg = (document.getElementById("ttsPersonalVoiceSelect")?.value || document.getElementById("createPersonalVoiceSelect")?.value || "").trim();
  const out = {
    model_version: document.getElementById("ttsModelVersion").value,
    lang: document.getElementById("ttsLang").value,
    speed: Number(document.getElementById("ttsSpeed").value),
    postprocess_mode: ppMode,
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
  if (chosenCfg) out.cfg_path = chosenCfg;
  return out;
}

function applyTtsParamsToForm(p) {
  if (!p) return;
  if (p.model_version) document.getElementById("ttsModelVersion").value = p.model_version;
  if (p.lang) document.getElementById("ttsLang").value = p.lang;
  if (p.postprocess_mode) syncPostprocessFromCreate(p.postprocess_mode);
  if (hasVal(p.speed)) syncSpeedFromCreate(p.speed);
  if (p.emo_mode) setEmoMode(p.emo_mode);
  if (hasVal(p.emo_alpha)) { document.getElementById("ttsEmoAlpha").value = p.emo_alpha; syncRangeVal("ttsEmoAlpha", "valEmoAlpha", ""); }
  if (Array.isArray(p.emo_vector) && p.emo_vector.length === 8) {
    p.emo_vector.forEach((val, i) => {
      const sl = document.getElementById(`emoSlider${i}`);
      const lb = document.getElementById(`emoVal${i}`);
      if (sl) sl.value = val;
      if (lb) lb.textContent = Number(val).toFixed(2);
    });
  }
  if (hasVal(p.emo_audio_prompt)) document.getElementById("ttsEmoAudioPath").value = p.emo_audio_prompt;
  if (hasVal(p.emo_text)) document.getElementById("ttsEmoText").value = p.emo_text;
  if (hasVal(p.diffusion_steps)) { document.getElementById("ttsSteps").value = p.diffusion_steps; syncRangeVal("ttsSteps", "valSteps", ""); }
  if (hasVal(p.num_beams)) { document.getElementById("ttsBeams").value = p.num_beams; syncRangeVal("ttsBeams", "valBeams", ""); }
  if (hasVal(p.interval_silence)) { document.getElementById("ttsSilence").value = p.interval_silence; syncRangeVal("ttsSilence", "valSilence", " 毫秒"); }
  if (hasVal(p.temperature)) { document.getElementById("ttsTemp").value = p.temperature; syncRangeVal("ttsTemp", "valTemp", ""); }
  if (hasVal(p.top_p)) { document.getElementById("ttsTopP").value = p.top_p; syncRangeVal("ttsTopP", "valTopP", ""); }
  if (hasVal(p.top_k)) { document.getElementById("ttsTopK").value = p.top_k; syncRangeVal("ttsTopK", "valTopK", ""); }
  if (hasVal(p.repetition_penalty)) { document.getElementById("ttsRep").value = p.repetition_penalty; syncRangeVal("ttsRep", "valRep", ""); }
  if (hasVal(p.chunk_size)) { document.getElementById("ttsChunk").value = p.chunk_size; syncRangeVal("ttsChunk", "valChunk", ""); }
  if (hasVal(p.auto_fast_on_long_text)) document.getElementById("ttsAutoFast").checked = Boolean(p.auto_fast_on_long_text);
  onTtsModelVersionChange();
  updateEmoSumHint();
  syncTuneSummaryAll();
}

function applyPresetById(pid) {
  const s1 = document.getElementById("ttsPresetSelect");
  const s2 = document.getElementById("createPresetSelect");
  if (s1) s1.value = pid || "";
  if (s2) s2.value = pid || "";
  const preset = state.presets.find((x) => String(x.id) === String(pid));
  if (preset) {
    applyTtsParamsToForm(preset.params);
    toast(`已套用预设：${preset.name}`, "success", 2200);
  } else {
    syncTuneSummaryAll();
  }
}

function applySelectedPreset() {
  applyPresetById(document.getElementById("ttsPresetSelect")?.value || "");
}

async function saveCurrentAsPreset() {
  const name = prompt("给这套配音风格起个名字：", "常用口播风格");
  if (!name || !name.trim()) return;
  try {
    const res = await api("/api/v1/tts/presets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), params: collectTtsParams() }),
    });
    toast(`已保存预设「${name.trim()}」`, "success");
    await refreshAll(true);
    if (res?.item?.id) applyPresetById(String(res.item.id));
  } catch (err) {
    toast(`保存预设失败: ${err.message}`, "error");
  }
}

async function deleteSelectedPreset() {
  const sel = document.getElementById("ttsPresetSelect");
  const pid = sel?.value;
  if (!pid) return toast("请先选择一个要删除的预设", "warning");
  const preset = state.presets.find((x) => String(x.id) === String(pid));
  const label = preset ? `「${preset.name}」` : `#${pid}`;
  if (!confirm(`确定删除预设 ${label} 吗？`)) return;
  try {
    await api(`/api/v1/tts/presets/${pid}`, { method: "DELETE" });
    sel.value = "";
    if (document.getElementById("createPresetSelect")) document.getElementById("createPresetSelect").value = "";
    toast(`已删除预设 ${label}`, "success");
    await refreshAll(true);
    syncTuneSummaryAll();
  } catch (err) {
    toast(`删除失败: ${err.message}`, "error");
  }
}

async function saveAsGlobalTtsConfig() {
  const p = collectTtsParams();
  const mapped = {
    index_tts_model_version: p.model_version,
    index_tts_lang: p.lang,
    index_tts_speed: p.speed,
    index_tts_postprocess_mode: p.postprocess_mode || "fixB",
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
    toast("已保存为默认配音参数", "success");
  } catch (err) {
    toast(`保存失败: ${err.message}`, "error");
  }
}

async function pickEmoAudioFile() {
  try {
    const pick = await pickLocalFileSafe("audio", false);
    if (pick.remote) { document.getElementById("emoAudioFileInput")?.click(); return; }
    if (pick.cancelled || !pick.paths?.length) return;
    document.getElementById("ttsEmoAudioPath").value = pick.paths[0];
    syncTuneSummaryAll();
    toast("已选择情感参考音频", "success", 2200);
  } catch (err) {
    toast(`切为网页上传：${err.message}`, "warning", 3500);
    document.getElementById("emoAudioFileInput")?.click();
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
    syncTuneSummaryAll();
    toast(`情感音频上传成功: ${res.filename}`, "success");
  } catch (err) {
    toast(`上传失败: ${err.message}`, "error");
  }
  input.value = "";
}

function getEffectiveVoiceId() {
  const chosen = document.getElementById("ttsVoiceSelect")?.value || document.getElementById("voiceStudioVoiceSelect")?.value;
  if (chosen) return Number(chosen);
  const model = getSelectedValidModel();
  return model?.voice_id && model?.audio_exists ? Number(model.voice_id) : null;
}

// ==================== 配音合成 ====================

async function runTtsSynthesis({ source }) {
  const text = (document.getElementById(source === "voice" ? "voiceTextInput" : "ttsTextInput")?.value || "").trim();
  if (!text) return toast("请先输入口播文案", "warning");
  const voiceId = getEffectiveVoiceId();
  if (!voiceId) return toast("请先选择参考音色，或先选一位含原声的出镜形象", "warning");
  const ttsParams = collectTtsParams();
  if (ttsParams.emo_mode === "emo_audio" && !ttsParams.emo_audio_prompt) {
    return toast("请先选择一段情感参考音频", "warning");
  }

  const btns = [document.getElementById("btnSynthesizeTts"), document.getElementById("btnVoiceStudioSynthesize")].filter(Boolean);
  const liveBoxes = [document.getElementById("ttsLiveBox"), document.getElementById("voiceLiveBox")].filter(Boolean);
  const liveMsgs = [document.getElementById("ttsLiveMsg"), document.getElementById("voiceLiveMsg")].filter(Boolean);
  const liveBars = [document.getElementById("ttsLiveBar"), document.getElementById("voiceLiveBar")].filter(Boolean);

  btns.forEach((b) => (b.disabled = true));
  liveBoxes.forEach((el) => el.classList.remove("hidden"));
  liveMsgs.forEach((el) => (el.textContent = "正在合成配音..."));
  liveBars.forEach((el) => (el.style.width = "8%"));

  clearInterval(state.ttsPollingTimer);
  state.ttsPollingTimer = setInterval(async () => {
    try {
      const st = await api("/api/v1/tts/status");
      const d = st.data || {};
      if (d.message) {
        const line = `${d.message}${d.elapsed_seconds ? `（已用 ${d.elapsed_seconds} 秒）` : ""}`;
        liveMsgs.forEach((el) => (el.textContent = line));
      }
      if (d.progress) {
        const pct = `${Math.max(8, d.progress)}%`;
        liveBars.forEach((el) => (el.style.width = pct));
      }
    } catch {}
  }, 1200);

  try {
    const nameInput = source === "voice"
      ? (document.getElementById("voiceAssetNameInput")?.value || "").trim()
      : (document.getElementById("jobNameInput")?.value || "").trim();
    const catVal = source === "voice"
      ? document.getElementById("voiceCategorySelect")?.value
      : document.getElementById("jobCategorySelect")?.value;

    const res = await api("/api/v1/tts/synthesize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        voice_id: voiceId,
        tts_params: ttsParams,
        name: nameInput || null,
        category_id: catVal ? Number(catVal) : null,
      }),
    });
    const item = res.item;
    state.lastAuditionAsset = item;

    const audTitle = document.getElementById("auditionTitle");
    const audPlayer = document.getElementById("auditionPlayer");
    const audDl = document.getElementById("auditionDownload");
    if (audTitle) audTitle.textContent = `${item.name} (${fmtDuration(item.duration)})`;
    if (audPlayer) audPlayer.src = item.stream_url;
    if (audDl) audDl.href = item.download_url;
    document.getElementById("ttsAuditionResult")?.classList.remove("hidden");

    const vTitle = document.getElementById("voiceLatestTitle");
    const vPlayer = document.getElementById("voiceLatestPlayer");
    const vDl = document.getElementById("voiceLatestDownload");
    if (vTitle) vTitle.textContent = `最新合成：${item.name} (${fmtDuration(item.duration)})`;
    if (vPlayer) vPlayer.src = item.stream_url;
    if (vDl) vDl.href = item.download_url;
    document.getElementById("voiceLatestCard")?.classList.remove("hidden");

    toast("配音已生成，满意可直接用于做视频", "success");
    await refreshAll(true);
  } catch (err) {
    toast(`语音合成失败: ${err.message}`, "error", 6000);
  } finally {
    clearInterval(state.ttsPollingTimer);
    btns.forEach((b) => (b.disabled = false));
    liveBoxes.forEach((el) => el.classList.add("hidden"));
  }
}

async function handleSynthesizeTts() { await runTtsSynthesis({ source: "create" }); }
async function handleVoiceStudioSynthesize() { await runTtsSynthesis({ source: "voice" }); }

async function handleCancelTts() {
  try {
    const res = await api("/api/v1/tts/cancel", { method: "POST" });
    toast(res.msg || "已中断配音", "warning");
  } catch (err) {
    toast(`中断失败: ${err.message}`, "error");
  }
}

function useLatestVoiceAssetForVideo() {
  if (!state.lastAuditionAsset) return toast("暂无最新合成音频", "warning");
  quickVideoFromAudio(state.lastAuditionAsset.id);
}

function formatAudioSourceLabel(sourceType) {
  if (sourceType === "tts" || sourceType === "indextts2") return "文字配音";
  if (sourceType === "local_import") return "本机导入";
  return "网页上传";
}

function renderVoiceHistoryList() {
  const wrap = document.getElementById("voiceHistoryList");
  const badge = document.getElementById("voiceAudioCountBadge");
  if (!wrap) return;
  if (badge) badge.textContent = `${state.audios.length} 条`;

  const kw = (document.getElementById("voiceHistorySearch")?.value || "").trim().toLowerCase();
  const items = state.audios.filter((a) => !kw || `${a.name} ${a.text_content}`.toLowerCase().includes(kw));

  if (!items.length) {
    wrap.innerHTML = `<div class="empty-box">暂无已合成或导入的音频</div>`;
    return;
  }

  const local = isLocalBrowser();
  wrap.innerHTML = items
    .map(
      (a) => `
      <div class="list-row-item">
        <div class="inline-row-between">
          <strong style="font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;" title="${esc(a.name)}">${esc(a.name)}</strong>
          <span class="state-tag st-muted">${formatAudioSourceLabel(a.source_type)} · ${fmtDuration(a.duration)}</span>
        </div>
        ${a.text_content ? `<div class="muted-text" style="font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(a.text_content)}">${esc(a.text_content)}</div>` : ""}
        ${a.stream_url ? `<audio src="${esc(a.stream_url)}" class="audio-player" controls></audio>` : `<div class="muted-text" style="color:var(--danger);">音频文件已缺失</div>`}
        <div class="inline-row">
          <button class="btn btn-xs btn-primary" onclick="quickVideoFromAudio(${a.id})">去做视频</button>
          ${a.download_url ? `<a class="btn btn-xs btn-secondary" href="${esc(a.download_url)}" download>下载</a>` : ""}
          <button class="btn btn-xs btn-ghost" onclick="renameAudioAsset(${a.id})">改名</button>
          ${local ? `<button class="btn btn-xs btn-ghost" onclick="openAudioPath(${a.id})">定位</button>` : ""}
          <button class="btn btn-xs btn-danger-ghost" onclick="deleteAudioAsset(${a.id})">删除</button>
        </div>
      </div>`
    )
    .join("");
}

// ==================== 提交视频生成 ====================

function notifyPostSubmit(jobId, extraText = "") {
  trackSessionJobId(jobId);
  if (!isEngineReady()) {
    toast(`任务 #${jobId} 已加入队列。本机引擎未启动，请先点顶部「一键启动服务」`, "warning", 7000);
  } else {
    toast(`已开始制作（任务 #${jobId}）${extraText}`, "success", 4200);
  }
}

async function createVideoFromAudition() {
  if (!state.lastAuditionAsset) return toast("暂无可复用的试听音频", "warning");
  const model = getSelectedValidModel();
  if (!model) return toast("请先在左侧选择一位出镜形象", "warning");

  const btn = document.getElementById("btnAuditionToVideo");
  if (btn) btn.disabled = true;
  const catVal = document.getElementById("jobCategorySelect").value;
  try {
    const res = await api("/api/v1/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model_id: model.id,
        name: (document.getElementById("jobNameInput").value || "").trim() || state.lastAuditionAsset.name,
        audio_asset_id: state.lastAuditionAsset.id,
        category_id: catVal ? Number(catVal) : null,
      }),
    });
    notifyPostSubmit(res.job_id, "（直接复用试听音频）");
    await refreshAll(true);
  } catch (err) {
    toast(`创建失败: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleSubmitTtsVideo() {
  const model = getSelectedValidModel();
  if (!model) return toast("请先在左侧选择一位出镜形象", "warning");
  const text = (document.getElementById("ttsTextInput").value || "").trim();
  if (!text) {
    toast("请先输入口播文案", "warning");
    document.getElementById("ttsTextInput")?.focus();
    return;
  }
  const voiceId = getEffectiveVoiceId();
  if (!voiceId) return toast("当前形象无原声，请先选择一个配音音色", "warning", 5000);
  const ttsParams = collectTtsParams();
  if (ttsParams.emo_mode === "emo_audio" && !ttsParams.emo_audio_prompt) {
    return toast("请先前往「配声音」选择情感参考音频", "warning");
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
        model_id: model.id,
        name: (document.getElementById("jobNameInput").value || "").trim() || null,
        text,
        voice_id: voiceVal ? Number(voiceVal) : null,
        tts_params: ttsParams,
        category_id: catVal ? Number(catVal) : null,
      }),
    });
    document.getElementById("jobNameInput").value = "";
    notifyPostSubmit(res.job_id);
    await refreshAll(true);
  } catch (err) {
    toast(`提交失败: ${err.message}`, "error", 5500);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function pickAndImportLocalAudios() {
  const btn = document.getElementById("btnPickLocalAudios");
  if (btn) { btn.disabled = true; btn.textContent = "等待选择..."; }
  let fallbackUpload = false;
  try {
    const pick = await pickLocalFileSafe("audio", true);
    if (pick.remote) { fallbackUpload = true; return; }
    if (pick.cancelled || !pick.paths?.length) return;
    if (btn) btn.textContent = "导入中...";
    const catVal = document.getElementById("jobCategorySelect").value;
    const res = await api("/api/v1/audios/import-local", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: pick.paths, category_id: catVal ? Number(catVal) : null }),
    });
    (res.items || []).forEach((it) => state.selectedAudioIds.add(it.id));
    toast(`已导入并勾选 ${res.count} 条录音`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`选录音失败，已切为网页上传：${err.message}`, "warning", 4500);
    fallbackUpload = true;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "从电脑选录音"; }
    if (fallbackUpload) document.getElementById("batchAudioInput")?.click();
  }
}

async function handleUploadBatchAudios(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  const catVal = document.getElementById("jobCategorySelect").value;
  let okCnt = 0;
  for (const f of files) {
    const fd = new FormData();
    fd.append("files", f);
    if (catVal) fd.append("category_id", catVal);
    try {
      const res = await api("/api/v1/audios/upload", { method: "POST", body: fd, timeoutMs: 180000 });
      const created = Array.isArray(res?.items) ? res.items : (res?.item ? [res.item] : []);
      created.forEach((it) => { if (it?.id) state.selectedAudioIds.add(it.id); });
      okCnt += created.length || 1;
    } catch (err) {
      toast(`上传 ${f.name} 失败: ${err.message}`, "error");
    }
  }
  if (okCnt > 0) {
    toast(`已上传并勾选 ${okCnt} 条录音`, "success");
    await refreshAll(true);
  }
  const inp = document.getElementById("batchAudioInput");
  if (inp) inp.value = "";
}

async function handleImportLocalAudioPaths() {
  const ta = document.getElementById("localAudioPathInput");
  const lines = (ta?.value || "").split(/\r?\n/).map((ln) => cleanLocalPath(ln)).filter(Boolean);
  if (!lines.length) return toast("请先粘贴至少一条本机音频路径", "warning");
  const btn = document.getElementById("btnImportLocalAudio");
  if (btn) btn.disabled = true;
  const catVal = document.getElementById("jobCategorySelect").value;
  try {
    const res = await api("/api/v1/audios/import-local", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: lines, category_id: catVal ? Number(catVal) : null }),
    });
    (res.items || []).forEach((it) => state.selectedAudioIds.add(it.id));
    ta.value = "";
    toast(`已导入并勾选 ${res.count} 条录音`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`导入失败: ${err.message}`, "error", 5500);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderCreateAudioLibrary() {
  const box = document.getElementById("createAudioLibraryList");
  if (!box) return;
  const countEl = document.getElementById("selectedAudioCount");
  if (countEl) countEl.textContent = `已选 ${state.selectedAudioIds.size} 条`;

  if (!state.audios.length) {
    box.innerHTML = `<div class="empty-box" style="padding:18px 10px;">暂无已导入录音，拖入音频文件或在「配声音」生成即可。</div>`;
    return;
  }

  box.innerHTML = state.audios
    .map((a) => {
      const checked = state.selectedAudioIds.has(a.id);
      return `
        <div class="audio-pick-row ${checked ? "selected" : ""}" onclick="toggleSelectAudio(${a.id})">
          <div class="inline-row-nowrap" style="min-width:0;flex:1;">
            <input type="checkbox" ${checked ? "checked" : ""} onclick="event.stopPropagation();toggleSelectAudio(${a.id})" />
            <div style="min-width:0;">
              <div class="avatar-name">${esc(a.name)}</div>
              <div class="avatar-sub">${formatAudioSourceLabel(a.source_type)} · ${fmtDuration(a.duration)} · ${a.size_kb} KB</div>
            </div>
          </div>
          <div onclick="event.stopPropagation()">
            ${a.stream_url ? `<audio src="${esc(a.stream_url)}" controls style="width:168px;height:28px;"></audio>` : ""}
          </div>
        </div>`;
    })
    .join("");
}

function toggleSelectAudio(id) {
  if (state.selectedAudioIds.has(id)) state.selectedAudioIds.delete(id);
  else state.selectedAudioIds.add(id);
  renderCreateAudioLibrary();
  updateWizardIndicators();
}

async function handleBatchCreateFromAudios() {
  const model = getSelectedValidModel();
  if (!model) return toast("请先在左侧选择一位出镜形象", "warning");
  const ta = document.getElementById("localAudioPathInput");
  const pastedLines = (ta?.value || "").split(/\r?\n/).map((ln) => cleanLocalPath(ln)).filter(Boolean);
  const ids = Array.from(state.selectedAudioIds);
  if (!ids.length && !pastedLines.length) return toast("请先勾选或上传至少一条录音", "warning");

  const btn = document.getElementById("btnBatchCreateAudios");
  if (btn) btn.disabled = true;
  const catVal = document.getElementById("audioJobCategorySelect")?.value || document.getElementById("jobCategorySelect")?.value;
  const baseName = (document.getElementById("audioJobNameInput")?.value || "").trim();

  try {
    if (pastedLines.length > 0) {
      const imp = await api("/api/v1/audios/import-local", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths: pastedLines, category_id: catVal ? Number(catVal) : null }),
      });
      (imp.items || []).forEach((it) => {
        if (!ids.includes(it.id)) ids.push(it.id);
        state.selectedAudioIds.add(it.id);
      });
      if (ta) ta.value = "";
    }

    const items = ids.map((aid, idx) => ({
      model_id: model.id,
      name: baseName ? (ids.length > 1 ? `${baseName}_${idx + 1}` : baseName) : null,
      audio_asset_id: aid,
      category_id: catVal ? Number(catVal) : null,
    }));

    const res = await api("/api/v1/jobs/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    });
    const createdIds = Array.isArray(res.job_ids) ? res.job_ids : (res.items || []).map((j) => j.id);
    createdIds.forEach((jid) => trackSessionJobId(jid));
    state.selectedAudioIds.clear();
    if (document.getElementById("audioJobNameInput")) document.getElementById("audioJobNameInput").value = "";
    toast(isEngineReady() ? `已开始制作 ${res.count} 条数字人视频` : `已加入 ${res.count} 条队列，请先启动本机引擎`, isEngineReady() ? "success" : "warning", 5000);
    await refreshAll(true);
  } catch (err) {
    toast(`批量创建失败: ${err.message}`, "error", 5500);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ==================== 实时制作条与生成进度 ====================

function buildPipelineStepsHtml(job) {
  const steps = [
    { key: "queued", label: "1.排队" },
    { key: "tts", label: "2.配音" },
    { key: "render", label: "3.对口型" },
    { key: "mux", label: "4.合成" },
    { key: "done", label: "5.完成" },
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

function statusTagHtml(job) {
  const map = {
    pending: ["st-pending", "制作中"],
    waiting: ["st-waiting", "排队中"],
    success: ["st-success", "已完成"],
    failed: ["st-failed", "制作失败"],
    cancelled: ["st-cancelled", "已中断"],
  };
  const [cls, text] = map[job.status] || ["st-muted", job.status];
  return `<span class="state-tag ${cls}">${text}</span>`;
}

function dismissBannerJob(jobId) {
  state.dismissedBannerJobIds.add(jobId);
  renderLiveMonitor();
}

function renderLiveMonitor() {
  const panel = document.getElementById("liveMonitorPanel");
  if (!panel) return;
  const activeJobs = state.jobs.filter((j) => j.status === "pending" || j.status === "waiting");
  const recentDone = state.jobs.filter(
    (j) => j.status === "success" && state.sessionSubmittedJobIds.has(j.id) && !state.dismissedBannerJobIds.has(j.id)
  );
  const recentFailed = state.jobs
    .filter((j) => j.status === "failed" && !state.dismissedBannerJobIds.has(j.id))
    .slice(0, 1);

  if (!activeJobs.length && !recentDone.length && !recentFailed.length) {
    panel.classList.add("hidden");
    panel.innerHTML = "";
    return;
  }

  const local = isLocalBrowser();
  panel.classList.remove("hidden");
  let html = "";

  activeJobs.slice(0, 2).forEach((j) => {
    const frameText = j.total_frames > 0
      ? `${j.rendered_frames}/${j.total_frames} 帧`
      : (j.rendered_frames > 0 ? `已渲染 ${j.rendered_frames} 帧` : "");
    html += `
      <div class="inline-status-box">
        <div class="inline-row-between">
          <div class="inline-row">
            <strong>#${j.id} ${esc(j.name)}</strong>
            ${statusTagHtml(j)}
          </div>
          <div class="inline-row">
            <button class="btn btn-xs btn-secondary" onclick="switchView('tasks')">全部进度</button>
            <button class="btn btn-xs btn-danger-ghost" onclick="handleCancelJob(${j.id})">中断</button>
          </div>
        </div>
        ${buildPipelineStepsHtml(j)}
        <div class="progress-bar"><div class="progress-fill" style="width:${Math.max(3, j.progress_float)}%;"></div></div>
        <div class="timeline-meta">
          <span>${esc(j.stage_label || j.message || "排队等待中")}</span>
          <span>${j.progress_float}% ${frameText ? `· ${frameText}` : ""} ${j.eta_seconds >= 0 ? `· 剩余 ${fmtEta(j.eta_seconds)}` : ""}</span>
        </div>
      </div>`;
  });

  recentDone.slice(0, 2).forEach((j) => {
    const isBig = Number(j.file_size_mb || 0) > 80;
    html += `
      <div class="inline-status-box ok">
        <div class="inline-row-between">
          <strong>制作完成：#${j.id} ${esc(j.name)}</strong>
          <div class="inline-row">
            ${j.file_exists ? `<button class="btn btn-xs btn-primary" onclick="previewJobVideo(${j.id})">播放</button>` : ""}
            ${j.file_exists && local ? `<button class="btn btn-xs btn-success" onclick="handleCopyJobFile(${j.id})">复制文件</button>` : ""}
            ${j.file_exists && local ? `<button class="btn btn-xs btn-secondary" onclick="handleOpenJobFolder(${j.id})">打开目录</button>` : ""}
            ${j.file_exists && isBig ? `<button class="btn btn-xs btn-secondary" onclick="handleCompactJobVideo(${j.id}, this)">${local ? "压小发微信" : "压小版下载"}</button>` : ""}
            ${j.file_exists ? `<a class="btn btn-xs btn-secondary" href="${esc(j.download_url)}" download>${local ? "另存" : "下载原片"}</a>` : ""}
            <button class="icon-btn" style="width:26px;height:26px;" onclick="dismissBannerJob(${j.id})" aria-label="关闭">${SVG_CLOSE}</button>
          </div>
        </div>
      </div>`;
  });

  recentFailed.forEach((j) => {
    html += `
      <div class="inline-status-box err">
        <div class="inline-row-between">
          <strong>制作失败：#${j.id} ${esc(j.name)}</strong>
          <div class="inline-row">
            <button class="btn btn-xs btn-primary" onclick="handleRetryJob(${j.id})">重试</button>
            <button class="btn btn-xs btn-secondary" onclick="switchView('tasks')">详情</button>
            <button class="icon-btn" style="width:26px;height:26px;" onclick="dismissBannerJob(${j.id})" aria-label="关闭">${SVG_CLOSE}</button>
          </div>
        </div>
        <div class="muted-text" style="color:var(--danger-text);font-size:12px;">${esc(j.message || j.error_detail)}</div>
      </div>`;
  });

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

function renderTasksView() {
  const wrap = document.getElementById("tasksList");
  if (!wrap) return;

  const setTxt = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  setTxt("kpiTotal", state.jobs.length);
  setTxt("kpiActive", state.jobs.filter((j) => j.status === "pending" || j.status === "waiting").length);
  setTxt("kpiSuccess", state.jobs.filter((j) => j.status === "success").length);
  setTxt("kpiFailed", state.jobs.filter((j) => j.status === "failed").length);

  const kw = (document.getElementById("taskSearch")?.value || "").trim().toLowerCase();
  const filtered = state.jobs.filter((j) => {
    if (state.taskFilter === "active" && j.status !== "pending" && j.status !== "waiting") return false;
    if (state.taskFilter !== "all" && state.taskFilter !== "active" && j.status !== state.taskFilter) return false;
    if (kw && !`${j.name} ${j.text_content} ${j.model_name} ${j.message}`.toLowerCase().includes(kw)) return false;
    return true;
  });

  if (!filtered.length) {
    wrap.innerHTML = `<div class="empty-box">暂无符合条件的制作任务</div>`;
    return;
  }

  const local = isLocalBrowser();
  wrap.innerHTML = filtered
    .map((j) => {
      const framesInfo = j.total_frames > 0
        ? `帧进度 ${j.rendered_frames}/${j.total_frames}${j.render_fps > 0 ? ` (${j.render_fps} 帧每秒)` : ""}`
        : (j.rendered_frames > 0 ? `已渲染 ${j.rendered_frames} 帧` : "");
      const errOpen = state.openTaskErrIds.has(j.id) ? "open" : "";
      const logOpen = state.openTaskLogIds.has(j.id) ? "open" : "";

      const thumbHtml = j.thumb_url
        ? `<div class="job-mini-thumb" onclick="${j.file_exists ? `previewJobVideo(${j.id})` : ""}"><img src="${esc(j.thumb_url)}" loading="lazy" alt="" onerror="handleCoverImgError(this)" /></div>`
        : "";

      return `
        <div class="timeline-row">
          ${thumbHtml}
          <div class="timeline-main">
            <div class="timeline-top">
              <div class="timeline-title">
                ${statusTagHtml(j)}
                <strong>#${j.id} ${esc(j.name)}</strong>
                <span class="state-tag st-muted">${esc(j.model_name || "#" + j.model_id)}</span>
                ${j.category_name && j.category_name !== "未分类" ? `<span class="state-tag st-muted">${esc(j.category_name)}</span>` : ""}
              </div>
              <span class="meta-num">${fmtTime(j.created_at)}${j.duration ? ` · ${fmtDuration(j.duration)}` : ""}</span>
            </div>

            ${(j.status === "pending" || j.status === "waiting") ? `
              ${buildPipelineStepsHtml(j)}
              <div class="progress-bar"><div class="progress-fill" style="width:${Math.max(2, j.progress_float)}%;"></div></div>
              <div class="timeline-meta">
                <span>${esc(j.stage_label || j.message)}</span>
                <span>${j.progress_float}% ${framesInfo ? `· ${framesInfo}` : ""} ${j.eta_seconds >= 0 ? `· 预计剩余 ${fmtEta(j.eta_seconds)}` : ""}</span>
              </div>
            ` : `<div class="muted-text" style="font-size:12.5px;">${esc(j.stage_label || j.message)}</div>`}

            ${j.status === "failed" ? `
              <div class="error-box">
                <div><strong>失败原因：</strong>${esc(j.message)}</div>
                ${j.error_detail && j.error_detail !== j.message ? `<details style="margin-top:6px;" ${errOpen} ontoggle="toggleTaskDetails('err',${j.id},this.open)"><summary style="cursor:pointer;">查看诊断详情</summary><pre class="logs-pre" style="max-height:140px;margin-top:6px;">${esc(j.error_detail)}</pre></details>` : ""}
              </div>
            ` : ""}

            ${j.recent_logs?.length && j.status === "pending" ? `
              <details style="margin-top:6px;" ${logOpen} ontoggle="toggleTaskDetails('log',${j.id},this.open)"><summary class="muted-text" style="cursor:pointer;font-size:12px;">实时渲染日志 (${j.recent_logs.length} 条)</summary>
                <pre class="logs-pre" style="max-height:140px;margin-top:6px;">${esc(j.recent_logs.join("\n"))}</pre>
              </details>` : ""}

            <div class="inline-row-between" style="margin-top:10px;">
              <div class="muted-text" style="font-size:12px;max-width:52%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
                ${j.text_content ? `文案：${esc(j.text_content.slice(0, 56))}` : ""}
              </div>
              <div class="inline-row">
                ${(j.status === "pending" || j.status === "waiting") ? `<button class="btn btn-xs btn-danger-ghost" onclick="handleCancelJob(${j.id})">中断</button>` : ""}
                ${(j.status === "failed" || j.status === "cancelled") ? `<button class="btn btn-xs btn-primary" onclick="handleRetryJob(${j.id})">重试</button>` : ""}
                ${j.file_exists ? `
                  <button class="btn btn-xs btn-primary" onclick="previewJobVideo(${j.id})">播放</button>
                  ${local ? `<button class="btn btn-xs btn-success" onclick="handleCopyJobFile(${j.id})">复制文件</button>` : ""}
                  ${local ? `<button class="btn btn-xs btn-secondary" onclick="handleOpenJobFolder(${j.id})">打开目录</button>` : ""}
                  ${Number(j.file_size_mb || 0) > 80 ? `<button class="btn btn-xs btn-secondary" onclick="handleCompactJobVideo(${j.id}, this)">${local ? "压小版" : "压小下载"}</button>` : ""}
                  <a class="btn btn-xs btn-secondary" href="${esc(j.download_url)}" download>${local ? "另存" : "下载原片"}</a>
                ` : ""}
                <button class="btn btn-xs btn-ghost" onclick="renameJobPrompt(${j.id})">改名</button>
                <button class="btn btn-xs btn-danger-ghost" onclick="handleDeleteJob(${j.id})">删除</button>
              </div>
            </div>
          </div>
        </div>`;
    })
    .join("");
}

async function handleCancelJob(id) {
  if (!confirm(`确定中断任务 #${id} 吗？`)) return;
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
    state.dismissedBannerJobIds.delete(id);
    trackSessionJobId(id);
    toast(`任务 #${id} 已重新加入队列`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`重试失败: ${err.message}`, "error");
  }
}

async function handleDeleteJob(id) {
  if (!confirm(`确定删除作品记录 #${id}？（不会删除出镜形象）`)) return;
  try {
    await api(`/api/v1/jobs/${id}`, { method: "DELETE" });
    toast(`已删除 #${id}`, "success");
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

async function handleCopyJobFile(id) {
  try {
    const res = await api(`/api/v1/jobs/${id}/copy-file`, { method: "POST" });
    toast(res.message || "已复制视频文件，去微信或桌面粘贴即可", "success", 4000);
  } catch (err) {
    toast(`复制失败: ${err.message}`, "error");
  }
}

async function handleCompactJobVideo(id, btnEl) {
  const local = isLocalBrowser();
  const prevText = btnEl ? btnEl.textContent : "";
  if (btnEl) { btnEl.disabled = true; btnEl.textContent = "压小中..."; }
  toast("正在压制清晰小体积版（保留原片），请稍候...", "info", 4200);
  try {
    const res = await api(`/api/v1/jobs/${id}/compact`, { method: "POST", timeoutMs: 300000 });
    if (local && res.copied_to_clipboard) {
      toast(`压好了（${res.orig_size_mb} MB 压缩至 ${res.compact_size_mb} MB），已自动复制文件，直接粘贴发送即可`, "success", 6000);
    } else {
      toast(`压小版就绪（${res.compact_size_mb} MB），开始下载...`, "success", 3600);
      const a = document.createElement("a");
      a.href = res.download_url;
      a.download = "";
      document.body.appendChild(a);
      a.click();
      a.remove();
    }
  } catch (err) {
    toast(`压小失败: ${err.message}`, "error", 5500);
  } finally {
    if (btnEl) { btnEl.disabled = false; btnEl.textContent = prevText || (local ? "压小版" : "压小下载"); }
  }
}

async function openDeliveryFolder() {
  let p = state.health?.paths?.delivery_root;
  if (!p) {
    try {
      const h = await api("/api/v1/health", { timeoutMs: 12000 });
      state.health = h;
      p = h?.paths?.delivery_root;
    } catch {}
  }
  if (!p) return toast("暂未获取到成片目录路径", "warning");
  await openAnyPath(p);
}

// ==================== 我的作品（支持点击标题直接改名） ====================

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
  const name = prompt("输入新作品分类名称：", "日常口播");
  if (!name || !name.trim()) return;
  try {
    await api("/api/v1/categories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), color: "#2f81f7" }),
    });
    toast(`已新建分类「${name.trim()}」`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`创建失败: ${err.message}`, "error");
  }
}

async function deleteCategoryById(catId) {
  const c = state.categories.find((x) => x.id === catId);
  const name = c?.name || `#${catId}`;
  if (!confirm(`确定删除分类「${name}」？（作品会保留在未分类中）`)) return;
  try {
    await api(`/api/v1/categories/${catId}`, { method: "DELETE" });
    if (String(state.selectedCategoryId) === String(catId)) state.selectedCategoryId = "all";
    toast(`已删除分类「${name}」`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`删除失败: ${err.message}`, "error");
  }
}

function startInlineRenameJob(jobId, rowEl) {
  const j = state.jobs.find((x) => x.id === jobId);
  if (!j || !rowEl) return;
  const oldName = j.name || "";
  rowEl.innerHTML = `<input class="inline-rename-input" type="text" value="${esc(oldName)}" autocomplete="off" />`;
  const inp = rowEl.querySelector("input");
  if (!inp) return;
  inp.focus();
  inp.select();
  let committed = false;

  const commit = async () => {
    if (committed) return;
    committed = true;
    const nextName = inp.value.trim();
    if (!nextName || nextName === oldName) {
      renderLibraryView();
      return;
    }
    try {
      await api(`/api/v1/jobs/${jobId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: nextName }),
      });
      toast(`已改名为「${nextName}」`, "success", 2200);
      await refreshAll(true);
    } catch (err) {
      toast(`改名失败: ${err.message}`, "error");
      renderLibraryView();
    }
  };

  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(); }
    else if (e.key === "Escape") { committed = true; renderLibraryView(); }
  });
  inp.addEventListener("blur", commit);
}

async function renameJobPrompt(jobId) {
  const j = state.jobs.find((x) => x.id === jobId);
  if (!j) return;
  const nextName = prompt("请输入新的作品标题：", j.name || "");
  if (nextName === null) return;
  const trimmed = nextName.trim();
  if (!trimmed || trimmed === j.name) return;
  try {
    await api(`/api/v1/jobs/${jobId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: trimmed }),
    });
    toast(`已改名为「${trimmed}」`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`改名失败: ${err.message}`, "error");
  }
}

function renderLibraryView() {
  const catBox = document.getElementById("categoryList");
  const grid = document.getElementById("libraryGrid");
  if (!catBox || !grid) return;

  const isVid = state.libraryType === "videos";
  const allVideos = state.jobs.filter((j) => j.status === "success");
  const totalCount = isVid ? allVideos.length : state.audios.length;
  const uncatCount = isVid
    ? allVideos.filter((j) => !j.category_id).length
    : state.audios.filter((a) => !a.category_id).length;

  let catHtml = `
    <button class="cat-pill ${state.selectedCategoryId === "all" ? "active" : ""}" onclick="selectCategory('all')">全部 <span class="meta-num">${totalCount}</span></button>
    <button class="cat-pill ${state.selectedCategoryId === "uncategorized" ? "active" : ""}" onclick="selectCategory('uncategorized')">未分类 <span class="meta-num">${uncatCount}</span></button>`;

  state.categories.forEach((c) => {
    const cnt = isVid ? allVideos.filter((j) => j.category_id === c.id).length : state.audios.filter((a) => a.category_id === c.id).length;
    catHtml += `
      <span class="cat-pill ${String(state.selectedCategoryId) === String(c.id) ? "active" : ""}" onclick="selectCategory('${c.id}')">
        <span>${esc(c.name)}</span>
        <span class="meta-num">${cnt}</span>
        <button class="cat-del" title="删除分类" onclick="event.stopPropagation();deleteCategoryById(${c.id})">${SVG_CLOSE}</button>
      </span>`;
  });
  catBox.innerHTML = catHtml;

  const kw = (document.getElementById("libSearch")?.value || "").trim().toLowerCase();
  const local = isLocalBrowser();

  if (isVid) {
    const items = allVideos.filter((j) => {
      if (state.selectedCategoryId === "uncategorized" && j.category_id) return false;
      if (state.selectedCategoryId !== "all" && state.selectedCategoryId !== "uncategorized" && String(j.category_id) !== String(state.selectedCategoryId)) return false;
      if (kw && !`${j.name} ${j.text_content} ${j.model_name}`.toLowerCase().includes(kw)) return false;
      return true;
    });

    if (!items.length) {
      grid.innerHTML = `<div class="studio-pane empty-box" style="grid-column:1/-1;">当前分类下暂无视频作品</div>`;
      return;
    }

    grid.innerHTML = items
      .map((j) => {
        const coverInner = j.thumb_url
          ? `<img src="${esc(j.thumb_url)}" alt="${esc(j.name)}" loading="lazy" onerror="handleCoverImgError(this)" />`
          : `<div class="media-cover-placeholder">暂无封面</div>`;
        return `
        <article class="media-item">
          <div class="media-cover" onclick="${j.file_exists ? `previewJobVideo(${j.id})` : ""}">
            ${coverInner}
            ${j.file_exists ? `<div class="play-overlay"><div class="play-circle">${SVG_PLAY}</div></div>` : ""}
            <div class="cover-pills">
              ${j.duration ? `<span class="cover-pill">${fmtDuration(j.duration)}</span>` : ""}
              <span class="cover-pill ${j.file_exists ? "ok" : "err"}">${j.file_exists ? fmtSizeMb(j.file_size_mb) : "文件已移走"}</span>
            </div>
          </div>
          <div class="media-body">
            <div class="media-title-row">
              <button class="media-title-btn" title="点击直接改名" onclick="startInlineRenameJob(${j.id}, this.parentElement)">${esc(j.name)}</button>
              <button class="icon-btn" style="width:26px;height:26px;" title="重命名" onclick="startInlineRenameJob(${j.id}, this.parentElement)">${SVG_EDIT}</button>
            </div>
            <div class="media-sub">形象：${esc(j.model_name || "-")} · ${fmtTime(j.created_at)}</div>
            <div class="inline-row-nowrap">
              <span class="muted-text" style="font-size:12px;flex-shrink:0;">分类</span>
              <select class="field-input field-sm" style="flex:1;" onchange="changeJobCategory(${j.id}, this.value)">
                <option value="" ${!j.category_id ? "selected" : ""}>未分类</option>
                ${state.categories.map((c) => `<option value="${c.id}" ${j.category_id === c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}
              </select>
            </div>
            <details class="fold-panel" style="margin-bottom:0;">
              <summary style="padding:6px 10px;font-size:12px;">文件位置与文案</summary>
              <div class="fold-body" style="padding:8px 10px;font-size:12px;word-break:break-all;">
                <div>成片：${esc(j.paths.delivered_path || j.paths.playable_path || "文件已移走")} <button class="btn-link" onclick="copyJobDeliveredPath(${j.id})">复制路径</button></div>
                ${j.text_content ? `<div class="muted-text" style="margin-top:6px;font-size:12px;">文案：${esc(j.text_content)}</div>` : ""}
              </div>
            </details>
            <div class="media-actions">
              ${j.file_exists ? `<button class="btn btn-xs btn-primary" onclick="previewJobVideo(${j.id})">播放</button>` : ""}
              ${j.file_exists && local ? `<button class="btn btn-xs btn-success" onclick="handleCopyJobFile(${j.id})">复制文件</button>` : ""}
              ${j.file_exists && Number(j.file_size_mb || 0) > 80 ? `<button class="btn btn-xs btn-secondary" onclick="handleCompactJobVideo(${j.id}, this)">${local ? "压小版" : "压小下载"}</button>` : ""}
              ${j.file_exists ? `<a class="btn btn-xs btn-secondary" href="${esc(j.download_url)}" download>${local ? "另存" : "原片"}</a>` : ""}
              ${j.file_exists && local ? `<button class="btn btn-xs btn-ghost" onclick="handleOpenJobFolder(${j.id})">位置</button>` : ""}
              <button class="btn btn-xs btn-ghost" onclick="cloneJobToCreate(${j.id})">复用</button>
              <button class="btn btn-xs btn-danger-ghost" onclick="handleDeleteJob(${j.id})">删除</button>
            </div>
          </div>
        </article>`;
      })
      .join("");
  } else {
    const items = state.audios.filter((a) => {
      if (state.selectedCategoryId === "uncategorized" && a.category_id) return false;
      if (state.selectedCategoryId !== "all" && state.selectedCategoryId !== "uncategorized" && String(a.category_id) !== String(state.selectedCategoryId)) return false;
      if (kw && !`${a.name} ${a.text_content}`.toLowerCase().includes(kw)) return false;
      return true;
    });

    if (!items.length) {
      grid.innerHTML = `<div class="studio-pane empty-box" style="grid-column:1/-1;">当前分类下暂无音频素材</div>`;
      return;
    }

    grid.innerHTML = items
      .map(
        (a) => `
        <article class="studio-pane" style="display:flex;flex-direction:column;gap:8px;">
          <div class="inline-row-between">
            <strong style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;" title="${esc(a.name)}">${esc(a.name)}</strong>
            <span class="state-tag st-muted">${formatAudioSourceLabel(a.source_type)} · ${fmtDuration(a.duration)}</span>
          </div>
          <div class="inline-row-nowrap">
            <span class="muted-text" style="font-size:12px;flex-shrink:0;">分类</span>
            <select class="field-input field-sm" style="flex:1;" onchange="changeAudioCategory(${a.id}, this.value)">
              <option value="" ${!a.category_id ? "selected" : ""}>未分类</option>
              ${state.categories.map((c) => `<option value="${c.id}" ${a.category_id === c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}
            </select>
          </div>
          ${a.stream_url ? `<audio src="${esc(a.stream_url)}" class="audio-player" controls></audio>` : `<div class="muted-text" style="color:var(--danger);">文件已缺失</div>`}
          <div class="media-actions">
            <button class="btn btn-xs btn-primary" onclick="quickVideoFromAudio(${a.id})">去做视频</button>
            ${a.download_url ? `<a class="btn btn-xs btn-secondary" href="${esc(a.download_url)}" download>下载</a>` : ""}
            <button class="btn btn-xs btn-ghost" onclick="renameAudioAsset(${a.id})">改名</button>
            ${local ? `<button class="btn btn-xs btn-ghost" onclick="openAudioPath(${a.id})">定位</button>` : ""}
            <button class="btn btn-xs btn-danger-ghost" onclick="deleteAudioAsset(${a.id})">删除</button>
          </div>
        </article>`
      )
      .join("");
  }
}

async function renameAudioAsset(audioId) {
  const a = state.audios.find((x) => x.id === audioId);
  if (!a) return;
  const nextName = prompt("请输入新的音频名称：", a.name || "");
  if (nextName === null) return;
  const trimmed = nextName.trim();
  if (!trimmed || trimmed === a.name) return;
  try {
    await api(`/api/v1/audios/${audioId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: trimmed }),
    });
    toast(`已改名为「${trimmed}」`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`改名失败: ${err.message}`, "error");
  }
}

async function changeAudioCategory(audioId, catVal) {
  try {
    await api(`/api/v1/audios/${audioId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category_id: catVal ? Number(catVal) : null }),
    });
    toast("分类已更新", "success", 2000);
    await refreshAll(true);
  } catch (err) {
    toast(`修改失败: ${err.message}`, "error");
  }
}

async function changeJobCategory(jobId, catVal) {
  try {
    await api(`/api/v1/jobs/${jobId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category_id: catVal ? Number(catVal) : null }),
    });
    toast("分类已更新", "success", 2200);
    await refreshAll(true);
  } catch (err) {
    toast(`修改失败: ${err.message}`, "error");
  }
}

function cloneJobToCreate(jobId) {
  const j = state.jobs.find((x) => x.id === jobId);
  if (!j) return;
  const linkedModel = state.models.find((m) => m.id === j.model_id && m.video_exists);
  if (linkedModel) {
    state.selectedModelId = linkedModel.id;
    savePrefs({ selectedModelId: linkedModel.id });
  }

  document.getElementById("jobNameInput").value = "";
  document.getElementById("jobCategorySelect").value = j.category_id ? String(j.category_id) : "";
  const jobVoiceId = j.voice_id ? String(j.voice_id) : "";
  [document.getElementById("ttsVoiceSelect"), document.getElementById("voiceStudioVoiceSelect")]
    .filter(Boolean)
    .forEach((voiceSel) => {
      const hasOpt = jobVoiceId && Array.from(voiceSel.options).some((opt) => opt.value === jobVoiceId);
      voiceSel.value = hasOpt ? jobVoiceId : "";
    });

  if (j.text_content) {
    setCreateMode("tts");
    document.getElementById("ttsTextInput").value = j.text_content;
    syncTextBetweenViews("create");
    if (j.tts_params && Object.keys(j.tts_params).length) applyTtsParamsToForm(j.tts_params);
  } else if (j.paths?.source_audio_path) {
    setCreateMode("audio");
    const normSrc = String(j.paths.source_audio_path).replace(/\\/g, "/").toLowerCase();
    const matchedAudio = state.audios.find((a) => String(a.file_path || "").replace(/\\/g, "/").toLowerCase() === normSrc);
    state.selectedAudioIds.clear();
    if (matchedAudio) state.selectedAudioIds.add(matchedAudio.id);
    else if (document.getElementById("localAudioPathInput")) document.getElementById("localAudioPathInput").value = j.paths.source_audio_path;
    renderCreateAudioLibrary();
  }
  switchView("create");
  renderCreateModelList();
  renderSelectedAvatarBar();
  updateVoicePreview();
  updateWizardIndicators();
  toast("已载入该作品的制作配置", "success", 2800);
}

function quickVideoFromAudio(audioId) {
  state.selectedAudioIds.clear();
  state.selectedAudioIds.add(audioId);
  setCreateMode("audio");
  switchView("create");
  renderCreateAudioLibrary();
  updateWizardIndicators();
  toast("已选中该音频，确认形象后点「开始生成视频」即可", "info");
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

// ==================== 媒体预览与本机操作 ====================

function previewJobVideo(jobId) {
  const j = state.jobs.find((x) => x.id === jobId);
  if (!j || !j.stream_url) return toast("视频文件不存在或尚未生成", "warning");
  previewVideo(j.stream_url, j.name || `作品 #${j.id}`, j.paths?.playable_path || "", j.thumb_url || "");
}

function previewModelVideo(modelId) {
  const m = state.models.find((x) => x.id === modelId);
  if (!m || !m.preview_url) return toast("形象视频文件不存在", "warning");
  previewVideo(m.preview_url, `出镜形象：${m.name}`, m.video_abs_path || "", m.thumb_url || "");
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

async function openPreviewInLocalPlayer() {
  if (!state.previewCurrentPath) return toast("暂无本地视频路径", "warning");
  try {
    const res = await api("/api/v1/system/open-media", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.previewCurrentPath }),
    });
    toast(res.message || "已调用电脑播放器打开", "success", 2800);
  } catch (err) {
    toast(`无法调用电脑播放器: ${err.message}`, "error");
  }
}

function openPreviewFolder() {
  if (state.previewCurrentPath) openAnyPath(state.previewCurrentPath);
}

function previewVideo(url, title, pathText, posterUrl = "") {
  state.previewCurrentPath = pathText || "";
  document.getElementById("previewModalTitle").textContent = title;
  document.getElementById("previewModalMeta").textContent = pathText ? `文件位置：${pathText}` : "";

  const vid = document.getElementById("previewModalVideo");
  const fb = document.getElementById("previewModalFallback");
  const fbImg = document.getElementById("previewModalPosterImg");
  const fbText = document.getElementById("previewFallbackText");
  const dlBtn = document.getElementById("btnPreviewDownload");
  const extBtn = document.getElementById("btnPreviewExternal");
  const canOpenLocal = Boolean(pathText) && isLocalBrowser();

  fb?.classList.add("hidden");
  extBtn?.classList.toggle("hidden", !canOpenLocal);
  document.getElementById("btnPreviewOpenPlayer")?.classList.toggle("hidden", !canOpenLocal);
  document.getElementById("btnPreviewOpenFolder")?.classList.toggle("hidden", !canOpenLocal);
  if (dlBtn) {
    if (!canOpenLocal && url) { dlBtn.href = url; dlBtn.classList.remove("hidden"); }
    else { dlBtn.removeAttribute("href"); dlBtn.classList.add("hidden"); }
  }
  if (fbText) {
    fbText.textContent = canOpenLocal
      ? "当前视频为高清编码，浏览器仅支持试听音轨（不影响制作），可直接用电脑播放器观看："
      : "当前浏览器暂不支持解码该高清画面，可下载原文件到本机观看：";
  }

  if (vid) {
    vid.classList.remove("audio-only", "hidden");
    if (posterUrl) vid.setAttribute("poster", posterUrl);
    else vid.removeAttribute("poster");

    const activateFallback = (hideCompletely = false) => {
      if (fb) {
        fb.classList.remove("hidden");
        if (fbImg) {
          if (posterUrl) { fbImg.src = posterUrl; fbImg.classList.remove("hidden"); }
          else fbImg.classList.add("hidden");
        }
      }
      extBtn?.classList.add("hidden");
      vid.removeAttribute("poster");
      if (hideCompletely) vid.classList.add("hidden");
      else vid.classList.add("audio-only");
    };

    vid.onloadedmetadata = () => { if (vid.videoWidth === 0 || vid.videoHeight === 0) activateFallback(false); };
    vid.onerror = () => activateFallback(true);
    vid.src = url;
  }
  openModal("previewModal");
}

// ==================== 形象与音色 ====================

function setModelSubTab(sub) {
  state.modelSubTab = sub;
  document.querySelectorAll("#modelSubTabs button").forEach((b) => b.classList.toggle("active", b.dataset.sub === sub));
  document.getElementById("modelsSubPanel")?.classList.toggle("hidden", sub !== "models");
  document.getElementById("voicesSubPanel")?.classList.toggle("hidden", sub !== "voices");
  renderModelsManageView();
}

function useVoiceInStudio(voiceId) {
  const val = String(voiceId);
  state.preferredVoiceId = val;
  savePrefs({ selectedVoiceId: val });
  [document.getElementById("ttsVoiceSelect"), document.getElementById("voiceStudioVoiceSelect")]
    .filter(Boolean)
    .forEach((s) => { if (Array.from(s.options).some((o) => o.value === val)) s.value = val; });
  updateVoicePreview();
  updateVoiceStudioRefPreview();
  switchView("voice");
  toast("已切换为该参考音色", "success", 2200);
}

function renderModelsManageView() {
  const local = isLocalBrowser();
  if (state.modelSubTab === "models") {
    const grid = document.getElementById("modelsManageGrid");
    const missingBar = document.getElementById("modelsManageMissingBar");
    const discoverBar = document.getElementById("modelsDiscoverBar");
    const missing = getMissingModels();
    const unimportedDiscovered = state.discoveredVideos.filter((v) => !v.already_imported);

    if (discoverBar) discoverBar.innerHTML = buildDiscoverHtml(unimportedDiscovered, false);

    if (missingBar) {
      if (missing.length > 0) {
        missingBar.classList.remove("hidden");
        missingBar.innerHTML = `
          <div class="inline-status-box err">
            <div class="inline-row-between">
              <span>检测到 ${missing.length} 条历史形象的原视频缺失，可点「补回视频」重新关联或清理失效记录。</span>
              <button class="btn btn-xs btn-danger" onclick="handleCleanupMissingModels()">清理失效记录</button>
            </div>
          </div>`;
      } else {
        missingBar.classList.add("hidden");
        missingBar.innerHTML = "";
      }
    }

    if (!state.models.length) {
      grid.innerHTML = `<div class="studio-pane empty-box" style="grid-column:1/-1;">暂无出镜形象，点右上角「+ 添加形象视频」即可开始。</div>`;
      return;
    }

    const sorted = [...state.models].sort((a, b) => Number(b.video_exists) - Number(a.video_exists) || b.id - a.id);
    grid.innerHTML = sorted
      .map(
        (m) => `
        <article class="media-item ${m.video_exists ? "" : "missing"}">
          <div class="media-cover" onclick="${m.video_exists ? `previewModelVideo(${m.id})` : `quickRelinkModelFromDisk(${m.id})`}">
            ${m.video_exists
              ? `<img src="${esc(m.thumb_url)}" alt="${esc(m.name)}" loading="lazy" data-preview="${esc(m.preview_url)}" onerror="handleThumbFallback(this, this.dataset.preview)" />
                 <div class="play-overlay"><div class="play-circle">${SVG_PLAY}</div></div>`
              : `<div class="media-cover-placeholder" style="color:var(--danger-text);">原视频缺失 · 点击补回</div>`}
            <div class="cover-pills">
              <span class="cover-pill ${m.video_exists ? "ok" : "err"}">${m.video_exists ? fmtSizeMb(m.video_size_mb) : "文件缺失"}</span>
            </div>
          </div>
          <div class="media-body">
            <div class="media-title-row">
              <strong style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(m.name)}">${esc(m.name)}</strong>
              <button class="icon-btn" style="width:26px;height:26px;" title="改名" onclick="renameModel(${m.id})">${SVG_EDIT}</button>
            </div>
            <div class="inline-row-nowrap">
              <span class="muted-text" style="font-size:12px;flex-shrink:0;">分组</span>
              <select class="field-input field-sm" style="flex:1;" onchange="changeModelGroup(${m.id}, this.value)">
                <option value="" ${!m.group_id ? "selected" : ""}>未分组</option>
                ${state.modelGroups.map((g) => `<option value="${g.id}" ${m.group_id === g.id ? "selected" : ""}>${esc(g.name)}</option>`).join("")}
              </select>
            </div>
            ${m.voice_preview_url ? `<audio src="${esc(m.voice_preview_url)}" class="audio-player" controls preload="none"></audio>` : `<div class="muted-text" style="font-size:12px;">无原声参考音</div>`}
            <div class="media-actions">
              ${m.video_exists ? `<button class="btn btn-xs btn-primary" onclick="selectModel(${m.id});switchView('create')">用TA做视频</button>` : ""}
              <button class="btn btn-xs ${m.video_exists ? "btn-secondary" : "btn-primary"}" onclick="quickRelinkModelFromDisk(${m.id})">${m.video_exists ? "替换" : "补回视频"}</button>
              ${m.video_exists && local ? `<button class="btn btn-xs btn-ghost" data-path="${esc(m.video_abs_path)}" onclick="openAnyPath(this.dataset.path)">位置</button>` : ""}
              <button class="btn btn-xs btn-danger-ghost" onclick="deleteModelById(${m.id})">删除</button>
            </div>
          </div>
        </article>`
      )
      .join("");
  } else {
    const grid = document.getElementById("voicesManageGrid");
    const validVoices = state.voices.filter((v) => v.exists);
    if (!validVoices.length) {
      grid.innerHTML = `<div class="studio-pane empty-box" style="grid-column:1/-1;">暂无可用参考音色，添加形象视频会自动提取原声，也可点右上角上传。</div>`;
      return;
    }
    grid.innerHTML = validVoices
      .map(
        (v) => `
        <article class="studio-pane" style="display:flex;flex-direction:column;gap:8px;">
          <div class="inline-row-between">
            <strong style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;">${esc(v.display_name)}</strong>
            <span class="state-tag st-success">可用</span>
          </div>
          <div class="muted-text" style="font-size:12px;">${v.model_name ? `关联形象：${esc(v.model_name)}` : "自定义上传音色"}</div>
          <audio src="${esc(v.preview_url)}" class="audio-player" controls preload="none"></audio>
          <div class="media-actions">
            <button class="btn btn-xs btn-primary" onclick="useVoiceInStudio(${v.id})">去配声音使用</button>
            ${local && v.abs_path ? `<button class="btn btn-xs btn-ghost" data-path="${esc(v.abs_path)}" onclick="openAnyPath(this.dataset.path)">打开位置</button>` : ""}
          </div>
        </article>`
      )
      .join("");
  }
}

async function promptNewModelGroup() {
  const name = prompt("输入新形象分组名称：", "常用出镜");
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
    toast(`新建失败: ${err.message}`, "error");
  }
}

async function renameModel(modelId) {
  const m = state.models.find((x) => x.id === modelId);
  if (!m) return;
  const nextName = prompt("请输入新的形象名称：", m.name || "");
  if (nextName === null) return;
  const trimmed = nextName.trim();
  if (!trimmed || trimmed === m.name) return;
  try {
    await api(`/api/v1/models/${modelId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: trimmed }),
    });
    toast(`已改名为「${trimmed}」`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`改名失败: ${err.message}`, "error");
  }
}

async function changeModelGroup(modelId, groupVal) {
  try {
    await api(`/api/v1/models/${modelId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ group_id: groupVal ? Number(groupVal) : null }),
    });
    toast("分组已更新", "success", 2000);
    await refreshAll(true);
  } catch (err) {
    toast(`更新失败: ${err.message}`, "error");
  }
}

async function deleteModelById(modelId) {
  const m = state.models.find((x) => x.id === modelId);
  const label = m ? `「${m.name}」` : `#${modelId}`;
  if (!confirm(`确定删除形象 ${label}？（不会删除已经做好的视频）`)) return;
  try {
    await api(`/api/v1/models/${modelId}`, { method: "DELETE" });
    if (state.selectedModelId === modelId) state.selectedModelId = null;
    toast(`已删除形象 ${label}`, "success");
    await refreshAll(true, true);
  } catch (err) {
    toast(`删除失败: ${err.message}`, "error");
  }
}

// ==================== 添加形象 & 上传音色弹窗 ====================

function openAddModelModal() {
  document.getElementById("modelModalTitle").textContent = "添加形象视频";
  document.getElementById("modelModalRelinkId").value = "";
  document.getElementById("modelModalNameField").classList.remove("hidden");
  document.getElementById("modelModalGroupField").classList.remove("hidden");
  document.getElementById("modelModalName").value = "";
  document.getElementById("modelModalLocalPath").value = "";
  document.getElementById("modelModalFile").value = "";
  document.getElementById("modelModalPickedHint").textContent = "还未选择视频文件";
  document.getElementById("btnSubmitModelModal").textContent = "确认添加";
  applyClientModeVisibility();
  openModal("modelModal");
}

function openRelinkModelModal(modelId, modelName) {
  document.getElementById("modelModalTitle").textContent = `补回或替换形象视频：${modelName}`;
  document.getElementById("modelModalRelinkId").value = String(modelId);
  document.getElementById("modelModalNameField").classList.add("hidden");
  document.getElementById("modelModalGroupField").classList.add("hidden");
  document.getElementById("modelModalLocalPath").value = "";
  document.getElementById("modelModalFile").value = "";
  document.getElementById("modelModalPickedHint").textContent = "请选择新的原视频文件";
  document.getElementById("btnSubmitModelModal").textContent = "确认替换";
  applyClientModeVisibility();
  openModal("modelModal");
}

async function pickVideoIntoModal() {
  const btn = document.getElementById("btnModalPickVideo");
  if (btn) { btn.disabled = true; btn.textContent = "等待选择..."; }
  let fallbackUpload = false;
  try {
    const pick = await pickLocalFileSafe("video", false);
    if (pick.remote) { fallbackUpload = true; return; }
    if (pick.cancelled || !pick.paths?.length) return;
    const p = pick.paths[0];
    document.getElementById("modelModalLocalPath").value = p;
    document.getElementById("modelModalFile").value = "";
    document.getElementById("modelModalPickedHint").textContent = `已选择本机视频：${p}`;
    const nameInput = document.getElementById("modelModalName");
    if (nameInput && !nameInput.value.trim()) nameInput.value = fileStemFromPath(p);
  } catch (err) {
    toast(`改用网页上传：${err.message}`, "warning", 4000);
    fallbackUpload = true;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "从电脑选择视频"; }
    if (fallbackUpload) document.getElementById("modelModalFile")?.click();
  }
}

function onModalVideoFilePicked(input) {
  const f = input.files?.[0];
  if (!f) return;
  document.getElementById("modelModalLocalPath").value = "";
  document.getElementById("modelModalPickedHint").textContent = `已选择文件：${f.name} (${fmtSizeMb(f.size / (1024 * 1024))})`;
  const nameInput = document.getElementById("modelModalName");
  if (nameInput && !nameInput.value.trim()) nameInput.value = f.name.replace(/\.[^.]+$/, "").trim();
}

function onModalLocalPathTyped() {
  const p = cleanLocalPath(document.getElementById("modelModalLocalPath").value);
  if (p) {
    document.getElementById("modelModalFile").value = "";
    document.getElementById("modelModalPickedHint").textContent = `将导入本机视频：${p}`;
    const nameInput = document.getElementById("modelModalName");
    if (nameInput && !nameInput.value.trim()) nameInput.value = fileStemFromPath(p);
  }
}

async function submitModelModal() {
  const relinkId = document.getElementById("modelModalRelinkId").value;
  const localPath = cleanLocalPath(document.getElementById("modelModalLocalPath").value);
  const file = document.getElementById("modelModalFile").files?.[0];
  if (!localPath && !file) return toast("请先选择一个视频文件", "warning");

  const fd = new FormData();
  if (file) fd.append("file", file);
  else fd.append("local_video_path", localPath);

  if (!relinkId) {
    const typedName = (document.getElementById("modelModalName").value || "").trim();
    const autoName = file ? file.name.replace(/\.[^.]+$/, "").trim() : fileStemFromPath(localPath);
    fd.append("name", typedName || autoName || "新形象");
    const grp = document.getElementById("modelModalGroup").value;
    if (grp) fd.append("group_id", grp);
  }

  const btn = document.getElementById("btnSubmitModelModal");
  if (btn) { btn.disabled = true; btn.textContent = "处理中..."; }
  try {
    const url = relinkId ? `/api/v1/models/${relinkId}/relink` : "/api/v1/models";
    const res = await api(url, { method: "POST", body: fd, timeoutMs: 300000 });
    if (res?.item?.id) {
      state.selectedModelId = res.item.id;
      savePrefs({ selectedModelId: res.item.id });
    }
    toast(relinkId ? "形象视频已更新" : "形象添加成功", "success");
    closeModal("modelModal");
    await refreshAll(true, true);
  } catch (err) {
    toast(`保存失败: ${err.message}`, "error", 6000);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = relinkId ? "确认替换" : "确认添加"; }
  }
}

function openUploadVoiceModal() {
  document.getElementById("voiceModalName").value = "";
  document.getElementById("voiceModalFile").value = "";
  openModal("voiceModal");
}

async function submitVoiceModal() {
  const file = document.getElementById("voiceModalFile").files?.[0];
  if (!file) return toast("请选择一段音频文件", "warning");
  const fd = new FormData();
  fd.append("name", (document.getElementById("voiceModalName").value || "").trim());
  fd.append("file", file);
  const btn = document.getElementById("btnSubmitVoiceModal");
  if (btn) { btn.disabled = true; btn.textContent = "上传中..."; }
  try {
    const res = await api("/api/v1/voices", { method: "POST", body: fd });
    toast(`参考音色「${res?.item?.display_name || "新音色"}」上传成功`, "success");
    closeModal("voiceModal");
    await refreshAll(true);
    if (res?.item?.id) {
      const vidStr = String(res.item.id);
      state.preferredVoiceId = vidStr;
      savePrefs({ selectedVoiceId: vidStr });
      [document.getElementById("ttsVoiceSelect"), document.getElementById("voiceStudioVoiceSelect")]
        .filter(Boolean)
        .forEach((sel) => { if (Array.from(sel.options).some((o) => o.value === vidStr)) sel.value = vidStr; });
      updateVoicePreview();
      updateVoiceStudioRefPreview();
      updateWizardIndicators();
    }
  } catch (err) {
    toast(`上传失败: ${err.message}`, "error");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "确认上传"; }
  }
}

// ==================== 外部自动化接口（收纳于设置页折叠面板） ====================

function renderApiView() {
  const origin = window.location.origin;
  const hostEl = document.getElementById("studioEndpointHost");
  if (hostEl) hostEl.textContent = origin;

  const firstModel = getValidModels()[0] || state.models[0];
  const sampleModelId = firstModel ? firstModel.id : 1;
  const snippet = `import time, requests

BASE = "${origin}"

resp = requests.post(f"{BASE}/api/v1/jobs", json={
    "model_id": ${sampleModelId},
    "name": "自动口播示例",
    "text": "大家好，这是一条通过自动化接口生成的数字人视频。",
    "tts_params": {"speed": 1.0, "emo_mode": "same_as_speaker"}
}).json()

job_id = resp["job_id"]
while True:
    st = requests.get(f"{BASE}/api/v1/jobs/{job_id}").json()["item"]
    print(f"{st['status']} | {st['progress_float']}% | {st['stage_label']}")
    if st["status"] in ("success", "failed", "cancelled"):
        break
    time.sleep(3)`;

  const codeEl = document.getElementById("apiPythonSnippet");
  if (codeEl) codeEl.textContent = snippet;

  const bodyEl = document.getElementById("apiTestBody");
  if (bodyEl && !bodyEl.value.trim()) fillApiDemo("text_job");
}

function fillApiDemo(kind) {
  const firstModel = getValidModels()[0] || state.models[0];
  const mid = firstModel ? firstModel.id : 1;
  if (kind === "health") {
    document.getElementById("apiTestMethod").value = "GET";
    document.getElementById("apiTestPath").value = "/api/v1/health";
    document.getElementById("apiTestBody").value = "";
  } else if (kind === "text_job") {
    document.getElementById("apiTestMethod").value = "POST";
    document.getElementById("apiTestPath").value = "/api/v1/jobs";
    document.getElementById("apiTestBody").value = JSON.stringify({ model_id: mid, name: "接口测试视频", text: "你好，这是一条接口测试视频。", tts_params: { speed: 1.0, emo_mode: "same_as_speaker" } }, null, 2);
  } else if (kind === "audio_job") {
    document.getElementById("apiTestMethod").value = "POST";
    document.getElementById("apiTestPath").value = "/api/v1/jobs";
    document.getElementById("apiTestBody").value = JSON.stringify({ model_id: mid, name: "本地音频测试", audio_path: "E:\\录音\\口播1.wav" }, null, 2);
  }
}

async function runApiTest() {
  const method = document.getElementById("apiTestMethod").value;
  const path = document.getElementById("apiTestPath").value.trim();
  const rawBody = document.getElementById("apiTestBody").value.trim();
  const out = document.getElementById("apiTestResponse");
  out.textContent = "请求发送中...";
  try {
    const opts = { method };
    if (method !== "GET" && rawBody) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = rawBody;
    }
    const res = await api(path, opts);
    out.textContent = JSON.stringify(res, null, 2);
    toast("接口调用成功", "success", 2200);
    if (path.includes("/jobs")) await refreshAll(true);
  } catch (err) {
    out.textContent = `错误：${err.message}`;
    toast(`接口返回错误: ${err.message}`, "error");
  }
}

// ==================== 云端自动收片（整合于「我的作品」顶部单行条） ====================

function setCloudRemoteTab(tab) {
  state.cloudRemoteTab = tab === "audios" ? "audios" : "videos";
  document.querySelectorAll("#cloudRemoteTypeTabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.rtype === state.cloudRemoteTab);
  });
  renderCloudRemoteList(state.lastCloudSyncStatus || {});
}

function renderCloudRemoteList(st) {
  const remoteSub = document.getElementById("cloudRemoteCountSub");
  const remoteListEl = document.getElementById("cloudRemoteList");
  if (!remoteListEl) return;
  const tab = state.cloudRemoteTab || "videos";
  const busyNow = Boolean(st.active_download);

  if (tab === "audios") {
    const remoteAudios = st.cloud_audios || [];
    if (remoteSub) remoteSub.textContent = `${remoteAudios.length} 条`;
    if (!remoteAudios.length) {
      remoteListEl.innerHTML = `<div class="empty-box">${st.connected ? "云端目前暂无音频记录" : "连接云端后将列出云端配音音频"}</div>`;
      return;
    }
    remoteListEl.innerHTML = remoteAudios.map((ca) => {
      const isTts = ca.source_type === "tts" || ca.source_type === "indextts2";
      const srcTag = isTts ? `<span class="state-tag st-pending">云端配音</span>` : `<span class="state-tag st-muted">已上传</span>`;
      const badge = ca.synced ? `<span class="state-tag st-success">已拉回</span>` : `<span class="state-tag st-muted">未拉回</span>`;
      const btnLabel = ca.synced ? "重新拉回" : "手动拉回";
      return `
        <div class="cloud-item-row">
          <div style="min-width:0;flex:1;">
            <div class="inline-row" style="gap:6px;">
              <strong style="font-size:13px;word-break:break-all;">#${ca.id} ${esc(ca.name)}</strong>
              ${srcTag}
              ${badge}
            </div>
            <div class="avatar-sub">时长 ${fmtDuration(ca.duration)}</div>
          </div>
          <button class="btn btn-xs ${ca.synced ? "btn-secondary" : "btn-primary"}" ${busyNow ? "disabled" : ""} onclick="pullCloudAudioNow(${ca.id})">${btnLabel}</button>
        </div>`;
    }).join("");
    return;
  }

  const remoteVids = st.cloud_videos || [];
  if (remoteSub) remoteSub.textContent = `${remoteVids.length} 条`;
  if (!remoteVids.length) {
    remoteListEl.innerHTML = `<div class="empty-box">${st.connected ? "云端目前暂无已完成视频" : "连接云端后将列出云端作品"}</div>`;
    return;
  }
  remoteListEl.innerHTML = remoteVids.map((cv) => {
    const badge = cv.synced ? `<span class="state-tag st-success">已拉回</span>` : `<span class="state-tag st-muted">未拉回</span>`;
    const btnLabel = cv.synced ? "重新拉回" : "手动拉回";
    const sz = cv.drive_compact_size_mb
      ? `压小版 ${cv.drive_compact_size_mb}M / 原片 ${cv.file_size_mb || "?"}M`
      : `原片 ${cv.file_size_mb || "?"}M`;
    return `
      <div class="cloud-item-row">
        <div style="min-width:0;flex:1;">
          <div class="inline-row" style="gap:6px;">
            <strong style="font-size:13px;word-break:break-all;">#${cv.id} ${esc(cv.name)}</strong>
            ${badge}
          </div>
          <div class="avatar-sub">形象：${esc(cv.model_name || "-")} · ${fmtDuration(cv.duration)} · ${sz}</div>
        </div>
        <button class="btn btn-xs ${cv.synced ? "btn-secondary" : "btn-primary"}" ${busyNow ? "disabled" : ""} onclick="pullCloudVideoNow(${cv.id})">${btnLabel}</button>
      </div>`;
  }).join("");
}

function toggleCloudSyncDrawer() {
  state.cloudSyncDrawerOpen = !state.cloudSyncDrawerOpen;
  const drawer = document.getElementById("cloudSyncDrawer");
  const txt = document.getElementById("cloudSyncToggleText");
  const chev = document.getElementById("cloudSyncChevron");
  drawer?.classList.toggle("hidden", !state.cloudSyncDrawerOpen);
  if (txt) txt.textContent = state.cloudSyncDrawerOpen ? "收起设置" : "展开设置与手动拉回";
  if (chev) chev.style.transform = state.cloudSyncDrawerOpen ? "rotate(180deg)" : "";
  if (state.cloudSyncDrawerOpen) refreshCloudSyncStatus(true);
}

async function refreshCloudSyncStatus(fillInputs = false) {
  try {
    const res = await api("/api/v1/cloud-sync/status", { timeoutMs: 8000 });
    const st = res.status || {};
    state.lastCloudSyncStatus = st;
    state.cloudSyncActive = Boolean(st.active_download);

    if (fillInputs) {
      const urlInput = document.getElementById("cloudSyncUrlInput");
      const dirInput = document.getElementById("cloudSyncSaveDirInput");
      const qualSel = document.getElementById("cloudSyncQualitySelect");
      const enToggle = document.getElementById("cloudSyncEnabledToggle");
      const auToggle = document.getElementById("cloudSyncAudioToggle");
      if (urlInput) urlInput.value = st.cloud_url || "";
      if (dirInput) dirInput.value = st.save_dir || "E:\\数字人视频文件";
      if (qualSel) qualSel.value = st.quality || "compact";
      if (enToggle) enToggle.checked = Boolean(st.enabled);
      if (auToggle) auToggle.checked = st.sync_audio !== false;
    }

    const dot = document.getElementById("cloudSyncConnDot");
    const txt = document.getElementById("cloudSyncConnText");
    const stripBarWrap = document.getElementById("cloudSyncStripBarWrap");
    const stripBar = document.getElementById("cloudSyncStripBar");

    if (dot && txt) {
      if (st.active_download) {
        const ad = st.active_download;
        const kindTxt = ad.media_type === "audio" ? "配音" : "视频";
        dot.className = "status-dot dot-accent pulse";
        txt.textContent = `正在拉回${kindTxt} #${ad.video_id || ad.audio_id} ${ad.name || ""} · ${Math.round(ad.percent || 0)}%${ad.speed_mbs ? ` (${ad.speed_mbs} MB/s)` : ""}`;
        stripBarWrap?.classList.remove("hidden");
        if (stripBar) stripBar.style.width = `${Math.min(100, Math.max(4, ad.percent || 0))}%`;
      } else {
        stripBarWrap?.classList.add("hidden");
        if (!st.cloud_url) {
          dot.className = "status-dot dot-muted";
          txt.textContent = "未配置云端网址 · 点右侧展开设置";
        } else if (!st.enabled) {
          dot.className = "status-dot dot-muted";
          txt.textContent = `已暂停自动收片 · 目录 ${st.save_dir || "E:\\数字人视频文件"}`;
        } else if (st.connected) {
          dot.className = "status-dot dot-ok";
          txt.textContent = `已连接（云端 ${st.cloud_video_count || 0} 条视频、${st.cloud_audio_count || 0} 条配音，已拉回 ${st.synced_count || 0} 条）· 存至 ${st.save_dir || "E:\\数字人视频文件"}`;
        } else {
          dot.className = "status-dot dot-warn";
          txt.textContent = "云端暂未连接（后台自动重试中）";
        }
      }
    }

    const activeBox = document.getElementById("cloudSyncActiveBox");
    if (activeBox) {
      if (st.active_download) {
        const ad = st.active_download;
        activeBox.classList.remove("hidden");
        const tEl = document.getElementById("cloudSyncActiveTitle");
        const mEl = document.getElementById("cloudSyncActiveMeta");
        const bEl = document.getElementById("cloudSyncActiveBar");
        if (tEl) tEl.textContent = `${ad.stage || "拉回中"} · #${ad.video_id || ad.audio_id} ${ad.name || ""}`;
        if (mEl) mEl.textContent = ad.total_mb ? `${ad.percent || 0}% (${ad.downloaded_mb}/${ad.total_mb} MB · ${ad.speed_mbs || 0} MB/s)` : `${ad.percent || 0}%`;
        if (bEl) bEl.style.width = `${Math.min(100, Math.max(0, ad.percent || 0))}%`;
      } else {
        activeBox.classList.add("hidden");
      }
    }

    const errBox = document.getElementById("cloudSyncErrorBox");
    if (errBox) {
      if (st.last_error) {
        errBox.textContent = st.last_error;
        errBox.classList.remove("hidden");
      } else {
        errBox.classList.add("hidden");
      }
    }

    renderCloudRemoteList(st);

    const cntSub = document.getElementById("cloudSyncCountSub");
    const listEl = document.getElementById("cloudSyncList");
    const items = st.synced_items || [];
    if (cntSub) cntSub.textContent = `${st.synced_count || items.length} 条`;
    if (listEl) {
      if (!items.length) {
        listEl.innerHTML = `<div class="empty-box">暂未拉回任何云端作品</div>`;
      } else {
        listEl.innerHTML = items.map((it) => {
          const fname = (it.local_path || "").split(/[\\/]/).pop() || it.name || `#${it.video_id || it.audio_id}`;
          const savedAt = it.saved_at ? new Date(it.saved_at * 1000).toLocaleString("zh-CN") : "";
          const escapedPath = (it.local_path || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
          const kindTag = it.media_type === "audio" ? `<span class="state-tag st-pending">音频</span>` : `<span class="state-tag st-muted">视频</span>`;
          const statusTag = it.exists ? `<span class="state-tag st-success">在本地</span>` : `<span class="state-tag st-muted">已移动</span>`;
          return `
            <div class="cloud-item-row">
              <div style="min-width:0;flex:1;">
                <div class="inline-row" style="gap:6px;">
                  <strong style="font-size:13px;word-break:break-all;">${esc(fname)}</strong>
                  ${kindTag}
                  ${statusTag}
                </div>
                <div class="avatar-sub">${it.size_mb || 0} MB${savedAt ? ` · ${savedAt}` : ""}</div>
              </div>
              <button class="btn btn-xs btn-secondary" onclick="openCloudSyncFolder('${escapedPath}')">定位</button>
            </div>`;
        }).join("");
      }
    }

    const logPre = document.getElementById("cloudSyncLogPre");
    if (logPre) {
      const evs = st.recent_events || [];
      logPre.textContent = evs.length ? evs.join("\n") : "暂无日志";
    }
  } catch {}
}

async function saveCloudSyncConfig() {
  const cloud_url = (document.getElementById("cloudSyncUrlInput")?.value || "").trim();
  const save_dir = (document.getElementById("cloudSyncSaveDirInput")?.value || "").trim() || "E:\\数字人视频文件";
  const quality = document.getElementById("cloudSyncQualitySelect")?.value || "compact";
  const enabled = Boolean(document.getElementById("cloudSyncEnabledToggle")?.checked);
  const sync_audio = Boolean(document.getElementById("cloudSyncAudioToggle")?.checked);
  if (enabled && !cloud_url) return toast("请先填入云端创作台网址", "error");
  const btn = document.getElementById("btnSaveCloudSync");
  if (btn) btn.disabled = true;
  try {
    await api("/api/v1/cloud-sync/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled, sync_audio, cloud_url, save_dir, quality }),
    });
    toast(enabled ? "已开启云端自动收片与配音同步" : "已暂停云端自动收片", "success", 2600);
    await refreshCloudSyncStatus(true);
  } catch (err) {
    toast(`保存失败：${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function triggerCloudSyncNow() {
  try {
    await api("/api/v1/cloud-sync/sync-now", { method: "POST" });
    toast("已开始检测云端新作品与配音...", "success", 2200);
    setTimeout(() => refreshCloudSyncStatus(false), 1200);
  } catch (err) {
    toast(err.message, "error");
  }
}

async function pullCloudVideoNow(videoId) {
  try {
    const res = await api(`/api/v1/cloud-sync/pull/${videoId}`, { method: "POST" });
    toast(res.message || `已开始拉回 #${videoId} 号视频`, "success", 2600);
    state.cloudSyncActive = true;
    setTimeout(() => refreshCloudSyncStatus(false), 1000);
  } catch (err) {
    toast(`拉取失败：${err.message}`, "error");
  }
}

async function pullCloudAudioNow(audioId) {
  try {
    const res = await api(`/api/v1/cloud-sync/pull-audio/${audioId}`, { method: "POST" });
    toast(res.message || `已开始拉回 #${audioId} 号配音`, "success", 2600);
    state.cloudSyncActive = true;
    setTimeout(() => refreshCloudSyncStatus(false), 1000);
  } catch (err) {
    toast(`拉取失败：${err.message}`, "error");
  }
}

async function openCloudSyncFolder(filePath = "") {
  try {
    await api("/api/v1/cloud-sync/open-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_path: filePath || null }),
    });
  } catch (err) {
    toast(`打开目录失败：${err.message}`, "error");
  }
}

// ==================== 启动入口 ====================

async function boot() {
  initEmoSliders();
  initDropzone();
  applyClientModeVisibility();
  onTtsModelVersionChange();
  syncTuneSummaryAll();

  await refreshAll(true);
  refreshCloudSyncStatus(true);

  try {
    const cfgRes = await api("/api/v1/tts/config");
    const cfg = cfgRes.config || cfgRes.data || {};
    populateRegisteredPersonalVoices(cfgRes.registered_voices || []);
    applyTtsParamsToForm({
      model_version: cfg.index_tts_model_version,
      lang: cfg.index_tts_lang,
      speed: cfg.index_tts_speed,
      postprocess_mode: cfg.index_tts_postprocess_mode || "fixB",
      emo_mode: cfg.index_tts_emo_mode,
      emo_alpha: cfg.index_tts_emo_alpha,
      emo_vector: cfg.index_tts_emo_vector,
      emo_audio_prompt: cfg.index_tts_emo_audio_prompt,
      emo_text: cfg.index_tts_emo_text,
      diffusion_steps: cfg.index_tts_diffusion_steps,
      num_beams: cfg.index_tts_num_beams,
      interval_silence: cfg.index_tts_interval_silence,
      temperature: cfg.index_tts_temperature,
      top_p: cfg.index_tts_top_p,
      top_k: cfg.index_tts_top_k,
      repetition_penalty: cfg.index_tts_repetition_penalty,
      chunk_size: cfg.index_tts_chunk_size,
      auto_fast_on_long_text: cfg.index_tts_auto_fast_on_long_text,
    });
  } catch {}

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollLiveTick(false);
  });

  scheduleNextPoll();
}

window.addEventListener("DOMContentLoaded", boot);
