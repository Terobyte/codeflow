// M1 slice 5 (§2.7): reconnect watchdog for the /client PWA. NOT wall-clock-based (R3/R4
// dispositions in the slice-5 run file: iOS suspends page timers while locked/backgrounded, so
// any elapsed-time heuristic reads a false "outage" on every ordinary wake). Instead this polls
// server TRUTH via /client/session-alive.
//
// Why reload is the only recovery: the prebuilt transport self-heals short blips (~5s
// disconnect grace + 3 retries x 2s) but then calls stop() PERMANENTLY -- there is no
// documented hook to restart it, and a standalone PWA has no pull-to-refresh gesture, so a
// stuck page would otherwise be dead until the user force-quits it. A fetch failure is treated
// as UNKNOWN, never a reload trigger: if the network is down, reloading fixes nothing.
let armed = false;

function maybeReload() {
  const last = Number(sessionStorage.getItem("synapse-last-reload") || 0);
  const now = Date.now();
  if (now - last < 10000) return; // anti-loop: at most one reload per 10s
  sessionStorage.setItem("synapse-last-reload", String(now));
  location.reload();
}

async function probe() {
  let res;
  try {
    res = await fetch("./session-alive", { cache: "no-store" });
  } catch {
    return; // network down -- unknown, never reload
  }
  if (!res.ok) return; // unknown -- never reload
  const data = await res.json();
  if (data.active) {
    armed = true;
    return;
  }
  if (armed) maybeReload();
}

setInterval(probe, 5000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) probe();
});
window.addEventListener("online", probe);
