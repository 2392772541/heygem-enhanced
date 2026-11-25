const EMO_LABELS = ["喜", "怒", "哀", "惧", "厌恶", "低落", "惊喜", "平静"];

const VIEW_META = {
  create: {
    title: "数字人制作",
    subtitle: "选定人物、输入文案或上传录音，一站式对口型生成成片",
  },
  voice: {
    title: "语音合成台",
    subtitle: "独立精调 IndexTTS-2 音色、语速与情绪，满意一键带入数字人制作",
  },
  tasks: {
    title: "任务与进度",
    subtitle: "实时追踪配音与对口型渲染帧进度，支持随时中断与一键重试",
  },
  library: {
    title: "成品作品库",
    subtitle: "按分类管理视频成片与配音音频，支持在线预览、下载与定位源文件",
  },
  cloudsync: {
    title: "云端自动同步",
    subtitle: "连接 Colab 云端创作台，出片后自动 8 线程高速拉回本机目录，全程零黑窗免手动下载",
  },
  models: {
    title: "人物与声音",
    subtitle: "管理人物底模视频、分组与参考音色，支持补回缺失底模与本机导入",
  },
  system: {
    title: "系统与服务",
    subtitle: "查看本机与局域网/服务器访问地址，监控渲染引擎、语音引擎、显卡与实时日志",
  },
};

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
  missingPanelOpen: false,
  pickingFile: false,
  importingBusy: false,
  importingLabel: "",
  lastHealthFetchAt: 0,
  lastSuccessCount: -1,
  pollInFlight: false,
  backendDisconnected: false,
  disconnectCount: 0,
  lastDisconnectReason: "",
};

// ==================== 基础工具 ====================

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
  return `HTTP ${status}`;
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
  while (wrap.children.length >= 3) {
    wrap.firstElementChild?.remove();
  }
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
      const timeoutErr = new Error("请求超时，后台服务未响应");
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
  const modeText = local ? "本机" : "远程";
  const sideMode = document.getElementById("sideClientMode");
  if (sideMode) sideMode.textContent = modeText;

  const btnDelivery = document.getElementById("btnOpenDeliveryFolder");
  if (btnDelivery) btnDelivery.classList.toggle("hidden", !local);
  const btnPickLocalAudios = document.getElementById("btnPickLocalAudios");
  if (btnPickLocalAudios) btnPickLocalAudios.classList.toggle("hidden", !local);
  const btnPickEmoLocal = document.getElementById("btnPickEmoAudioLocal");
  if (btnPickEmoLocal) btnPickEmoLocal.classList.toggle("hidden", !local);
  const btnModalPick = document.getElementById("btnModalPickVideo");
  if (btnModalPick) btnModalPick.classList.toggle("hidden", !local);

  const sumAudioPath = document.getElementById("summaryLocalAudioPaths");
  if (sumAudioPath) {
    sumAudioPath.textContent = local
      ? "粘贴本机音频路径批量导入（可选）"
      : "粘贴服务器端音频路径批量导入（高级 · 可选）";
  }
  const sumVideoPath = document.getElementById("summaryModalLocalVideoPath");
  if (sumVideoPath) {
    sumVideoPath.textContent = local
      ? "手动粘贴本机视频路径（可选）"
      : "手动粘贴服务器端视频路径（高级 · 可选）";
  }
}

function toggleMobileSidebar() {
  const sb = document.getElementById("appSidebar");
  const bd = document.getElementById("sidebarBackdrop");
  if (!sb) return;
  const open = !sb.classList.contains("mobile-open");
  sb.classList.toggle("mobile-open", open);
  if (bd) bd.classList.toggle("hidden", !open);
}

function closeMobileSidebar() {
  document.getElementById("appSidebar")?.classList.remove("mobile-open");
  document.getElementById("sidebarBackdrop")?.classList.add("hidden");
}

// ==================== 视图导航 ====================

function switchView(viewName) {
  if (!VIEW_META[viewName]) viewName = "create";
  closeMobileSidebar();
  state.view = viewName;
  document.querySelectorAll(".nav-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === viewName);
  });
  document.querySelectorAll(".view").forEach((v) => {
    v.classList.toggle("active", v.id === `view-${viewName}`);
  });

  const meta = VIEW_META[viewName];
  const titleEl = document.getElementById("pageTitle");
  const subEl = document.getElementById("pageSubtitle");
  if (titleEl) titleEl.textContent = meta.title;
  if (subEl) subEl.textContent = meta.subtitle;

  const activeViewEl = document.getElementById(`view-${viewName}`);
  if (activeViewEl) activeViewEl.scrollTop = 0;

  if (viewName === "voice") {
    syncTextBetweenViews("create");
    renderVoiceHistoryList();
    updateVoiceStudioRefPreview();
  }
  if (viewName === "tasks") renderTasksView();
  if (viewName === "library") renderLibraryView();
  if (viewName === "cloudsync") refreshCloudSyncStatus(true);
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

// ==================== 初始化与后台轮询 ====================

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
    hint.innerHTML = `<span style="color:#fcd34d;">当前 8 维情感总权重 ${sum.toFixed(2)}（超过 0.80 将自动柔和归一化）</span>`;
  } else {
    hint.textContent = `当前 8 维情感总权重：${sum.toFixed(2)} / 0.80`;
  }
}

function initEmoSliders() {
  const box = document.getElementById("emoVectorBox");
  if (!box) return;
  box.innerHTML = EMO_LABELS.map(
    (label, idx) => `
      <div class="emo-slider">
        <span><strong>${label}</strong><em id="emoVal${idx}">0.00</em></span>
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

function bindVideoDropzone(el) {
  if (!el || el.dataset.dropBound === "1") return;
  el.dataset.dropBound = "1";
  ["dragenter", "dragover"].forEach((evt) =>
    el.addEventListener(evt, (e) => {
      e.preventDefault();
      el.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach((evt) =>
    el.addEventListener(evt, (e) => {
      e.preventDefault();
      el.classList.remove("dragover");
    })
  );
  el.addEventListener("drop", (e) => {
    const f = e.dataTransfer?.files?.[0];
    if (f) handleQuickUploadModelFile(f);
  });
}

function markBackendDisconnected(err) {
  const wasDisconnected = state.backendDisconnected;
  const isNetwork = err?.isNetworkError !== false;
  const httpStatus = Number(err?.httpStatus || 0);
  state.backendDisconnected = true;
  state.disconnectCount += 1;
  state.lastDisconnectReason = err?.message || "无法连接工作台后台";

  const chip = document.getElementById("topStatusChip");
  const dot = document.getElementById("topStatusDot");
  const txt = document.getElementById("topStatusText");
  if (chip) chip.className = "status-chip err";
  if (dot) dot.className = "dot err";
  if (txt) txt.textContent = isNetwork ? "后台未连接" : `后台异常 (${httpStatus})`;

  const sDot = document.getElementById("sideDotOverall");
  const sLab = document.getElementById("sideStatusLabel");
  if (sDot) sDot.className = "dot err";
  if (sLab) sLab.textContent = "后台未连接";

  const strip = document.getElementById("alertStrip");
  if (strip) {
    strip.className = "alert-strip";
    document.getElementById("alertText").textContent = isNetwork
      ? "与工作台后台断开连接，页面正在自动重连（也可重新双击桌面 Start-Studio 启动）。"
      : `后台接口返回异常 (HTTP ${httpStatus})：${state.lastDisconnectReason}`;
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
  toast("已重新连接后台服务", "success", 3000);
  return true;
}

async function refreshAll(silent = false, forceHealth = false) {
  try {
    const healthPromise = api(`/api/v1/health${forceHealth ? "?force=true" : ""}`, { timeoutMs: 15000 })
      .then((health) => {
        state.health = health;
        state.lastHealthFetchAt = Date.now();
        applyClientModeVisibility();
        renderHealthBar();
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

    const activeCnt = state.jobs.filter((j) => j.status === "pending" || j.status === "waiting").length;
    const navAct = document.getElementById("navActiveCount");
    const navSuc = document.getElementById("navSuccessCount");
    const navMod = document.getElementById("navModelCount");
    if (navAct) navAct.textContent = activeCnt;
    if (navSuc) navSuc.textContent = state.lastSuccessCount;
    if (navMod) navMod.textContent = getValidModels().length;

    const validAudioIds = new Set(state.audios.map((a) => a.id));
    for (const sid of Array.from(state.selectedAudioIds)) {
      if (!validAudioIds.has(sid)) state.selectedAudioIds.delete(sid);
    }

    const curModel = state.models.find((m) => m.id === state.selectedModelId);
    if (!curModel || !curModel.video_exists) {
      const firstValid = getValidModels()[0];
      state.selectedModelId = firstValid ? firstValid.id : null;
    }

    applyClientModeVisibility();
    renderSelectOptions();
    renderCreateModelList();
    renderSelectedModelStage();
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

    if (!silent || !state.health) {
      await healthPromise;
    }
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

    const activeCnt = state.jobs.filter((j) => j.status === "pending" || j.status === "waiting").length;
    const successCnt = state.jobs.filter((j) => j.status === "success").length;
    const navAct = document.getElementById("navActiveCount");
    const navSuc = document.getElementById("navSuccessCount");
    if (navAct) navAct.textContent = activeCnt;
    if (navSuc) navSuc.textContent = successCnt;

    renderLiveMonitor();
    if (state.view === "tasks") renderTasksView();
    if (state.view === "library" && successCnt !== state.lastSuccessCount) {
      renderLibraryView();
    }
    if (state.view === "cloudsync" || state.cloudSyncActive) {
      refreshCloudSyncStatus(false);
    }
    state.lastSuccessCount = successCnt;
    if (wasDisconnected) {
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
  const delay = state.backendDisconnected ? 3000 : (hasActive ? 2500 : 6000);
  setTimeout(async () => {
    await pollLiveTick(false);
    scheduleNextPoll();
  }, delay);
}

// ==================== 系统状态、本地与服务器访问面板 ====================

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

  // 更新「系统与服务」页面指标卡
  const dotDocker = document.getElementById("dotDocker");
  const txtDocker = document.getElementById("txtDocker");
  if (dotDocker) dotDocker.className = `dot ${dockerOk ? "ok" : "err"}`;
  if (txtDocker) txtDocker.textContent = dockerOk ? "已就绪" : "未启动";

  const dotTts = document.getElementById("dotTts");
  const txtTts = document.getElementById("txtTts");
  if (dotTts) dotTts.className = `dot ${ttsOk ? (ttsState && ttsState !== "idle" ? "warn" : "ok") : "err"}`;
  if (txtTts) txtTts.textContent = ttsOk ? (ttsState && ttsState !== "idle" ? "正在合成配音" : "已就绪") : "未启动";

  const dotQueue = document.getElementById("dotQueue");
  const txtQueue = document.getElementById("txtQueue");
  if (dotQueue) dotQueue.className = `dot ${qRun ? (qPause ? "warn" : "ok") : "err"}`;
  if (txtQueue) txtQueue.textContent = qRun ? (qPause ? "已暂停派发" : "正常运行中") : "未启动";

  // 侧边栏迷你态势灯
  const sdD = document.getElementById("sideDotDocker");
  const sdT = document.getElementById("sideDotTts");
  const sdQ = document.getElementById("sideDotQueue");
  if (sdD) sdD.className = `dot ${dockerOk ? "ok" : "err"}`;
  if (sdT) sdT.className = `dot ${ttsOk ? (ttsState && ttsState !== "idle" ? "warn" : "ok") : "err"}`;
  if (sdQ) sdQ.className = `dot ${qRun ? (qPause ? "warn" : "ok") : "err"}`;

  // 暂停/恢复按钮同步（系统页 & 任务页）
  [document.getElementById("btnPauseResume"), document.getElementById("btnTasksPauseResume")]
    .filter(Boolean)
    .forEach((btn) => {
      btn.textContent = qPause ? "恢复队列" : "暂停队列";
      btn.classList.toggle("btn-success", qPause);
    });

  // 显卡与磁盘
  const g = h.gpu;
  const gpuText = g?.available
    ? `${g.name.replace("NVIDIA GeForce ", "")} · ${(g.memory_used_mb / 1024).toFixed(1)}/${(g.memory_total_mb / 1024).toFixed(0)} GB · ${g.temperature_c}°C`
    : "未检测到 NVIDIA 显卡";
  const pillGpu = document.getElementById("pillGpu");
  if (pillGpu) pillGpu.textContent = gpuText;

  const disks = Array.isArray(h.disks) ? h.disks : [];
  const diskText = disks.length
    ? disks.map((d) => `${d.label} (${String(d.drive || "").replace(/\\+$/, "")}) 剩 ${d.free_gb} GB`).join(" · ")
    : "磁盘空间未知";
  const pillDisk = document.getElementById("pillDisk");
  if (pillDisk) pillDisk.textContent = diskText;

  const sideGpu = document.getElementById("sideGpuBrief");
  if (sideGpu) {
    sideGpu.textContent = g?.available
      ? `${g.name.replace("NVIDIA GeForce ", "")} · 显存 ${(g.memory_used_mb / 1024).toFixed(1)}G`
      : `端口 ${h.access?.port || 8390} · 点击管理`;
  }

  // 本机与局域网/服务器访问信息
  const access = h.access || {};
  const localUrl = access.local_url || `${window.location.protocol}//127.0.0.1:${window.location.port || 8390}`;
  const sysLocalUrl = document.getElementById("sysLocalUrl");
  if (sysLocalUrl) sysLocalUrl.textContent = localUrl;

  const clientLocal = isLocalBrowser();
  const modeBadge = document.getElementById("sysClientModeBadge");
  if (modeBadge) {
    modeBadge.className = `badge ${clientLocal ? "badge-success" : "badge-info"}`;
    modeBadge.textContent = clientLocal ? "当前：本机直接访问（支持秒选本机文件）" : "当前：局域网 / 服务器远程访问（网页上传下载模式）";
  }

  const lanWrap = document.getElementById("sysLanUrlsWrap");
  if (lanWrap) {
    const entries = Array.isArray(access.lan_entries) && access.lan_entries.length
      ? access.lan_entries
      : (Array.isArray(access.lan_urls) ? access.lan_urls.map((u) => ({ url: u, label: "局域网" })) : []);
    if (!access.lan_enabled) {
      lanWrap.innerHTML = `<span class="section-sub">当前仅监听本机 (${esc(access.bind_host || "127.0.0.1")})，需远程访问时设置 HEYGEM_STUDIO_HOST=0.0.0.0 重启即可。</span>`;
    } else if (entries.length === 0) {
      lanWrap.innerHTML = `<span class="section-sub">已监听所有网卡 (0.0.0.0:${esc(access.port || 8390)})，暂未检测到可用局域网 IPv4 地址。</span>`;
    } else {
      lanWrap.innerHTML = entries
        .map(
          (e) => `
          <div class="access-value-row">
            <div class="flex-row" style="gap:6px;min-width:0;">
              <span class="badge badge-info">${esc(e.label || "局域网")}</span>
              <code>${esc(e.url)}</code>
            </div>
            <button class="btn btn-xs" data-copy="${esc(e.url)}" onclick="copyText(this.dataset.copy)">复制地址</button>
          </div>`
        )
        .join("");
    }
  }

  // 更新左侧栏计数
  const activeCnt = (h.job_counts?.pending || 0) + (h.job_counts?.waiting || 0);
  const successCnt = h.job_counts?.success || 0;
  document.getElementById("navActiveCount").textContent = activeCnt;
  document.getElementById("navSuccessCount").textContent = successCnt;
  document.getElementById("navModelCount").textContent = getValidModels().length;

  // 顶部与侧栏状态胶囊
  const chip = document.getElementById("topStatusChip");
  const dot = document.getElementById("topStatusDot");
  const txt = document.getElementById("topStatusText");
  const sideDot = document.getElementById("sideDotOverall");
  const sideLab = document.getElementById("sideStatusLabel");

  const downParts = [];
  if (!dockerOk) downParts.push("视频渲染");
  if (!ttsOk) downParts.push("语音配音");
  if (!qRun) downParts.push("后台队列");

  if (downParts.length === 0) {
    if (qPause) {
      chip.className = "status-chip warn";
      dot.className = "dot warn";
      txt.textContent = "队列已暂停";
      if (sideDot) sideDot.className = "dot warn";
      if (sideLab) sideLab.textContent = "队列已暂停";
    } else {
      chip.className = "status-chip ok";
      dot.className = "dot ok";
      txt.textContent = "引擎全部就绪";
      if (sideDot) sideDot.className = "dot ok";
      if (sideLab) sideLab.textContent = "引擎全部就绪";
    }
  } else {
    chip.className = "status-chip err";
    dot.className = "dot err";
    txt.textContent = `未启动：${downParts.join(" / ")}`;
    if (sideDot) sideDot.className = "dot err";
    if (sideLab) sideLab.textContent = "后台服务未启动";
  }

  // 顶部异常单行提示条（仅关键运行问题才展示）
  const strip = document.getElementById("alertStrip");
  const alertText = document.getElementById("alertText");
  document.getElementById("btnAlertReconnect")?.classList.add("hidden");
  document.getElementById("btnAlertStart")?.classList.add("hidden");
  document.getElementById("btnAlertResume")?.classList.add("hidden");
  document.getElementById("btnAlertRecover")?.classList.add("hidden");

  if (downParts.length > 0) {
    strip.className = "alert-strip";
    alertText.textContent = `后台引擎尚未启动（${downParts.join("、")}），点击右侧「一键启动服务」即可开始制作视频。`;
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

  // 系统页诊断告警
  const sysAlertsWrap = document.getElementById("sysAlertsWrap");
  const alerts = (h.alerts || []).filter((a) => a.code !== "MODEL_FILES_MISSING");
  if (sysAlertsWrap) {
    if (!alerts.length) {
      sysAlertsWrap.classList.add("hidden");
      sysAlertsWrap.innerHTML = "";
    } else {
      sysAlertsWrap.classList.remove("hidden");
      sysAlertsWrap.innerHTML = alerts
        .map((a) => `<div class="error-box" style="margin-top:0;margin-bottom:6px;">${a.severity === "error" ? "[异常]" : "[提醒]"} ${esc(a.message)}</div>`)
        .join("");
    }
  }
}

async function handleStartAll() {
  const btns = [document.getElementById("btnStartAll"), document.getElementById("btnAlertStart")].filter(Boolean);
  btns.forEach((b) => { b.disabled = true; b.dataset.origText = b.textContent; b.textContent = "启动中..."; });
  toast("正在后台拉起视频渲染与语音服务，请稍候...", "info", 5000);
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
      <div class="job-card mb-8" style="padding:10px 12px;border-color:${s.ok ? "rgba(16,185,129,0.4)" : "rgba(239,68,68,0.5)"}">
        <div class="flex-between">
          <strong>${esc(s.step)}</strong>
          <span class="badge ${s.ok ? "badge-success" : "badge-danger"}">${s.ok ? "正常" : "异常"}</span>
        </div>
        <div class="section-sub mt-6">${esc(s.detail)}</div>
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
  if (!confirm("确认停止后台渲染与语音服务吗？（不会关闭当前网页，随时可点「一键启动全部服务」恢复）")) return;
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

// ==================== 制作台：人物选择、舞台与就绪检查 ====================

function handleQuickAddModelClick() {
  if (isLocalBrowser()) {
    quickPickAndImportModel();
  } else {
    openAddModelModal();
  }
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
  const needsVoiceAttention = hasModel && state.createMode === "tts" && !effectiveVoice;

  const voiceBar = document.getElementById("voiceBar");
  if (voiceBar) voiceBar.classList.toggle("needs-attention", needsVoiceAttention);
  const upVoiceBtn = document.getElementById("btnUploadVoiceQuick");
  if (upVoiceBtn) upVoiceBtn.classList.toggle("btn-warning", needsVoiceAttention && usableVoiceCount === 0);

  const hint = document.getElementById("wizardReadyHint");
  if (hint) {
    if (state.importingBusy) {
      hint.className = "ready-summary";
      hint.textContent = state.importingLabel || "正在导入人物视频并提取声音，请稍候...";
    } else if (!hasModel) {
      hint.className = "ready-summary";
      hint.textContent = "第 1 步：请先在左侧选择或添加一段人物视频";
    } else if (!hasContent) {
      hint.className = "ready-summary";
      hint.textContent = state.createMode === "tts"
        ? `已选「${selModel.name}」 · 请在中间输入口播文案`
        : `已选「${selModel.name}」 · 请选择或上传至少一条录音`;
    } else if (state.createMode === "tts" && !effectiveVoice) {
      hint.className = "ready-summary";
      hint.textContent = usableVoiceCount > 0
        ? `「${selModel.name}」无原声，请在上方的配音声音里选一个声音`
        : `「${selModel.name}」无原声且声音库为空，请先上传一段参考声音`;
    } else {
      hint.className = "ready-summary ready";
      if (state.createMode === "tts") {
        hint.textContent = `已就绪：人物「${selModel.name}」 + ${text.length} 字文案（约 ${fmtDuration(Math.round(text.length / 4.2))}）`;
      } else {
        hint.textContent = `已就绪：人物「${selModel.name}」 + ${audioCount} 条录音`;
      }
    }
  }

  const batchBtn = document.getElementById("btnBatchCreateAudios");
  if (batchBtn) {
    batchBtn.textContent = audioCount > 1 ? `批量制作 ${audioCount} 条数字人视频` : "开始制作数字人视频";
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

  // 作品分类下拉（制作台 & 语音合成台）
  [document.getElementById("jobCategorySelect"), document.getElementById("voiceCategorySelect")]
    .filter(Boolean)
    .forEach((catSel) => {
      const curCat = catSel.value;
      catSel.innerHTML = `<option value="">默认根目录（不分类）</option>` +
        state.categories.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
      if (curCat && state.categories.some((c) => String(c.id) === String(curCat))) {
        catSel.value = curCat;
      } else {
        catSel.value = "";
      }
    });

  // 配音声音下拉（制作台 & 语音合成台）
  const selModel = getSelectedValidModel();
  const usableVoices = state.voices.filter((v) => v.exists);
  let defaultLabel = "自动跟随所选人物原声";
  if (selModel) {
    if (selModel.audio_exists) {
      defaultLabel = `跟随人物原声（${selModel.name}）`;
    } else if (usableVoices.length > 0) {
      defaultLabel = `当前人物无原声，请在下方选声音`;
    } else {
      defaultLabel = `当前人物无原声，请先上传声音`;
    }
  }
  const voiceOpts = `<option value="">${esc(defaultLabel)}</option>` +
    usableVoices.map((v) => `<option value="${v.id}">${esc(v.display_name)} (#${v.id})</option>`).join("");

  [document.getElementById("ttsVoiceSelect"), document.getElementById("voiceStudioVoiceSelect")]
    .filter(Boolean)
    .forEach((vSel) => {
      const curV = vSel.value;
      vSel.innerHTML = voiceOpts;
      if (curV && usableVoices.some((v) => String(v.id) === String(curV))) {
        vSel.value = curV;
      } else {
        vSel.value = "";
      }
    });

  // 预设下拉（制作台 & 语音合成台同步）
  const presetOpts = `<option value="">-- 风格预设 --</option>` +
    state.presets.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  [document.getElementById("ttsPresetSelect"), document.getElementById("createPresetSelect")]
    .filter(Boolean)
    .forEach((pSel) => {
      const curP = pSel.value;
      pSel.innerHTML = presetOpts;
      if (curP && state.presets.some((p) => String(p.id) === String(curP))) {
        pSel.value = curP;
      } else {
        pSel.value = "";
      }
    });

  const mg = document.getElementById("modelModalGroup");
  if (mg) {
    mg.innerHTML = `<option value="">不分组</option>` +
      state.modelGroups.map((g) => `<option value="${g.id}">${esc(g.name)}</option>`).join("");
  }

  const badge = document.getElementById("createModelCountBadge");
  if (badge) badge.textContent = validModels.length;
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
  ph.innerHTML = `<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="2" y="4" width="20" height="16" rx="2"></rect><polygon points="10 9 16 12 10 15 10 9"></polygon></svg><span>视频封面暂不可用</span>`;
  imgEl.replaceWith(ph);
}

function buildDiscoverHtml(unimportedList, compact = false) {
  if (!unimportedList.length || !isLocalBrowser()) return "";
  if (compact) {
    return `
      <details class="adv-box mt-10">
        <summary>发现电脑里还有 ${unimportedList.length} 个未导入拍摄视频</summary>
        <div style="display:flex;flex-direction:column;gap:6px;margin-top:8px;">
          ${unimportedList.map((v) => `
            <div class="discover-item">
              <div class="discover-item-info">
                <div class="discover-item-name" title="${esc(v.filename)}">${esc(v.name)}</div>
                <div class="section-sub" style="font-size:11px;">${fmtSizeMb(v.size_mb)}</div>
              </div>
              <button class="btn btn-xs btn-primary" ${state.importingBusy ? "disabled" : ""} data-path="${esc(v.path)}" onclick="importDiscoveredVideoByPath(this.dataset.path)">添加</button>
            </div>`).join("")}
        </div>
      </details>`;
  }
  return `
    <div class="discover-box">
      <div class="flex-between mb-8">
        <strong style="font-size:12.5px;color:#bfdbfe;">发现你电脑里有现成的拍摄视频，点一下即可导入：</strong>
      </div>
      <div class="discover-list">
        ${unimportedList.map((v) => `
          <div class="discover-item">
            <div class="discover-item-info">
              <div class="discover-item-name" title="${esc(v.filename)}">${esc(v.name)}</div>
              <div class="section-sub" style="font-size:11px;">${esc(v.filename)} · ${fmtSizeMb(v.size_mb)}</div>
            </div>
            <button class="btn btn-xs btn-primary" ${state.importingBusy ? "disabled" : ""} data-path="${esc(v.path)}" onclick="importDiscoveredVideoByPath(this.dataset.path)">导入</button>
          </div>`).join("")}
      </div>
    </div>`;
}

function renderSelectedModelStage() {
  const stage = document.getElementById("selectedModelStage");
  if (!stage) return;
  const sel = getSelectedValidModel();
  const validModels = getValidModels();
  const unimportedDiscovered = state.discoveredVideos.filter((v) => !v.already_imported);

  if (validModels.length === 0) {
    const local = isLocalBrowser();
    stage.innerHTML = `
      <div class="empty-onboarding" id="emptyOnboardingDrop">
        <div class="empty-onboarding-title">
          ${state.importingBusy ? esc(state.importingLabel || "正在导入人物视频并提取声音，请稍候...") : "先放进一段真人说话视频，就能开始制作数字人"}
        </div>
        <div class="empty-onboarding-sub">
          支持 MP4 / MOV，自动保存为人物底模并提取原声供配音克隆
        </div>
        <div class="empty-actions">
          ${local ? `<button class="btn btn-primary" ${state.importingBusy ? "disabled" : ""} onclick="quickPickAndImportModel()">从电脑选择视频（秒导入）</button>` : ""}
          <button class="btn ${local ? "" : "btn-primary"}" ${state.importingBusy ? "disabled" : ""} onclick="document.getElementById('quickUploadVideoInput').click()">网页上传视频</button>
        </div>
        ${buildDiscoverHtml(unimportedDiscovered, false)}
      </div>`;
    bindVideoDropzone(document.getElementById("emptyOnboardingDrop"));
    return;
  }

  if (!sel) {
    stage.innerHTML = `<div class="section-sub" style="padding:8px 4px;">请在左侧点击选择一位人物视频。</div>`;
    return;
  }

  const groupName = sel.group_name ? `<span class="badge badge-muted">${esc(sel.group_name)}</span>` : "";
  const voiceBadge = sel.audio_exists
    ? `<span class="badge badge-success">含原声</span>`
    : `<span class="badge badge-warning">无原声 · 需右侧选声</span>`;

  stage.innerHTML = `
    <div class="stage-hero-row">
      <div class="stage-hero-thumb" onclick="previewModelVideo(${sel.id})" title="点击预览人物视频">
        ${sel.thumb_url
          ? `<img src="${esc(sel.thumb_url)}" alt="${esc(sel.name)}" data-preview="${esc(sel.preview_url)}" onerror="handleThumbFallback(this, this.dataset.preview)" />`
          : `<div class="media-cover-placeholder">暂无封面</div>`}
        <div class="stage-hero-play-overlay"><div class="stage-hero-play-circle">▶</div></div>
      </div>
      <div class="stage-hero-info">
        <div class="stage-hero-top">
          <div class="stage-hero-name">
            <span class="stage-hero-name-text" title="${esc(sel.name)}">${esc(sel.name)}</span>
            ${voiceBadge}
            ${groupName}
          </div>
          <div class="stage-hero-actions">
            <button class="btn btn-xs" onclick="previewModelVideo(${sel.id})">预览视频</button>
            ${sel.audio_exists ? `<button class="btn btn-xs" onclick="playSelectedModelVoice()">听原声</button>` : ""}
            <button class="btn btn-xs btn-ghost" onclick="switchView('models')">管理</button>
          </div>
        </div>
        <div class="stage-hero-meta" title="${esc(sel.name)} · ${fmtSizeMb(sel.video_size_mb)}">
          ${fmtSizeMb(sel.video_size_mb)} · ${sel.audio_exists ? "默认自动用 TA 的原声克隆配音" : "该底模无音轨，请在右侧指定参考声音"}
        </div>
      </div>
    </div>
    ${sel.audio_exists ? `<div id="stageModelVoiceWrap" class="hidden mt-8"><audio id="stageModelVoicePlayer" controls></audio></div>` : ""}`;
}

function playSelectedModelVoice() {
  const m = getSelectedValidModel();
  if (!m?.voice_preview_url) return toast("当前人物没有可试听的原声", "warning");
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
      importingBanner.textContent = state.importingLabel || "正在导入人物视频并提取原声，请稍候...";
    } else {
      importingBanner.classList.add("hidden");
      importingBanner.textContent = "";
    }
  }

  if (filterBar) {
    filterBar.classList.toggle("hidden", validModels.length <= 4);
  }

  if (validModels.length === 0) {
    list.innerHTML = `<div class="section-sub" style="text-align:center;padding:24px 8px;">暂无可用人物，点上方「+ 添加」或在右侧舞台拖入人物视频。</div>`;
    if (discoverWrap) discoverWrap.innerHTML = "";
  } else {
    const gf = document.getElementById("createModelGroupFilter")?.value || "all";
    const kw = (document.getElementById("createModelSearch")?.value || "").trim().toLowerCase();
    const filtered = validModels.filter((m) => {
      if (gf === "ungrouped" && m.group_id) return false;
      if (gf !== "all" && gf !== "ungrouped" && String(m.group_id) !== String(gf)) return false;
      if (kw && !(m.name || "").toLowerCase().includes(kw)) return false;
      return true;
    });

    if (filtered.length === 0) {
      list.innerHTML = `<div class="section-sub" style="text-align:center;padding:22px 8px;">当前筛选下没有匹配的人物。</div>`;
    } else {
      list.innerHTML = `<div class="avatar-grid-compact">
        ${filtered.map((m) => {
          const sel = m.id === state.selectedModelId;
          return `
            <div class="avatar-card ${sel ? "selected" : ""}" onclick="selectModel(${m.id})">
              <div class="avatar-thumb">
                <img src="${esc(m.thumb_url)}" alt="${esc(m.name)}" loading="lazy" data-preview="${esc(m.preview_url)}" onerror="handleThumbFallback(this, this.dataset.preview)" />
                <span class="avatar-check">✓</span>
                <button class="avatar-play-btn" title="预览视频" onclick="event.stopPropagation();previewModelVideo(${m.id})">▶</button>
              </div>
              <div class="avatar-meta">
                <div class="avatar-name" title="${esc(m.name)}">${esc(m.name)}</div>
                <div class="avatar-sub">${fmtSizeMb(m.video_size_mb)} · ${m.audio_exists ? "有声" : "无声"}</div>
              </div>
            </div>`;
        }).join("")}
      </div>`;
    }

    if (discoverWrap) {
      discoverWrap.innerHTML = buildDiscoverHtml(unimportedDiscovered, true);
    }
  }

  if (missingModels.length > 0 && missingWrap) {
    missingWrap.classList.remove("hidden");
    missingWrap.innerHTML = `
      <div class="missing-collapse">
        <div class="missing-collapse-head">
          <span class="section-sub" style="color:#fcd34d;">有 ${missingModels.length} 条历史人物文件缺失</span>
          <button class="btn btn-xs" onclick="switchView('models')">去处理</button>
        </div>
      </div>`;
  } else if (missingWrap) {
    missingWrap.classList.add("hidden");
    missingWrap.innerHTML = "";
  }
}

function selectModel(id) {
  state.selectedModelId = id;
  renderCreateModelList();
  renderSelectedModelStage();
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
  renderSelectedModelStage();
  updateWizardIndicators();
}

async function pickLocalFileSafe(kind = "video", multi = false) {
  if (!isLocalBrowser()) return { remote: true, cancelled: true, paths: [] };
  if (state.pickingFile) {
    toast("系统选文件窗口已经打开，请在桌面或任务栏查看", "info", 2800);
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
      toast(err.message || "文件选择窗口已打开，请先完成选择", "info", 2800);
      return { busy: true, cancelled: true, paths: [] };
    }
    if (err.httpStatus === 403) {
      return { remote: true, cancelled: true, paths: [] };
    }
    throw err;
  } finally {
    state.pickingFile = false;
  }
}

async function quickPickAndImportModel() {
  if (state.importingBusy) return toast("正在导入视频，请稍候...", "info");
  try {
    const pick = await pickLocalFileSafe("video", false);
    if (pick.remote) {
      openAddModelModal();
      return;
    }
    if (pick.cancelled || !pick.paths?.length) return;
    const chosenPath = pick.paths[0];
    await importModelByLocalPath(chosenPath, fileStemFromPath(chosenPath));
  } catch (err) {
    toast(`无法打开系统选文件窗口，已切换为添加弹窗：${err.message}`, "warning", 4200);
    openAddModelModal();
  }
}

async function importDiscoveredVideoByPath(targetPath) {
  if (!targetPath) return;
  if (state.importingBusy) return toast("正在导入视频，请稍候...", "info");
  await importModelByLocalPath(targetPath, fileStemFromPath(targetPath));
}

async function importModelByLocalPath(localPath, autoName) {
  const name = autoName || fileStemFromPath(localPath) || "新人物";
  setImportingState(true, `正在导入「${name}」并提取声音...`);
  toast(`正在导入「${name}」（大视频秒复制并提取原声）...`, "info", 4200);
  try {
    const fd = new FormData();
    fd.append("name", name);
    fd.append("local_video_path", localPath);
    const res = await api("/api/v1/models", { method: "POST", body: fd, timeoutMs: 300000 });
    if (res?.item?.id) state.selectedModelId = res.item.id;
    toast(`人物「${res?.item?.name || name}」已添加完成`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`导入人物视频失败: ${err.message}`, "error", 6000);
  } finally {
    setImportingState(false);
  }
}

async function handleQuickUploadModelFile(file) {
  if (!file) return;
  const inputEl = document.getElementById("quickUploadVideoInput");
  if (inputEl) inputEl.value = "";
  if (state.importingBusy) return toast("正在导入视频，请稍候...", "info");
  const name = file.name.replace(/\.[^.]+$/, "").trim() || "新人物";
  setImportingState(true, `正在上传「${name}」并提取声音...`);
  toast(`正在上传「${name}」，请勿关闭页面...`, "info", 4200);
  try {
    const fd = new FormData();
    fd.append("name", name);
    fd.append("file", file);
    const res = await api("/api/v1/models", { method: "POST", body: fd, timeoutMs: 300000 });
    if (res?.item?.id) state.selectedModelId = res.item.id;
    toast(`人物「${res?.item?.name || name}」上传并添加完成`, "success");
    await refreshAll(true, true);
  } catch (err) {
    toast(`上传人物视频失败: ${err.message}`, "error", 6000);
  } finally {
    setImportingState(false);
  }
}

async function quickRelinkModelFromDisk(modelId) {
  const m = state.models.find((x) => x.id === modelId);
  if (!m) return;
  if (!isLocalBrowser()) {
    openRelinkModelModal(m.id, m.name || `#${m.id}`);
    return;
  }
  try {
    const pick = await pickLocalFileSafe("video", false);
    if (pick.remote) {
      openRelinkModelModal(m.id, m.name || `#${m.id}`);
      return;
    }
    if (pick.cancelled || !pick.paths?.length) return;
    toast(`正在为「${m.name}」补回原视频...`, "info", 3800);
    const fd = new FormData();
    fd.append("local_video_path", pick.paths[0]);
    const res = await api(`/api/v1/models/${modelId}/relink`, { method: "POST", body: fd, timeoutMs: 300000 });
    if (res?.item?.id) state.selectedModelId = res.item.id;
    toast(`人物「${m.name}」原视频已补回，现在可以正常使用`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`补回失败: ${err.message}`, "error", 5500);
  }
}

async function handleCleanupMissingModels() {
  const missing = getMissingModels();
  if (!missing.length) return toast("当前没有缺失底模记录", "info");
  if (!confirm(`确定清理这 ${missing.length} 条原视频已缺失的历史人物记录吗？（只会清理列表里的失效记录，不会删除任何视频文件，也不会删除已完成的作品）`)) return;
  try {
    const res = await api("/api/v1/models/cleanup-missing", { method: "POST" });
    state.missingPanelOpen = false;
    const deletedCount = Number(res.deleted_count ?? 0);
    const skipMsg = res.skipped_active_ids?.length
      ? `（已跳过 ${res.skipped_active_ids.length} 条有进行中任务的记录）`
      : "";
    toast(`已清理 ${deletedCount} 条失效人物记录${skipMsg}`, "success", 4500);
    await refreshAll(true, true);
  } catch (err) {
    toast(`清理失败: ${err.message}`, "error");
  }
}

// ==================== 制作台 & 语音合成台：文案、声音与 IndexTTS-2 双向同步 ====================

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
  if (source === "create" && voiceEl.value !== createEl.value) {
    voiceEl.value = createEl.value;
  } else if (source === "voice" && createEl.value !== voiceEl.value) {
    createEl.value = voiceEl.value;
  }
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
  const estLabel = txt.length === 0 ? "0 秒" : fmtDuration(Math.max(1, estSec));
  const label = `${txt.length} 字 · 约 ${estLabel}`;
  const s1 = document.getElementById("textStats");
  const s2 = document.getElementById("voiceTextStats");
  if (s1) s1.textContent = label;
  if (s2) s2.textContent = label;
}

function onCreateVoiceSelectChanged() {
  const val = document.getElementById("ttsVoiceSelect")?.value || "";
  const v2 = document.getElementById("voiceStudioVoiceSelect");
  if (v2 && v2.value !== val) v2.value = val;
  updateVoicePreview();
  updateVoiceStudioRefPreview();
}

function onVoiceStudioVoiceChanged() {
  const val = document.getElementById("voiceStudioVoiceSelect")?.value || "";
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
  const m = getSelectedValidModel();
  return m?.voice_preview_url || "";
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
    if (wrap) wrap.classList.add("hidden");
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
    if (wrap) wrap.classList.add("hidden");
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

function setCreateMode(mode) {
  state.createMode = mode;
  document.querySelectorAll("#createModeTabs button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  document.getElementById("createModeTts")?.classList.toggle("hidden", mode !== "tts");
  document.getElementById("createModeAudio")?.classList.toggle("hidden", mode !== "audio");
  document.getElementById("createVoiceInspector")?.classList.toggle("hidden", mode !== "tts");
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
  const num = Number(val);
  const vEl = document.getElementById("ttsSpeed");
  const cEl = document.getElementById("createSpeedRange");
  if (vEl && String(vEl.value) !== String(num)) vEl.value = String(num);
  if (cEl && String(cEl.value) !== String(num)) cEl.value = String(num);
  const s1 = document.getElementById("createValSpeed");
  const s2 = document.getElementById("valSpeed");
  if (s1) s1.textContent = `${num.toFixed(2)}x`;
  if (s2) s2.textContent = `${num.toFixed(2)}x`;
  syncTuneSummaryAll();
}

function onTtsModelVersionChange() {
  const v = document.getElementById("ttsModelVersion")?.value || "IndexTTS-2";
  const langField = document.getElementById("ttsLangField");
  if (langField) langField.style.opacity = v === "IndexTTS-2.5" ? "1" : "0.5";
  const badge = document.getElementById("voiceEngineBadge");
  if (badge) badge.textContent = v;
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
    emo_vector: "8维情感",
    emo_audio: "情感参考音",
    emo_text: "自然语言情感",
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
  if (badge) {
    badge.textContent = `${emoLabel} · ${speed}x`;
  }

  const emoHint = document.getElementById("createEmoHint");
  if (emoHint) {
    if (state.emoMode === "same_as_speaker") {
      emoHint.textContent = "自动保持参考人声的自然语气。";
    } else if (state.emoMode === "emo_vector") {
      const vec = EMO_LABELS.map((lb, i) => ({ lb, v: Number(document.getElementById(`emoSlider${i}`)?.value || 0) }))
        .filter((x) => x.v > 0)
        .map((x) => `${x.lb} ${x.v.toFixed(2)}`);
      emoHint.textContent = vec.length
        ? `当前 8 维情感：${vec.join("、")}`
        : "尚未拖动 8 维滑块，点上方「精细调 8 维情感 →」配置。";
    } else if (state.emoMode === "emo_audio") {
      const p = cleanLocalPath(document.getElementById("ttsEmoAudioPath")?.value);
      emoHint.textContent = p ? `已选情感音频：${fileStemFromPath(p)}` : "还未选择情感参考音频，请前往语音合成台选择。";
    } else if (state.emoMode === "emo_text") {
      const t = (document.getElementById("ttsEmoText")?.value || "").trim();
      emoHint.textContent = t ? `情感描述：「${t.slice(0, 26)}」` : "还未输入情感描述词，请前往语音合成台填写。";
    }
  }
}

function collectTtsParams() {
  const vec = EMO_LABELS.map((_, i) => Number(document.getElementById(`emoSlider${i}`)?.value || 0));
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
  if (hasVal(p.speed)) {
    document.getElementById("ttsSpeed").value = p.speed;
    const cs = document.getElementById("createSpeedRange");
    if (cs) cs.value = p.speed;
    syncRangeVal("ttsSpeed", "valSpeed", "x");
    const cv = document.getElementById("createValSpeed");
    if (cv) cv.textContent = `${Number(p.speed).toFixed(2)}x`;
  }
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
    toast(`已应用预设：${preset.name}`, "success", 2200);
  } else {
    syncTuneSummaryAll();
  }
}

function applySelectedPreset() {
  applyPresetById(document.getElementById("ttsPresetSelect")?.value || "");
}

async function saveCurrentAsPreset() {
  const name = prompt("给这套语音参数预设起个名字：", "我的专属口播参数");
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
      const pid = String(res.item.id);
      if (document.getElementById("ttsPresetSelect")) document.getElementById("ttsPresetSelect").value = pid;
      if (document.getElementById("createPresetSelect")) document.getElementById("createPresetSelect").value = pid;
      syncTuneSummaryAll();
    }
  } catch (err) {
    toast(`保存预设失败: ${err.message}`, "error");
  }
}

async function deleteSelectedPreset() {
  const sel = document.getElementById("ttsPresetSelect");
  const pid = sel?.value;
  if (!pid) return toast("请先在预设下拉框中选择一个要删除的预设", "warning");
  const preset = state.presets.find((x) => String(x.id) === String(pid));
  const label = preset ? `「${preset.name}」` : `#${pid}`;
  if (!confirm(`确定删除参数预设 ${label} 吗？`)) return;
  try {
    await api(`/api/v1/tts/presets/${pid}`, { method: "DELETE" });
    sel.value = "";
    if (document.getElementById("createPresetSelect")) document.getElementById("createPresetSelect").value = "";
    toast(`已删除预设 ${label}`, "success");
    await refreshAll(true);
    syncTuneSummaryAll();
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
    toast("已保存为系统默认语音参数", "success");
  } catch (err) {
    toast(`保存失败: ${err.message}`, "error");
  }
}

async function pickEmoAudioFile() {
  try {
    const pick = await pickLocalFileSafe("audio", false);
    if (pick.remote) {
      document.getElementById("emoAudioFileInput")?.click();
      return;
    }
    if (pick.cancelled || !pick.paths?.length) return;
    document.getElementById("ttsEmoAudioPath").value = pick.paths[0];
    syncTuneSummaryAll();
    toast("已选择情感参考音频", "success", 2200);
  } catch (err) {
    toast(`打开系统选文件失败，已切为网页上传：${err.message}`, "warning", 4000);
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
    toast(`情感参考音上传成功: ${res.filename}`, "success");
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

// ==================== 试听合成（制作台 & 独立语音合成台共用核心流程） ====================

async function runTtsSynthesis({ source }) {
  const text = (document.getElementById(source === "voice" ? "voiceTextInput" : "ttsTextInput")?.value || "").trim();
  if (!text) return toast("请先输入要合成语音的口播文案", "warning");
  const voiceId = getEffectiveVoiceId();
  if (!voiceId) return toast("请先选择参考声音，或先在数字人制作台选一位含原声的人物", "warning");
  const ttsParams = collectTtsParams();
  if (ttsParams.emo_mode === "emo_audio" && !ttsParams.emo_audio_prompt) {
    return toast("当前为「情感参考音频」模式，请先选择一段情感音频", "warning");
  }

  const btns = [document.getElementById("btnSynthesizeTts"), document.getElementById("btnVoiceStudioSynthesize")].filter(Boolean);
  const liveBoxes = [document.getElementById("ttsLiveBox"), document.getElementById("voiceLiveBox")].filter(Boolean);
  const liveMsgs = [document.getElementById("ttsLiveMsg"), document.getElementById("voiceLiveMsg")].filter(Boolean);
  const liveBars = [document.getElementById("ttsLiveBar"), document.getElementById("voiceLiveBar")].filter(Boolean);

  btns.forEach((b) => (b.disabled = true));
  liveBoxes.forEach((el) => el.classList.remove("hidden"));
  liveMsgs.forEach((el) => (el.textContent = "正在连接 IndexTTS-2 语音引擎并合成配音..."));
  liveBars.forEach((el) => (el.style.width = "8%"));

  clearInterval(state.ttsPollingTimer);
  state.ttsPollingTimer = setInterval(async () => {
    try {
      const st = await api("/api/v1/tts/status");
      const d = st.data || {};
      if (d.message) {
        const line = `${d.message}${d.elapsed_seconds ? `（已用 ${d.elapsed_seconds}s）` : ""}`;
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

    // 同步更新制作台试听条与语音合成台最新成果卡
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

    toast("配音已生成，满意可直接一键用于制作数字人视频", "success");
    await refreshAll(true);
  } catch (err) {
    toast(`语音合成失败: ${err.message}`, "error", 6000);
  } finally {
    clearInterval(state.ttsPollingTimer);
    btns.forEach((b) => (b.disabled = false));
    liveBoxes.forEach((el) => el.classList.add("hidden"));
  }
}

async function handleSynthesizeTts() {
  await runTtsSynthesis({ source: "create" });
}

async function handleVoiceStudioSynthesize() {
  await runTtsSynthesis({ source: "voice" });
}

async function handleCancelTts() {
  try {
    const res = await api("/api/v1/tts/cancel", { method: "POST" });
    toast(res.msg || "已发送中断配音指令", "warning");
  } catch (err) {
    toast(`中断失败: ${err.message}`, "error");
  }
}

function useLatestVoiceAssetForVideo() {
  if (!state.lastAuditionAsset) return toast("暂无最新合成音频", "warning");
  quickVideoFromAudio(state.lastAuditionAsset.id);
}

function renderVoiceHistoryList() {
  const wrap = document.getElementById("voiceHistoryList");
  const badge = document.getElementById("voiceAudioCountBadge");
  if (!wrap) return;
  if (badge) badge.textContent = state.audios.length;

  const kw = (document.getElementById("voiceHistorySearch")?.value || "").trim().toLowerCase();
  const items = state.audios.filter((a) => !kw || `${a.name} ${a.text_content}`.toLowerCase().includes(kw));

  if (!items.length) {
    wrap.innerHTML = `<div class="section-sub" style="text-align:center;padding:28px 10px;">暂无已合成或导入的音频。左侧输入文案并点击「合成配音音频」即可试听并保存。</div>`;
    return;
  }

  const local = isLocalBrowser();
  wrap.innerHTML = items
    .map(
      (a) => `
      <div class="voice-history-card">
        <div class="flex-between mb-6" style="flex-wrap:nowrap;">
          <strong style="font-size:13px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(a.name)}">${esc(a.name)}</strong>
          <span class="badge badge-info" style="flex-shrink:0;">${formatAudioSourceLabel(a.source_type)} · ${fmtDuration(a.duration)}</span>
        </div>
        ${a.text_content ? `<div class="section-sub mb-6" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(a.text_content)}">${esc(a.text_content)}</div>` : ""}
        ${a.stream_url ? `<audio src="${esc(a.stream_url)}" controls class="mb-8"></audio>` : `<div class="section-sub mb-8" style="color:#fca5a5;">音频文件已缺失</div>`}
        <div class="flex-row" style="gap:5px;">
          <button class="btn btn-xs btn-primary" onclick="quickVideoFromAudio(${a.id})">去做数字人</button>
          ${a.download_url ? `<a class="btn btn-xs" href="${esc(a.download_url)}" download>下载</a>` : ""}
          <button class="btn btn-xs" onclick="renameAudioAsset(${a.id})">改名</button>
          ${local ? `<button class="btn btn-xs" onclick="openAudioPath(${a.id})">定位</button>` : ""}
          <button class="btn btn-xs btn-danger" onclick="deleteAudioAsset(${a.id})">删除</button>
        </div>
      </div>`
    )
    .join("");
}

// ==================== 提交数字人视频制作 ====================

function notifyPostSubmit(jobId, extraText = "") {
  trackSessionJobId(jobId);
  if (!isEngineReady()) {
    toast(`任务 #${jobId} 已加入队列。后台引擎尚未启动，请点顶部「一键启动服务」立即开始制作`, "warning", 7000);
  } else {
    toast(`已开始制作（任务 #${jobId}）${extraText}，上方将实时显示渲染进度`, "success", 4500);
  }
}

async function createVideoFromAudition() {
  if (!state.lastAuditionAsset) return toast("暂无可复用的试听音频", "warning");
  const model = getSelectedValidModel();
  if (!model) return toast("请先在左侧选择或添加一段可用的人物视频", "warning");

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
    notifyPostSubmit(res.job_id, "（直接复用试听音频，免重复配音）");
    await refreshAll(true);
  } catch (err) {
    toast(`创建失败: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleSubmitTtsVideo() {
  const model = getSelectedValidModel();
  if (!model) {
    toast("请先在左侧选择或添加一段可用的人物视频", "warning");
    return;
  }
  const text = (document.getElementById("ttsTextInput").value || "").trim();
  if (!text) {
    toast("请先输入要口播的文案", "warning");
    document.getElementById("ttsTextInput")?.focus();
    return;
  }
  const voiceId = getEffectiveVoiceId();
  if (!voiceId) {
    toast("当前人物视频无原声，请在右侧「配音声音」里选一个声音或上传新声音", "warning", 5000);
    return;
  }
  const ttsParams = collectTtsParams();
  if (ttsParams.emo_mode === "emo_audio" && !ttsParams.emo_audio_prompt) {
    return toast("已选「情感参考音频」模式，请先前往语音合成台选择情感音频", "warning");
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
    notifyPostSubmit(res.job_id);
    await refreshAll(true);
  } catch (err) {
    toast(`提交任务失败: ${err.message}`, "error", 5500);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ==================== 模式 B：用已有录音（批量制作） ====================

async function pickAndImportLocalAudios() {
  const btn = document.getElementById("btnPickLocalAudios");
  if (btn) { btn.disabled = true; btn.textContent = "等待选择..."; }
  let fallbackUpload = false;
  try {
    const pick = await pickLocalFileSafe("audio", true);
    if (pick.remote) {
      fallbackUpload = true;
      return;
    }
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
      created.forEach((it) => {
        if (it?.id) state.selectedAudioIds.add(it.id);
      });
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

function formatAudioSourceLabel(sourceType) {
  if (sourceType === "tts" || sourceType === "indextts2") return "IndexTTS-2";
  if (sourceType === "local_import") return "本机导入";
  return "网页上传";
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
    box.innerHTML = `<div class="section-sub" style="padding:18px 8px;text-align:center;">暂无已导入录音。拖入音频文件、点上方上传，或在「语音合成台」合成配音。</div>`;
    return;
  }

  box.innerHTML = state.audios
    .map((a) => {
      const checked = state.selectedAudioIds.has(a.id);
      return `
        <div class="model-item ${checked ? "selected" : ""}" onclick="toggleSelectAudio(${a.id})">
          <div style="display:flex;align-items:center;gap:10px;min-width:0;flex:1;">
            <input type="checkbox" ${checked ? "checked" : ""} onclick="event.stopPropagation();toggleSelectAudio(${a.id})" />
            <div style="min-width:0;">
              <div style="font-weight:600;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(a.name)}</div>
              <div class="section-sub">${formatAudioSourceLabel(a.source_type)} · ${fmtDuration(a.duration)} · ${a.size_kb} KB</div>
            </div>
          </div>
          <div class="flex-row" onclick="event.stopPropagation()">
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
  if (!model) return toast("请先在左侧选择或添加一段可用的人物视频", "warning");
  const ta = document.getElementById("localAudioPathInput");
  const pastedLines = (ta?.value || "").split(/\r?\n/).map((ln) => cleanLocalPath(ln)).filter(Boolean);
  const ids = Array.from(state.selectedAudioIds);
  if (!ids.length && !pastedLines.length) return toast("请先勾选或上传至少一条录音", "warning");

  const btn = document.getElementById("btnBatchCreateAudios");
  if (btn) btn.disabled = true;
  const catVal = document.getElementById("jobCategorySelect").value;
  const baseName = (document.getElementById("jobNameInput").value || "").trim();

  try {
    const importedById = new Map();
    if (pastedLines.length > 0) {
      const imp = await api("/api/v1/audios/import-local", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths: pastedLines, category_id: catVal ? Number(catVal) : null }),
      });
      (imp.items || []).forEach((it) => {
        if (!ids.includes(it.id)) ids.push(it.id);
        state.selectedAudioIds.add(it.id);
        importedById.set(it.id, it);
      });
      if (ta) ta.value = "";
    }

    const items = ids.map((aid, idx) => {
      const asset = state.audios.find((x) => x.id === aid) || importedById.get(aid);
      const name = baseName ? (ids.length > 1 ? `${baseName}_${idx + 1}` : baseName) : (asset?.name || null);
      return {
        model_id: model.id,
        name,
        audio_asset_id: aid,
        category_id: catVal ? Number(catVal) : null,
      };
    });

    const res = await api("/api/v1/jobs/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    });
    const createdIds = Array.isArray(res.job_ids) ? res.job_ids : (res.items || []).map((j) => j.id);
    createdIds.forEach((jid) => trackSessionJobId(jid));
    state.selectedAudioIds.clear();
    if (!isEngineReady()) {
      toast(`已加入 ${res.count} 条任务队列。请先点顶部「一键启动服务」开始制作`, "warning", 6500);
    } else {
      toast(`已开始制作 ${res.count} 条数字人视频，将自动串行渲染`, "success");
    }
    await refreshAll(true);
  } catch (err) {
    toast(`批量创建失败: ${err.message}`, "error", 5500);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ==================== 动态制作状态条 & 任务进度监控中心 ====================

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

function statusBadgeHtml(job) {
  const map = {
    pending: ["badge-info", "制作中"],
    waiting: ["badge-warning", "排队中"],
    success: ["badge-success", "已完成"],
    failed: ["badge-danger", "制作失败"],
    cancelled: ["badge-muted", "已中断"],
  };
  const [cls, text] = map[job.status] || ["badge-muted", job.status];
  return `<span class="badge ${cls}">${text}</span>`;
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
      <div class="live-job-card">
        <div class="flex-between">
          <div class="flex-row">
            <strong style="font-size:13px;">正在制作：#${j.id} ${esc(j.name)}</strong>
            ${statusBadgeHtml(j)}
          </div>
          <div class="flex-row">
            <button class="btn btn-xs" onclick="switchView('tasks')">全部进度</button>
            <button class="btn btn-xs btn-danger" onclick="handleCancelJob(${j.id})">中断</button>
          </div>
        </div>
        ${buildPipelineStepsHtml(j)}
        <div class="progress-track"><div class="progress-fill" style="width:${Math.max(3, j.progress_float)}%;"></div></div>
        <div class="flex-between section-sub mt-6">
          <span>${esc(j.stage_label || j.message || "排队等待中")}</span>
          <span class="mono-num">${j.progress_float}% ${frameText ? `· ${frameText}` : ""} ${j.eta_seconds >= 0 ? `· 剩余 ${fmtEta(j.eta_seconds)}` : ""}</span>
        </div>
      </div>`;
  });

  recentDone.slice(0, 2).forEach((j) => {
    const isBig = Number(j.file_size_mb || 0) > 80;
    html += `
      <div class="live-job-card done">
        <div class="flex-between">
          <strong style="color:#6ee7b7;font-size:13px;">制作完成：#${j.id} ${esc(j.name)}</strong>
          <div class="flex-row">
            ${j.file_exists ? `<button class="btn btn-xs btn-primary" onclick="previewJobVideo(${j.id})">播放</button>` : ""}
            ${j.file_exists && local ? `<button class="btn btn-xs btn-success" title="0秒复制文件，去微信/桌面/剪映按 Ctrl+V 直接粘贴，不用等下载" onclick="handleCopyJobFile(${j.id})">复制文件</button>` : ""}
            ${j.file_exists && local ? `<button class="btn btn-xs" title="成片已直接存在电脑里，点此 0 秒打开文件夹" onclick="handleOpenJobFolder(${j.id})">打开文件夹</button>` : ""}
            ${j.file_exists && isBig ? `<button class="btn btn-xs" title="显卡几秒压成 1080p 小文件（约原体积 1/8），适合发微信/传手机" onclick="handleCompactJobVideo(${j.id}, this)">${local ? "压小发微信" : "轻量下载"}</button>` : ""}
            ${j.file_exists ? `<a class="btn btn-xs ${local ? "" : "btn-success"}" href="${esc(j.download_url)}" download>${local ? "另存" : "下载原片"}</a>` : ""}
            <button class="btn btn-xs btn-ghost" onclick="dismissBannerJob(${j.id})">✕</button>
          </div>
        </div>
      </div>`;
  });

  recentFailed.forEach((j) => {
    html += `
      <div class="live-job-card failed">
        <div class="flex-between">
          <strong style="color:#fca5a5;font-size:13px;">制作失败：#${j.id} ${esc(j.name)}</strong>
          <div class="flex-row">
            <button class="btn btn-xs btn-primary" onclick="handleRetryJob(${j.id})">重试</button>
            <button class="btn btn-xs" onclick="switchView('tasks')">详情</button>
            <button class="btn btn-xs btn-ghost" onclick="dismissBannerJob(${j.id})">✕</button>
          </div>
        </div>
        <div class="section-sub mt-6" style="color:#fecaca;">${esc(j.message || j.error_detail)}</div>
      </div>`;
  });

  panel.innerHTML = html;
}

function setTaskFilter(f) {
  state.taskFilter = f;
  document.querySelectorAll("#taskFilterTabs button").forEach((b) => b.classList.toggle("active", b.dataset.status === f));
  document.querySelectorAll("#tasksKpiRow .kpi-card").forEach((c) => c.classList.toggle("active", c.dataset.kpi === f));
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

  // 更新顶部 4 枚 KPI 统计卡
  const totalCnt = state.jobs.length;
  const activeCnt = state.jobs.filter((j) => j.status === "pending" || j.status === "waiting").length;
  const successCnt = state.jobs.filter((j) => j.status === "success").length;
  const failedCnt = state.jobs.filter((j) => j.status === "failed").length;
  const setTxt = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  setTxt("kpiTotal", totalCnt);
  setTxt("kpiActive", activeCnt);
  setTxt("kpiSuccess", successCnt);
  setTxt("kpiFailed", failedCnt);

  const kw = (document.getElementById("taskSearch")?.value || "").trim().toLowerCase();
  const filtered = state.jobs.filter((j) => {
    if (state.taskFilter === "active" && j.status !== "pending" && j.status !== "waiting") return false;
    if (state.taskFilter !== "all" && state.taskFilter !== "active" && j.status !== state.taskFilter) return false;
    if (kw && !`${j.name} ${j.text_content} ${j.model_name} ${j.message}`.toLowerCase().includes(kw)) return false;
    return true;
  });

  if (!filtered.length) {
    wrap.innerHTML = `<div class="card section-sub" style="text-align:center;padding:34px;">暂无符合条件的制作任务。</div>`;
    return;
  }

  const local = isLocalBrowser();
  wrap.innerHTML = filtered
    .map((j) => {
      const barCls = j.status === "success" ? "success" : j.status === "failed" ? "failed" : "";
      const framesInfo = j.total_frames > 0
        ? `帧进度 ${j.rendered_frames}/${j.total_frames}${j.render_fps > 0 ? ` (${j.render_fps} FPS)` : ""}`
        : (j.rendered_frames > 0 ? `已渲染 ${j.rendered_frames} 帧` : "");
      const errOpen = state.openTaskErrIds.has(j.id) ? "open" : "";
      const logOpen = state.openTaskLogIds.has(j.id) ? "open" : "";

      const thumbHtml = j.thumb_url
        ? `<div class="job-mini-thumb" onclick="${j.file_exists ? `previewJobVideo(${j.id})` : ""}" title="${j.file_exists ? "点击播放" : "任务封面"}"><img src="${esc(j.thumb_url)}" loading="lazy" alt="" onerror="handleCoverImgError(this)" /></div>`
        : "";

      return `
        <div class="job-card status-${esc(j.status)}">
          <div class="job-head-row">
            ${thumbHtml}
            <div class="job-main-col">
              <div class="flex-between">
                <div class="flex-row">
                  <strong>#${j.id} ${esc(j.name)}</strong>
                  ${statusBadgeHtml(j)}
                  <span class="badge badge-muted">人物: ${esc(j.model_name || "#" + j.model_id)}</span>
                  <span class="badge badge-info">${esc(j.category_name)}</span>
                </div>
                <div class="section-sub">${fmtTime(j.created_at)} ${j.duration ? `· ${fmtDuration(j.duration)}` : ""}</div>
              </div>

              ${(j.status === "pending" || j.status === "waiting") ? `
                ${buildPipelineStepsHtml(j)}
                <div class="progress-track"><div class="progress-fill ${barCls}" style="width:${Math.max(2, j.progress_float)}%;"></div></div>
                <div class="flex-between section-sub mt-6">
                  <span>${esc(j.stage_label || j.message)}</span>
                  <span class="mono-num">${j.progress_float}% ${framesInfo ? `· ${framesInfo}` : ""} ${j.eta_seconds >= 0 ? `· 预计剩余 ${fmtEta(j.eta_seconds)}` : ""}</span>
                </div>
              ` : `<div class="section-sub mt-6">${esc(j.stage_label || j.message)}</div>`}

              ${j.status === "failed" ? `
                <div class="error-box">
                  <div><strong>失败原因${j.error_code ? ` [${esc(j.error_code)}]` : ""}：</strong>${esc(j.message)}</div>
                  ${j.retry_count ? `<div class="mt-6">已自动重试：${j.retry_count} / ${j.max_retries} 次</div>` : ""}
                  ${j.error_detail && j.error_detail !== j.message ? `<details class="mt-6" ${errOpen} ontoggle="toggleTaskDetails('err',${j.id},this.open)"><summary style="cursor:pointer;">查看详细诊断日志</summary><pre class="console-box mt-6" style="max-height:140px;">${esc(j.error_detail)}</pre></details>` : ""}
                </div>
              ` : ""}

              ${j.recent_logs?.length && j.status === "pending" ? `
                <details class="mt-6" ${logOpen} ontoggle="toggleTaskDetails('log',${j.id},this.open)"><summary class="section-sub" style="cursor:pointer;">实时渲染日志 (${j.recent_logs.length} 条)</summary>
                  <pre class="console-box mt-6" style="max-height:140px;">${esc(j.recent_logs.join("\n"))}</pre>
                </details>` : ""}

              <div class="flex-between mt-10">
                <div class="section-sub" style="max-width:56%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
                  ${j.text_content ? `文案：${esc(j.text_content.slice(0, 56))}` : (j.paths?.playable_path ? `成片：${esc(j.paths.playable_path)}` : "")}
                </div>
                <div class="flex-row">
                  ${(j.status === "pending" || j.status === "waiting") ? `<button class="btn btn-xs btn-danger" onclick="handleCancelJob(${j.id})">中断</button>` : ""}
                  ${(j.status === "failed" || j.status === "cancelled") ? `<button class="btn btn-xs btn-primary" onclick="handleRetryJob(${j.id})">重试</button>` : ""}
                  ${j.file_exists ? `
                    <button class="btn btn-xs btn-primary" onclick="previewJobVideo(${j.id})">播放</button>
                    ${local ? `<button class="btn btn-xs btn-success" title="0秒复制文件，去微信/桌面/剪映按 Ctrl+V 直接粘贴，不用等下载" onclick="handleCopyJobFile(${j.id})">复制文件</button>` : ""}
                    ${local ? `<button class="btn btn-xs" title="视频已存在电脑里，点此 0 秒打开所在文件夹" onclick="handleOpenJobFolder(${j.id})">打开目录</button>` : ""}
                    ${Number(j.file_size_mb || 0) > 80 ? `<button class="btn btn-xs" title="显卡几秒压成 1080p 小文件，适合发微信或远程快传" onclick="handleCompactJobVideo(${j.id}, this)">${local ? "压小版" : "轻量下载"}</button>` : ""}
                    <a class="btn btn-xs ${local ? "" : "btn-success"}" href="${esc(j.download_url)}" download>${local ? "另存" : "下载原片"}</a>
                  ` : ""}
                  <button class="btn btn-xs" onclick="renameJob(${j.id})">重命名</button>
                  <button class="btn btn-xs btn-danger" onclick="handleDeleteJob(${j.id})">删除</button>
                </div>
              </div>
            </div>
          </div>
        </div>`;
    })
    .join("");
}

async function handleCancelJob(id) {
  if (!confirm(`确定要中断任务 #${id} 吗？（记录会保留，之后随时可点重试）`)) return;
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
    toast(`任务 #${id} 已重新加入制作队列`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`重试失败: ${err.message}`, "error");
  }
}

async function handleDeleteJob(id) {
  if (!confirm(`确定删除任务记录 #${id}？（不会删除人物视频）`)) return;
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

async function handleCopyJobFile(id) {
  try {
    const res = await api(`/api/v1/jobs/${id}/copy-file`, { method: "POST" });
    toast(res.message || "已复制视频文件，去微信/桌面/剪映按 Ctrl+V 直接粘贴即可", "success", 4200);
  } catch (err) {
    toast(`复制文件失败: ${err.message}`, "error");
  }
}

async function handleCompactJobVideo(id, btnEl) {
  const local = isLocalClientMode();
  const prevText = btnEl ? btnEl.textContent : "";
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = "压小中...";
  }
  toast("正在用显卡压成清晰 1080p 小文件（保留原 4K 大片），通常只需几秒…", "info", 4500);
  try {
    const res = await api(`/api/v1/jobs/${id}/compact`, { method: "POST", timeoutMs: 300000 });
    if (local && res.copied_to_clipboard) {
      toast(
        `压好了（耗时 ${res.elapsed_seconds}s）：${res.orig_size_mb} MB → ${res.compact_size_mb} MB！已自动复制小文件到剪贴板，去微信/桌面按 Ctrl+V 直接发`,
        "success",
        6500
      );
    } else {
      toast(
        `轻量版就绪（${res.orig_size_mb} MB → ${res.compact_size_mb} MB），正在开始高速下载…`,
        "success",
        4000
      );
      const a = document.createElement("a");
      a.href = res.download_url;
      a.download = "";
      document.body.appendChild(a);
      a.click();
      a.remove();
    }
  } catch (err) {
    toast(`压小版失败: ${err.message}`, "error", 5500);
  } finally {
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.textContent = prevText || (local ? "压小版" : "轻量下载");
    }
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
  if (!p) return toast("暂未获取到成片目录路径，请稍候重试", "warning");
  await openAnyPath(p);
}

// ==================== 成品作品库 ====================

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
      body: JSON.stringify({ name: name.trim(), color: "#3b82f6" }),
    });
    toast(`已新建分类「${name.trim()}」`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`创建分类失败: ${err.message}`, "error");
  }
}

async function deleteCategory(id, name) {
  if (!confirm(`确定删除分类「${name}」？（作品会移回未分类，不删文件）`)) return;
  try {
    await api(`/api/v1/categories/${id}`, { method: "DELETE" });
    if (String(state.selectedCategoryId) === String(id)) state.selectedCategoryId = "all";
    toast(`已删除分类「${name}」`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`删除失败: ${err.message}`, "error");
  }
}

function deleteCategoryById(catId) {
  const c = state.categories.find((x) => x.id === catId);
  deleteCategory(catId, c?.name || `#${catId}`);
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
    <div class="cat-item ${state.selectedCategoryId === "all" ? "active" : ""}" onclick="selectCategory('all')">
      <span>全部作品</span><span class="nav-count">${totalCount}</span>
    </div>
    <div class="cat-item ${state.selectedCategoryId === "uncategorized" ? "active" : ""}" onclick="selectCategory('uncategorized')">
      <span>未分类</span><span class="nav-count">${uncatCount}</span>
    </div>`;

  state.categories.forEach((c) => {
    const cnt = isVid ? allVideos.filter((j) => j.category_id === c.id).length : state.audios.filter((a) => a.category_id === c.id).length;
    catHtml += `
      <div class="cat-item ${String(state.selectedCategoryId) === String(c.id) ? "active" : ""}" onclick="selectCategory('${c.id}')">
        <span><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${esc(c.color)};margin-right:6px;"></span>${esc(c.name)}</span>
        <span class="flex-row" style="gap:4px;">
          <span class="nav-count">${cnt}</span>
          <button class="btn btn-xs btn-ghost" style="padding:1px 5px;" onclick="event.stopPropagation();deleteCategoryById(${c.id})">✕</button>
        </span>
      </div>`;
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
      grid.innerHTML = `<div class="card section-sub" style="grid-column:1/-1;text-align:center;padding:34px;">该分类下暂无已完成的视频成品。</div>`;
      return;
    }

    grid.innerHTML = items
      .map((j) => {
        const coverInner = j.thumb_url
          ? `<img src="${esc(j.thumb_url)}" alt="${esc(j.name)}" loading="lazy" onerror="handleCoverImgError(this)" />`
          : `<div class="media-cover-placeholder">暂无封面</div>`;
        return `
        <div class="media-card">
          <div class="media-cover" onclick="${j.file_exists ? `previewJobVideo(${j.id})` : ""}">
            ${coverInner}
            <span class="badge ${j.file_exists ? "badge-success" : "badge-danger"} cover-badge-tr">${j.file_exists ? fmtSizeMb(j.file_size_mb) : "成片已移走"}</span>
            ${j.duration ? `<span class="cover-badge-br">${fmtDuration(j.duration)}</span>` : ""}
            ${j.file_exists ? `<div class="cover-play-center"><div class="cover-play-btn">▶</div></div>` : ""}
          </div>
          <div class="media-card-body">
            <div class="media-card-title" title="${esc(j.name)}">${esc(j.name)}</div>
            <div class="section-sub">人物：${esc(j.model_name || "-")} · ${fmtTime(j.created_at)}</div>
            <div class="flex-row" style="flex-wrap:nowrap;">
              <span class="section-sub" style="flex-shrink:0;">分类</span>
              <select class="select select-sm" style="flex:1;" onchange="changeJobCategory(${j.id}, this.value)">
                <option value="" ${!j.category_id ? "selected" : ""}>未分类</option>
                ${state.categories.map((c) => `<option value="${c.id}" ${j.category_id === c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}
              </select>
            </div>
            <details class="adv-box" style="padding:6px 9px;">
              <summary>源文件位置与文案</summary>
              <div class="path-box" style="font-size:11px;">
                <div><strong>成片：</strong>${esc(j.paths.delivered_path || j.paths.playable_path || "(文件已移走)")} <button class="btn btn-xs" style="padding:1px 6px;" onclick="copyJobDeliveredPath(${j.id})">复制</button></div>
                <div class="mt-6"><strong>音频：</strong>${esc(j.paths.source_audio_path || "-")}</div>
                ${j.text_content ? `<div class="mt-6"><strong>文案：</strong>${esc(j.text_content)}</div>` : ""}
              </div>
            </details>
            <div class="media-card-actions">
              ${j.file_exists ? `<button class="btn btn-xs btn-primary" onclick="previewJobVideo(${j.id})">播放</button>` : ""}
              ${j.file_exists && local ? `<button class="btn btn-xs btn-success" title="0秒复制视频文件，去微信/桌面/剪映按 Ctrl+V 直接粘贴，不用等下载" onclick="handleCopyJobFile(${j.id})">复制文件</button>` : ""}
              ${j.file_exists && local ? `<button class="btn btn-xs" title="视频已直接存在电脑里，点此 0 秒打开文件夹" onclick="handleOpenJobFolder(${j.id})">位置</button>` : ""}
              ${j.file_exists && Number(j.file_size_mb || 0) > 80 ? `<button class="btn btn-xs" title="显卡几秒压成 1080p 小文件，适合发微信或传手机" onclick="handleCompactJobVideo(${j.id}, this)">${local ? "压小版" : "轻量下载"}</button>` : ""}
              ${j.file_exists ? `<a class="btn btn-xs ${local ? "" : "btn-success"}" href="${esc(j.download_url)}" download>${local ? "另存" : "下载原片"}</a>` : ""}
              <button class="btn btn-xs" onclick="renameJob(${j.id})">改名</button>
              <button class="btn btn-xs" onclick="cloneJobToCreate(${j.id})">复用</button>
            </div>
          </div>
        </div>`;
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
      grid.innerHTML = `<div class="card section-sub" style="grid-column:1/-1;text-align:center;padding:34px;">该分类下暂无音频素材。</div>`;
      return;
    }

    grid.innerHTML = items
      .map(
        (a) => `
        <div class="card" style="display:flex;flex-direction:column;gap:8px;">
          <div class="flex-between" style="flex-wrap:nowrap;">
            <strong style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(a.name)}">${esc(a.name)}</strong>
            <span class="badge badge-info" style="flex-shrink:0;">${formatAudioSourceLabel(a.source_type)} · ${fmtDuration(a.duration)}</span>
          </div>
          <div class="flex-row" style="flex-wrap:nowrap;">
            <span class="section-sub" style="flex-shrink:0;">分类</span>
            <select class="select select-sm" style="flex:1;" onchange="changeAudioCategory(${a.id}, this.value)">
              <option value="" ${!a.category_id ? "selected" : ""}>未分类</option>
              ${state.categories.map((c) => `<option value="${c.id}" ${a.category_id === c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}
            </select>
          </div>
          ${a.stream_url ? `<audio src="${esc(a.stream_url)}" controls></audio>` : `<div class="section-sub" style="color:#fca5a5;">文件已缺失</div>`}
          <div class="media-card-actions">
            <button class="btn btn-xs btn-primary" onclick="quickVideoFromAudio(${a.id})">去做视频</button>
            ${a.download_url ? `<a class="btn btn-xs" href="${esc(a.download_url)}" download>下载</a>` : ""}
            <button class="btn btn-xs" onclick="renameAudioAsset(${a.id})">改名</button>
            ${local ? `<button class="btn btn-xs" onclick="openAudioPath(${a.id})">定位</button>` : ""}
            <button class="btn btn-xs btn-danger" onclick="deleteAudioAsset(${a.id})">删除</button>
          </div>
        </div>`
      )
      .join("");
  }
}

async function renameJob(jobId) {
  const j = state.jobs.find((x) => x.id === jobId);
  if (!j) return;
  const nextName = prompt("请输入新的作品标题：", j.name || "");
  if (nextName === null) return;
  const trimmed = nextName.trim();
  if (!trimmed) return toast("标题不能为空", "warning");
  if (trimmed === j.name) return;
  try {
    await api(`/api/v1/jobs/${jobId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: trimmed }),
    });
    toast(`已重命名为「${trimmed}」`, "success");
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
  if (!trimmed) return toast("名称不能为空", "warning");
  if (trimmed === a.name) return;
  try {
    await api(`/api/v1/audios/${audioId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: trimmed }),
    });
    toast(`已重命名为「${trimmed}」`, "success");
    await refreshAll(true);
  } catch (err) {
    toast(`重命名失败: ${err.message}`, "error");
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
    toast(`修改分类失败: ${err.message}`, "error");
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
  const linkedModel = state.models.find((m) => m.id === j.model_id && m.video_exists);
  const missingOriginalModel = !linkedModel && Boolean(j.model_id);
  if (linkedModel) state.selectedModelId = linkedModel.id;

  document.getElementById("jobNameInput").value = `${j.name}_复制`;
  document.getElementById("jobCategorySelect").value = j.category_id ? String(j.category_id) : "";

  const jobVoiceId = j.voice_id ? String(j.voice_id) : "";
  [document.getElementById("ttsVoiceSelect"), document.getElementById("voiceStudioVoiceSelect")]
    .filter(Boolean)
    .forEach((voiceSel) => {
      const hasOpt = jobVoiceId && Array.from(voiceSel.options).some((opt) => opt.value === jobVoiceId);
      voiceSel.value = hasOpt ? jobVoiceId : "";
    });

  let toastMsg = "已载入该作品的文案与参数";
  if (j.text_content) {
    setCreateMode("tts");
    document.getElementById("ttsTextInput").value = j.text_content;
    syncTextBetweenViews("create");
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
    state.selectedAudioIds.clear();
    if (matchedAudio) {
      state.selectedAudioIds.add(matchedAudio.id);
      if (localInput) localInput.value = "";
    } else if (localInput) {
      localInput.value = j.paths.source_audio_path;
    }
    renderCreateAudioLibrary();
    toastMsg = "已载入该录音作品的配置";
  }
  if (missingOriginalModel) {
    toastMsg += "（原人物视频已丢失，请先在左侧选择新人物）";
  }
  switchView("create");
  renderCreateModelList();
  renderSelectedModelStage();
  updateVoicePreview();
  updateWizardIndicators();
  toast(toastMsg, missingOriginalModel ? "warning" : "success", missingOriginalModel ? 5200 : 3400);
}

function quickVideoFromAudio(audioId) {
  state.selectedAudioIds.clear();
  state.selectedAudioIds.add(audioId);
  setCreateMode("audio");
  switchView("create");
  renderCreateAudioLibrary();
  updateWizardIndicators();
  toast("已选中该音频，确认左侧人物后点「开始制作数字人视频」即可", "info");
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
  previewVideo(j.stream_url, j.name || `视频 #${j.id}`, j.paths?.playable_path || "", j.thumb_url || "");
}

function previewModelVideo(modelId) {
  const m = state.models.find((x) => x.id === modelId);
  if (!m || !m.preview_url) return toast("人物视频文件不存在", "warning");
  previewVideo(m.preview_url, `人物视频：${m.name}`, m.video_abs_path || "", m.thumb_url || "");
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
    toast(res.message || "已调用本机播放器打开", "success", 2800);
  } catch (err) {
    toast(`无法调用本机播放器: ${err.message}`, "error");
  }
}

function openPreviewFolder() {
  if (!state.previewCurrentPath) return;
  openAnyPath(state.previewCurrentPath);
}

function previewVideo(url, title, pathText, posterUrl = "") {
  state.previewCurrentPath = pathText || "";
  document.getElementById("previewModalTitle").textContent = title;
  document.getElementById("previewModalMeta").textContent = `文件路径：${pathText || "-"}`;

  const vid = document.getElementById("previewModalVideo");
  const fb = document.getElementById("previewModalFallback");
  const fbImg = document.getElementById("previewModalPosterImg");
  const fbText = document.getElementById("previewFallbackText");
  const dlBtn = document.getElementById("btnPreviewDownload");
  const extBtn = document.getElementById("btnPreviewExternal");
  const canOpenLocal = Boolean(pathText) && isLocalBrowser();

  if (fb) fb.classList.add("hidden");
  if (extBtn) extBtn.classList.toggle("hidden", !canOpenLocal);
  document.getElementById("btnPreviewOpenPlayer")?.classList.toggle("hidden", !canOpenLocal);
  document.getElementById("btnPreviewOpenFolder")?.classList.toggle("hidden", !canOpenLocal);
  if (dlBtn) {
    if (!canOpenLocal && url) {
      dlBtn.href = url;
      dlBtn.classList.remove("hidden");
    } else {
      dlBtn.removeAttribute("href");
      dlBtn.classList.add("hidden");
    }
  }
  if (fbText) {
    fbText.textContent = canOpenLocal
      ? "当前视频为高清 HEVC / MOV 编码，浏览器仅解码音轨（不影响数字人制作），可直接用电脑播放器观看高清画面："
      : "当前浏览器暂不支持直接解码该高清 HEVC / MOV 视频画面（不影响数字人制作），可下载原文件到本机播放器查看：";
  }

  if (vid) {
    vid.classList.remove("audio-only", "hidden");
    if (posterUrl) vid.setAttribute("poster", posterUrl);
    else vid.removeAttribute("poster");

    const activateAudioOnlyFallback = (hidePlayerCompletely = false) => {
      if (fb) {
        fb.classList.remove("hidden");
        if (fbImg) {
          if (posterUrl) {
            fbImg.src = posterUrl;
            fbImg.classList.remove("hidden");
          } else {
            fbImg.classList.add("hidden");
          }
        }
      }
      if (extBtn) extBtn.classList.add("hidden");
      vid.removeAttribute("poster");
      if (hidePlayerCompletely) vid.classList.add("hidden");
      else vid.classList.add("audio-only");
    };

    vid.onloadedmetadata = () => {
      if (vid.videoWidth === 0 || vid.videoHeight === 0) activateAudioOnlyFallback(false);
    };
    vid.onerror = () => activateAudioOnlyFallback(true);
    vid.src = url;
  }
  openModal("previewModal");
}

// ==================== 人物与声音资产中心 ====================

function setModelSubTab(sub) {
  state.modelSubTab = sub;
  document.querySelectorAll("#modelSubTabs button").forEach((b) => b.classList.toggle("active", b.dataset.sub === sub));
  document.getElementById("modelsSubPanel")?.classList.toggle("hidden", sub !== "models");
  document.getElementById("voicesSubPanel")?.classList.toggle("hidden", sub !== "voices");
  renderModelsManageView();
}

function useVoiceInStudio(voiceId) {
  const val = String(voiceId);
  [document.getElementById("ttsVoiceSelect"), document.getElementById("voiceStudioVoiceSelect")]
    .filter(Boolean)
    .forEach((s) => {
      if (Array.from(s.options).some((o) => o.value === val)) s.value = val;
    });
  updateVoicePreview();
  updateVoiceStudioRefPreview();
  switchView("voice");
  toast("已切换为该参考声音，可直接配音试听", "success", 2400);
}

function renderModelsManageView() {
  const local = isLocalBrowser();
  if (state.modelSubTab === "models") {
    const grid = document.getElementById("modelsManageGrid");
    const missingBar = document.getElementById("modelsManageMissingBar");
    const discoverBar = document.getElementById("modelsDiscoverBar");
    const missing = getMissingModels();
    const unimportedDiscovered = state.discoveredVideos.filter((v) => !v.already_imported);

    if (discoverBar) {
      discoverBar.innerHTML = buildDiscoverHtml(unimportedDiscovered, false);
    }

    if (missingBar) {
      if (missing.length > 0) {
        missingBar.classList.remove("hidden");
        missingBar.innerHTML = `
          <div class="missing-collapse" style="margin-top:0;">
            <div class="missing-collapse-head">
              <span style="color:#fcd34d;font-size:13px;">检测到 ${missing.length} 条历史人物的原视频文件缺失，可逐个点「补回视频」重新关联，或一键清理失效记录：</span>
              <button class="btn btn-xs btn-danger" onclick="handleCleanupMissingModels()">清理这 ${missing.length} 条失效记录</button>
            </div>
          </div>`;
      } else {
        missingBar.classList.add("hidden");
        missingBar.innerHTML = "";
      }
    }

    if (!state.models.length) {
      grid.innerHTML = `<div class="card section-sub" style="grid-column:1/-1;text-align:center;padding:34px;">暂无人物视频，点右上角「+ 添加人物视频」放入一段人物出镜视频即可开始。</div>`;
      return;
    }

    const sorted = [...state.models].sort((a, b) => Number(b.video_exists) - Number(a.video_exists) || b.id - a.id);
    grid.innerHTML = sorted
      .map(
        (m) => `
        <div class="media-card" style="${m.video_exists ? "" : "border-style:dashed;border-color:rgba(239,68,68,0.45);"}">
          <div class="media-cover" onclick="${m.video_exists ? `previewModelVideo(${m.id})` : `quickRelinkModelFromDisk(${m.id})`}">
            ${m.video_exists
              ? `<img src="${esc(m.thumb_url)}" alt="${esc(m.name)}" loading="lazy" data-preview="${esc(m.preview_url)}" onerror="handleThumbFallback(this, this.dataset.preview)" />
                 <div class="cover-play-center"><div class="cover-play-btn">▶</div></div>`
              : `<div class="media-cover-placeholder" style="color:#fca5a5;">原视频文件已缺失 · 点击补回</div>`}
            <span class="badge ${m.video_exists ? "badge-success" : "badge-danger"} cover-badge-tr">${m.video_exists ? fmtSizeMb(m.video_size_mb) : "文件缺失"}</span>
          </div>
          <div class="media-card-body">
            <div class="media-card-title" title="${esc(m.name)}">${esc(m.name)}</div>
            <div class="flex-row" style="flex-wrap:nowrap;">
              <span class="section-sub" style="flex-shrink:0;">分组</span>
              <select class="select select-sm" style="flex:1;" onchange="changeModelGroup(${m.id}, this.value)">
                <option value="" ${!m.group_id ? "selected" : ""}>未分组</option>
                ${state.modelGroups.map((g) => `<option value="${g.id}" ${m.group_id === g.id ? "selected" : ""}>${esc(g.name)}</option>`).join("")}
              </select>
            </div>
            ${m.voice_preview_url ? `<audio src="${esc(m.voice_preview_url)}" controls preload="none"></audio>` : `<div class="section-sub">无原声参考音</div>`}
            <details class="adv-box" style="padding:6px 9px;">
              <summary>查看源文件路径</summary>
              <div class="path-box" style="font-size:11px;">${esc(m.video_abs_path || "-")}</div>
            </details>
            <div class="media-card-actions">
              ${m.video_exists ? `<button class="btn btn-xs btn-primary" onclick="selectModel(${m.id});switchView('create')">用TA制作</button>` : ""}
              <button class="btn btn-xs ${m.video_exists ? "" : "btn-primary"}" onclick="quickRelinkModelFromDisk(${m.id})">${m.video_exists ? "替换" : "补回视频"}</button>
              ${m.video_exists && local ? `<button class="btn btn-xs" data-path="${esc(m.video_abs_path)}" onclick="openAnyPath(this.dataset.path)">位置</button>` : ""}
              <button class="btn btn-xs" onclick="renameModel(${m.id})">改名</button>
              <button class="btn btn-xs btn-danger" onclick="deleteModelById(${m.id})">删除</button>
            </div>
          </div>
        </div>`
      )
      .join("");
  } else {
    const grid = document.getElementById("voicesManageGrid");
    const validVoices = state.voices.filter((v) => v.exists);
    if (!validVoices.length) {
      grid.innerHTML = `<div class="card section-sub" style="grid-column:1/-1;text-align:center;padding:34px;">暂无可用声音。添加人物视频时会自动提取原声，或点右上角「+ 上传声音」。</div>`;
      return;
    }
    grid.innerHTML = validVoices
      .map(
        (v) => `
        <div class="card" style="display:flex;flex-direction:column;gap:8px;">
          <div class="flex-between">
            <strong style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(v.display_name)} (#${v.id})</strong>
            <span class="badge badge-success">可用</span>
          </div>
          <div class="section-sub">${v.model_name ? `关联人物：${esc(v.model_name)}` : "自定义参考声音"}</div>
          <audio src="${esc(v.preview_url)}" controls preload="none"></audio>
          <div class="media-card-actions">
            <button class="btn btn-xs btn-primary" onclick="useVoiceInStudio(${v.id})">去语音合成台使用</button>
            ${local && v.abs_path ? `<button class="btn btn-xs" data-path="${esc(v.abs_path)}" onclick="openAnyPath(this.dataset.path)">打开位置</button>` : ""}
          </div>
        </div>`
      )
      .join("");
  }
}

async function promptNewModelGroup() {
  const name = prompt("输入新人物分组名称：", "常用主播");
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
  const nextName = prompt("请输入新的人物名称：", m.name || "");
  if (nextName === null) return;
  const trimmed = nextName.trim();
  if (!trimmed) return toast("名称不能为空", "warning");
  if (trimmed === m.name) return;
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
    toast("人物分组已更新", "success", 2000);
    await refreshAll(true);
  } catch (err) {
    toast(`更新失败: ${err.message}`, "error");
  }
}

async function deleteModelById(modelId) {
  const m = state.models.find((x) => x.id === modelId);
  const label = m ? `「${m.name}」` : `#${modelId}`;
  if (!confirm(`确定删除人物 ${label} 的记录吗？（不会删除已经做好的成片视频）`)) return;
  try {
    await api(`/api/v1/models/${modelId}`, { method: "DELETE" });
    if (state.selectedModelId === modelId) state.selectedModelId = null;
    toast(`已删除人物 ${label}`, "success");
    await refreshAll(true, true);
  } catch (err) {
    toast(`删除失败: ${err.message}`, "error");
  }
}

// ==================== 添加 / 补回人物 & 上传声音弹窗 ====================

function openAddModelModal() {
  document.getElementById("modelModalTitle").textContent = "添加人物视频";
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
  document.getElementById("modelModalTitle").textContent = `补回 / 替换人物视频：${modelName}`;
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
    if (pick.remote) {
      fallbackUpload = true;
      return;
    }
    if (pick.cancelled || !pick.paths?.length) return;
    const p = pick.paths[0];
    document.getElementById("modelModalLocalPath").value = p;
    document.getElementById("modelModalFile").value = "";
    document.getElementById("modelModalPickedHint").textContent = `已选择本机视频：${p}`;
    const nameInput = document.getElementById("modelModalName");
    if (nameInput && !nameInput.value.trim()) nameInput.value = fileStemFromPath(p);
  } catch (err) {
    toast(`系统选文件失败，请改用网页上传：${err.message}`, "warning", 4000);
    fallbackUpload = true;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "从电脑选择视频（秒导入）"; }
    if (fallbackUpload) document.getElementById("modelModalFile")?.click();
  }
}

function onModalVideoFilePicked(input) {
  const f = input.files?.[0];
  if (!f) return;
  document.getElementById("modelModalLocalPath").value = "";
  document.getElementById("modelModalPickedHint").textContent = `已选择上传文件：${f.name} (${fmtSizeMb(f.size / (1024 * 1024))})`;
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
    fd.append("name", typedName || autoName || "新人物");
    const grp = document.getElementById("modelModalGroup").value;
    if (grp) fd.append("group_id", grp);
  }

  const btn = document.getElementById("btnSubmitModelModal");
  if (btn) { btn.disabled = true; btn.textContent = "处理中..."; }
  try {
    const url = relinkId ? `/api/v1/models/${relinkId}/relink` : "/api/v1/models";
    const res = await api(url, { method: "POST", body: fd, timeoutMs: 300000 });
    if (res?.item?.id) state.selectedModelId = res.item.id;
    toast(relinkId ? "人物视频已更新" : "人物视频添加成功", "success");
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
    toast(`参考声音「${res?.item?.display_name || "新声音"}」上传成功`, "success");
    closeModal("voiceModal");
    await refreshAll(true);
    if (res?.item?.id) {
      const vidStr = String(res.item.id);
      [document.getElementById("ttsVoiceSelect"), document.getElementById("voiceStudioVoiceSelect")]
        .filter(Boolean)
        .forEach((sel) => {
          if (Array.from(sel.options).some((o) => o.value === vidStr)) sel.value = vidStr;
        });
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

// ==================== 开发者自动化接口（收纳于系统页折叠抽屉） ====================

function renderApiView() {
  const origin = window.location.origin;
  const hostEl = document.getElementById("studioEndpointHost");
  if (hostEl) hostEl.textContent = origin;

  const firstModel = getValidModels()[0] || state.models[0];
  const sampleModelId = firstModel ? firstModel.id : 1;
  const snippet = `import time, requests

BASE = "${origin}"

# 提交口播任务（自动 IndexTTS-2 配音 + Duix.Avatar 渲染）
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
    time.sleep(3)

if st["status"] == "success":
    open(f"job_{job_id}.mp4", "wb").write(requests.get(f"{BASE}{st['download_url']}").content)`;

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
    document.getElementById("apiTestBody").value = JSON.stringify(
      {
        model_id: mid,
        name: "接口测试视频",
        text: "你好，这是一条通过自动化接口创建的测试视频。",
        tts_params: { speed: 1.0, emo_mode: "same_as_speaker" },
      },
      null,
      2
    );
  } else if (kind === "audio_job") {
    document.getElementById("apiTestMethod").value = "POST";
    document.getElementById("apiTestPath").value = "/api/v1/jobs";
    document.getElementById("apiTestBody").value = JSON.stringify(
      {
        model_id: mid,
        name: "本地音频直驱测试",
        audio_path: "E:\\录音\\口播1.wav",
      },
      null,
      2
    );
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

// ==================== 云端成片自动拉回本地 ====================

async function refreshCloudSyncStatus(fillInputs = false) {
  try {
    const res = await api("/api/v1/cloud-sync/status", { timeoutMs: 8000 });
    const st = res.status || {};
    state.cloudSyncActive = Boolean(st.active_download);

    const badge = document.getElementById("navCloudSyncBadge");
    if (badge) {
      if (st.active_download) badge.textContent = `${Math.round(st.active_download.percent || 0)}%`;
      else if (st.enabled && st.connected) badge.textContent = "同步中";
      else if (st.enabled) badge.textContent = "待连";
      else badge.textContent = "未开";
    }

    const urlInput = document.getElementById("cloudSyncUrlInput");
    const dirInput = document.getElementById("cloudSyncSaveDirInput");
    const qualSel = document.getElementById("cloudSyncQualitySelect");
    const enToggle = document.getElementById("cloudSyncEnabledToggle");
    if (fillInputs) {
      if (urlInput) urlInput.value = st.cloud_url || "";
      if (dirInput) dirInput.value = st.save_dir || "E:\\数字人视频文件";
      if (qualSel) qualSel.value = st.quality || "compact";
      if (enToggle) enToggle.checked = Boolean(st.enabled);
    }

    const dot = document.getElementById("cloudSyncConnDot");
    const txt = document.getElementById("cloudSyncConnText");
    if (dot && txt) {
      dot.className = "dot";
      if (!st.cloud_url) {
        txt.textContent = "等待填写云端网址";
      } else if (!st.enabled) {
        txt.textContent = "已暂停自动拉回";
      } else if (st.connected) {
        dot.classList.add("ok");
        txt.textContent = `已连云端（${st.cloud_video_count || 0} 条成片）· 每 ${st.poll_interval || 15}s 巡检`;
      } else {
        dot.classList.add("warn");
        txt.textContent = "暂时未连上云端（自动重试中）";
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
        if (tEl) tEl.textContent = `${ad.stage || "拉回中"} · #${ad.video_id} ${ad.name || ""}`;
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

    const remoteSub = document.getElementById("cloudRemoteCountSub");
    const remoteListEl = document.getElementById("cloudRemoteList");
    const remoteVids = st.cloud_videos || [];
    if (remoteSub) remoteSub.textContent = `云端共 ${remoteVids.length} 条已完成视频 · 任意一条都能点右侧按钮 8 线程高速拉回本地`;
    if (remoteListEl) {
      if (!remoteVids.length) {
        remoteListEl.className = "empty-state";
        remoteListEl.innerHTML = st.connected ? "云端目前暂无已完成视频。" : "暂未获取到云端成品列表。";
      } else {
        remoteListEl.className = "";
        const busyNow = Boolean(st.active_download);
        remoteListEl.innerHTML = remoteVids.map((cv) => {
          const badge = cv.synced ? `<span class="tag ok">已拉回</span>` : `<span class="tag">未拉回</span>`;
          const btnLabel = cv.synced ? "重新拉回" : "⬇ 手动拉回";
          const sz = cv.drive_compact_size_mb
            ? `压小版 ${cv.drive_compact_size_mb}M / 原片 ${cv.file_size_mb || "?"}M`
            : `原片 ${cv.file_size_mb || "?"}M`;
          const dtStr = cv.created_at ? new Date(cv.created_at).toLocaleString("zh-CN") : "";
          return `
            <div class="list-row" style="display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--border);">
              <div style="min-width:0;flex:1;">
                <div style="font-weight:600;word-break:break-all;">#${cv.id} · ${esc(cv.name)} ${badge}</div>
                <div class="section-sub" style="margin-top:2px;">底版：${esc(cv.model_name || "-")} · 时长 ${cv.duration || 0}s · ${sz}${dtStr ? ` · ${dtStr}` : ""}</div>
              </div>
              <div class="flex-row" style="flex-shrink:0;gap:6px;">
                <button class="btn btn-sm ${cv.synced ? "" : "btn-primary"}" ${busyNow ? "disabled" : ""} onclick="pullCloudVideoNow(${cv.id})">${btnLabel}</button>
              </div>
            </div>`;
        }).join("");
      }
    }

    const cntSub = document.getElementById("cloudSyncCountSub");
    const listEl = document.getElementById("cloudSyncList");
    const items = st.synced_items || [];
    if (cntSub) cntSub.textContent = `共 ${st.synced_count || items.length} 条记录 · 保存目录：${st.save_dir || "E:\\数字人视频文件"}`;
    if (listEl) {
      if (!items.length) {
        listEl.className = "empty-state";
        listEl.innerHTML = "暂未拉回任何云端视频。贴入今天的 Colab 网址并点「保存并开启同步」即可自动开始。";
      } else {
        listEl.className = "";
        listEl.innerHTML = items.map((it) => {
          const fname = (it.local_path || "").split(/[\\/]/).pop() || it.name || `#${it.video_id}`;
          const savedAt = it.saved_at ? new Date(it.saved_at * 1000).toLocaleString("zh-CN") : "";
          const escapedPath = (it.local_path || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
          const statusTag = it.exists ? `<span class="tag ok">已在本地</span>` : `<span class="tag">已移走/重命名</span>`;
          return `
            <div class="list-row" style="display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--border);">
              <div style="min-width:0;flex:1;">
                <div style="font-weight:600;word-break:break-all;">${esc(fname)} ${statusTag}</div>
                <div class="section-sub" style="margin-top:2px;">${it.size_mb || 0} MB · ${esc(it.meta || "")}${savedAt ? ` · ${savedAt}` : ""}</div>
              </div>
              <div class="flex-row" style="flex-shrink:0;gap:6px;">
                <button class="btn btn-sm" onclick="openCloudSyncFolder('${escapedPath}')">定位文件</button>
              </div>
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
  if (enabled && !cloud_url) {
    toast("请先粘贴今天 Colab 弹出的云端网页地址", "error");
    return;
  }
  const btn = document.getElementById("btnSaveCloudSync");
  if (btn) btn.disabled = true;
  try {
    await api("/api/v1/cloud-sync/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled, cloud_url, save_dir, quality }),
    });
    toast(enabled ? "已开启云端自动拉回，正在检测新视频..." : "已暂停云端自动拉回", "success", 2800);
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
    toast("已开始检测云端最新成片...", "success", 2200);
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
    applyTtsParamsToForm({
      model_version: cfg.index_tts_model_version,
      lang: cfg.index_tts_lang,
      speed: cfg.index_tts_speed,
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
