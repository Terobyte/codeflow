(() => {
  // kora status UI (tero run 2026-07-12): светофор Коры поверх prebuilt UI. Одна fixed-точка
  // 14px с максимальным z-index; цвет приходит ГОТОВЫМ с сервера (/client/kora-status,
  // _status_color в webrtc_server.py) — здесь только отрисовка: никакой логики статуса и
  // никакого wall-clock (урок R4 слайса-5: только интервал полла, решений по времени нет).
  // Клик — навигация location.href на /client/logs (АБСОЛЮТНЫЙ путь: виджет инжектится в чужую
  // страницу, относительный ./logs ломался вне /client/ — B-UI-7), НЕ новая вкладка (R3: iOS
  // standalone PWA не умеет вкладки). Только style/textContent/title, никакого сырого HTML
  // (XSS: task_text произвольный). Весь код в IIFE — виджет self-contained, не течёт в window
  // хост-страницы (B-UI-5). Ошибка сети/парсинга = серый «неизвестно», решений не принимаем.
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
    location.href = "/client/logs";
  });
  document.body.appendChild(dot); // defer-скрипт: DOM уже распарсен, body есть

  async function poll() {
    // B-UI-6: fetch И res.json() под ОДНИМ try — 200 с битым/обрезанным телом (таймаут прокси
    // в tailnet) больше не роняет полл незамеченным SyntaxError'ом.
    try {
      const res = await fetch("./kora-status", { cache: "no-store" });
      if (!res.ok) {
        dot.style.background = "#888";
        return;
      }
      const data = await res.json();
      dot.style.background = COLORS[data.color] || "#888";
      dot.title =
        (data.task_text ? "Кора: " + data.task_text : "Кора: нет задачи") + " · " + data.liveness;
    } catch {
      dot.style.background = "#888"; // сеть/парсинг упали — неизвестно, не гадаем
    }
  }

  // B-UI-8: в фоне гасим интервал (iOS PWA на локе часами долбил бы сеть каждые 3с); на
  // возврате — немедленный полл + рестарт (идиома reconnect.js).
  let timer = setInterval(poll, 3000);
  poll();
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearInterval(timer);
      timer = null;
    } else if (!timer) {
      poll();
      timer = setInterval(poll, 3000);
    }
  });
})();
