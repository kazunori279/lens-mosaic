import { AudioRecorder } from "./audio-recorder.js";
import { AudioPlayer } from "./audio-player.js";

const statusEl = document.getElementById("status");
const messagesEl = document.getElementById("messages");
const chatEmptyStateEl = document.getElementById("chat-empty-state");
const textInput = document.getElementById("text-input");
const startBtn = document.getElementById("start-btn");
const closeRecommendedBtn = document.getElementById("close-recommended-btn");
const flipCameraBtn = document.getElementById("flip-camera-btn");
const videoContainer = document.getElementById("video-container");
const videoEl = document.getElementById("camera");
const canvasEl = document.getElementById("canvas");

let ws = null;
let micOn = false;
let camOn = false;
let recorder = null;
let player = null;
let camStream = null;
let camInterval = null;
let currentFacingMode = isMobileDevice() ? "environment" : "user";
let hasStartedExperience = false;
let tileWs = null;
let displayTileSource = "similar";
let similarTileItems = [];
let recommendedTileItems = [];

const userId = "user-" + Math.random().toString(36).slice(2, 8);
const sessionId = "session-" + Date.now();
const runtimeConfig = resolveRuntimeConfig();
const liveWsOrigin = toWebSocketOrigin(runtimeConfig.liveOrigin);

statusEl.title = `Mode: ${runtimeConfig.mode}. Search: ${runtimeConfig.searchOrigin}. Live: ${runtimeConfig.liveOrigin}`;

function resolveRuntimeConfig() {
  const params = new URLSearchParams(window.location.search);
  const configured = window.LENS_MOSAIC_CONFIG || {};
  const mode = params.get("mode") || configured.mode || (params.get("backend") ? "local" : "demo");
  const searchOrigin = normalizeOrigin(
    params.get("search") || configured.searchOrigin || window.location.origin,
  );
  const liveOrigin = normalizeOrigin(
    params.get("backend")
      || configured.liveOrigin
      || (mode === "local" ? "http://127.0.0.1:8000" : window.location.origin),
  );
  return { mode, searchOrigin, liveOrigin };
}

function normalizeOrigin(value) {
  try {
    return new URL(value, window.location.origin).origin;
  } catch {
    return window.location.origin;
  }
}

function toWebSocketOrigin(origin) {
  const url = new URL(origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.origin;
}

function buildUrl(origin, path) {
  return new URL(path, `${origin}/`).toString();
}

// --- WebSocket ---

function connect() {
  ws = new WebSocket(`${liveWsOrigin}/ws/${userId}/${sessionId}`);
  startBtn.disabled = true;

  ws.onopen = () => {
    statusEl.textContent = "Connected";
    statusEl.className = "connected";
    startBtn.disabled = false;
  };

  ws.onclose = () => {
    statusEl.textContent = "Disconnected – reconnecting...";
    statusEl.className = "";
    startBtn.disabled = true;
    setTimeout(connect, 3000);
  };

  ws.onmessage = (e) => {
    handleEvent(JSON.parse(e.data));
  };
}

// --- Event handling ---

let currentAgentText = "";
let currentAgentEl = null;
let hasOutputTranscription = false;
let currentInputEl = null;
let currentInputText = "";

function handleEvent(event) {
  // Handle turn complete — reset accumulator
  if (event.turnComplete) {
    currentAgentEl = null;
    currentAgentText = "";
    hasOutputTranscription = false;
    currentInputEl = null;
    currentInputText = "";
    return;
  }

  // Handle interrupted — mark partial message and stop audio
  if (event.interrupted) {
    if (currentAgentEl) {
      currentAgentEl.classList.add("interrupted");
    }
    if (player && player._worklet) player._worklet.port.postMessage({ command: "endOfAudio" });
    currentAgentEl = null;
    currentAgentText = "";
    hasOutputTranscription = false;
    currentInputEl = null;
    currentInputText = "";
    return;
  }

  // Handle input transcription (user's spoken words)
  if (event.inputTranscription && event.inputTranscription.text) {
    if (!currentInputEl) {
      currentInputEl = addMessage("you (voice)", "");
      currentInputText = "";
    }
    if (event.inputTranscription.finished) {
      currentInputText = event.inputTranscription.text;
    } else {
      currentInputText += event.inputTranscription.text;
    }
    currentInputEl.querySelector(".text").textContent = cleanCJKSpaces(currentInputText);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // Handle output transcription (agent's spoken words)
  if (event.outputTranscription && event.outputTranscription.text) {
    hasOutputTranscription = true;
    if (!currentAgentEl) {
      currentAgentEl = addMessage("agent", "");
      currentAgentText = "";
    }
    if (event.outputTranscription.finished) {
      // Final transcription contains the complete text — replace
      currentAgentText = event.outputTranscription.text;
    } else {
      // Partial chunk — append
      currentAgentText += event.outputTranscription.text;
    }
    currentAgentEl.querySelector(".text").textContent = cleanCJKSpaces(currentAgentText);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // Handle content events (text or audio)
  const content = event.content;
  if (content && content.parts) {
    for (const part of content.parts) {
      // Skip hidden reasoning parts; only render user-facing text content.
      if (isHiddenThoughtPart(part)) {
        continue;
      }

      // Text response (skip if output transcription already delivered it)
      if (part.text && !hasOutputTranscription) {
        if (!currentAgentEl) {
          currentAgentEl = addMessage("agent", "");
          currentAgentText = "";
        }
        currentAgentText += part.text;
        currentAgentEl.querySelector(".text").textContent = cleanCJKSpaces(currentAgentText);
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }

      // Audio response
      if (part.inlineData) {
        const bytes = base64ToBytes(part.inlineData.data);
        if (player) player.play(bytes);
      }
    }
  }
}

function isHiddenThoughtPart(part) {
  return Boolean(part.thought || part.thoughtSignature);
}

function cleanCJKSpaces(text) {
  const cjk = /[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9faf]/;
  if (!cjk.test(text)) return text;
  return text.replace(/\s+/g, "");
}

function addMessage(role, text) {
  const div = document.createElement("div");
  div.className = `message ${role === "agent" ? "agent" : "user"}`;
  div.innerHTML = `<span class="role">${role}:</span><span class="text">${escapeHtml(text)}</span>`;
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  updateChatEmptyState();
  return div;
}

function updateChatEmptyState() {
  if (!chatEmptyStateEl) return;
  chatEmptyStateEl.classList.toggle("prestart", !hasStartedExperience);
  chatEmptyStateEl.classList.toggle("hidden", hasStartedExperience && messagesEl.childElementCount > 0);
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function base64ToBytes(b64) {
  // Convert base64url to standard base64
  let std = b64.replace(/-/g, "+").replace(/_/g, "/");
  while (std.length % 4) std += "=";
  const bin = atob(std);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function canUseMediaDevices() {
  return Boolean(window.isSecureContext && navigator.mediaDevices?.getUserMedia);
}

function isMobileDevice() {
  return window.matchMedia("(pointer: coarse)").matches;
}

function addSystemMessage(text) {
  return addMessage("system", text);
}

function getMediaAccessErrorMessage() {
  if (navigator.mediaDevices?.getUserMedia) {
    return "Camera and microphone require HTTPS on mobile browsers. Open the secure URL for this app and try again.";
  }
  return "Camera and microphone are unavailable in this browser context. On phones, open this app over HTTPS and try again.";
}

// --- Send text ---

function sendText() {
  const text = textInput.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "text", text }));
  addMessage("you", text);
  textInput.value = "";
}

textInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendText();
});

document.querySelectorAll(".suggestion-btn").forEach((button) => {
  button.addEventListener("click", () => {
    textInput.value = button.dataset.suggestion || "";
    textInput.focus();
  });
});

// --- Mic ---

async function startMic() {
  player = new AudioPlayer();
  await player.init();

  recorder = new AudioRecorder((pcmBuffer) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(new Uint8Array(pcmBuffer));
    }
  });
  await recorder.start();
  micOn = true;
}

// --- Camera ---

async function startCamera() {
  if (camStream) stopCamera();
  camStream = await navigator.mediaDevices.getUserMedia({
    video: {
      width: { ideal: 640 },
      height: { ideal: 480 },
      facingMode: { ideal: currentFacingMode },
    },
  });
  videoEl.srcObject = camStream;
  videoContainer.classList.add("active");
  camOn = true;

  // Send frames every 1 second
  camInterval = setInterval(captureAndSend, 1000);
}

function stopCamera() {
  if (camInterval) {
    clearInterval(camInterval);
    camInterval = null;
  }
  if (camStream) {
    camStream.getTracks().forEach((t) => t.stop());
    camStream = null;
  }
  videoEl.srcObject = null;
  videoContainer.classList.remove("active");
  camOn = false;
}

function captureAndSend() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (!videoEl.videoWidth) return;

  canvasEl.width = videoEl.videoWidth;
  canvasEl.height = videoEl.videoHeight;
  const ctx = canvasEl.getContext("2d");
  ctx.drawImage(videoEl, 0, 0);

  const dataUrl = canvasEl.toDataURL("image/jpeg", 0.6);
  const b64 = dataUrl.split(",")[1];
  ws.send(JSON.stringify({ type: "image", data: b64, mimeType: "image/jpeg" }));
}

async function flipCamera() {
  if (!camOn) return;
  currentFacingMode = currentFacingMode === "user" ? "environment" : "user";
  try {
    await startCamera();
  } catch (e) {
    currentFacingMode = currentFacingMode === "user" ? "environment" : "user";
    console.error("Flip camera error:", e);
    addSystemMessage("Camera flip error: " + e.message);
  }
}

// --- Image Tile WebSocket ---

const imageTileEl = document.getElementById("image-tile");
// Track tiles: id -> { el, lastSeen, createdAt, cell }
const tileMap = new Map();
function fadeOutAndRemove(el) {
  el.classList.remove("loaded");
  el.classList.add("fade-out");
  const remove = () => el.remove();
  el.addEventListener("transitionend", remove, { once: true });
  setTimeout(remove, 5500); // fallback if transitionend never fires
}

// Track occupied cells (0-99)
const occupiedCells = new Set();

function getGridSize() {
  const size = Number.parseInt(
    getComputedStyle(imageTileEl).getPropertyValue("--tile-grid-size"),
    10,
  );
  return Number.isFinite(size) && size > 0 ? size : 9;
}

function getCellsByDistance() {
  const gridSize = getGridSize();
  const totalCells = gridSize * gridSize;
  return Array.from({ length: totalCells }, (_, i) => {
    const row = Math.floor(i / gridSize);
    const col = i % gridSize;
    const dist = Math.hypot(
      row - (gridSize - 1) / 2,
      col - (gridSize - 1) / 2,
    );
    return { cell: i, dist };
  }).sort((a, b) => a.dist - b.dist).map((e) => e.cell);
}

function getCellCoords(cell, gridSize) {
  return {
    row: Math.floor(cell / gridSize),
    col: cell % gridSize,
  };
}

function getNextEmptyCell() {
  // Return the empty cell that keeps the visible cluster centered.
  const gridSize = getGridSize();
  const center = (gridSize - 1) / 2;
  const cellsByDistance = getCellsByDistance();
  const occupied = Array.from(occupiedCells, (cell) => getCellCoords(cell, gridSize));
  let bestCell = null;
  let bestScore = Number.POSITIVE_INFINITY;
  let bestDist = Number.POSITIVE_INFINITY;

  for (const cell of cellsByDistance) {
    if (occupiedCells.has(cell)) continue;
    const candidate = getCellCoords(cell, gridSize);
    const cluster = [...occupied, candidate];
    const avgRow = cluster.reduce((sum, item) => sum + item.row, 0) / cluster.length;
    const avgCol = cluster.reduce((sum, item) => sum + item.col, 0) / cluster.length;
    const centerOffset = Math.hypot(avgRow - center, avgCol - center);
    const distFromCenter = Math.hypot(candidate.row - center, candidate.col - center);

    if (
      centerOffset < bestScore ||
      (centerOffset === bestScore && distFromCenter < bestDist)
    ) {
      bestCell = cell;
      bestScore = centerOffset;
      bestDist = distFromCenter;
    }
  }

  return bestCell;
}

function getCellDistRank() {
  return new Map(getCellsByDistance().map((cell, index) => [cell, index]));
}

function replaceFarthestTile(newItem, brightness, normalized) {
  // Evict the tile farthest from center so new tile can take a center spot
  let farthest = null;
  let farthestRank = -1;
  const cellDistRank = getCellDistRank();
  for (const [id, entry] of tileMap) {
    const rank = cellDistRank.get(entry.cell) ?? 0;
    if (rank > farthestRank) {
      farthestRank = rank;
      farthest = id;
    }
  }
  if (!farthest) return;
  const entry = tileMap.get(farthest);
  const oldCell = entry.cell;
  occupiedCells.delete(oldCell);
  tileMap.delete(farthest);
  fadeOutAndRemove(entry.el);
  const cell = getNextEmptyCell() ?? oldCell;
  addTileAt(cell, newItem, brightness);
}

function getTileSize() {
  const gridSize = getGridSize();
  const w = Math.ceil(imageTileEl.clientWidth / gridSize);
  const h = Math.ceil(imageTileEl.clientHeight / gridSize);
  return { w, h };
}

function addTileAt(cell, item, brightness) {
  const id = item.id;
  const gridSize = getGridSize();
  const { w, h } = getTileSize();
  const img = document.createElement("img");
  img.alt = item.name || id;
  img.title = `${item.name || id} (score: ${item.score?.toFixed(3) ?? "N/A"})`;
  img.style.filter = `brightness(${brightness})`;
  img.style.cursor = "pointer";
  img.addEventListener("click", () => showItemPopup(id));
  const row = Math.floor(cell / gridSize) + 1;
  const col = (cell % gridSize) + 1;
  img.style.gridRow = row;
  img.style.gridColumn = col;
  img.onload = () => {
    img.offsetHeight;
    img.classList.add("loaded");
  };
  imageTileEl.appendChild(img);
  img.src = `https://u-mercari-images.mercdn.net/photos/${id}_1.jpg?w=${w}&h=${h}&fitcrop&sharpen`;
  occupiedCells.add(cell);
  tileMap.set(id, { el: img, lastSeen: Date.now(), createdAt: Date.now(), cell });
}

let tilePausedUntil = 0;

function updateRecommendedControls() {
  closeRecommendedBtn.classList.toggle(
    "visible",
    camOn && displayTileSource === "recommended" && recommendedTileItems.length > 0,
  );
}

function clearAllTiles() {
  imageTileEl.querySelectorAll("img").forEach((img) => img.remove());
  tileMap.clear();
  occupiedCells.clear();
}

function fadeOutAllTiles() {
  for (const [, entry] of tileMap) {
    fadeOutAndRemove(entry.el);
  }
  tileMap.clear();
  occupiedCells.clear();
}

function renderTileItems(items, source) {
  const prevSource = displayTileSource;
  displayTileSource = source;
  updateRecommendedControls();
  if (Date.now() < tilePausedUntil) return;
  if (prevSource !== displayTileSource) clearAllTiles();
  imageTileEl.querySelectorAll("img.fade-out").forEach((img) => img.remove());

  const filteredItems = items.filter((item) => item.score != null);
  if (!filteredItems.length) {
    clearAllTiles();
    return;
  }

  const scores = filteredItems.map((item) => item.score ?? 0);
  const minScore = Math.min(...scores);
  const maxScore = Math.max(...scores);
  const scoreRange = maxScore - minScore || 1;

  const currentIds = new Set(filteredItems.filter((item) => {
    const normalized = ((item.score ?? 0) - minScore) / scoreRange;
    return normalized >= 0.2;
  }).map((item) => item.id));

  for (const [id, entry] of tileMap) {
    if (!currentIds.has(id)) {
      fadeOutAndRemove(entry.el);
      occupiedCells.delete(entry.cell);
      tileMap.delete(id);
    }
  }

  const sorted = [...filteredItems].sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
  const now = Date.now();

  for (const item of sorted) {
    const id = item.id;
    const normalized = ((item.score ?? 0) - minScore) / scoreRange;
    if (normalized < 0.2) continue;
    const brightness = 0.3 + normalized * 0.7;

    if (tileMap.has(id)) {
      const entry = tileMap.get(id);
      entry.lastSeen = now;
      entry.el.style.filter = `brightness(${brightness})`;
    } else {
      const cell = getNextEmptyCell();
      if (cell !== null) {
        addTileAt(cell, item, brightness);
      } else {
        replaceFarthestTile(item, brightness, normalized);
      }
    }
  }
}

// Periodic cleanup: remove tiles not seen in results for 5s (only in similar mode)
setInterval(() => {
  if (displayTileSource !== "similar") return;
  if (Date.now() < tilePausedUntil) return;
  const now = Date.now();
  for (const [id, entry] of tileMap) {
    if (now - entry.lastSeen > 5000) {
      fadeOutAndRemove(entry.el);
      occupiedCells.delete(entry.cell);
      tileMap.delete(id);
    }
  }
}, 1000);

function connectImageTile() {
  tileWs = new WebSocket(`${liveWsOrigin}/ws_image_tile/${userId}`);

  tileWs.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.kind === "snapshot") {
      similarTileItems = msg.similarItems ?? [];
      recommendedTileItems = msg.recommendedItems ?? [];
      const source = displayTileSource === "recommended" && recommendedTileItems.length > 0
        ? "recommended"
        : "similar";
      const items = source === "recommended" ? recommendedTileItems : similarTileItems;
      renderTileItems(items, source);
      return;
    }
    if (msg.kind === "similar") {
      similarTileItems = msg.items ?? [];
      if (displayTileSource === "similar") {
        renderTileItems(similarTileItems, "similar");
      }
      return;
    }
    if (msg.kind === "recommended") {
      recommendedTileItems = msg.items ?? [];
      renderTileItems(recommendedTileItems, "recommended");
    }
  };

  tileWs.onclose = () => {
    updateRecommendedControls();
    setTimeout(connectImageTile, 3000);
  };
}

// --- Item popup ---

const itemPopup = document.getElementById("item-popup");
const popupImage = document.getElementById("popup-image");
const popupName = document.getElementById("popup-name");
const popupPrice = document.getElementById("popup-price");
const popupDescription = document.getElementById("popup-description");
const popupUrl = document.getElementById("popup-url");

document.getElementById("popup-close").addEventListener("click", closePopup);
document.getElementById("popup-backdrop").addEventListener("click", closePopup);

function closePopup() {
  itemPopup.classList.remove("visible");
  itemPopup.classList.remove("active");
}

async function showItemPopup(itemId) {
  tilePausedUntil = Date.now() + 10000;
  itemPopup.classList.add("active");
  itemPopup.classList.remove("visible");
  try {
    const res = await fetch(
      buildUrl(runtimeConfig.searchOrigin, `/api/item/${encodeURIComponent(itemId)}`),
    );
    if (!res.ok) { closePopup(); return; }
    const item = await res.json();
    popupName.textContent = item.name || itemId;
    popupPrice.textContent = item.price ? `$${Number(item.price).toLocaleString()}` : "";
    popupPrice.style.display = item.price ? "block" : "none";
    popupDescription.textContent = item.description || "";
    if (item.url) {
      popupUrl.href = item.url;
      popupUrl.style.display = "inline-block";
    } else {
      popupUrl.style.display = "none";
    }
    const img = new Image();
    img.onload = () => {
      popupImage.src = img.src;
      itemPopup.classList.add("visible");
    };
    img.src = `https://u-mercari-images.mercdn.net/photos/${itemId}_1.jpg?w=480&h=480&fitcrop&sharpen`;
  } catch (e) {
    console.error("Failed to load item details:", e);
    closePopup();
  }
}

// --- Start button (init mic + camera together) ---

startBtn.addEventListener("click", async () => {
  if (!canUseMediaDevices()) {
    addSystemMessage(getMediaAccessErrorMessage());
    return;
  }
  hasStartedExperience = true;
  updateChatEmptyState();
  try {
    await startCamera();
    await startMic();
    updateRecommendedControls();
  } catch (e) {
    console.error("Start error:", e);
    addSystemMessage("Start error: " + e.message);
  }
});

closeRecommendedBtn.addEventListener("click", () => {
  recommendedTileItems = [];
  displayTileSource = "similar";
  updateRecommendedControls();
  fadeOutAllTiles();
  if (similarTileItems.length) {
    setTimeout(() => {
      renderTileItems(similarTileItems, "similar");
    }, 120);
  }
});

flipCameraBtn.addEventListener("click", async () => {
  await flipCamera();
});

if (!canUseMediaDevices()) {
  addSystemMessage(getMediaAccessErrorMessage());
}

updateChatEmptyState();

// --- Init ---
connect();
connectImageTile();
