// Синапс UI v3: SPA-shell — hash-роуты #/ и #/thread/<id>, переходы без перезагрузки,
// поэтому голосовая WebRTC-сессия живёт при навигации (Ж6). Только textContent /
// style-присваивания (XSS: текст ленты, имена папок и заголовки тредов произвольны).
// Цвет статуса Коры приходит ГОТОВЫМ с /client/kora-status — на клиенте ноль логики статуса.
import { PipecatClient, SmallWebRTCTransport } from "./vendor/pipecat.mjs";

const $ = (id) => document.getElementById(id);
// PF7: Organic-рампа вместо traffic-light цветов; механизм inline-точки (setKora) не трогаем.
const COLORS = { green: "#7a8a5e", yellow: "#f6a06b", red: "#b2622d" };
const KIND_ICONS = { task: "▶", text: "💬", thinking: "🧠", tool_use: "🔧",
                     tool_result: "·", result: "🏁", system: "⚙", user: "🗣", assistant: "🤖" };

// R6 guardrail: никакой сырой HTML-вставки — динамические иконки идут через SVG-узлы,
// собранные createElementNS-ом, текст — только textContent.
const SVG_NS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs = {}) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}
function iconSvg(paths, size = 12) {
  const svg = svgEl("svg", { viewBox: "0 0 24 24", width: size, height: size,
    fill: "currentColor", stroke: "none" });
  paths.forEach((d) => svg.appendChild(svgEl("path", { d })));
  return svg;
}
const ICON_PLAY = ["M8 5v14l11-7z"];
const ICON_PAUSE = ["M7 5h4v14H7z", "M13 5h4v14h-4z"];
// stroke-иконки аватаров (канон макета): Кора — код-скобки, Диспетчер — workflow-узлы.
function strokeIcon(parts, size = 14) {
  const svg = svgEl("svg", { viewBox: "0 0 24 24", width: size, height: size, fill: "none",
    stroke: "currentColor", "stroke-width": "2.75",
    "stroke-linecap": "round", "stroke-linejoin": "round" });
  parts.forEach(([tag, attrs]) => svg.appendChild(svgEl(tag, attrs)));
  return svg;
}
const AV_KORA = [["polyline", { points: "16 18 22 12 16 6" }],
                 ["polyline", { points: "8 6 2 12 8 18" }]];
const AV_DISP = [["circle", { cx: "6", cy: "19", r: "3" }],
                 ["path", { d: "M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15" }],
                 ["circle", { cx: "18", cy: "5", r: "3" }]];

const FETCH_TIMEOUT_MS = 15000;
async function getJSON(url) {
  // B-CORE-2: AbortController + таймаут — «молчащий» сервер не оставит промис висеть навсегда.
  const ctrl = new AbortController();
  const timeout = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(url, { cache: "no-store", signal: ctrl.signal });
    if (!res.ok) {
      // B55: статус едет на ошибке — вызыватель различает «треда нет» (404) от сетевого блипа.
      const err = new Error(url + " → " + res.status);
      err.status = res.status;
      throw err;
    }
    return await res.json();
  } finally { clearTimeout(timeout); }
}
function postJSON(url, body) {
  const ctrl = new AbortController();
  const timeout = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS); // B-CORE-2
  return fetch(url, { method: "POST", headers: { "content-type": "application/json" },
                      body: JSON.stringify(body), signal: ctrl.signal })
    .finally(() => clearTimeout(timeout));
}
// UI-5 (S30): PATCH для rename треда — тот же CSRF/timeout-паттерн, что postJSON.
function patchJSON(url, body) {
  const ctrl = new AbortController();
  const timeout = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  return fetch(url, { method: "PATCH", headers: { "content-type": "application/json" },
                      body: JSON.stringify(body), signal: ctrl.signal })
    .finally(() => clearTimeout(timeout));
}
// UI-5 (S31): DELETE для удаления проекта.
function deleteJSON(url) {
  const ctrl = new AbortController();
  const timeout = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  return fetch(url, { method: "DELETE", headers: { "content-type": "application/json" },
                      signal: ctrl.signal })
    .finally(() => clearTimeout(timeout));
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
  if (location.hash === "#/activity") return { view: "activity" };
  const m = location.hash.match(/^#\/thread\/([^/?&#]+)/);
  if (!m) return { view: "home" };
  // B-UX-6: битые %-эскейпы в хеше кидают URIError из decodeURIComponent — ловим,
  // иначе одно кривое звено рушит весь render-цикл (route зовётся на каждый рендер).
  try { return { view: "thread", id: decodeURIComponent(m[1]) }; }
  catch { return { view: "home" }; }
}

let threads = [];
let projects = [];
let feedThread = null;          // чей фид сейчас в DOM
const renderedKeys = new Set();     // подписи уже отрисованных записей — курсор по КОНТЕНТУ, а не по
                                // индексу окна: скользящее ?limit=500 больше не «замерзает» после
                                // 500 записей (B-CORE-3) и поздний параллельный полл не плодит
                                // дубли (B-CORE-7).
let feedInFlight = false;       // сериализуем параллельные pollFeed (B-CORE-7)
let feedNotFound = null;        // B55: id треда, чей фид ответил 404 — поллинг остановлен,
                                // в ленте одна event-строка «не найден»; сброс при смене треда
let listsLoaded = false;        // первая успешная загрузка списков состоялась (B-CORE-9)
let listsSeq = 0;               // B-UX-5: секвенс-токен — устаревший ответ не затирает свежие данные
let lastActiveThread = "";      // дедуп POST /api/active-thread (B-CORE-13)
const LOAD_ERR = "нет связи с сервером — тяну снова…";

// единый форматтер исхода (B-CORE-14): бейдж и карточка треда больше не расходятся в значках
const OUTCOME = {
  failed: { icon: "✖", text: "✖ ошибка", bad: true },
  cancelled: { icon: "⏹", text: "⏹ отменено", bad: false },
  completed: { icon: "✓", text: "✓ готово", bad: false },
};
function outcomeLabel(outcome) { return OUTCOME[outcome] || null; }

const STAGES = {
  collect: "СБОР", propose: "ЗАПРОС", spec_plan: "СПЕКА·ПЛАН", code: "КОД", done: "ГОТОВО",
};

// B45: пока открыт inline-редактор rename, узел #view-title ЗАКОННО снят из DOM
// (replaceWith(input)) — фоновые писатели title (render на hashchange, loadLists каждые 5с)
// обязаны молча пропустить тик, а не кидать TypeError на null.
function setViewTitle(text) {
  const n = $("view-title");
  if (n) n.textContent = text;
}
const KORA_MODELS = ["claude-opus-4-8", "claude-sonnet-5", "claude-fable-5"];

// B-UX-4: id (tool_use_id) в ключе — два параллельных ToolResult с одинаковыми ts|kind|text
// («ок»/«ошибка») больше не схлопываются в одну запись ленты.
function feedKey(e) { return (e.id || "") + "|" + (e.ts || 0) + "|" + (e.kind || "") + "|" + (e.text || ""); }

// узкий экран: длинный плейсхолдер макета не влезает в композер-пилюлю рядом с чипом
const narrowMq = window.matchMedia("(max-width: 480px)");
function taskPlaceholder() {
  return narrowMq.matches ? "Задача…" : "Скажите или напишите задачу…";
}

// ---------- активный проект: дом рожает треды-ветки в нём ----------
// Персистится в localStorage; валидируется против загруженного списка — удалённый
// на сервере проект не должен молча утаскивать новые треды в никуда.
let activeProject = localStorage.getItem("synapse-active-project") || null;

function setActiveProject(pid) {
  activeProject = activeProject === pid ? null : pid; // повторный тап снимает выбор
  if (activeProject) localStorage.setItem("synapse-active-project", activeProject);
  else localStorage.removeItem("synapse-active-project");
  render();
}

function validateActiveProject() {
  // Storage — источник правды, здесь только гейт отображения: транзиентно-пустой
  // список проектов (гонка загрузки, рестарт сервера) не должен НАВСЕГДА стирать выбор.
  const stored = localStorage.getItem("synapse-active-project");
  activeProject = stored && projects.some((p) => p.id === stored) ? stored : null;
}

// ---------- тумблер «Диспетчер · реалтайм» (плейсхолдер P1: чисто клиентский гейт входа
// в live, сервер не знает) ----------
let dispOn = localStorage.getItem("synapse-disp-on") !== "false"; // default true

function applyDispToggleUI() {
  $("disp-toggle").setAttribute("aria-checked", String(dispOn));
  // R2: НИКОГДА не ставим disabled — визуальный dim не блокирует hang-up, когда client!=null.
  $("mic-btn").classList.toggle("disp-off", !dispOn);
  $("mic-btn").title = dispOn ? "микрофон" : "Диспетчер выключен — только Кора, доступен чат";
}
$("disp-toggle").addEventListener("click", () => {
  dispOn = !dispOn;
  localStorage.setItem("synapse-disp-on", String(dispOn));
  applyDispToggleUI();
});

// ---------- вкладки Чат / Дифф в топбаре треда (плейсхолдер P2: реальный git diff не подключён) ----------
let tab = "chat";
function setTab(next) {
  tab = next;
  $("tab-chat").setAttribute("aria-selected", String(tab === "chat"));
  $("tab-diff").setAttribute("aria-selected", String(tab === "diff"));
  const inThread = route().view === "thread";
  $("view-thread").hidden = !(inThread && tab === "chat");
  $("view-diff").hidden = !(inThread && tab === "diff");
}
$("tab-chat").addEventListener("click", () => setTab("chat"));
$("tab-diff").addEventListener("click", () => setTab("diff"));

function render() {
  const r = route();
  closeDrawer();
  $("view-home").hidden = r.view !== "home";
  $("view-thread").hidden = !(r.view === "thread" && tab === "chat");
  $("view-diff").hidden = !(r.view === "thread" && tab === "diff");
  $("view-activity").hidden = r.view !== "activity";
  $("thread-tabs").hidden = r.view !== "thread";
  if (r.view === "thread") {
    const t = threads.find((x) => x.id === r.id);
    // B54/B55: тред не в списке ПОСЛЕ успешной загрузки списков = его реально нет (архив/
    // мусорный id) — честный title вместо generic-заглушки; до первой загрузки не паникуем.
    setViewTitle(t ? t.title : (listsLoaded ? "тред не найден" : "тред"));
    $("msg-input").placeholder = "Сообщение…";
    renderBadge(t);
    renderStageChip(t);
    if (feedThread !== r.id) {
      feedThread = r.id;
      feedNotFound = null; // B55: новый тред — прошлый not-found не должен глушить его поллинг
      renderedKeys.clear();
      $("feed-list").replaceChildren();
      setTab("chat"); // новый тред всегда открывается на вкладке «Чат»
    }
    pollFeed();
  } else if (r.view === "activity") {
    setViewTitle("Активность Коры");
    $("msg-input").placeholder = taskPlaceholder();
    $("thread-badge").hidden = true;
    renderStageChip(null);
    feedThread = null;
    pollActivity();
  } else {
    setViewTitle("CodeFlow");
    $("msg-input").placeholder = taskPlaceholder();
    $("thread-badge").hidden = true;
    renderStageChip(null);
    feedThread = null;
  }
  // Голос адресуется открытому треду; дом = авто-тред диспетчера в активном проекте.
  // B-CORE-13: не спамим active-thread на каждый ре-рендер — шлём только при смене цели.
  // gate v2 B2' (MINOR render()): пока голос подключён (client!=null), привязку звонка НЕ
  // меняем — навигация домой/по тредам мид-колл не должна разрывать/уводить звонок.
  // B43: в окнах ПЕРВОГО коннекта и тихого реконнекта client временно null, хотя звонок
  // жив/оживает — гейт расширен на connecting/liveRequested, иначе навигация в это окно
  // переклеивает voice_thread на другой тред и история звонка расщепляется.
  // lastActiveThread не трогаем: после hang-up первый render дошлёт актуальную привязку.
  const activeThreadKey = (r.view === "thread" ? r.id : "") + "|" +
    (r.view === "home" ? (activeProject || "") : "");
  if (activeThreadKey !== lastActiveThread && !client && !connecting && !liveRequested) {
    lastActiveThread = activeThreadKey;
    postJSON("/api/active-thread", {
      id: r.view === "thread" ? r.id : null,
      project_id: r.view === "home" ? activeProject : null,
    }).catch(() => {});
  }
  renderChip(r);
  renderSidebar();
  renderHome();
}
window.addEventListener("hashchange", render);

function renderBadge(t) {
  const b = $("thread-badge");
  const o = outcomeLabel(t && t.last_outcome);
  // running/queued/неизвестный исход больше не рисуются как «✓ готово» — бейдж просто скрыт.
  if (!o) { b.hidden = true; return; }
  b.textContent = o.text;
  b.className = o.bad ? "bad" : "";
  b.hidden = false;
}

function renderStageChip(t) {
  const chip = $("stage-chip");
  // ГОТОВО-чип дублирует бейдж исхода («✓ готово») — прячем, одна пилюля на топбар
  if (t && t.stage === "done" && outcomeLabel(t.last_outcome)) {
    chip.hidden = true; chip.textContent = ""; return;
  }
  const label = t && STAGES[t.stage];
  chip.textContent = label || "";
  chip.hidden = !label;
  chip.className = t && t.stage ? "stage-" + t.stage : "";
}

// ---------- сайдбар: дерево проект → его треды-ветки ----------
function threadCard(t, cur, showProj) {
  // UI-5 (S31): карточка = контейнер (ссылка + кнопка «архив»), не голая <a>.
  const wrap = el("div", "tc-wrap");
  const a = el("a", "thread-card" + (cur.view === "thread" && cur.id === t.id ? " active" : ""));
  a.href = "#/thread/" + encodeURIComponent(t.id);
  const o = outcomeLabel(t.last_outcome);
  // канон макета: строка «точка-статус + заголовок», под ней «время · проект | пилюля стадии»
  const row = el("div", "tc-row");
  row.appendChild(el("span", "tc-dot" + (t.stage ? " stage-" + t.stage : "")));
  row.appendChild(el("span", "tc-title", (o ? o.icon + " " : "") + t.title));
  a.appendChild(row);
  const proj = showProj ? projects.find((p) => p.id === t.project_id) : null;
  const sub = el("div", "tc-sub");
  sub.appendChild(el("span", "tc-meta", relTime(t.updated_ts) + (proj ? " · " + proj.name : "")));
  if (STAGES[t.stage]) sub.appendChild(el("span", "tc-stage stage-" + t.stage, STAGES[t.stage]));
  a.appendChild(sub);
  wrap.appendChild(a);
  const ar = el("button", "tc-archive");
  ar.type = "button";
  ar.textContent = "архив";
  ar.title = "Архивировать тред";
  ar.addEventListener("click", (ev) => { ev.preventDefault(); ev.stopPropagation(); archiveThread(t); });
  wrap.appendChild(ar);
  return wrap;
}

async function archiveThread(t) {
  if (!window.confirm("Архивировать тред «" + t.title + "»?")) return;
  try {
    const res = await postJSON(`/api/threads/${encodeURIComponent(t.id)}/archive`, {});
    if (res.ok) {
      // B54: архивировали ОТКРЫТЫЙ тред — уходим на дом ДО перезагрузки списков,
      // иначе вью молча деградирует в пустую страницу без единого фидбека.
      if (route().view === "thread" && route().id === t.id) location.hash = "#/";
      setConn("тред «" + t.title + "» в архиве");
      await loadLists();
    } else setConn("не архивировать");
  } catch { setConn("сеть недоступна"); }
}

async function deleteProject(p) {
  if (!window.confirm("Удалить проект «" + p.name + "»? Треды останутся, но потеряют привязку.")) return;
  try {
    const res = await deleteJSON(`/api/projects/${encodeURIComponent(p.id)}`);
    if (res.ok) {
      if (activeProject === p.id) setActiveProject(null);
      await loadLists();
    } else setConn("не удалить проект");
  } catch { setConn("сеть недоступна"); }
}

function renderSidebar() {
  const cur = route();
  const known = new Set(projects.map((p) => p.id));
  const pul = $("projects-list");
  pul.replaceChildren();
  projects.forEach((p) => {
    const li = el("li", "project");
    const row = el("button", "project-row" + (p.id === activeProject ? " active" : ""));
    row.type = "button";
    row.title = p.path;
    row.appendChild(el("span", "pr-name", "📁 " + p.name));
    row.addEventListener("click", () => setActiveProject(p.id));
    li.appendChild(row);
    // UI-5 (S31): удалить проект — confirm() перед опасным действием; треды не гибнут.
    const del = el("button", "pr-delete");
    del.type = "button";
    del.textContent = "×";
    del.title = "Удалить проект (треды останутся)";
    del.addEventListener("click", (ev) => { ev.preventDefault(); ev.stopPropagation(); deleteProject(p); });
    li.appendChild(del);
    const branch = el("ul", "branch");
    threads.filter((t) => t.project_id === p.id).forEach((t) => {
      const bi = el("li");
      bi.appendChild(threadCard(t, cur, false)); // имя проекта уже над веткой
      branch.appendChild(bi);
    });
    li.appendChild(branch);
    pul.appendChild(li);
  });
  // мёртвый project_id (проект удалили) — тред не исчезает, а падает в «Без проекта»
  const loose = threads.filter((t) => !t.project_id || !known.has(t.project_id));
  const tul = $("threads-list");
  tul.replaceChildren();
  loose.forEach((t) => {
    const li = el("li");
    li.appendChild(threadCard(t, cur, false));
    tul.appendChild(li);
  });
  $("loose-h").hidden = loose.length === 0;
}

// ---------- чип проекта в композере: куда родится тред с дома ----------
function renderChip(r) {
  const chip = $("proj-chip");
  if (r.view !== "home") { chip.hidden = true; return; }
  const proj = projects.find((p) => p.id === activeProject);
  chip.textContent = proj ? "📁 " + proj.name : "без проекта";
  chip.classList.toggle("has-proj", !!proj);
  chip.hidden = false;
}
$("proj-chip").addEventListener("click", () => {
  // тап по чипу ведёт к списку проектов — выбор делается тапом по строке проекта
  openDrawer();
});

function renderHome() {
  const cur = route();
  const ul = $("home-recent");
  ul.replaceChildren();
  threads.slice(0, 6).forEach((t) => {
    const li = el("li");
    li.appendChild(threadCard(t, cur, true)); // на доме ветки перемешаны — имя проекта нужно
    ul.appendChild(li);
  });
  $("recent-h").hidden = threads.length === 0;
}

async function loadLists() {
  const my = ++listsSeq;
  try {
    const [tData, pData] = await Promise.all([getJSON("/api/threads"), getJSON("/api/projects")]);
    // B-UX-5: пока ждали ответ, стартовал более новый loadLists — этот устарел, не затираем
    // им свежие данные (last-to-resolve-wins гонка, как у pollFeed/browse).
    if (my !== listsSeq) return;
    threads = tData.threads;
    projects = pData.projects;
    // B-CORE-9: первая удачная загрузка снимает баннер «нет связи», если он висел.
    if (!listsLoaded) { listsLoaded = true; if ($("conn-status").textContent === LOAD_ERR) setConn(""); }
  } catch {
    // B-CORE-9: молчаливый catch оставлял пустой UI без обратной связи на холодном старте.
    if (!listsLoaded) setConn(LOAD_ERR);
    return; // после старта — тихо оставляем прошлый рендер, фоновый ретрайер подхватит
  }
  validateActiveProject();
  renderChip(route());
  renderSidebar();
  renderHome();
  const r = route();
  if (r.view === "thread") {
    const t = threads.find((x) => x.id === r.id);
    if (t) { setViewTitle(t.title); renderBadge(t); renderStageChip(t); }
  }
}

// ---------- лента треда ----------
// PF3/P3-плейсхолдер: визуальный toggle озвучки, без реального аудио (парк P3).
function playButton(role) {
  const btn = el("button", "play-btn");
  btn.type = "button";
  btn.title = "TTS · голос " + (role === "disp" ? "диспетчера" : "Коры");
  btn.appendChild(iconSvg(ICON_PLAY));
  btn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const playing = btn.classList.toggle("playing");
    btn.replaceChildren(iconSvg(playing ? ICON_PAUSE : ICON_PLAY));
  });
  return btn;
}

// 3.1: assistant (Диспетчер) и text (Кора) — разные роли в чате; user остаётся как был.
function addEntry(e) {
  const li = el("li", "feed-" + (e.kind || "misc"));
  if (e.kind === "user" || e.kind === "assistant" || e.kind === "text") {
    const role = e.kind === "user" ? "user" : e.kind === "assistant" ? "disp" : "kora";
    li.classList.add("msg", role === "user" ? "msg-user" : role === "disp" ? "msg-disp" : "msg-kora");
    if (role === "user") {
      const body = el("div", "msg-body");
      body.appendChild(el("p", "msg-text", e.text || ""));
      li.appendChild(body);
    } else {
      // канон макета: аватар-иконка + имя роли над пузырём + play под ним
      const av = el("span", "msg-avatar");
      av.appendChild(strokeIcon(role === "disp" ? AV_DISP : AV_KORA));
      li.appendChild(av);
      const col = el("div", "msg-col");
      col.appendChild(el("span", "msg-who", role === "disp" ? "Диспетчер" : "Кора"));
      const body = el("div", "msg-body");
      body.appendChild(el("p", "msg-text", e.text || ""));
      col.appendChild(body);
      col.appendChild(playButton(role));
      li.appendChild(col);
    }
  } else if (e.kind === "thinking") {
    // PF8: thinking остаётся светлым collapsible, отдельно от тёмной tool-карточки.
    const det = document.createElement("details");
    det.className = "think-card";
    det.appendChild(el("summary", "", "🧠 размышления"));
    if (e.text) det.appendChild(el("pre", "", e.text));
    li.appendChild(det);
  } else if (e.kind === "tool_use" || e.kind === "tool_result") {
    const det = document.createElement("details");
    det.className = "tool-card";
    det.appendChild(el("summary", "", e.kind === "tool_use" ? "🔧 инструмент" : "· результат инструмента"));
    if (e.text) det.appendChild(el("pre", "", e.text));
    li.appendChild(det);
  } else if (e.kind === "result") {
    li.textContent = "🏁 " + (e.text || "");
    if (/fail|ошибк|прерв|отмен/i.test(e.text || "")) li.classList.add("bad");
  } else if (e.kind === "task") {
    li.textContent = "▶ " + (e.text || "");
  } else if (e.kind === "gate_card") {
    renderGateCard(li, e);
  } else if (e.kind === "event" || e.kind === "clear") {
    // gate v2 C4': kind "clear" — event-стиль строка («история очищена»); запись несёт
    // id-штамп с сервера, так что feedKey не схлопывает повторные clear.
    li.textContent = "• " + (e.text || "");
  } else {
    li.textContent = (KIND_ICONS[e.kind] || "·") + " " + (e.text || "");
  }
  $("feed-list").appendChild(li);
}

function gateButton(label, action, opts = {}) {
  const button = el("button", "gate-action", label);
  button.type = "button";
  return { button, action, opts };
}

function renderGateCard(li, entry) {
  const current = route();
  const thread = current.view === "thread" ? threads.find((t) => t.id === current.id) : null;
  const stage = entry.stage || "";
  const live = !!thread && thread.stage === stage;
  li.classList.add("gate-card");
  li.appendChild(el("p", "gate-title", stage === "propose" ? "Запрос готов" :
    stage === "spec_plan" ? "План готов" : stage === "code" ? "Правки или запуск" : "Запуск Коры"));
  if (entry.action === "run_started") {
    li.appendChild(el("p", "gate-note", "Кора запущена" + (entry.model ? " · " + entry.model : "")));
    return;
  }
  const select = el("select", "gate-model");
  select.setAttribute("aria-label", "Модель Коры");
  const preferred = entry.model || (thread && thread.last_model) || "";
  const automatic = el("option", "", "модель по умолчанию");
  automatic.value = "";
  select.appendChild(automatic);
  KORA_MODELS.forEach((model) => {
    const option = el("option", "", model);
    option.value = model;
    option.selected = model === preferred;
    select.appendChild(option);
  });
  li.appendChild(select);

  const actions = el("div", "gate-actions");
  const buttons = [];
  if (stage === "propose") {
    buttons.push(gateButton("Отправить Коре", "send_to_kora", { confirm: true }));
    buttons.push(gateButton("Сразу писать код", "send_to_kora", { fast: true, dangerous: true }));
    buttons.push(gateButton("Правки", "revise"));
  } else if (stage === "spec_plan") {
    buttons.push(gateButton("Пиши код", "write_code", { dangerous: true }));
    buttons.push(gateButton("Правки", "revise"));
  } else if (stage === "code") {
    buttons.push(gateButton("Правки", "revise"));
  }
  const note = el("p", "gate-note");
  // B52/B53: consumed — карточка потрачена ТОЛЬКО на успехе (сервер выпустит свежий gate_card
  // на следующую стадию); любой другой исход (409/ошибка/сеть) ре-энейблит кнопки для ретрая.
  let consumed = false;
  buttons.forEach(({ button, action, opts }) => {
    button.disabled = !live;
    button.addEventListener("click", async () => {
      if (!live || button.disabled) return;
      if (opts.dangerous && !button.dataset.confirmed) {
        button.dataset.confirmed = "true";
        button.textContent = "точно пишем код?";
        note.textContent = "Второй тап запустит запись кода в проект.";
        return;
      }
      const payload = { action, model: select.value || null, confirm: !!opts.confirm || !!opts.dangerous };
      if (opts.fast) payload.fast = true;
      actions.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      note.textContent = "запускаю…";
      try {
        const response = await postJSON(`/api/threads/${encodeURIComponent(current.id)}/gate`, payload);
        if (response.status === 409) {
          note.textContent = "Кора занята — попробуй ещё раз"; // B52: ретрай разрешён
          return;
        }
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          note.textContent = "⛔ " + (body.error || "не удалось запустить");
          return;
        }
        // B53: успех — кнопки ОСТАЮТСЯ disabled (защита от дубля), note подтверждает.
        consumed = true;
        note.textContent = "готово ✓ — стадия обновлена";
        await Promise.all([loadLists(), pollFeed()]);
      } catch {
        note.textContent = "⛔ сеть недоступна";
      } finally {
        if (!consumed) {
          actions.querySelectorAll("button").forEach((b) => { b.disabled = false; });
        }
      }
    });
    actions.appendChild(button);
  });
  if (!live) note.textContent = "стадия изменилась — карточка больше не активна";
  li.appendChild(actions);
  li.appendChild(note);
}

function nearBottom() {
  const v = $("view-thread");
  return v.scrollHeight - v.scrollTop - v.clientHeight < 120;
}

async function pollFeed() {
  const r = route();
  if (r.view !== "thread") return;
  if (feedNotFound === r.id) return; // B55: фид уже ответил 404 — не долбим мёртвый тред
  if (feedInFlight) return; // B-CORE-7: три источника зовут pollFeed — не даём им гоняться
  feedInFlight = true;
  try {
    let data;
    try { data = await getJSON(`/api/threads/${encodeURIComponent(r.id)}/feed?limit=500`); }
    catch (err) {
      // B55: 404 = треда нет (мусорный роут / архивирован под ногами, B54) — глушим поллинг
      // этого id и показываем явную event-строку вместо вечно пустой страницы. Сетевые/5xx
      // ошибки по-прежнему тихий ретрай следующим тиком.
      if (err && err.status === 404 && route().id === r.id && feedThread === r.id) {
        feedNotFound = r.id;
        addEntry({ kind: "event", text: "тред не найден или удалён" });
      }
      return;
    }
    if (route().id !== r.id || feedThread !== r.id) return; // роут сменился, пока ждали
    // B-CORE-3: рендерим только НЕвиденные записи по подписи — окно «последние 500» может
    // сдвинуться, но каждая запись отрисуется ровно раз; после 500 лента больше не мертва.
    const first = renderedKeys.size === 0;
    const stick = first || nearBottom();
    let added = false;
    for (const e of data.entries) {
      const k = feedKey(e);
      if (renderedKeys.has(k)) continue;
      renderedKeys.add(k);
      addEntry(e);
      added = true;
    }
    if (added && stick) $("feed-list").lastElementChild.scrollIntoView({ block: "end" });
  } finally {
    feedInFlight = false;
  }
}

// ---------- статус Коры: карточка в сайдбаре (цвет готовым с сервера) ----------
function setKora(color, sub, threadId = null) {
  $("kora-card-dot").style.background = color;
  $("kora-card-sub").textContent = sub;
  const card = $("kora-card");
  const activeThread = typeof threadId === "string" && threadId;
  card.href = activeThread ? "#/thread/" + encodeURIComponent(threadId) : "#/activity";
  card.title = activeThread ? "Открыть активный тред" : "Открыть активность Коры";
}
async function pollStatus() {
  let data;
  try { data = await getJSON("./kora-status"); }
  catch { setKora("#888", "нет связи"); return; }
  const context = data.thread_id
    ? (data.thread_title || "тред") + (data.thread_stage && STAGES[data.thread_stage]
      ? " · " + STAGES[data.thread_stage] : "")
    : (data.task_text || "");
  // task_text живёт и ПОСЛЕ завершения задачи — «работает» только при running.
  const sub = data.awaiting_answer ? "ждёт ответа в " + context
    : data.task_status === "running" ? "работает в " + context : "свободна";
  setKora(COLORS[data.color] || "#888", sub, data.thread_id || null);
}

async function pollActivity() {
  if (route().view !== "activity") return;
  let data;
  try { data = await getJSON("./kora-log"); }
  catch { return; }
  if (route().view !== "activity") return;
  const list = $("activity-list");
  list.replaceChildren();
  data.entries.forEach((entry) => {
    const item = el("li", "activity-entry");
    item.textContent = (entry.kind || entry.type || "событие") + ": " + (entry.text || entry.detail || "");
    list.appendChild(item);
  });
  if (!data.entries.length) list.appendChild(el("li", "activity-entry", "пока нет событий"));
}

// ---------- композер: текст ----------
function setConn(text) {
  $("conn-status").textContent = text;
  $("conn-status").hidden = !text;
}

function resizeMessageInput() {
  const input = $("msg-input");
  input.style.height = "auto";
  const maxHeight = 148;
  input.style.height = Math.min(input.scrollHeight, maxHeight) + "px";
  input.style.overflowY = input.scrollHeight > maxHeight ? "auto" : "hidden";
}

async function sendMessage() {
  const input = $("msg-input");
  const text = input.value.trim();
  if (!text) return;
  const r = route();
  $("msg-send").disabled = true;
  // B-CORE-1: поле НЕ чистим заранее — иначе отказ сети/ошибка сервера бесследно съедают
  // набранный текст. Очистим только после подтверждённой отправки POST /message.
  try {
    let id = r.view === "thread" ? r.id : null;
    if (!id) {
      // Дом: первое сообщение создаёт тред-ветку активного проекта (Ж1 + иерархия)
      const tRes = await postJSON("/api/threads",
                                  { title: text.slice(0, 60), project_id: activeProject });
      if (!tRes.ok) { setConn("не удалось создать тред"); return; } // текст остаётся в поле
      id = (await tRes.json()).id;
      location.hash = "#/thread/" + encodeURIComponent(id); // render() переключит вью без reload
    }
    $("typing").hidden = false;
    const res = await postJSON(`/api/threads/${encodeURIComponent(id)}/message`, { text });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      setConn("⛔ " + (d.error || "ошибка " + res.status));
      return; // текст остаётся — можно переотправить, не перепечатывая
    }
    setConn("");
    input.value = ""; // успех подтверждён — теперь чистим
    resizeMessageInput();
    await Promise.all([pollFeed(), loadLists()]);
  } catch {
    setConn("сеть недоступна"); // текст остаётся в поле
  } finally {
    $("typing").hidden = true;
    $("msg-send").disabled = false;
  }
}
$("msg-send").addEventListener("click", sendMessage);
$("msg-input").addEventListener("input", resizeMessageInput);
// B-CORE-15: не отправляем во время IME-композиции (китайский/японский/эмодзи-клавиатура iOS —
// Enter там подтверждает набор, а не сообщение).
$("msg-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
    // B-UX-2: Enter не обходит guard кнопки — sendMessage синхронно ставит msg-send.disabled=true
    // в самом начале, так что этот чек фенсит окно и не даёт двойной отправки.
    if ($("msg-send").disabled) return;
    e.preventDefault();
    sendMessage();
  }
});

// UI-5 (S30): rename треда по тапу на заголовке (#view-title, НЕ #thread-badge — тот скрыт
// до конца рана). Inline-редактор (input), НЕ window.prompt — проектная дисциплина его выкинула.
// textContent/appendChild only; hash-router и голос не трогаются.
let renaming = false;
async function commitRename(input, titleEl, oldTitle, cur) {
  if (renaming) return;
  renaming = true;
  // B45: ПЕРВЫМ делом возвращаем ОРИГИНАЛЬНЫЙ узел с id в DOM — до любой строки, которая
  // может кинуть. Раньше $("view-title") давал null (узел снят replaceWith) → TypeError до
  // try, renaming навсегда true, каждый последующий render()/loadLists() падал на том же null.
  input.replaceWith(titleEl);
  const trimmed = input.value.trim();
  titleEl.textContent = trimmed || oldTitle;
  try {
    if (trimmed && trimmed !== oldTitle) {
      const res = await patchJSON(`/api/threads/${encodeURIComponent(cur.id)}`, { title: trimmed });
      if (res.ok) {
        cur.title = trimmed.slice(0, 80);
        titleEl.textContent = cur.title;
        await loadLists();
      } else {
        titleEl.textContent = oldTitle;
        setConn("не переименовать");
      }
    }
  } catch {
    titleEl.textContent = oldTitle;
    setConn("сеть недоступна");
  } finally {
    renaming = false;
  }
}
function renameCurrentThread() {
  if (renaming) return;
  const r = route();
  if (r.view !== "thread") return;
  const cur = threads.find((t) => t.id === r.id);
  if (!cur) return;
  const titleEl = $("view-title");
  const input = el("input");
  input.value = cur.title;
  input.maxLength = 80;
  input.className = "rename-input";
  titleEl.replaceWith(input);
  input.focus();
  input.select();
  // B45: узел с id передаётся в commitRename ЗАХВАЧЕННЫМ — lookup по $("view-title")
  // после replaceWith вернул бы null.
  const restore = () => commitRename(input, titleEl, cur.title, cur);
  input.addEventListener("blur", restore, { once: true });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    else if (e.key === "Escape") { input.value = cur.title; input.blur(); }
  });
}
$("view-title").addEventListener("click", renameCurrentThread);

// ---------- голос: vendored SDK → session-less POST /api/offer, видимые стейты ----------
const botAudio = $("bot-audio");
let client = null;
let connecting = false;
// R1: overlay-видимость — производная от setMicState, не отдельный источник правды.
// Клик мика лишь СТАВИТ liveRequested; сама видимость решается в syncLiveOverlay().
let liveRequested = false;
let liveMuteFn = null; // R5: живёт внутри connectVoice-замыкания, guard client===me

function setLiveStatus(text, speaking) {
  $("live-status").textContent = text;
  $("live-overlay").classList.toggle("speaking", !!speaking);
}

// PF5+R4: overlay встроен в модальный стек — его открытость учитывается в syncScrollLock().
function syncLiveOverlay(state) {
  const show = state === "on" && liveRequested;
  $("live-overlay").hidden = !show;
  $("shell").classList.toggle("live-open", show);
  if (show) setLiveStatus("Диспетчер слушает…", false);
  syncScrollLock();
}

function setMicState(state, msg) {
  $("mic-btn").dataset.state = state;
  if (state === "error") setConn("⛔ " + msg);
  else setConn(state === "connecting" ? "подключаю голос…"
    : state === "on" ? "🎙 говори — я слушаю" : "");
  // R1: idle/error всегда закрывают live — обрыв звонка больше не морозит «слушает…».
  if (state === "idle" || state === "error") liveRequested = false;
  syncLiveOverlay(state);
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
  // Identity-guard (урок слайса 3): колбэки действуют только пока `me` — текущий клиент,
  // иначе поздний onDisconnected СТАРОЙ сессии глушил бы новую после авто-реконнекта.
  const me = new PipecatClient({
    transport: new SmallWebRTCTransport({ webrtcUrl: "/api/offer" }),
    enableMic: true,
    callbacks: {
      onConnected: () => { if (client === me) setMicState("on"); },
      onDisconnected: () => { if (client === me) { client = null; setMicState("idle"); } },
      onTrackStarted: (track, participant) => {
        // SmallWebRTCTransport зовёт onTrackStarted(track) БЕЗ participant для
        // remote-треков (vendor: `onTrackStarted?.(n.track)`), local через этот
        // колбэк не ходит вовсе — «participant &&» молча выбрасывал аудио бота.
        if (client === me && track.kind === "audio" && !participant?.local) {
          botAudio.srcObject = new MediaStream([track]);
        }
      },
      // Live-overlay: «Диспетчер отвечает» + wave-бары, пока бот говорит.
      onBotStartedSpeaking: () => { if (client === me) setLiveStatus("Диспетчер отвечает", true); },
      onBotStoppedSpeaking: () => { if (client === me) setLiveStatus("Диспетчер слушает…", false); },
      onError: (e) => {
        // B-CORE-5: не только логируем — гасим кнопку и обнуляем client, иначе UI застревал
        // («слушаю» + «⛔ ошибка»), а следующий тап шёл в ветку «отключить» мёртвого клиента.
        console.error("voice error:", e);
        if (client === me) {
          client = null;
          setMicState("error", "соединение прервано");
          abandonVoice(me);
        }
      },
    },
  });
  // R5: mute-функция создаётся ВНУТРИ этого замыкания и гоняется только со СВОЕЙ сессией —
  // module-level `client` мог уехать не туда после тихого авто-реконнекта вотчдога.
  liveMuteFn = (on) => {
    if (client !== me) return;
    try {
      const result = typeof me.enableMic === "function" ? me.enableMic(on) : null;
      if (result && typeof result.catch === "function") result.catch(() => {});
    } catch { /* vendored SDK без enableMic — визуальный toggle остаётся, парк P7 */ }
  };
  client = me;
  await withTimeout(me.connect(), 20000);
}

// Privacy-инвариант: любой путь, роняющий client по ошибке (onError / провал реконнекта),
// ОБЯЗАН погасить транспорт — иначе pipecat держит peer-connection + локальный мик-трек
// живыми, а handle/кнопки/вотчдог уже мертвы (zombie-мик, лечится только reload).
// c.disconnect()→stop()→mediaManager.disconnect() отпускает getUserMedia; закрытие pc
// сам трек НЕ глушит.
function abandonVoice(c) {
  try { const r = c && c.disconnect(); if (r && r.catch) r.catch(() => {}); }
  catch { /* уже мёртв */ }
}

// Общий hang-up путь: клик по мику (когда уже подключены), «Завершить — в чат», Escape.
async function disconnectVoice() {
  if (!client) return;
  const c = client;
  client = null;
  setMicState("idle");
  await c.disconnect().catch(() => {});
  // gate v2 B2': «Завершить — В ЧАТ» ведёт в чат звонка. Тред создаёт сервер (клиент его id
  // не знал) — читаем из session-alive (B1': отдельный GET не плодим). Лента звонка уже на
  // диске (D1'/D3'), навигация открывает полную историю. Сеть упала → остаёмся на месте.
  try {
    const data = await getJSON("./session-alive");
    const tid = data && data.voice_thread;
    if (tid && route().id !== tid) {
      location.hash = "#/thread/" + encodeURIComponent(tid); // render() переключит вью
    }
    await Promise.all([loadLists(), pollFeed()]);
  } catch { /* нет связи — навигация в тред не критична */ }
}

$("mic-btn").addEventListener("click", async () => {
  if (connecting) return;
  // R2: dispOn=false блокирует только СТАРТ звонка; disabled-атрибут не ставится никогда,
  // поэтому кнопка остаётся единственным hang-up, пока client!=null.
  if (!client && !dispOn) return;
  if (client) { await disconnectVoice(); return; }
  liveRequested = true;
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

let liveMuted = false;
$("live-mute").addEventListener("click", () => {
  liveMuted = !liveMuted;
  if (liveMuteFn) liveMuteFn(!liveMuted);
  $("live-mute").textContent = liveMuted ? "Включить микрофон" : "Заглушить";
  $("live-mute").setAttribute("aria-pressed", String(liveMuted));
  $("live-mute").classList.toggle("muted", liveMuted);
});
$("live-end").addEventListener("click", disconnectVoice);

// ---------- вотчдог (§2.7, наследник reconnect.js): правда сервера, не wall-clock ----------
// iOS замораживает таймеры страницы при локе — elapsed-time эвристика ложно видит «разрыв»
// на каждом пробуждении, поэтому только поллинг /client/session-alive. В отличие от prebuilt
// (умирал навсегда после 3 ретраев → reload был единственным спасением) наш клиент умеет
// реконнект на месте; location.reload остаётся ПОСЛЕДНИМ резервом по прежним правилам:
// сервер доступен И говорит «сессии нет» И голос был жив.
let aliveMisses = 0;

function maybeReload() {
  const last = Number(sessionStorage.getItem("synapse-last-reload") || 0);
  if (Date.now() - last < 10000) return; // анти-луп: не чаще раза в 10с
  // B-CORE-10: reload пересоздаёт сессию и стирает набранное — сохраняем черновик input,
  // восстановим на следующем старте.
  sessionStorage.setItem("synapse-draft", $("msg-input").value);
  sessionStorage.setItem("synapse-last-reload", String(Date.now()));
  location.reload();
}

async function probeSession() {
  if (!client || connecting) return; // вотчдог сторожит только живой голос
  let data;
  try { data = await getJSON("./session-alive"); }
  catch { return; } // сеть упала — неизвестность, реконнект ничего не починит
  if (data.active) { aliveMisses = 0; return; }
  // B-CORE-10: 3 промаха (~15с стабильного «нет сессии») вместо 2 — /session-alive может
  // кратко лгать, а лишний реконнект дороже задержки.
  if (++aliveMisses < 3) return;
  aliveMisses = 0;
  // зомби: клиент думает «on», сервер сессию не держит → тихий реконнект на месте.
  // B-UX-1: connecting=true ПЕРВЫМ — до обнуления client и await-разрыва — иначе mic-btn
  // (if (connecting) return) не фенсит окно, и тап по кнопке гоняется с авто-реконнектом.
  connecting = true;
  const c = client;
  client = null;
  setMicState("connecting");
  await c.disconnect().catch(() => {});
  setConn("связь потеряна — переподключаю…");
  try {
    await connectVoice();
  } catch (err) {
    console.error("auto-reconnect failed:", err);
    const dead = client; client = null;
    abandonVoice(dead);  // реконнект мог поднять мик — не осиротить его
    setMicState("error", "связь потеряна — тапни микрофон");
    maybeReload();
  } finally {
    connecting = false;
  }
}
setInterval(probeSession, 5000);

// ---------- drawer (мобайл) + модалки: Escape + scroll-lock (B-CORE-6) ----------
function syncScrollLock() {
  // фон не скроллится, пока открыт drawer, пикер или live-overlay (PF5+R4: тот же модальный
  // стек, iOS scroll-chaining за модалкой)
  const anyModal = !picker.hidden || $("shell").classList.contains("drawer-open") ||
    !$("live-overlay").hidden;
  document.body.style.overflow = anyModal ? "hidden" : "";
}
// B-UX-10: сайдбар — off-canvas на мобайле, но ВСЕГДА виден на десктопе (>768px). inert/aria-hidden
// обязаны быть медиа-осознанными: убираем сайдбар из tab-порядка ТОЛЬКО когда он реально спрятан
// (мобайл И drawer закрыт), иначе десктопный сайдбар стал бы inert (closeDrawer зовётся на каждый render).
const drawerMq = window.matchMedia("(max-width: 768px)");
function syncDrawerA11y() {
  const off = drawerMq.matches && !$("shell").classList.contains("drawer-open");
  $("sidebar").inert = off;
  $("sidebar").setAttribute("aria-hidden", off ? "true" : "false");
}
function openDrawer() {
  $("shell").classList.add("drawer-open"); $("backdrop").hidden = false; syncScrollLock();
  syncDrawerA11y();
  requestAnimationFrame(() => $("side-close").focus()); // фокус внутрь открытого drawer
}
function closeDrawer() {
  $("shell").classList.remove("drawer-open"); $("backdrop").hidden = true; syncScrollLock();
  syncDrawerA11y();
}
drawerMq.addEventListener("change", syncDrawerA11y);
$("menu-btn").addEventListener("click", openDrawer);
$("side-close").addEventListener("click", closeDrawer);
$("backdrop").addEventListener("click", closeDrawer);
$("new-thread").addEventListener("click", () => {
  location.hash = "#/";
  closeDrawer();
  // B-CORE-16: rAF после закрытия drawer — синхронный .focus() на iOS Safari не поднимает клавиатуру
  requestAnimationFrame(() => $("msg-input").focus());
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return; // Escape закрывает верхнюю открытую модалку (B-CORE-6)
  // PF5+R4: live-overlay — верх модального стека; Escape = «Завершить — в чат».
  if (!$("live-overlay").hidden) { disconnectVoice(); return; }
  if (!picker.hidden) { closePicker(); return; }
  if ($("shell").classList.contains("drawer-open")) closeDrawer();
});

// ---------- пикер папки для «+ проект» (GET /api/browse — сервер листает сам) ----------
const picker = $("picker");
const pickerPath = $("picker-path");
const pickerDirs = $("picker-dirs");
const pickerError = $("picker-error");
let pickerCur = null;
let latestBrowse = 0; // B-CORE-12: игнорируем ответ не от последнего browse (быстрые тапы папок)
let pickerPrevFocus = null; // B-UX-8: куда вернуть фокус после закрытия модалки-пикера

function openPicker() {
  // B-UX-8: aria-modal-диалог обязан переместить фокус внутрь себя и вернуть его при закрытии.
  pickerPrevFocus = document.activeElement;
  picker.hidden = false; pickerError.textContent = ""; syncScrollLock(); browse(null);
  $("picker-choose").focus();
}
function closePicker() {
  picker.hidden = true; syncScrollLock();
  if (pickerPrevFocus && pickerPrevFocus.focus) pickerPrevFocus.focus();
  pickerPrevFocus = null;
}

async function browse(path) {
  const my = ++latestBrowse;
  let data;
  try {
    const url = "/api/browse" + (path ? "?path=" + encodeURIComponent(path) : "");
    data = await getJSON(url);
  } catch { return; }
  if (my !== latestBrowse) return; // устаревший ответ — новый browse уже в пути, не перерисовываем
  pickerCur = data.path;
  pickerPath.textContent = data.path;
  pickerError.textContent = "";
  pickerDirs.replaceChildren();
  // B-UX-7: строки пикера доступны с клавиатуры — tab-фокус + role=button + Enter/Space,
  // не только мышиный click (иначе <li> недостижимы клавиатурой и скринридером).
  const pickerRow = (label, go) => {
    const li = el("li", "", label);
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    li.addEventListener("click", go);
    li.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); }
    });
    return li;
  };
  if (data.parent) {
    pickerDirs.appendChild(pickerRow("‹ назад", () => browse(data.parent)));
  }
  data.dirs.forEach((name) => {
    pickerDirs.appendChild(pickerRow("📁 " + name, () => browse(data.path + "/" + name)));
  });
}

$("add-project").addEventListener("click", openPicker);
$("picker-cancel").addEventListener("click", closePicker);
$("picker-choose").addEventListener("click", async () => {
  if (!pickerCur) return;
  const res = await postJSON("/api/projects", { name: "", path: pickerCur }).catch(() => null);
  // B-CORE-4: сеть упала (res === null) — это НЕ успех, пикер не закрываем
  if (res === null) { pickerError.textContent = "⛔ нет связи"; return; }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    // B-CORE-11: причину — в отдельный #picker-error, путь в pickerPath не затираем
    pickerError.textContent = "⛔ " + (data.error || "не удалось добавить");
    return;
  }
  closePicker();
  loadLists();
});

// ---------- init ----------
// B-CORE-10: восстановить черновик, сохранённый перед вынужденным reload вотчдога
const draft = sessionStorage.getItem("synapse-draft");
if (draft) { $("msg-input").value = draft; sessionStorage.removeItem("synapse-draft"); }
resizeMessageInput();
applyDispToggleUI();
setTab("chat");
loadLists().then(render);
pollStatus();
setInterval(loadLists, 5000);
setInterval(pollFeed, 3000);
setInterval(pollStatus, 3000);
setInterval(pollActivity, 3000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { loadLists(); pollFeed(); pollStatus(); pollActivity(); probeSession(); }
});
window.addEventListener("online", () => probeSession());
