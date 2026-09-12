const EXTERNAL_SCRIPTS = [
  "https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js",
  "https://cdnjs.cloudflare.com/ajax/libs/pixi.js/6.5.10/browser/pixi.min.js",
  "https://cdn.jsdelivr.net/npm/pixi-live2d-display@0.4.0/dist/cubism4.min.js",
];

const LEGACY_SCRIPT_PATHS = [
  "../../live2d_soullink_vendor/frontend_legacy/services/config.js",
  "../../live2d_soullink_vendor/frontend_legacy/services/i18n.js",
  "../../live2d_soullink_vendor/frontend_legacy/services/tts.js",
  "../../live2d_soullink_vendor/frontend_legacy/live2d/shared-state.js",
  "../../live2d_soullink_vendor/frontend_legacy/live2d/helpers.js",
  "../../live2d_soullink_vendor/frontend_legacy/live2d/idle-motion.js",
  "../../live2d_soullink_vendor/frontend_legacy/live2d/param-control.js",
  "../../live2d_soullink_vendor/frontend_legacy/live2d/ambient-lighting.js",
  "./shell-legacy-compat.js",
  "../../live2d_soullink_vendor/frontend_legacy/live2d/model-loader.js",
  "../../live2d_soullink_vendor/frontend_legacy/live2d/background.js",
  "../../live2d_soullink_vendor/frontend_legacy/live2d/interaction.js",
  "../../live2d_soullink_vendor/frontend_legacy/live2d/loader.js",
  "../../live2d_soullink_vendor/frontend_legacy/services/expression.js",
  "../../live2d_soullink_vendor/frontend_legacy/services/websocket.js",
];

const shellRoot = document.getElementById("shell-root");
const menuButton = document.getElementById("shell-menu-button");
const sideMenu = document.getElementById("shell-side-menu");
const wsStatus = document.getElementById("ws-status");
const loadingText = document.querySelector(".shell-loading__text");
const systemInfo = document.getElementById("system-info");
const controlPanel = document.getElementById("control-panel");

const shellState = {
  connected: false,
  currentModel: "",
  interactive: false,
  menuOpen: false,
  autoOpenedControls: false,
  legacyLoaded: false,
  modelReady: false,
  pendingModelPayloads: [],
  avatarInteraction: {
    dragEnabled: false,
    zoomEnabled: false,
  },
  shellControls: {
    enabled: false,
    click_through: true,
    interactive: false,
    actions: [],
  },
};

let legacyLoadPromise = null;
let socket = null;
let modelSanitizeTimer = null;

function assetUrl(path) {
  return new URL(path, import.meta.url).toString();
}

function setStatus(text, connected = false) {
  if (wsStatus) {
    wsStatus.textContent = text;
    wsStatus.dataset.state = connected ? "connected" : "disconnected";
  }
  shellState.connected = connected;
  renderSystemInfo();
}

function setMenuOpen(open) {
  shellState.menuOpen = Boolean(open);
  sideMenu.classList.toggle("shell-side-menu--hidden", !shellState.menuOpen);
  sideMenu.setAttribute("aria-hidden", String(!shellState.menuOpen));
}

function setInteractive(interactive) {
  shellState.interactive = Boolean(interactive);
  shellRoot.classList.toggle("shell-root--controls-visible", shellState.interactive);
  if (!shellState.interactive) {
    setMenuOpen(false);
  }
  renderSystemInfo();
}

function renderSystemInfo() {
  if (!systemInfo) {
    return;
  }
  const clickThrough = shellState.shellControls?.click_through !== false;
  const interactionHint = clickThrough
    ? "Mouse pass-through is on. Use subtitle controls or press Alt+M after unlocking drag."
    : "Interactive mode is on. Drag the frameless window to reposition it.";
  const modelLabel = shellState.currentModel || "No model loaded yet";
  systemInfo.innerHTML = `
    <div class="shell-info-line"><strong>Model:</strong> ${modelLabel}</div>
    <div class="shell-info-line"><strong>Socket:</strong> ${shellState.connected ? "connected" : "offline"}</div>
    <div class="shell-info-line"><strong>Mode:</strong> ${clickThrough ? "click-through" : "interactive"}</div>
    <div class="shell-info-hint">${interactionHint}</div>
  `;
}

function sendControlAction(action) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    return;
  }
  socket.send(JSON.stringify({ type: "control_action", action }));
}

function actionLabel(action) {
  return {
    toggle_click_through: "Toggle click-through",
    unlock_drag: "Unlock drag mode",
    reset_position: "Reset position",
    grow_window: "Grow shell",
    shrink_window: "Shrink shell",
    minimize_window: "Minimize shell",
    open_settings: "Focus shell window",
    close_window: "Close shell window",
    reopen_window: "Reopen shell window",
  }[action] || action.replaceAll("_", " ");
}

function renderControlPanel() {
  if (!controlPanel) {
    return;
  }
  const payload = shellState.shellControls || {};
  const actions = Array.isArray(payload.actions) ? payload.actions : [];
  controlPanel.innerHTML = "";
  controlPanel.hidden = !payload.enabled;
  if (!payload.enabled) {
    return;
  }
  for (const action of actions) {
    const button = document.createElement("button");
    button.className = "shell-action-button";
    button.type = "button";
    button.textContent = actionLabel(action);
    button.addEventListener("click", () => sendControlAction(action));
    controlPanel.appendChild(button);
  }

  const localActions = [
    {
      label: "Reset avatar position",
      onClick: () => {
        window.resetModel?.();
        applyAvatarInteractionState();
      },
    },
    {
      label: shellState.avatarInteraction.dragEnabled ? "Disable avatar drag" : "Enable avatar drag",
      onClick: () => {
        shellState.avatarInteraction.dragEnabled = !shellState.avatarInteraction.dragEnabled;
        applyAvatarInteractionState();
        renderControlPanel();
      },
    },
    {
      label: shellState.avatarInteraction.zoomEnabled ? "Disable avatar zoom" : "Enable avatar zoom",
      onClick: () => {
        shellState.avatarInteraction.zoomEnabled = !shellState.avatarInteraction.zoomEnabled;
        applyAvatarInteractionState();
        renderControlPanel();
      },
    },
  ];

  for (const action of localActions) {
    const button = document.createElement("button");
    button.className = "shell-action-button";
    button.type = "button";
    button.textContent = action.label;
    button.addEventListener("click", action.onClick);
    controlPanel.appendChild(button);
  }
}

function wrapZoomHandlerIfNeeded() {
  const container = document.getElementById("live2d-container");
  if (!container || typeof container._zoomHandler !== "function" || container._shellZoomWrapped) {
    return;
  }
  const originalZoomHandler = container._zoomHandler;
  container.removeEventListener("wheel", originalZoomHandler);
  container._zoomHandler = (event) => {
    if (!shellState.avatarInteraction.zoomEnabled) {
      return;
    }
    return originalZoomHandler(event);
  };
  container.addEventListener("wheel", container._zoomHandler, { passive: false });
  container._shellZoomWrapped = true;
}

function applyAvatarInteractionState() {
  const currentModel = window.model;
  if (currentModel) {
    const dragEnabled = Boolean(shellState.avatarInteraction.dragEnabled);
    currentModel.interactive = dragEnabled;
    currentModel.buttonMode = dragEnabled;
    currentModel.cursor = dragEnabled ? "grab" : "default";
  }
  wrapZoomHandlerIfNeeded();
}

function flushPendingModelPayloads() {
  if (!shellState.modelReady || !Array.isArray(shellState.pendingModelPayloads) || shellState.pendingModelPayloads.length === 0) {
    return;
  }
  const queued = [...shellState.pendingModelPayloads];
  shellState.pendingModelPayloads = [];
  for (const payload of queued) {
    dispatchPayload(payload);
  }
}

function suppressVisibleHitAreaDrawables(targetModel) {
  const coreModel = targetModel?.internalModel?.coreModel;
  const rawModel = coreModel?._model;
  const drawableIds = rawModel?.drawables?.ids;
  if (!coreModel || !rawModel || !drawableIds) {
    return [];
  }

  const ids = Array.from(drawableIds);
  const patched = [];
  for (let index = 0; index < ids.length; index += 1) {
    const drawableId = String(ids[index] || "");
    if (!/^HitArea/i.test(drawableId)) {
      continue;
    }
    const uvs = coreModel.getDrawableVertexUvs(index);
    if (!uvs || uvs.length < 2) {
      continue;
    }
    for (let uvIndex = 0; uvIndex < uvs.length; uvIndex += 2) {
      uvs[uvIndex] = 0.01;
      uvs[uvIndex + 1] = 0.01;
    }
    patched.push(drawableId);
  }
  return patched;
}

function scheduleModelSanitization(attempts = 24, delayMs = 80) {
  if (modelSanitizeTimer) {
    window.clearTimeout(modelSanitizeTimer);
    modelSanitizeTimer = null;
  }

  const trySanitize = (remaining) => {
    const patched = suppressVisibleHitAreaDrawables(window.model);
    if (patched.length > 0) {
      console.log("SoulLink shell sanitized visible hit-area drawables:", patched);
      modelSanitizeTimer = null;
      return;
    }
    if (remaining <= 0) {
      modelSanitizeTimer = null;
      return;
    }
    modelSanitizeTimer = window.setTimeout(() => trySanitize(remaining - 1), delayMs);
  };

  trySanitize(Math.max(0, attempts));
}

function defineShellStubs() {
  window.syncPositionControlsFromModel = window.syncPositionControlsFromModel || (() => {});
  window.refreshControlPanelLanguage = window.refreshControlPanelLanguage || (() => {});
  window.toggleChatPanel = window.toggleChatPanel || (() => {});
  window.clearChat = window.clearChat || (() => {});
  window.toggleVoiceRecording = window.toggleVoiceRecording || (() => {});
}

function hasLoadedScript(src) {
  return Boolean(document.querySelector(`script[src="${src}"]`));
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (hasLoadedScript(src)) {
      resolve();
      return;
    }

    const script = document.createElement("script");
    script.src = src;
    script.async = false;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error(`Failed to load script: ${src}`));
    document.head.appendChild(script);
  });
}

async function loadLegacyCore() {
  if (legacyLoadPromise) {
    return legacyLoadPromise;
  }

  legacyLoadPromise = (async () => {
    defineShellStubs();

    for (const src of EXTERNAL_SCRIPTS) {
      await loadScript(src);
    }

    for (const path of LEGACY_SCRIPT_PATHS) {
      await loadScript(assetUrl(path));
    }

    shellState.legacyLoaded = true;
  })();

  return legacyLoadPromise;
}

function shellSocketUrl() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/shell/ws`;
}

function dispatchPayload(payload) {
  if (!payload || typeof payload.type !== "string") {
    return;
  }

  const payloadType = payload.type;

  if ((payloadType === "expression" || payloadType === "tts_motion_frame" || payloadType === "reset") && !shellState.modelReady) {
    shellState.pendingModelPayloads.push(payload);
    return;
  }

  switch (payload.type) {
    case "load_model":
      shellState.currentModel = payload.model?.name || payload.model?.id || "";
      shellState.modelReady = false;
      shellState.pendingModelPayloads = [];
      renderSystemInfo();
      Promise.resolve(window.loadModelFromServer?.(payload.model))
        .catch((error) => {
          console.warn("SoulLink shell model load failed", error);
        })
        .then(() => {
          shellState.modelReady = Boolean(window.model);
          applyAvatarInteractionState();
          flushPendingModelPayloads();
        })
        .finally(() => {
          scheduleModelSanitization();
        });
      break;
    case "expression":
      window.transitionToExpression?.(
        payload.parameters,
        payload.duration_ms ?? 800,
        null,
        false,
      );
      break;
    case "tts_motion_frame":
      window.transitionToExpression?.(
        payload.parameters,
        payload.duration_ms ?? 16,
        null,
        false,
      );
      break;
    case "reset":
      window.resetExpression?.(payload.duration_ms ?? 800);
      break;
    case "set_interactive":
      setInteractive(Boolean(payload.interactive));
      break;
    case "shell_controls":
      shellState.shellControls = {
        enabled: Boolean(payload.enabled),
        click_through: Boolean(payload.click_through),
        interactive: Boolean(payload.interactive),
        actions: Array.isArray(payload.actions) ? payload.actions : [],
      };
      renderSystemInfo();
      renderControlPanel();
      if (shellState.interactive && shellState.shellControls.enabled && !shellState.autoOpenedControls) {
        shellState.autoOpenedControls = true;
        setMenuOpen(true);
      }
      break;
    case "toggle_menu":
      setMenuOpen(payload.open ?? !shellState.menuOpen);
      break;
    default:
      break;
  }
}

function connectShellSocket() {
  if (socket && socket.readyState <= WebSocket.OPEN) {
    return socket;
  }

  setStatus("connecting", false);
  socket = new WebSocket(shellSocketUrl());

  socket.addEventListener("open", () => {
    setStatus("connected", true);
    if (loadingText) {
      loadingText.textContent = "Shell connected. Waiting for model payload...";
    }
  });

  socket.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    dispatchPayload(payload);
  });

  socket.addEventListener("close", () => {
    setStatus("offline", false);
  });

  socket.addEventListener("error", () => {
    setStatus("error", false);
  });

  return socket;
}

async function bootShell() {
  setStatus("booting", false);
  await loadLegacyCore();

  if (window.loadConfig) {
    try {
      await window.loadConfig();
    } catch (error) {
      console.warn("SoulLink shell config load failed", error);
    }
  }

  if (window.I18N?.syncLanguageFromConfig) {
    window.I18N.syncLanguageFromConfig();
  }
  if (window.I18N?.applyPageTranslations) {
    window.I18N.applyPageTranslations();
  }

  if (loadingText) {
    loadingText.textContent = "Waiting for shell runtime...";
  }
  renderSystemInfo();
  renderControlPanel();
  connectShellSocket();
  scheduleModelSanitization();
}

menuButton?.addEventListener("click", () => {
  setMenuOpen(!shellState.menuOpen);
});

window.addEventListener("keydown", (event) => {
  if (event.altKey && event.key.toLowerCase() === "m") {
    setInteractive(true);
    setMenuOpen(!shellState.menuOpen);
  } else if (event.key === "Escape") {
    setMenuOpen(false);
  }
});

window.addEventListener("soullink-shell:toggle-menu", (event) => {
  setMenuOpen(event.detail?.open ?? !shellState.menuOpen);
});

window.addEventListener("soullink-shell:set-interactive", (event) => {
  setInteractive(Boolean(event.detail?.interactive));
});

window.loadLive2DCore = loadLegacyCore;
window.SoulLinkShell = {
  bootShell,
  connectShellSocket,
  dispatchPayload,
  loadLegacyCore,
  setInteractive,
  setMenuOpen,
  state: shellState,
};

bootShell().catch((error) => {
  console.error("SoulLink shell boot failed", error);
  setStatus("boot-failed", false);
  if (loadingText) {
    loadingText.textContent = "Shell boot failed. Check console for details.";
  }
});
