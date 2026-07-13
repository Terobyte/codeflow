// Синапс UI v3: SPA-shell — hash-роуты #/ и #/thread/<id>, переходы без перезагрузки,
// поэтому голосовая WebRTC-сессия живёт при навигации (Ж6). Только textContent /
// style-присваивания (XSS: текст ленты, имена папок и заголовки тредов произвольны).
// Цвет статуса Коры приходит ГОТОВЫМ с /client/kora-status — на клиенте ноль логики статуса.
import { PipecatClient, SmallWebRTCTransport } from "./vendor/pipecat.mjs";

const $ = (id) => document.getElementById(id);
const COLORS = { green: "#2ecc71", yellow: "#f1c40f", red: "#e74c3c" };
const KIND_ICONS = { task: "▶", text: "💬", thinking: "🧠", tool_use: "🔧",
                     tool_result: "·", result: "🏁", system: "⚙", user: "🗣", assistant: "🤖" };

async function getJSON(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(url + " → " + res.status);
  return res.json();
}
function postJSON(url, body) {
  return fetch(url, { method: "POST", headers: { "content-type": "application/json" },
                      body: JSON.stringify(body) });
}
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text) n.textContent = text;
  return n;
}
function relTime(ts) {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "только что";
  if (d < 3600) return Math.floor(d / 60) + " мин";
  if (d < 86400) return Math.floor(d / 3600) + " ч";
  return Math.floor(d / 86400) + " дн";
}

// ---------- роутер ----------
function route() {
  const m = location.hash.match(/^#\/thread\/([^/?&#]+)/);
  return m ? { view: "thread", id: decodeURIComponent(m[1]) } : { view: "home" };
}

let threads = [];
let projects = [];
let feedThread = null; // чей фид сейчас в DOM
let feedCount = 0;     // сколько записей уже отрендерено (инкрементальный append)

function render() {
  const r = route();
  closeDrawer();
  $("view-home").hidden = r.view !== "home";
  $("view-thread").hidden = r.view !== "thread";
  if (r.view === "thread") {
    const t = threads.find((x) => x.id === r.id);
    $("view-title").textContent = t ? t.title : "тред";
    $("msg-input").placeholder = "Сообщение…";
    renderBadge(t);
    if (feedThread !== r.id) {
      feedThread = r.id;
      feedCount = 0;
      $("feed-list").replaceChildren();
    }
    pollFeed();
  } else {
    $("view-title").textContent = "Синапс";
    $("msg-input").placeholder = "Опиши задачу…";
    $("thread-badge").hidden = true;
    feedThread = null;
  }
  // Голос адресуется открытому треду; дом = авто-тред диспетчера (сброс в null).
  postJSON("/api/active-thread", { id: r.view === "thread" ? r.id : null }).catch(() => {});
  renderSidebar();
  renderHome();
}
window.addEventListener("hashchange", render);

function renderBadge(t) {
  const b = $("thread-badge");
  if (!t || !t.last_outcome) { b.hidden = true; return; }
  b.textContent = t.last_outcome === "failed" ? "✖ ошибка"
    : t.last_outcome === "cancelled" ? "⏹ отменено" : "✓ готово";
  b.className = t.last_outcome === "failed" ? "bad" : "";
  b.hidden = false;
}

// ---------- сайдбар: проекты + треды-карточки ----------
function threadCard(t, cur) {
  const a = el("a", "thread-card" + (cur.view === "thread" && cur.id === t.id ? " active" : ""));
  a.href = "#/thread/" + encodeURIComponent(t.id);
  const badge = t.last_outcome === "failed" ? "✖ " : t.last_outcome === "completed" ? "✓ " : "";
  a.appendChild(el("span", "tc-title", badge + t.title));
  const proj = projects.find((p) => p.id === t.project_id);
  a.appendChild(el("span", "tc-meta", relTime(t.updated_ts) + (proj ? " · " + proj.name : "")));
  return a;
}

function renderSidebar() {
  const cur = route();
  const pul = $("projects-list");
  pul.replaceChildren();
  projects.forEach((p) => {
    const li = el("li", "", "📁 " + p.name);
    li.title = p.path;
    pul.appendChild(li);
  });
  const tul = $("threads-list");
  tul.replaceChildren();
  threads.forEach((t) => {
    const li = el("li");
    li.appendChild(threadCard(t, cur));
    tul.appendChild(li);
  });
}

function renderHome() {
  const cur = route();
  const ul = $("home-recent");
  ul.replaceChildren();
  threads.slice(0, 6).forEach((t) => {
    const li = el("li");
    li.appendChild(threadCard(t, cur));
    ul.appendChild(li);
  });
  $("recent-h").hidden = threads.length === 0;
}

async function loadLists() {
  try {
    const [tData, pData] = await Promise.all([getJSON("/api/threads"), getJSON("/api/projects")]);
    threads = tData.threads;
    projects = pData.projects;
  } catch { return; } // сеть упала — оставляем прошлый рендер, не гадаем
  renderSidebar();
  renderHome();
  const r = route();
  if (r.view === "thread") {
    const t = threads.find((x) => x.id === r.id);
    if (t) { $("view-title").textContent = t.title; renderBadge(t); }
  }
}

// ---------- лента треда ----------
function addEntry(e) {
  const li = el("li", "feed-" + (e.kind || "misc"));
  if (e.kind === "user" || e.kind === "assistant" || e.kind === "text") {
    li.classList.add("msg", e.kind === "user" ? "msg-user" : "msg-bot");
    li.textContent = e.text || "";
  } else if (e.kind === "thinking" || e.kind === "tool_use" || e.kind === "tool_result") {
    const det = document.createElement("details");
    det.appendChild(el("summary", "", e.kind === "thinking" ? "🧠 размышления"
      : e.kind === "tool_use" ? "🔧 инструмент" : "· результат инструмента"));
    if (e.text) det.appendChild(el("pre", "", e.text));
    li.appendChild(det);
  } else if (e.kind === "result") {
    li.textContent = "🏁 " + (e.text || "");
    if (/fail|ошибк|прерв|отмен/i.test(e.text || "")) li.classList.add("bad");
  } else if (e.kind === "task") {
    li.textContent = "▶ " + (e.text || "");
  } else {
    li.textContent = (KIND_ICONS[e.kind] || "·") + " " + (e.text || "");
  }
  $("feed-list").appendChild(li);
}

function nearBottom() {
  const v = $("view-thread");
  return v.scrollHeight - v.scrollTop - v.clientHeight < 120;
}

async function pollFeed() {
  const r = route();
  if (r.view !== "thread") return;
  let data;
  try { data = await getJSON(`/api/threads/${encodeURIComponent(r.id)}/feed?limit=500`); }
  catch { return; }
  if (route().id !== r.id || feedThread !== r.id) return; // роут сменился, пока ждали
  const fresh = data.entries.slice(feedCount);
  if (!fresh.length) return;
  const stick = feedCount === 0 || nearBottom();
  fresh.forEach(addEntry);
  feedCount = data.entries.length;
  if (stick) $("feed-list").lastElementChild.scrollIntoView({ block: "end" });
}

// ---------- статус Коры: карточка в сайдбаре (цвет готовым с сервера) ----------
function setKora(color, sub) {
  $("kora-card-dot").style.background = color;
  $("kora-card-sub").textContent = sub;
}
async function pollStatus() {
  let data;
  try { data = await getJSON("./kora-status"); }
  catch { setKora("#888", "нет связи"); return; }
  // task_text живёт и ПОСЛЕ завершения задачи — «работает» только при running.
  const sub = data.awaiting_answer ? "ждёт ответа: " + (data.task_text || "")
    : data.task_status === "running" ? "работает: " + (data.task_text || "") : "свободна";
  setKora(COLORS[data.color] || "#888", sub);
}

// ---------- композер: текст ----------
function setConn(text) {
  $("conn-status").textContent = text;
  $("conn-status").hidden = !text;
}

async function sendMessage() {
  const input = $("msg-input");
  const text = input.value.trim();
  if (!text) return;
  const r = route();
  $("msg-send").disabled = true;
  input.value = "";
  try {
    let id = r.view === "thread" ? r.id : null;
    if (!id) {
      // Дом: первое сообщение создаёт тред с человеческим именем (Ж1 — не «новый тред»)
      const tRes = await postJSON("/api/threads", { title: text.slice(0, 60) });
      if (!tRes.ok) { setConn("не удалось создать тред"); return; }
      id = (await tRes.json()).id;
      location.hash = "#/thread/" + encodeURIComponent(id); // render() переключит вью без reload
    }
    $("typing").hidden = false;
    const res = await postJSON(`/api/threads/${encodeURIComponent(id)}/message`, { text });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      setConn("⛔ " + (d.error || "ошибка " + res.status));
    } else {
      setConn("");
    }
    await Promise.all([pollFeed(), loadLists()]);
  } catch {
    setConn("сеть недоступна");
  } finally {
    $("typing").hidden = true;
    $("msg-send").disabled = false;
  }
}
$("msg-send").addEventListener("click", sendMessage);
$("msg-input").addEventListener("keydown", (e) => { if (e.key === "Enter") sendMessage(); });

// ---------- голос: vendored SDK → session-less POST /api/offer, видимые стейты ----------
const botAudio = $("bot-audio");
let client = null;
let connecting = false;

function setMicState(state, msg) {
  $("mic-btn").dataset.state = state;
  if (state === "error") setConn("⛔ " + msg);
  else setConn(state === "connecting" ? "подключаю голос…"
    : state === "on" ? "🎙 говори — я слушаю" : "");
}

function withTimeout(promise, ms) {
  return Promise.race([promise, new Promise((_, rej) =>
    setTimeout(() => rej(new Error("таймаут — проверь разрешение микрофона")), ms))]);
}

async function connectVoice() {
  // Явная диагностика вместо вечного зависания getUserMedia (Ж2): запрещённый микрофон
  // и таймаут дают видимую ошибку. permissions.query нет в старом Safari → пропускаем.
  const perm = await navigator.permissions.query({ name: "microphone" }).catch(() => null);
  if (perm && perm.state === "denied") throw new Error("микрофон запрещён для этого сайта");
  client = new PipecatClient({
    transport: new SmallWebRTCTransport({ webrtcUrl: "/api/offer" }),
    enableMic: true,
    callbacks: {
      onConnected: () => setMicState("on"),
      onDisconnected: () => { client = null; setMicState("idle"); },
      onTrackStarted: (track, participant) => {
        // SmallWebRTCTransport зовёт onTrackStarted(track) БЕЗ participant для
        // remote-треков (vendor: `onTrackStarted?.(n.track)`), local через этот
        // колбэк не ходит вовсе — «participant &&» молча выбрасывал аудио бота.
        if (track.kind === "audio" && !participant?.local) {
          botAudio.srcObject = new MediaStream([track]);
        }
      },
      onError: (e) => { console.error("voice error:", e); setConn("⛔ ошибка соединения"); },
    },
  });
  await withTimeout(client.connect(), 20000);
}

$("mic-btn").addEventListener("click", async () => {
  if (connecting) return;
  if (client) {
    const c = client;
    client = null;
    setMicState("idle");
    await c.disconnect().catch(() => {});
    return;
  }
  connecting = true;
  setMicState("connecting");
  try {
    await connectVoice();
  } catch (err) {
    console.error("voice connect failed:", err);
    try { client && client.disconnect(); } catch { /* уже мёртв */ }
    client = null;
    setMicState("error", err && err.message ? err.message : "не удалось подключиться");
  } finally {
    connecting = false;
  }
});

// ---------- drawer (мобайл) ----------
function closeDrawer() { $("shell").classList.remove("drawer-open"); $("backdrop").hidden = true; }
$("menu-btn").addEventListener("click", () => {
  $("shell").classList.add("drawer-open");
  $("backdrop").hidden = false;
});
$("side-close").addEventListener("click", closeDrawer);
$("backdrop").addEventListener("click", closeDrawer);
$("new-thread").addEventListener("click", () => {
  location.hash = "#/";
  closeDrawer();
  $("msg-input").focus();
});

// ---------- пикер папки для «+ проект» (GET /api/browse — сервер листает сам) ----------
const picker = $("picker");
const pickerPath = $("picker-path");
const pickerDirs = $("picker-dirs");
let pickerCur = null;

async function browse(path) {
  let data;
  try {
    const url = "/api/browse" + (path ? "?path=" + encodeURIComponent(path) : "");
    data = await getJSON(url);
  } catch { return; }
  pickerCur = data.path;
  pickerPath.textContent = data.path;
  pickerDirs.replaceChildren();
  if (data.parent) {
    const up = el("li", "", "‹ назад");
    up.addEventListener("click", () => browse(data.parent));
    pickerDirs.appendChild(up);
  }
  data.dirs.forEach((name) => {
    const li = el("li", "", "📁 " + name);
    li.addEventListener("click", () => browse(data.path + "/" + name));
    pickerDirs.appendChild(li);
  });
}

$("add-project").addEventListener("click", () => {
  picker.hidden = false;
  browse(null);
});
$("picker-cancel").addEventListener("click", () => { picker.hidden = true; });
$("picker-choose").addEventListener("click", async () => {
  if (!pickerCur) return;
  const res = await postJSON("/api/projects", { name: "", path: pickerCur }).catch(() => null);
  if (res && !res.ok) {
    const data = await res.json().catch(() => ({}));
    pickerPath.textContent = "⛔ " + (data.error || "не удалось добавить");
    return; // пикер остаётся открытым — видно причину, можно выбрать другую папку
  }
  picker.hidden = true;
  loadLists();
});

// ---------- init ----------
loadLists().then(render);
pollStatus();
setInterval(loadLists, 5000);
setInterval(pollFeed, 3000);
setInterval(pollStatus, 3000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { loadLists(); pollFeed(); pollStatus(); }
});
