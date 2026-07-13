// kora status UI (tero run 2026-07-12): светофор Коры поверх prebuilt UI. Одна fixed-точка
// 14px с максимальным z-index; цвет приходит ГОТОВЫМ с сервера (/client/kora-status,
// _status_color в webrtc_server.py) — здесь только отрисовка: никакой логики статуса и
// никакого wall-clock (урок R4 слайса-5: только интервал полла, решений по времени нет).
// Клик — навигация location.href на ./logs, НЕ новая вкладка (R3: standalone iOS PWA не
// умеет вкладки — попытка открыть новое окно уводит из PWA в Safari или молча не работает).
// Только style/textContent/title-присваивания, никакой вставки сырого HTML (XSS: task_text —
// произвольный текст задачи). Ошибка сети = серый «неизвестно», решений не принимаем.
const COLORS = { green: "#2ecc71", yellow: "#f1c40f", red: "#e74c3c" };

const dot = document.createElement("div");
dot.id = "synapse-kora-status";
dot.style.position = "fixed";
dot.style.top = "calc(env(safe-area-inset-top, 0px) + 10px)";
dot.style.right = "10px";
dot.style.width = "14px";
dot.style.height = "14px";
dot.style.borderRadius = "50%";
dot.style.background = "#888"; // серый = неизвестно (до первого ответа / сеть упала)
dot.style.zIndex = "2147483647";
dot.style.cursor = "pointer";
dot.style.boxShadow = "0 0 4px rgba(0,0,0,.6)";
dot.title = "Кора: статус неизвестен";
dot.addEventListener("click", () => {
  location.href = "./logs";
});
document.body.appendChild(dot); // defer-скрипт: DOM уже распарсен, body есть

async function poll() {
  let res;
  try {
    res = await fetch("./kora-status", { cache: "no-store" });
  } catch {
    dot.style.background = "#888"; // сеть упала — неизвестно, не гадаем
    return;
  }
  if (!res.ok) {
    dot.style.background = "#888";
    return;
  }
  const data = await res.json();
  dot.style.background = COLORS[data.color] || "#888";
  dot.title =
    (data.task_text ? "Кора: " + data.task_text : "Кора: нет задачи") + " · " + data.liveness;
}

poll();
setInterval(poll, 3000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) poll(); // немедленный полл на возврат в приложение (идиома reconnect.js)
});
