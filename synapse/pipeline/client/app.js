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
