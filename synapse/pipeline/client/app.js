// Синапс UI v2, слайс UI-1: дом. Светофор — цвет приходит ГОТОВЫМ с /client/kora-status
// (_status_color на сервере, здесь ни логики статуса, ни wall-clock). Только
// textContent/style-присваивания (XSS: task_text — произвольный текст задачи).
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

const btn = document.getElementById("agent-btn");
const connStatus = document.getElementById("conn-status");
const botAudio = document.getElementById("bot-audio");
let client = null;

function setConn(text) { connStatus.textContent = text; }

async function connectVoice() {
  client = new PipecatClient({
    transport: new SmallWebRTCTransport({ webrtcUrl: "/api/offer" }),
    enableMic: true,
    callbacks: {
      onConnected: () => { setConn("подключено — говори"); btn.textContent = "⏹ Завершить"; },
      onDisconnected: () => { setConn("не подключено"); btn.textContent = "🎙 Открыть агента"; client = null; },
      onTrackStarted: (track, participant) => {
        if (track.kind === "audio" && participant && !participant.local) {
          botAudio.srcObject = new MediaStream([track]);
        }
      },
      onError: () => setConn("ошибка соединения"),
    },
  });
  setConn("подключаюсь…");
  await client.connect();
}

btn.addEventListener("click", async () => {
  if (client) { const c = client; client = null; await c.disconnect(); return; }
  try {
    await connectVoice();
  } catch {
    setConn("не удалось подключиться");
    client = null;
  }
});

// UI v2 слайс UI-3: дом наполняет списки тредов/проектов. Дом = голос на авто-треде:
// открытие дома сбрасывает активный тред.
fetch("/api/active-thread", { method: "POST", headers: { "content-type": "application/json" },
                              body: JSON.stringify({ id: null }) }).catch(() => {});

async function addProject() {
  // prompt() допустим v1 (план UI-3 Task 15); форма — полировка UI-4.
  const name = prompt("Имя проекта:");
  if (name === null) return;
  const path = prompt("Абсолютный путь к директории проекта:");
  if (!path) return;
  await fetch("/api/projects", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name, path }),
  }).catch(() => {});
  loadLists();
}

async function loadLists() {
  try {
    const [tRes, pRes] = await Promise.all([
      fetch("/api/threads", { cache: "no-store" }), fetch("/api/projects", { cache: "no-store" }),
    ]);
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
      document.getElementById("threads-section").hidden = threads.length === 0;
    }
    if (pRes.ok) {
      const { projects } = await pRes.json();
      const ul = document.getElementById("projects-list");
      ul.replaceChildren();
      projects.forEach((p) => {
        const li = document.createElement("li");
        li.textContent = p.name;
        ul.appendChild(li);
      });
      const addLi = document.createElement("li");
      const addBtn = document.createElement("a");
      addBtn.href = "#";
      addBtn.textContent = "+ проект";
      addBtn.addEventListener("click", (e) => { e.preventDefault(); addProject(); });
      addLi.appendChild(addBtn);
      ul.appendChild(addLi);
      document.getElementById("projects-section").hidden = false;
    }
  } catch { /* сеть упала — дом остаётся пустым, не гадаем */ }
}
loadLists();
