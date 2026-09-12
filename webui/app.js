const STORAGE_KEY = "maibot.subtitleWebUI.settings.v2";
const MENU_TOGGLE_KEY = "KeyM";
const SILENT_WAV =
  "data:audio/wav;base64,UklGRlQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YRAAAAAAAAAAAAAAAAAAAAAA";

const state = {
  socket: null,
  reconnectTimer: 0,
  processing: false,
  replyQueue: [],
  audioUnlocked: false,
  menuOpen: false,
  activePanel: "layout",
  defaults: {
    boxWidthPx: 960,
    boxHeightPx: 260,
    leftPx: 72,
    bottomPx: 72,
    backgroundColor: "#0a0e16",
    backgroundOpacity: 0,
    fontFamily: '"Microsoft YaHei UI", "PingFang SC", sans-serif',
    fontSizePx: 34,
    textColor: "#f7f8fb",
  },
  settings: {},
};

const elements = {};

document.addEventListener("DOMContentLoaded", () => {
  cacheElements();
  bindControls();
  applySettings(loadSettings());
  setActivePanel(state.activePanel);
  connectSocket();
});

function cacheElements() {
  elements.stage = document.getElementById("stage");
  elements.menuTrigger = document.getElementById("menu-trigger");
  elements.controlDrawer = document.getElementById("control-drawer");
  elements.drawerShell = document.getElementById("drawer-shell");
  elements.closeMenu = document.getElementById("close-menu");
  elements.statusDot = document.getElementById("socket-status");
  elements.statusText = document.getElementById("socket-status-text");
  elements.replyMeta = document.getElementById("reply-meta");
  elements.subtitleBox = document.getElementById("subtitle-box");
  elements.subtitleScroll = document.getElementById("subtitle-scroll");
  elements.audioUnlock = document.getElementById("audio-unlock");
  elements.clearSubtitles = document.getElementById("clear-subtitles");
  elements.resetSettings = document.getElementById("reset-settings");
  elements.boxWidth = document.getElementById("box-width");
  elements.boxHeight = document.getElementById("box-height");
  elements.boxLeft = document.getElementById("box-left");
  elements.boxBottom = document.getElementById("box-bottom");
  elements.fontFamily = document.getElementById("font-family");
  elements.fontSize = document.getElementById("font-size");
  elements.textColor = document.getElementById("text-color");
  elements.backgroundColor = document.getElementById("background-color");
  elements.backgroundOpacity = document.getElementById("background-opacity");
  elements.backgroundOpacityValue = document.getElementById("background-opacity-value");
  elements.menuTabs = Array.from(document.querySelectorAll("[data-panel-target]"));
  elements.menuPanels = Array.from(document.querySelectorAll("[data-panel-id]"));
}

function bindControls() {
  elements.menuTrigger.addEventListener("click", () => {
    toggleMenu();
  });

  elements.closeMenu.addEventListener("click", () => {
    closeMenu();
  });

  elements.stage.addEventListener("dblclick", (event) => {
    if (elements.drawerShell.contains(event.target)) {
      return;
    }
    toggleMenu();
  });

  elements.controlDrawer.addEventListener("click", (event) => {
    if (event.target === elements.controlDrawer) {
      closeMenu();
    }
  });

  document.addEventListener("keydown", handleGlobalShortcuts);

  elements.menuTabs.forEach((button) => {
    button.addEventListener("click", () => {
      setActivePanel(button.dataset.panelTarget || state.activePanel);
    });
  });

  elements.audioUnlock.addEventListener("click", unlockAudio);
  elements.clearSubtitles.addEventListener("click", clearSubtitles);
  elements.resetSettings.addEventListener("click", resetSettings);

  const controlMap = {
    boxWidthPx: elements.boxWidth,
    boxHeightPx: elements.boxHeight,
    leftPx: elements.boxLeft,
    bottomPx: elements.boxBottom,
    fontFamily: elements.fontFamily,
    fontSizePx: elements.fontSize,
    textColor: elements.textColor,
    backgroundColor: elements.backgroundColor,
    backgroundOpacity: elements.backgroundOpacity,
  };

  Object.entries(controlMap).forEach(([key, input]) => {
    input.addEventListener("input", () => {
      const nextSettings = normalizeSettings({
        ...state.settings,
        [key]: readInputValue(key, input.value),
      });
      applySettings(nextSettings);
      saveSettings(nextSettings);
    });
  });
}

function handleGlobalShortcuts(event) {
  if (event.key === "Escape") {
    closeMenu();
    return;
  }
  if (event.code !== MENU_TOGGLE_KEY || isTypingTarget(event.target)) {
    return;
  }
  event.preventDefault();
  toggleMenu();
}

function isTypingTarget(target) {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  const tagName = target.tagName;
  return target.isContentEditable || tagName === "INPUT" || tagName === "TEXTAREA" || tagName === "SELECT";
}

function toggleMenu() {
  state.menuOpen = !state.menuOpen;
  syncMenuState();
}

function closeMenu() {
  if (!state.menuOpen) {
    return;
  }
  state.menuOpen = false;
  syncMenuState();
}

function syncMenuState() {
  document.body.classList.toggle("menu-open", state.menuOpen);
  elements.controlDrawer.setAttribute("aria-hidden", String(!state.menuOpen));
  if (state.menuOpen) {
    elements.closeMenu.focus({ preventScroll: true });
  }
}

function setActivePanel(panelId) {
  state.activePanel = panelId;
  elements.menuTabs.forEach((button) => {
    const isActive = button.dataset.panelTarget === panelId;
    button.classList.toggle("is-active", isActive);
    button.setAttribute("aria-selected", String(isActive));
  });
  elements.menuPanels.forEach((panel) => {
    const isActive = panel.dataset.panelId === panelId;
    panel.classList.toggle("is-active", isActive);
    panel.hidden = !isActive;
  });
}

function clearSubtitles() {
  elements.subtitleScroll.replaceChildren();
  elements.replyMeta.textContent = "字幕已清空";
}

function resetSettings() {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch (_error) {
    // ignore storage failures and still reset the current session.
  }
  const reset = normalizeSettings({ ...state.defaults });
  applySettings(reset);
  saveSettings(reset);
  elements.replyMeta.textContent = "已恢复默认绿幕样式";
}

function readInputValue(key, rawValue) {
  if (["boxWidthPx", "boxHeightPx", "leftPx", "bottomPx", "fontSizePx", "backgroundOpacity"].includes(key)) {
    const numericValue = Number.parseInt(String(rawValue), 10);
    return Number.isFinite(numericValue) ? numericValue : state.defaults[key];
  }
  return String(rawValue || "").trim() || state.defaults[key];
}

function normalizeSettings(settings) {
  const merged = {
    ...state.defaults,
    ...settings,
  };

  return {
    boxWidthPx: clamp(merged.boxWidthPx, 240, 2400),
    boxHeightPx: clamp(merged.boxHeightPx, 96, 1200),
    leftPx: clamp(merged.leftPx, 0, 2400),
    bottomPx: clamp(merged.bottomPx, 0, 1600),
    backgroundColor: normalizeHexColor(merged.backgroundColor, state.defaults.backgroundColor),
    backgroundOpacity: clamp(merged.backgroundOpacity, 0, 100),
    fontFamily: String(merged.fontFamily || state.defaults.fontFamily).trim() || state.defaults.fontFamily,
    fontSizePx: clamp(merged.fontSizePx, 16, 144),
    textColor: normalizeHexColor(merged.textColor, state.defaults.textColor),
  };
}

function loadSettings() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return normalizeSettings({ ...state.defaults });
    }
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") {
      return normalizeSettings({ ...state.defaults });
    }
    return normalizeSettings(parsed);
  } catch (_error) {
    return normalizeSettings({ ...state.defaults });
  }
}

function saveSettings(settings) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(normalizeSettings(settings)));
  } catch (_error) {
    return;
  }
}

function applySettings(settings) {
  state.settings = normalizeSettings(settings);

  elements.boxWidth.value = String(state.settings.boxWidthPx);
  elements.boxHeight.value = String(state.settings.boxHeightPx);
  elements.boxLeft.value = String(state.settings.leftPx);
  elements.boxBottom.value = String(state.settings.bottomPx);
  elements.fontFamily.value = state.settings.fontFamily;
  elements.fontSize.value = String(state.settings.fontSizePx);
  elements.textColor.value = state.settings.textColor;
  elements.backgroundColor.value = state.settings.backgroundColor;
  elements.backgroundOpacity.value = String(state.settings.backgroundOpacity);
  elements.backgroundOpacityValue.value = `${state.settings.backgroundOpacity}%`;
  elements.backgroundOpacityValue.textContent = `${state.settings.backgroundOpacity}%`;

  elements.subtitleBox.style.width = `${state.settings.boxWidthPx}px`;
  elements.subtitleBox.style.height = `${state.settings.boxHeightPx}px`;
  elements.subtitleBox.style.left = `${state.settings.leftPx}px`;
  elements.subtitleBox.style.bottom = `${state.settings.bottomPx}px`;
  elements.subtitleBox.style.fontFamily = state.settings.fontFamily;
  elements.subtitleBox.style.fontSize = `${state.settings.fontSizePx}px`;
  elements.subtitleBox.style.color = state.settings.textColor;

  const alpha = state.settings.backgroundOpacity / 100;
  const hasBackdrop = alpha > 0.001;
  elements.subtitleBox.style.background = hasBackdrop ? hexToRgba(state.settings.backgroundColor, alpha) : "transparent";
  elements.subtitleBox.style.borderColor = hasBackdrop ? "rgba(255, 255, 255, 0.14)" : "transparent";
  elements.subtitleBox.style.boxShadow = hasBackdrop ? "0 20px 44px rgba(0, 0, 0, 0.26)" : "none";

  pruneOverflow();
}

function connectSocket() {
  window.clearTimeout(state.reconnectTimer);
  state.reconnectTimer = 0;

  if (!window.location.host) {
    setSocketStatus("error", "本地预览模式");
    return;
  }

  if (state.socket) {
    try {
      state.socket.close();
    } catch (_error) {
      // noop
    }
  }

  setSocketStatus("connecting", "连接中");
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socketUrl = `${protocol}://${window.location.host}/ws`;
  const socket = new WebSocket(socketUrl);
  state.socket = socket;

  socket.addEventListener("open", () => {
    if (state.socket !== socket) {
      return;
    }
    setSocketStatus("online", "已连接");
  });

  socket.addEventListener("message", (event) => {
    handleSocketMessage(event.data);
  });

  socket.addEventListener("close", () => {
    if (state.socket !== socket) {
      return;
    }
    state.socket = null;
    setSocketStatus("error", "连接断开，正在重连");
    state.reconnectTimer = window.setTimeout(connectSocket, 2000);
  });

  socket.addEventListener("error", () => {
    if (state.socket !== socket) {
      return;
    }
    setSocketStatus("error", "连接异常");
  });
}

function handleSocketMessage(rawMessage) {
  let payload = {};
  try {
    payload = JSON.parse(rawMessage);
  } catch (_error) {
    return;
  }
  if (!payload || typeof payload !== "object") {
    return;
  }

  if (payload.type === "subtitle.bootstrap" && payload.subtitle_defaults) {
    state.defaults = normalizeSettings({
      ...state.defaults,
      ...normalizeSubtitleDefaults(payload.subtitle_defaults),
    });
    applySettings(loadSettings());
    return;
  }

  if (payload.type === "subtitle.reply" && Array.isArray(payload.segments)) {
    state.replyQueue.push(payload);
    if (!state.processing) {
      void processReplyQueue();
    }
  }
}

async function processReplyQueue() {
  state.processing = true;
  while (state.replyQueue.length > 0) {
    const reply = state.replyQueue.shift();
    const replyLabel = reply.source_platform ? `${reply.source_platform} reply` : "replyer 输出";
    elements.replyMeta.textContent = `${replyLabel} · ${reply.segments.length} 句`;
    for (const segment of reply.segments) {
      await renderSegment(reply, segment);
    }
  }
  state.processing = false;
}

async function renderSegment(reply, segment) {
  const preparedAudio = await prepareSegmentAudio(segment);
  const durationMs = Math.max(120, preparedAudio.durationMs || segment.duration_ms || 600);
  const entry = document.createElement("article");
  entry.className = "subtitle-entry";
  const textNode = document.createElement("p");
  textNode.className = "subtitle-entry__text";
  textNode.textContent = segment.text || "";
  textNode.style.animationDuration = `${durationMs}ms`;
  entry.appendChild(textNode);
  elements.subtitleScroll.appendChild(entry);
  pruneOverflow();

  await nextFrame();
  entry.classList.add("is-visible");
  textNode.classList.add("is-running");

  const playbackPromise = preparedAudio.audio
    ? playAudio(preparedAudio.audio, durationMs, reply, segment)
    : playSilentSegment(durationMs, reply, segment);
  await playbackPromise;

  entry.classList.add("is-complete");
  textNode.classList.remove("is-running");
  pruneOverflow();
}

async function prepareSegmentAudio(segment) {
  const fallbackDurationMs = Math.max(200, Number(segment.duration_ms) || 0);
  const audioUrl = String(segment.audio_url || "").trim();
  if (!audioUrl) {
    return { audio: null, durationMs: fallbackDurationMs };
  }

  const audio = new Audio(audioUrl);
  audio.preload = "auto";
  await Promise.race([
    once(audio, "loadedmetadata"),
    once(audio, "canplaythrough"),
    sleep(2000),
  ]).catch(() => null);
  const metadataDurationMs =
    Number.isFinite(audio.duration) && audio.duration > 0 ? Math.round(audio.duration * 1000) : fallbackDurationMs;
  return { audio, durationMs: metadataDurationMs };
}

async function playAudio(audio, durationMs, reply, segment) {
  try {
    const maybePromise = audio.play();
    if (maybePromise && typeof maybePromise.then === "function") {
      await maybePromise;
    }
    notifyAudioStarted(reply, segment);
  } catch (_error) {
    setSocketStatus("error", "音频被拦截，点一下启用音频");
    await sleep(durationMs);
    return;
  }

  await Promise.race([
    once(audio, "ended"),
    once(audio, "error"),
    sleep(Math.max(durationMs + 200, 400)),
  ]).catch(() => null);
}

async function playSilentSegment(durationMs, reply, segment) {
  notifyAudioStarted(reply, segment);
  await sleep(durationMs);
}

function notifyAudioStarted(reply, segment) {
  const replyId = String((reply && reply.reply_id) || "").trim();
  if (!replyId) {
    return;
  }
  sendSocketEvent({
    type: "subtitle.audio.started",
    reply_id: replyId,
    segment_index: Number(segment && segment.index) || 0,
    started_at_ms: Date.now(),
  });
}

function sendSocketEvent(payload) {
  if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
    return;
  }
  try {
    state.socket.send(JSON.stringify(payload));
  } catch (_error) {
    return;
  }
}

async function unlockAudio() {
  try {
    const silentAudio = new Audio(SILENT_WAV);
    silentAudio.volume = 0;
    const maybePromise = silentAudio.play();
    if (maybePromise && typeof maybePromise.then === "function") {
      await maybePromise;
    }
    state.audioUnlocked = true;
    elements.audioUnlock.textContent = "音频已启用";
    setSocketStatus("online", "已连接");
  } catch (_error) {
    elements.audioUnlock.textContent = "音频待手动允许";
  }
}

function pruneOverflow() {
  const maxHeight = elements.subtitleBox.clientHeight;
  while (
    elements.subtitleScroll.scrollHeight > maxHeight &&
    elements.subtitleScroll.firstElementChild &&
    elements.subtitleScroll.childElementCount > 1
  ) {
    elements.subtitleScroll.removeChild(elements.subtitleScroll.firstElementChild);
  }
}

function setSocketStatus(kind, text) {
  elements.statusDot.classList.remove("is-online", "is-error");
  if (kind === "online") {
    elements.statusDot.classList.add("is-online");
  } else if (kind === "error") {
    elements.statusDot.classList.add("is-error");
  }
  elements.statusText.textContent = text;
}

function normalizeSubtitleDefaults(defaults) {
  const parsedBackground = parseBackground(defaults.background_color || defaults.backgroundColor);
  return {
    boxWidthPx: Number(defaults.box_width_px || defaults.boxWidthPx || 960),
    boxHeightPx: Number(defaults.box_height_px || defaults.boxHeightPx || 260),
    leftPx: Number(defaults.left_px || defaults.leftPx || 72),
    bottomPx: Number(defaults.bottom_px || defaults.bottomPx || 72),
    backgroundColor: parsedBackground.color,
    backgroundOpacity: parsedBackground.opacity,
    fontFamily: String(defaults.font_family || defaults.fontFamily || state.defaults.fontFamily),
    fontSizePx: Number(defaults.font_size_px || defaults.fontSizePx || 34),
    textColor: String(defaults.text_color || defaults.textColor || state.defaults.textColor),
  };
}

function parseBackground(rawValue) {
  const normalized = String(rawValue || "").trim();
  const rgbaMatch = normalized.match(
    /^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})(?:\s*,\s*([01]?(?:\.\d+)?))?\s*\)$/i,
  );
  if (rgbaMatch) {
    const [, r, g, b, alpha] = rgbaMatch;
    return {
      color: rgbToHex(Number(r), Number(g), Number(b)),
      opacity: alpha ? Math.round(Number(alpha) * 100) : 100,
    };
  }
  const normalizedHex = normalizeHexColor(normalized, "");
  return {
    color: normalizedHex || state.defaults.backgroundColor,
    opacity: normalizedHex ? 100 : state.defaults.backgroundOpacity,
  };
}

function normalizeHexColor(rawValue, fallback = "#f7f8fb") {
  const normalized = String(rawValue || "").trim();
  if (/^#[0-9a-fA-F]{6}$/.test(normalized)) {
    return normalized.toLowerCase();
  }
  if (/^#[0-9a-fA-F]{3}$/.test(normalized)) {
    return `#${normalized[1]}${normalized[1]}${normalized[2]}${normalized[2]}${normalized[3]}${normalized[3]}`.toLowerCase();
  }
  return fallback.toLowerCase();
}

function hexToRgba(hexColor, alpha) {
  const normalized = normalizeHexColor(hexColor);
  const r = Number.parseInt(normalized.slice(1, 3), 16);
  const g = Number.parseInt(normalized.slice(3, 5), 16);
  const b = Number.parseInt(normalized.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function rgbToHex(r, g, b) {
  return `#${[r, g, b]
    .map((value) => clamp(Math.round(value), 0, 255).toString(16).padStart(2, "0"))
    .join("")}`;
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, Number(value) || 0));
}

function nextFrame() {
  return new Promise((resolve) => window.requestAnimationFrame(() => resolve()));
}

function sleep(durationMs) {
  return new Promise((resolve) => window.setTimeout(resolve, durationMs));
}

function once(target, eventName) {
  return new Promise((resolve, reject) => {
    const onResolve = (event) => {
      cleanup();
      resolve(event);
    };
    const onReject = (event) => {
      cleanup();
      reject(event);
    };
    const cleanup = () => {
      target.removeEventListener(eventName, onResolve);
      target.removeEventListener("error", onReject);
    };
    target.addEventListener(eventName, onResolve, { once: true });
    target.addEventListener("error", onReject, { once: true });
  });
}
