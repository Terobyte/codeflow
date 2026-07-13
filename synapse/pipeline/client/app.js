// Синапс UI v2: дом «как Codex» — сайдбар проектов слева, треды в центре, минимум слов
// (фидбек Теро 2026-07-13). Светофор — цвет приходит ГОТОВЫМ с /client/kora-status
// (_status_color на сервере, здесь ни логики статуса, ни wall-clock). Только
// textContent/style-присваивания (XSS: task_text/имена папок — произвольный текст).
const COLORS = { green: "#2ecc71", yellow: "#f1c40f", red: "#e74c3c" };
const dot = document.getElementById("kora-dot");

async function pollStatus() {
  let res;
  try {
    res = await fetch("./kora-status", { cache: "no-store" });
  } catch {
    dot.style.background = "#888";
    return;
  }
  if (!res.ok) { dot.style.background = "#888"; return; }
  const data = await res.json();
  dot.style.background = COLORS[data.color] || "#888";
  dot.title = (data.task_text ? "Кора: " + data.task_text : "Кора: нет задачи") + " · " + data.liveness;
}
pollStatus();
setInterval(pollStatus, 3000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) pollStatus(); });
dot.addEventListener("click", () => { location.href = "./logs"; });

// Голос (Р1): vendored SDK → session-less POST /api/offer (курлобельный роут
// webrtc_server.py; /start-хендшейк — деталь prebuilt-клиента, нам не нужен).
// Коннект-логика НАША — закрывает парковку слайса 5 (prebuilt умирал после 3 ретраев).
import { PipecatClient, SmallWebRTCTransport } from "./vendor/pipecat.mjs";

const micBtn = document.getElementById("mic-btn");
const connStatus = document.getElementById("conn-status");
const botAudio = document.getElementById("bot-audio");
let client = null;

// Строка состояния видна только когда есть что сказать — «не подключено» по умолчанию
// было мусорным словом на пустом доме (фидбек Теро).
function setConn(text) { connStatus.textContent = text; connStatus.hidden = !text; }

// Кнопка-микрофон = тумблер голоса: горит, пока WebRTC-сессия жива.
function setMic(on) { micBtn.style.background = on ? "#1f6f3f" : ""; micBtn.title = on ? "голос включён" : "включить голос"; }

async function connectVoice() {
  client = new PipecatClient({
    transport: new SmallWebRTCTransport({ webrtcUrl: "/api/offer" }),
    enableMic: true,
    callbacks: {
      onConnected: () => { setConn("подключено — говори"); setMic(true); },
      onDisconnected: () => { setConn(""); setMic(false); client = null; },
      onTrackStarted: (track, participant) => {
        // SmallWebRTCTransport зовёт onTrackStarted(track) БЕЗ participant для
        // remote-треков (vendor: `onTrackStarted?.(n.track)`), local через этот
        // колбэк не ходит вовсе — «participant &&» молча выбрасывал аудио бота.
        if (track.kind === "audio" && !participant?.local) {
          botAudio.srcObject = new MediaStream([track]);
        }
      },
      onError: () => setConn("ошибка соединения"),
    },
  });
  setConn("подключаюсь…");
  await client.connect();
}

micBtn.addEventListener("click", async () => {
  if (client) { const c = client; client = null; await c.disconnect(); return; }
  try {
    await connectVoice();
  } catch {
    setConn("не удалось подключиться");
    setMic(false);
    client = null;
  }
});

// Чат внизу дома: первое сообщение создаёт тред и открывает его (Codex-паттерн).
const msgInput = document.getElementById("msg-input");
const msgSend = document.getElementById("msg-send");
msgSend.addEventListener("click", async () => {
  const text = msgInput.value.trim();
  if (!text) return;
  msgSend.disabled = true;
  setConn("отправляю…");
  try {
    const tRes = await fetch("/api/threads", {
      method: "POST", headers: { "content-type": "application/json" }, body: "{}",
    });
    if (!tRes.ok) { setConn("не удалось создать тред"); return; }
    const t = await tRes.json();
    await fetch(`/api/threads/${t.id}/message`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ text }),
    });
    location.href = "./thread?id=" + encodeURIComponent(t.id);
  } catch {
    setConn("сеть недоступна");
  } finally {
    msgSend.disabled = false;
  }
});
msgInput.addEventListener("keydown", (e) => { if (e.key === "Enter") msgSend.click(); });

// Дом = голос на авто-треде: открытие дома сбрасывает активный тред.
fetch("/api/active-thread", { method: "POST", headers: { "content-type": "application/json" },
                              body: JSON.stringify({ id: null }) }).catch(() => {});

// --- пикер папки для «+ проект» (GET /api/browse — сервер локальный, листает сам;
// абсолютный путь руками больше не вводится) ------------------------------------------
const picker = document.getElementById("picker");
const pickerPath = document.getElementById("picker-path");
const pickerDirs = document.getElementById("picker-dirs");
let pickerCur = null;

async function browse(path) {
  let res;
  try {
    const url = "/api/browse" + (path ? "?path=" + encodeURIComponent(path) : "");
    res = await fetch(url, { cache: "no-store" });
  } catch { return; }
  if (!res.ok) return;
  const data = await res.json();
  pickerCur = data.path;
  pickerPath.textContent = data.path;
  pickerDirs.replaceChildren();
  if (data.parent) {
    const up = document.createElement("li");
    up.textContent = "‹ назад";
    up.addEventListener("click", () => browse(data.parent));
    pickerDirs.appendChild(up);
  }
  data.dirs.forEach((name) => {
    const li = document.createElement("li");
    li.textContent = "📁 " + name;
    li.addEventListener("click", () => browse(data.path + "/" + name));
    pickerDirs.appendChild(li);
  });
}

document.getElementById("add-project").addEventListener("click", () => {
  picker.hidden = false;
  browse(null);
});
document.getElementById("picker-cancel").addEventListener("click", () => { picker.hidden = true; });
document.getElementById("picker-choose").addEventListener("click", async () => {
  if (!pickerCur) return;
  const res = await fetch("/api/projects", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name: "", path: pickerCur }),
  }).catch(() => null);
  if (res && !res.ok) {
    const data = await res.json().catch(() => ({}));
    pickerPath.textContent = "⛔ " + (data.error || "не удалось добавить");
    return; // пикер остаётся открытым — видно причину, можно выбрать другую папку
  }
  picker.hidden = true;
  loadLists();
});

// --- списки: проекты в сайдбар, треды в центр ----------------------------------------
async function loadLists() {
  try {
    const [tRes, pRes] = await Promise.all([
      fetch("/api/threads", { cache: "no-store" }), fetch("/api/projects", { cache: "no-store" }),
    ]);
    if (pRes.ok) {
      const { projects } = await pRes.json();
      const ul = document.getElementById("projects-list");
      ul.replaceChildren();
      projects.forEach((p) => {
        const li = document.createElement("li");
        li.textContent = p.name;
        li.title = p.path;
        ul.appendChild(li);
      });
    }
    if (tRes.ok) {
      const { threads } = await tRes.json();
      const ul = document.getElementById("threads-list");
      ul.replaceChildren();
      threads.slice(0, 20).forEach((t) => {
        const li = document.createElement("li");
        const a = document.createElement("a");
        a.href = "./thread?id=" + encodeURIComponent(t.id);
        a.textContent = (t.last_outcome === "failed" ? "✖ " : "") + t.title;
        li.appendChild(a);
        ul.appendChild(li);
      });
    }
  } catch { /* сеть упала — дом остаётся пустым, не гадаем */ }
}
loadLists();
