const state = {
  socket: null,
  reconnectTimer: 0,
  audioUnlocked: false,
};

const elements = {};

document.addEventListener("DOMContentLoaded", () => {
  elements.layer = document.getElementById("effect-layer");
  elements.status = document.getElementById("status-dot");
  elements.unlock = document.getElementById("audio-unlock");
  bindAudioUnlock();
  connectSocket();
});

function bindAudioUnlock() {
  const unlock = () => {
    if (state.audioUnlocked) {
      return;
    }
    state.audioUnlocked = true;
    elements.unlock.classList.add("is-unlocked");
    const audio = new Audio(silentWav());
    audio.volume = 0;
    audio.play().catch(() => {});
  };

  elements.unlock.addEventListener("pointerdown", unlock, { once: true });
  window.addEventListener("keydown", unlock, { once: true });
}

function connectSocket() {
  clearTimeout(state.reconnectTimer);
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws`);
  state.socket = socket;

  socket.addEventListener("open", () => {
    setDisconnected(false);
  });

  socket.addEventListener("message", (event) => {
    let payload = null;
    try {
      payload = JSON.parse(event.data);
    } catch (_error) {
      return;
    }
    if (!payload || payload.type !== "soundboard.trigger") {
      return;
    }
    renderCue(payload);
  });

  socket.addEventListener("close", () => {
    setDisconnected(true);
    state.reconnectTimer = window.setTimeout(connectSocket, 1000);
  });

  socket.addEventListener("error", () => {
    setDisconnected(true);
  });
}

function setDisconnected(disconnected) {
  elements.status.classList.toggle("is-disconnected", Boolean(disconnected));
}

function renderCue(payload) {
  const duration = Math.max(120, Number(payload.duration_ms || 1600));
  const repeatCount = normalizeRepeatCount(payload.repeat_count);
  const hasMedia = Boolean(payload.media_url && payload.media_kind);
  const hasAudio = Boolean(payload.audio_url);
  if (!hasMedia && !hasAudio) {
    return;
  }

  let lifetime = createDetachedLifetime();
  if (hasMedia) {
    const root = document.createElement("div");
    root.className = "fx has-media";
    root.style.setProperty("--fx-duration", `${duration}ms`);
    lifetime = createRootLifetime(root, duration);
    appendMedia(root, payload, lifetime, repeatCount);
    elements.layer.appendChild(root);
  }

  playCueAudio(payload, lifetime, repeatCount);
}

function createDetachedLifetime() {
  return {
    extendTo() {},
    remove() {},
  };
}

function createRootLifetime(root, fallbackDurationMs) {
  let removed = false;
  let scheduledDurationMs = Math.max(120, Number(fallbackDurationMs || 0));
  let timerId = 0;

  const remove = () => {
    if (removed) {
      return;
    }
    removed = true;
    clearTimeout(timerId);
    root.remove();
  };

  const schedule = () => {
    clearTimeout(timerId);
    timerId = window.setTimeout(remove, scheduledDurationMs + 220);
  };

  const extendTo = (durationMs) => {
    const nextDurationMs = Math.max(120, Number(durationMs || 0));
    if (nextDurationMs > scheduledDurationMs) {
      scheduledDurationMs = nextDurationMs;
      root.style.setProperty("--fx-duration", `${scheduledDurationMs}ms`);
      schedule();
    }
  };

  schedule();
  return { extendTo, remove };
}

function playCueAudio(payload, lifetime, repeatCount) {
  if (!payload.audio_url) {
    return;
  }
  const totalRepeats = normalizeRepeatCount(repeatCount);

  const playOnce = (remainingRepeats) => {
    const audio = new Audio(payload.audio_url);
    audio.volume = clamp(Number(payload.volume ?? 1), 0, 1);
    audio.addEventListener("loadedmetadata", () => {
      const durationMs = durationToMs(audio.duration);
      if (durationMs > 0) {
        lifetime.extendTo(durationMs * totalRepeats);
      }
    });
    audio.addEventListener(
      "ended",
      () => {
        if (remainingRepeats > 1) {
          playOnce(remainingRepeats - 1);
          return;
        }
        lifetime.remove();
      },
      { once: true },
    );
    audio.play().catch(() => {});
  };

  playOnce(totalRepeats);
}

function appendMedia(root, payload, lifetime, repeatCount) {
  const frame = document.createElement("div");
  frame.className = "fx-media-frame";
  if (payload.media_kind === "video") {
    const totalRepeats = normalizeRepeatCount(repeatCount);
    let remainingRepeats = totalRepeats;
    const video = document.createElement("video");
    video.className = "fx-media";
    video.src = payload.media_url;
    video.autoplay = true;
    video.muted = !payload.media_audio_enabled;
    video.loop = false;
    video.playsInline = true;
    video.volume = clamp(Number(payload.volume ?? 1), 0, 1);
    video.addEventListener("loadedmetadata", () => {
      const durationMs = durationToMs(video.duration);
      if (durationMs > 0) {
        lifetime.extendTo(durationMs * totalRepeats);
      }
    });
    video.addEventListener("ended", () => {
      remainingRepeats -= 1;
      if (remainingRepeats > 0) {
        video.currentTime = 0;
        window.requestAnimationFrame(() => {
          video.play().catch(() => {});
        });
        return;
      }
      if (!payload.audio_url) {
        lifetime.remove();
      }
    });
    window.requestAnimationFrame(() => {
      video.play().catch(() => {});
    });
    frame.appendChild(video);
  } else {
    const image = document.createElement("img");
    image.className = "fx-media";
    image.src = payload.media_url;
    image.alt = String(payload.label || "soundboard media");
    frame.appendChild(image);
  }
  root.appendChild(frame);
}

function clamp(value, min, max) {
  if (Number.isNaN(value)) {
    return min;
  }
  return Math.min(max, Math.max(min, value));
}

function durationToMs(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return 0;
  }
  return Math.ceil(seconds * 1000);
}

function normalizeRepeatCount(value) {
  const parsed = Math.trunc(Number(value));
  if (!Number.isFinite(parsed) || parsed < 1) {
    return 1;
  }
  return parsed;
}

function silentWav() {
  return "data:audio/wav;base64,UklGRlQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YRAAAAAAAAAAAAAAAAAAAAAA";
}
