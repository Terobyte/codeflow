// UI v2 слайс UI-3: тред-вью. Лента = персист-файл треда (poll), текстовый ход =
// POST message тем же диспетчером. Только textContent (XSS: текст ленты произволен).
const params = new URLSearchParams(location.search);
const threadId = params.get("id");
const feedList = document.getElementById("feed-list");
const input = document.getElementById("msg-input");
const send = document.getElementById("msg-send");
const title = document.getElementById("thread-title");
const dot = document.getElementById("kora-dot");
const COLORS = { green: "#2ecc71", yellow: "#f1c40f", red: "#e74c3c" };
const KIND_ICONS = { task: "▶", text: "💬", thinking: "🧠", tool_use: "🔧",
                     tool_result: "·", result: "🏁", system: "⚙", user: "🗣", assistant: "🤖" };
let renderedCount = 0;

async function post(url, body) {
  return fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

function addEntry(e) {
  const li = document.createElement("li");
  li.textContent = (KIND_ICONS[e.kind] || "·") + " " + (e.text || "");
  if (e.kind === "thinking") {  // сворачиваемые рассуждения: строка, тап разворачивает
    const full = li.textContent;
    li.textContent = "🧠 Кора размышляет… (тап)";
    li.addEventListener("click", () => { li.textContent = full; }, { once: true });
  }
  feedList.appendChild(li);
}

async function pollFeed() {
  let res;
  try { res = await fetch(`/api/threads/${threadId}/feed?limit=500`, { cache: "no-store" }); }
  catch { return; }
  if (!res.ok) return;
  const data = await res.json();
  const fresh = data.entries.slice(renderedCount);
  fresh.forEach(addEntry);
  if (fresh.length) {
    renderedCount = data.entries.length;
    feedList.lastElementChild.scrollIntoView({ block: "end" });
  }
}

async function pollStatus() {
  let res;
  try { res = await fetch("./kora-status", { cache: "no-store" }); } catch { dot.style.background = "#888"; return; }
  if (!res.ok) { dot.style.background = "#888"; return; }
  const data = await res.json();
  dot.style.background = COLORS[data.color] || "#888";
}

send.addEventListener("click", async () => {
  const text = input.value.trim();
  if (!text) return;
  input.value = ""; send.disabled = true;
  try {
    const res = await post(`/api/threads/${threadId}/message`, { text });
    if (res.ok) await pollFeed();
  } finally {
    send.disabled = false;
  }
});
input.addEventListener("keydown", (e) => { if (e.key === "Enter") send.click(); });

title.textContent = "тред " + (threadId || "");
post("/api/active-thread", { id: threadId });  // голос теперь адресуется этому треду
pollFeed(); pollStatus();
setInterval(pollFeed, 3000); setInterval(pollStatus, 3000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) { pollFeed(); pollStatus(); } });
