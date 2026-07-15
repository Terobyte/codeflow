// CodeFlow redesign prototype. Backend-free, but every control here maps 1:1 to a real
// function of the production client (synapse/pipeline/client/app.js). Demo state lives in
// the arrays below; production integration replaces them with /api/* data + polling.
(() => {
  const $ = (id) => document.getElementById(id);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;
    return n;
  };

  // ---------- demo data (production: GET /api/projects, /api/threads, feed, kora-status) ----
  const KORA_MODELS = ["claude-opus-4-8", "claude-sonnet-5", "claude-fable-5"];
  const STAGES = { collect: "Collect", propose: "Propose", spec_plan: "Plan", code: "Coding", done: "Done" };
  const STAGE_ORDER = ["collect", "propose", "spec_plan", "code", "done"];
  const OUTCOME = {
    failed: { icon: "✖", text: "✖ failed", bad: true },
    cancelled: { icon: "⏹", text: "⏹ cancelled", bad: false },
    completed: { icon: "✓", text: "✓ done", bad: false },
  };

  let projects = [
    { id: "p-luma", name: "Luma Mobile", path: "/Users/you/Projects/luma-mobile" },
    { id: "p-atlas", name: "Atlas API", path: "/Users/you/Projects/atlas-api" },
  ];
  let threads = [
    { id: "t-onboard", title: "Redesign onboarding flow", project_id: "p-luma", stage: "spec_plan", last_outcome: "completed", ago: "8 min" },
    { id: "t-audio", title: "Fix background audio session", project_id: "p-luma", stage: "code", last_outcome: null, ago: "32 min" },
    { id: "t-tokens", title: "Add theme tokens", project_id: null, stage: "done", last_outcome: "completed", ago: "1 d" },
  ];
  const feeds = {
    "t-onboard": [
      { kind: "user", text: "Redesign onboarding with fewer screens, a clearer voice-mode value proposition, and a calmer visual rhythm. Show me the plan first." },
      { kind: "assistant", text: "I’ve captured the request. One question: can we change the navigation, or should we preserve the existing four-step structure?" },
      { kind: "user", text: "Reduce it to three steps." },
      { kind: "gate_card", stage: "propose" },
      { kind: "task", text: "Plan: three-step onboarding focused on voice" },
      { kind: "thinking", text: "Preserving analytics identifiers across the reduced step count. The permission primer must render before the native microphone request." },
      { kind: "tool_use", text: "Read Sources/Onboarding/OnboardingView.swift" },
      { kind: "tool_result", text: "412 lines" },
      { kind: "text", text: "Plan is ready: 1) map the current onboarding state, 2) build the three-screen flow, 3) add the permission primer, 4) run tests and review the diff. Written to docs/plans/t-onboard.md." },
      { kind: "result", text: "planning finished · 4 steps" },
      { kind: "file", file_id: "k7Qp0aZk3fUw", name: "onboarding-plan.md", title: "Onboarding plan", size: 18432, mime: "text/markdown" },
      { kind: "gate_card", stage: "spec_plan" },
    ],
    "t-audio": [
      { kind: "user", text: "Audio dies when the app goes to background during a live session." },
      { kind: "assistant", text: "Reproduced. Sending it to Code — the session category needs .playAndRecord with .mixWithOthers." },
      { kind: "gate_card", stage: "propose", action: "run_started", model: "claude-sonnet-5" },
      { kind: "task", text: "Fix background audio session" },
      { kind: "tool_use", text: "Edit Sources/Audio/SessionController.swift" },
    ],
    "t-tokens": [
      { kind: "user", text: "Add theme tokens for the marketing site." },
      { kind: "text", text: "Done — tokens.css with 24 custom properties, both themes verified." },
      { kind: "result", text: "completed · 2 files changed" },
    ],
  };
  const diffs = {
    "t-onboard": {
      files: [
        { status: "M", path: "Sources/Onboarding/OnboardingView.swift" },
        { status: "A", path: "Sources/Onboarding/PermissionPrimer.swift" },
        { status: "M", path: "Tests/OnboardingTests.swift" },
      ],
      diff: [
        "diff --git a/Sources/Onboarding/OnboardingView.swift b/Sources/Onboarding/OnboardingView.swift",
        "@@ -38,12 +38,18 @@ struct OnboardingView: View {",
        "   var body: some View {",
        "-    LegacyPageView(currentStep: $step)",
        "-      .pageCount(4)",
        "+    NightJourneyView(selection: $step) {",
        "+      WelcomeScene()",
        "+      VoicePrimerScene()",
        "+      ReadyScene()",
        "+    }",
        "       .onChange(of: step) { value in",
        "+        analytics.track(.onboardingStep(value))",
        "       }",
        "   }",
      ].join("\n"),
    },
  };
  const activityLog = [
    { kind: "task", text: "Fix background audio session" },
    { kind: "tool_use", text: "Read Sources/Audio/SessionController.swift" },
    { kind: "thinking", text: "The session deactivates on .background — needs the .mixWithOthers option." },
    { kind: "tool_use", text: "Edit Sources/Audio/SessionController.swift" },
    { kind: "tool_result", text: "ok" },
  ];
  // production: GET /client/kora-status (color computed server-side)
  const koraStatus = { color: "#7a8a5e", thread_id: "t-audio", running: true };

  // ---------- demo data: AI settings store (spec 2026-07-15 §4.7/§4.8; production: /api/settings/ai) ----
  const PROVIDERS = {
    openrouter: { name: "OpenRouter", env: "OPENROUTER_API_KEY", mask: "sk-or-…f4a2", test_ms: 820 },
    anthropic: { name: "Anthropic", env: "ANTHROPIC_API_KEY", mask: "sk-ant-…9c1d", test_ms: 640 },
    google: { name: "Google AI Studio", env: "GOOGLE_API_KEY", mask: null, test_ms: 1100 },
  };
  // production: GET /api/settings/ai/providers/<id>/models (6h fresh / 30d stale-if-error cache)
  const MODELS = {
    openrouter: [
      { id: "google/gemini-3.5-flash", tools: true },
      { id: "anthropic/claude-haiku-4-5", tools: true },
      { id: "meta-llama/llama-4-70b", tools: false },
    ],
    anthropic: [
      { id: "claude-haiku-4-5", tools: true },
      { id: "claude-sonnet-5", tools: true },
    ],
    google: [
      { id: "gemini-3.5-flash", tools: true },
      { id: "gemini-3.5-pro", tools: true },
    ],
  };
  let aiSettings = {
    schema_version: 1,
    revision: 4,
    providers: {
      openrouter: { enabled: true, selected_model: "google/gemini-3.5-flash" },
      anthropic: { enabled: true, selected_model: "claude-haiku-4-5" },
      google: { enabled: false, selected_model: null },
    },
    routing: {
      primary: { provider: "openrouter", model: "google/gemini-3.5-flash" },
      fallback: { provider: "anthropic", model: "claude-haiku-4-5" }, // null = no fallback
    },
    kora: { default_model: "claude-sonnet-5", max_turns: 40, max_budget_usd: 1.0, deadline_s: 900 },
  };
  let savedSettings = JSON.stringify(aiSettings); // last committed revision (production: server store)
  // v1: voices are env-backed and read-only; the editor is owned by Settings → Voice (M+1)
  const VOICES = [
    { role: "Dispatcher", env: "FISH_REFERENCE_ID", mask: "fd2a…91b3", note: "configured from env" },
    { role: "Kora", env: "FISH_VOICE_KORA", mask: "c5e8…04ba", note: "configured from env" },
    { role: "Narrator", env: "FISH_VOICE_NARRATOR", mask: null, note: "not set — falls back to the Dispatcher voice" },
  ];

  const JOURNAL_ICONS = { task: "▶", thinking: "✦", tool_use: "⌁", tool_result: "·", result: "✓", text: "💬" };
  let seq = 0;
  const uid = (p) => p + "-" + (++seq);

  // ---------- router (routes: #/ , #/thread/<id> , #/activity , #/settings/ai) ----------
  function route() {
    if (location.hash === "#/activity") return { view: "activity" };
    if (location.hash.startsWith("#/settings")) return { view: "settings" };
    const m = location.hash.match(/^#\/thread\/([^/?&#]+)/);
    if (m) return { view: "thread", id: decodeURIComponent(m[1]) };
    return { view: "home" };
  }

  // ---------- active project: home births threads into it ----------
  let activeProject = localStorage.getItem("codeflow-active-project") || null;
  function setActiveProject(pid) {
    activeProject = activeProject === pid ? null : pid; // second tap clears
    if (activeProject) localStorage.setItem("codeflow-active-project", activeProject);
    else localStorage.removeItem("codeflow-active-project");
    render();
  }
  function validateActiveProject() {
    if (activeProject && !projects.some((p) => p.id === activeProject)) activeProject = null;
  }

  // ---------- Flow · realtime toggle: gates voice START only, never hang-up ----------
  let dispOn = localStorage.getItem("codeflow-disp-on") !== "false";
  function applyDispToggleUI() {
    $("disp-toggle").setAttribute("aria-checked", String(dispOn));
    $("mic-btn").classList.toggle("disp-off", !dispOn);
    $("mic-btn").title = dispOn ? "Microphone" : "Flow is off — Code only, chat available";
  }
  $("disp-toggle").addEventListener("click", () => {
    dispOn = !dispOn;
    localStorage.setItem("codeflow-disp-on", String(dispOn));
    applyDispToggleUI();
  });

  // ---------- Chat / Diff tabs ----------
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
  $("tab-diff").addEventListener("click", () => { setTab("diff"); renderDiff(); });

  // ---------- render ----------
  const narrowMq = window.matchMedia("(max-width: 480px)");
  const taskPlaceholder = () => (narrowMq.matches ? "Task…" : "Say or type a task…");
  let feedThread = null;

  function render() {
    const r = route();
    closeDrawer();
    $("view-home").hidden = r.view !== "home";
    $("view-thread").hidden = !(r.view === "thread" && tab === "chat");
    $("view-diff").hidden = !(r.view === "thread" && tab === "diff");
    $("view-activity").hidden = r.view !== "activity";
    $("view-settings").hidden = r.view !== "settings";
    $("thread-tabs").hidden = r.view !== "thread";
    $("settings-link").classList.toggle("active", r.view === "settings");
    if (r.view === "thread") {
      const t = threads.find((x) => x.id === r.id);
      $("view-title").textContent = t ? t.title : "thread not found";
      $("msg-input").placeholder = "Message…";
      renderBadge(t);
      renderStageChip(t);
      if (feedThread !== r.id) {
        feedThread = r.id;
        setTab("chat"); // a thread always opens on Chat
      }
      renderThread(t);
    } else if (r.view === "activity") {
      $("view-title").textContent = "Code activity";
      $("msg-input").placeholder = taskPlaceholder();
      renderBadge(null); renderStageChip(null);
      feedThread = null;
      renderActivity();
    } else if (r.view === "settings") {
      $("view-title").textContent = "Settings";
      $("msg-input").placeholder = taskPlaceholder();
      renderBadge(null); renderStageChip(null);
      feedThread = null;
      renderSettings();
    } else {
      $("view-title").textContent = "CodeFlow";
      $("msg-input").placeholder = taskPlaceholder();
      renderBadge(null); renderStageChip(null);
      feedThread = null;
    }
    renderChip(r);
    renderSidebar();
    renderHome();
    renderKora();
  }
  window.addEventListener("hashchange", render);

  function renderBadge(t) {
    const b = $("thread-badge");
    const o = t && OUTCOME[t.last_outcome];
    if (!o) { b.hidden = true; return; }
    b.textContent = o.text;
    b.className = "thread-badge" + (o.bad ? " bad" : "");
    b.hidden = false;
  }
  function renderStageChip(t) {
    const chip = $("stage-chip");
    if (t && t.stage === "done" && OUTCOME[t.last_outcome]) { chip.hidden = true; return; }
    const label = t && STAGES[t.stage];
    chip.textContent = label || "";
    chip.hidden = !label;
    chip.className = "stage-chip" + (t && t.stage ? " stage-" + t.stage : "");
  }

  // ---------- sidebar: project → thread branches, loose threads below ----------
  function threadCard(t, cur, showProj) {
    const wrap = el("div", "tc-wrap");
    const a = el("a", "thread-card" + (cur.view === "thread" && cur.id === t.id ? " active" : ""));
    a.href = "#/thread/" + encodeURIComponent(t.id);
    const o = OUTCOME[t.last_outcome];
    const row = el("div", "tc-row");
    row.appendChild(el("span", "tc-dot" + (t.stage ? " stage-" + t.stage : "")));
    row.appendChild(el("span", "tc-title", (o ? o.icon + " " : "") + t.title));
    a.appendChild(row);
    const proj = showProj ? projects.find((p) => p.id === t.project_id) : null;
    const sub = el("div", "tc-sub");
    sub.appendChild(el("span", "tc-meta", t.ago + (proj ? " · " + proj.name : "")));
    if (STAGES[t.stage]) sub.appendChild(el("span", "tc-stage stage-" + t.stage, STAGES[t.stage]));
    a.appendChild(sub);
    wrap.appendChild(a);
    const ar = el("button", "tc-archive", "archive");
    ar.type = "button";
    ar.title = "Archive thread";
    ar.addEventListener("click", (ev) => { ev.preventDefault(); ev.stopPropagation(); archiveThread(t); });
    wrap.appendChild(ar);
    return wrap;
  }

  function archiveThread(t) {
    if (!window.confirm("Archive thread “" + t.title + "”?")) return;
    threads = threads.filter((x) => x.id !== t.id);
    if (route().view === "thread" && route().id === t.id) location.hash = "#/";
    setConn("thread “" + t.title + "” archived");
    render();
  }
  function deleteProject(p) {
    if (!window.confirm("Delete project “" + p.name + "”? Threads remain but lose their link.")) return;
    projects = projects.filter((x) => x.id !== p.id);
    if (activeProject === p.id) activeProject = null;
    render();
  }

  function renderSidebar() {
    const cur = route();
    const known = new Set(projects.map((p) => p.id));
    const tree = $("project-tree");
    tree.replaceChildren();
    projects.forEach((p) => {
      const li = el("li", "project");
      const head = el("div", "project-head");
      const row = el("button", "project-row" + (p.id === activeProject ? " active" : ""));
      row.type = "button";
      row.title = p.path;
      row.appendChild(el("span", "pr-name", "📁 " + p.name));
      row.addEventListener("click", () => setActiveProject(p.id));
      head.appendChild(row);
      const del = el("button", "pr-delete", "×");
      del.type = "button";
      del.title = "Delete project (threads remain)";
      del.addEventListener("click", (ev) => { ev.stopPropagation(); deleteProject(p); });
      head.appendChild(del);
      li.appendChild(head);
      const branch = el("ul", "thread-list branch");
      threads.filter((t) => t.project_id === p.id).forEach((t) => {
        const bi = el("li");
        bi.appendChild(threadCard(t, cur, false));
        branch.appendChild(bi);
      });
      li.appendChild(branch);
      tree.appendChild(li);
    });
    const loose = threads.filter((t) => !t.project_id || !known.has(t.project_id));
    const lul = $("loose-list");
    lul.replaceChildren();
    loose.forEach((t) => {
      const li = el("li");
      li.appendChild(threadCard(t, cur, false));
      lul.appendChild(li);
    });
    $("loose-h").hidden = loose.length === 0;
  }

  // ---------- composer project chip (home only): where a new thread is born ----------
  function renderChip(r) {
    const chip = $("proj-chip");
    if (r.view !== "home") { chip.hidden = true; return; }
    const proj = projects.find((p) => p.id === activeProject);
    chip.textContent = proj ? "📁 " + proj.name : "no project";
    chip.classList.toggle("has-proj", !!proj);
    chip.hidden = false;
  }
  $("proj-chip").addEventListener("click", openDrawer); // pick a project in the sidebar

  function renderHome() {
    const cur = route();
    const ul = $("home-recent");
    ul.replaceChildren();
    threads.slice(0, 6).forEach((t) => {
      const li = el("li");
      li.appendChild(threadCard(t, cur, true));
      ul.appendChild(li);
    });
    $("recent-h").hidden = threads.length === 0;
  }

  // ---------- thread view: header (rename), display-only stage rail, feed ----------
  function renderThread(t) {
    const wrap = $("view-thread");
    if (!t) {
      $("thread-title").textContent = "thread not found";
      $("thread-folder").textContent = "";
      $("thread-ago").textContent = "";
      $("stage-rail").replaceChildren();
      $("feed-list").replaceChildren(el("li", "feed-event", "• thread not found or deleted"));
      $("radio-bar").hidden = true;
      return;
    }
    $("thread-title").textContent = t.title;
    const proj = projects.find((p) => p.id === t.project_id);
    $("thread-folder").textContent = proj ? "📁 " + proj.name : "no project";
    $("thread-ago").textContent = t.ago === "just now" ? "updated just now" : "updated " + t.ago + " ago";
    renderStageRail(t);
    renderFeed(t);
    renderRadio();
    wrap.scrollTop = wrap.scrollHeight;
  }

  // Stages are a server-side FSM in production — the rail only displays them; transitions
  // happen exclusively through gate cards.
  function renderStageRail(t) {
    const rail = $("stage-rail");
    rail.replaceChildren();
    const idx = STAGE_ORDER.indexOf(t.stage);
    STAGE_ORDER.forEach((sid, i) => {
      const s = el("span", "stage" + (i < idx ? " done" : i === idx ? " active" : ""));
      const icon = el("i", "", i < idx ? "✓" : i === idx ? "✦" : String(i + 1));
      s.appendChild(icon);
      s.appendChild(el("b", "", STAGES[sid]));
      rail.appendChild(s);
      if (i < STAGE_ORDER.length - 1) rail.appendChild(el("span", "rail-line" + (i === idx - 1 ? " active-line" : "")));
    });
  }

  function renderFeed(t) {
    const list = $("feed-list");
    list.replaceChildren();
    (feeds[t.id] || []).forEach((e) => list.appendChild(feedEntry(e, t)));
  }

  // demo stand-in for the real TTS play button (production: POST /api/tts → WAV)
  function playButton(role) {
    const btn = el("button", "play-btn", "▶");
    btn.type = "button";
    btn.title = "TTS · " + (role === "disp" ? "Flow" : "Code") + " voice";
    btn.addEventListener("click", () => {
      const playing = btn.classList.toggle("playing");
      btn.textContent = playing ? "❚❚" : "▶";
      if (playing) {
        pauseRadio(); // nowPlaying is a singleton: starting one audio stops the other
        setTimeout(() => { btn.classList.remove("playing"); btn.textContent = "▶"; }, 2500);
      }
    });
    return btn;
  }

  function feedEntry(e, t) {
    const li = el("li", "feed-" + (e.kind || "misc"));
    if (e.kind === "user" || e.kind === "assistant" || e.kind === "text") {
      const role = e.kind === "user" ? "user" : e.kind === "assistant" ? "disp" : "kora";
      li.classList.add("msg", "msg-" + role);
      if (role === "user") {
        li.appendChild(el("p", "msg-text", e.text || ""));
      } else {
        const av = el("span", "msg-avatar" + (role === "kora" ? " avatar-kora" : ""), role === "disp" ? "⌣" : "⌗");
        li.appendChild(av);
        const col = el("div", "msg-col");
        col.appendChild(el("span", "msg-who", role === "disp" ? "Flow" : "Code"));
        col.appendChild(el("p", "msg-text", e.text || ""));
        if (e.text) col.appendChild(playButton(role));
        li.appendChild(col);
      }
    } else if (e.kind === "thinking" || e.kind === "tool_use" || e.kind === "tool_result") {
      const det = document.createElement("details");
      det.className = e.kind === "thinking" ? "think-card" : "tool-card";
      det.appendChild(el("summary", "",
        e.kind === "thinking" ? "🧠 thinking" : e.kind === "tool_use" ? "🔧 tool" : "· tool result"));
      if (e.text) det.appendChild(el("pre", "", e.text));
      li.appendChild(det);
    } else if (e.kind === "result") {
      li.textContent = "🏁 " + (e.text || "");
      if (/fail|error|cancel/i.test(e.text || "")) li.classList.add("bad");
    } else if (e.kind === "task") {
      li.textContent = "▶ " + (e.text || "");
    } else if (e.kind === "file") {
      renderFileCard(li, e, t);
    } else if (e.kind === "gate_card") {
      renderGateCard(li, e, t);
    } else {
      li.textContent = "• " + (e.text || "");
    }
    return li;
  }

  // Gate card — the only way a task moves between stages. Buttons are enabled only while
  // the thread is still ON this card's stage; dangerous actions need a second tap.
  function renderGateCard(li, entry, t) {
    const stage = entry.stage || "";
    const live = !!t && t.stage === stage;
    li.classList.add("gate-card");
    li.appendChild(el("p", "gate-title", stage === "propose" ? "Request ready" :
      stage === "spec_plan" ? "Plan ready" : stage === "code" ? "Revise or run" : "Run Code"));
    if (entry.action === "run_started") {
      li.appendChild(el("p", "gate-note", "Code started" + (entry.model ? " · " + entry.model : "")));
      return;
    }
    const select = el("select", "gate-model");
    select.setAttribute("aria-label", "Code model");
    const auto = el("option", "", "default model");
    auto.value = "";
    select.appendChild(auto);
    KORA_MODELS.forEach((m) => {
      const o = el("option", "", m);
      o.value = m;
      select.appendChild(o);
    });
    li.appendChild(select);

    const actions = el("div", "gate-actions");
    const note = el("p", "gate-note");
    const defs = stage === "propose"
      ? [["Send to Code", false], ["Write code now", true], ["Revise", false]]
      : stage === "spec_plan" ? [["Write code", true], ["Revise", false]]
      : [["Revise", false]];
    defs.forEach(([label, dangerous]) => {
      const b = el("button", "gate-action", label);
      b.type = "button";
      b.disabled = !live;
      b.addEventListener("click", () => {
        if (b.disabled) return;
        if (dangerous && !b.dataset.confirmed) {
          b.dataset.confirmed = "true";
          b.textContent = "really write code?";
          note.textContent = "A second tap starts writing code into the project.";
          return;
        }
        actions.querySelectorAll("button").forEach((x) => { x.disabled = true; });
        if (label === "Revise") {
          note.textContent = "sent for revision";
          return;
        }
        // demo of the real transition: stage advances, a run_started card + task land in the feed
        note.textContent = "done ✓ — stage updated";
        t.stage = stage === "propose" && label !== "Send to Code" ? "code"
          : stage === "propose" ? "spec_plan" : "code";
        feeds[t.id].push({ kind: "gate_card", stage, action: "run_started", model: select.value || "default model" });
        feeds[t.id].push({ kind: "task", text: t.title });
        render();
      });
      actions.appendChild(b);
    });
    if (!live) note.textContent = "stage changed — this card is no longer active";
    li.appendChild(actions);
    li.appendChild(note);
  }

  // ---------- file card (kind="file") — artifact delivered by deliver_file (spec §4.5) ----------
  const fmtSize = (b) => (b >= 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1024)) + " KB");

  function downloadFile(e) {
    // production: fetch GET /api/threads/<id>/files/<file_id> with Authorization → Blob → object URL
    // (a plain <a href> cannot send the bearer header)
    const blob = new Blob(["# " + (e.title || e.name) + "\n\ndemo artifact — production serves the immutable snapshot blob.\n"], { type: e.mime || "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = el("a");
    a.href = url;
    a.download = e.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  function renderFileCard(li, e, t) { // feedEntry already set the "feed-file" class
    li.appendChild(el("span", "file-ic", "📎"));
    const col = el("div", "file-col");
    col.appendChild(el("b", "", e.name));
    col.appendChild(el("small", "", fmtSize(e.size) + " · " + e.mime + (e.title ? " · " + e.title : "")));
    li.appendChild(col);
    const acts = el("div", "file-actions");
    const dl = el("button", "file-btn", "Download");
    dl.type = "button";
    dl.addEventListener("click", () => downloadFile(e));
    acts.appendChild(dl);
    if (/^text\/(markdown|plain)$/.test(e.mime || "")) { // radio accepts only text/markdown + text/plain
      const bm = parseInt(localStorage.getItem(radioKey(t.id, e.file_id)) || "0", 10);
      const active = radio && radio.fileId === e.file_id;
      const listen = el("button", "file-btn", active ? "Playing…" : bm > 1 ? "Continue · fragment " + bm : "Listen");
      listen.type = "button";
      listen.title = "Narrator voice · streamed one bounded fragment at a time";
      listen.disabled = !!active;
      listen.addEventListener("click", () => startRadio(t.id, e));
      acts.appendChild(listen);
    }
    li.appendChild(acts);
  }

  // ---------- radio — narrator playback of a delivered text file (spec §4.6) ----------
  // production: POST /api/threads/<id>/radio-sessions → GET /api/radio/<sid>/segments/<n> (one
  // bounded MP3 per GET, next only after `ended`); DELETE /api/radio-sessions/<sid> to stop.
  let radio = null; // { threadId, fileId, name, seg, total, playing, timer }
  const RADIO_TOTAL = 12;
  const RADIO_SEG_MS = 6000; // demo segment length; production: real <audio> `ended`
  const radioKey = (tid, fid) => "codeflow-radio-" + tid + ":" + fid;

  function startRadio(threadId, entry) {
    if (voiceOn) disconnectVoice(false); // mic and radio are mutually exclusive (one Fish WS per host)
    $$(".play-btn.playing").forEach((b) => { b.classList.remove("playing"); b.textContent = "▶"; });
    const saved = parseInt(localStorage.getItem(radioKey(threadId, entry.file_id)) || "1", 10);
    radio = {
      threadId, fileId: entry.file_id, name: entry.name,
      seg: Math.min(Math.max(saved, 1), RADIO_TOTAL), total: RADIO_TOTAL, playing: false, timer: null,
    };
    playRadio();
    render();
  }
  function playRadio() {
    if (!radio || radio.playing) return;
    radio.playing = true;
    radio.timer = setInterval(() => {
      // bookmark saves on segment `ended` — a pause mid-segment replays that segment, never skips text
      localStorage.setItem(radioKey(radio.threadId, radio.fileId), String(radio.seg + 1));
      radio.seg += 1;
      if (radio.seg > radio.total) { finishRadio(); return; }
      renderRadio();
    }, RADIO_SEG_MS);
    renderRadio();
  }
  function pauseRadio() {
    if (!radio || !radio.playing) return;
    clearInterval(radio.timer);
    radio.playing = false;
    renderRadio();
  }
  function stopRadio() {
    if (!radio) return;
    clearInterval(radio.timer);
    radio = null; // bookmark survives → the file card offers “Continue · fragment N”
    render();
  }
  function finishRadio() {
    clearInterval(radio.timer);
    localStorage.removeItem(radioKey(radio.threadId, radio.fileId));
    radio = null;
    setConn("radio finished — last fragment played");
    render();
  }
  function renderRadio() {
    const bar = $("radio-bar");
    const r = route();
    if (!radio || r.view !== "thread" || r.id !== radio.threadId) { bar.hidden = true; return; }
    bar.replaceChildren();
    bar.appendChild(el("span", "radio-ic", "📻"));
    const col = el("div", "radio-text");
    col.appendChild(el("b", "", radio.name));
    col.appendChild(el("small", "", "narrator voice · fragment " + Math.min(radio.seg, radio.total) + " of " + radio.total));
    bar.appendChild(col);
    const pp = el("button", "radio-btn", radio.playing ? "❚❚" : "▶");
    pp.type = "button";
    pp.title = radio.playing ? "Pause — the current fragment finishes, the next is not fetched" : "Play";
    pp.addEventListener("click", () => (radio.playing ? pauseRadio() : playRadio()));
    bar.appendChild(pp);
    const st = el("button", "radio-btn radio-stop", "■");
    st.type = "button";
    st.title = "Stop radio — resume later from the saved fragment";
    st.addEventListener("click", stopRadio);
    bar.appendChild(st);
    bar.hidden = false;
  }

  // ---------- rename thread: inline editor, Enter/blur commits, Escape cancels ----------
  let renaming = false;
  function startRename() {
    if (renaming) return;
    const r = route();
    if (r.view !== "thread") return;
    const t = threads.find((x) => x.id === r.id);
    if (!t) return;
    renaming = true;
    const h = $("thread-title");
    const input = el("input", "rename-input");
    input.value = t.title;
    input.maxLength = 80;
    h.replaceWith(input);
    input.focus();
    input.select();
    const commit = () => {
      input.replaceWith(h);
      renaming = false;
      const trimmed = input.value.trim();
      if (trimmed && trimmed !== t.title) {
        t.title = trimmed.slice(0, 80); // production: PATCH /api/threads/<id>
        render();
      }
    };
    input.addEventListener("blur", commit, { once: true });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); input.blur(); }
      else if (e.key === "Escape") { input.value = t.title; input.blur(); }
    });
  }
  $("thread-title").addEventListener("click", startRename);
  $("view-title").addEventListener("click", startRename);

  // ---------- diff view (production: GET /api/threads/<id>/diff — live git status) ----------
  function renderDiff() {
    const r = route();
    if (r.view !== "thread") return;
    const box = $("diff-wrap");
    const d = diffs[r.id];
    const kids = [];
    if (!d) {
      kids.push(el("p", "diff-empty", "No changes yet — the agent is still working on the task."));
    } else {
      const fl = el("div", "diff-files");
      d.files.forEach((f) => fl.appendChild(el("span", "diff-file", f.status + " " + f.path)));
      kids.push(fl);
      const pre = el("pre", "diff-body");
      d.diff.split("\n").forEach((line) => {
        let cls = "";
        if (line.startsWith("@@") || line.startsWith("diff --git")) cls = "diff-hunk";
        else if (line.startsWith("+")) cls = "diff-add";
        else if (line.startsWith("-")) cls = "diff-del";
        pre.appendChild(el("span", cls, line + "\n"));
      });
      kids.push(pre);
    }
    box.replaceChildren(...kids);
  }

  // ---------- Code status card + activity page (production: kora-status / kora-log) ----------
  function renderKora() {
    const t = threads.find((x) => x.id === koraStatus.thread_id);
    const running = koraStatus.running && !!t;
    $("kora-dot").style.background = running ? koraStatus.color : "#888";
    const context = t ? t.title + (STAGES[t.stage] ? " · " + STAGES[t.stage] : "") : "";
    $("kora-sub").textContent = running ? "working in " + context : "idle";
    $("kora-card").href = running ? "#/thread/" + encodeURIComponent(t.id) : "#/activity";
    $("kora-card").title = running ? "Open active thread" : "Open Code activity";
    $("sky-status").textContent = running ? "LIVE · " + t.title.toUpperCase() : "IDLE";
    $("sky-title").textContent = running ? "Code is working" : "Code is idle";
    $("sky-detail").textContent = running ? context : "No task running";
  }

  function renderActivity() {
    const list = $("activity-list");
    list.replaceChildren();
    activityLog.forEach((e) => {
      const li = el("li", "journal-entry");
      li.appendChild(el("i", "journal-icon ji-" + e.kind, JOURNAL_ICONS[e.kind] || "·"));
      const div = el("div");
      div.appendChild(el("b", "", e.kind));
      div.appendChild(el("p", "", e.text));
      li.appendChild(div);
      list.appendChild(li);
    });
    if (!activityLog.length) list.appendChild(el("li", "journal-entry empty", "no events yet"));
  }

  // ---------- settings page (#/settings/ai — spec 2026-07-15 §4.7/§4.8) ----------
  const settingsDirty = () => JSON.stringify(aiSettings) !== savedSettings;

  function switchBtn(checked, label, onToggle) {
    const b = el("button", "switch");
    b.type = "button";
    b.setAttribute("role", "switch");
    b.setAttribute("aria-checked", String(checked));
    b.setAttribute("aria-label", label);
    b.addEventListener("click", () => { onToggle(!checked); renderSettings(); });
    return b;
  }
  function setCard(title, sub) {
    const c = el("section", "set-card");
    c.appendChild(el("h2", "", title));
    if (sub) c.appendChild(el("p", "set-sub", sub));
    return c;
  }
  function modelSelect(providerId, value, { routeOnly = false, allowEmpty = false } = {}) {
    const s = el("select", "set-select");
    s.setAttribute("aria-label", "Model");
    if (allowEmpty) {
      const o = el("option", "", "select model…");
      o.value = "";
      s.appendChild(o);
    }
    let list = MODELS[providerId] || [];
    if (value && !list.some((m) => m.id === value)) list = [...list, { id: value, tools: true }];
    list.forEach((m) => {
      const o = el("option", "", m.id + (m.tools ? "" : " · no tools"));
      o.value = m.id;
      if (routeOnly && !m.tools) o.disabled = true; // the dispatcher route requires tool support
      s.appendChild(o);
    });
    s.value = value || "";
    return s;
  }
  function routeError() {
    const rt = aiSettings.routing;
    if (rt.fallback && rt.fallback.provider === rt.primary.provider)
      return "Primary and fallback must be different providers — same-provider failures are not independent.";
    if (!aiSettings.providers[rt.primary.provider].enabled) return "Primary provider is disabled.";
    if (rt.fallback && !aiSettings.providers[rt.fallback.provider].enabled) return "Fallback provider is disabled.";
    return "";
  }

  function renderSettings() {
    const body = $("settings-body");
    body.replaceChildren();

    // Appearance — the style switch lives here; instant + per-device, outside the AI revision
    const ap = setCard("Appearance", "The theme applies instantly and is stored on this device.");
    const pick = el("div", "theme-pick");
    const drive = document.body.classList.contains("hero-mode");
    [["night", "🌙 Night Atlas", "calm indigo · moon guide"], ["drive", "⚡ Hero Drive", "warm slate · racing hero"]].forEach(([key, name, sub]) => {
      const active = (key === "drive") === drive;
      const b = el("button", "theme-opt" + (active ? " active" : ""));
      b.type = "button";
      b.setAttribute("aria-pressed", String(active));
      b.appendChild(el("b", "", name));
      b.appendChild(el("small", "", sub));
      b.addEventListener("click", () => { applyTheme(key); renderSettings(); });
      pick.appendChild(b);
    });
    ap.appendChild(pick);
    body.appendChild(ap);

    // Dispatcher — providers + manual primary/fallback route
    const disp = setCard("Dispatcher", "Keys live in .env and are never editable or returned here — only status and an irreversible mask.");
    Object.entries(PROVIDERS).forEach(([pid, meta]) => {
      const p = aiSettings.providers[pid];
      const box = el("div", "prov");
      const head = el("div", "prov-head");
      head.appendChild(switchBtn(p.enabled, meta.name + " enabled", (v) => { p.enabled = v; }));
      head.appendChild(el("b", "", meta.name));
      const testOut = el("span", "test-out");
      const test = el("button", "test-btn", "Test");
      test.type = "button";
      test.disabled = !p.enabled || !meta.mask || !p.selected_model;
      test.title = "Minimal request without user context (production: POST …/providers/" + pid + "/test)";
      test.addEventListener("click", () => {
        test.disabled = true;
        testOut.className = "test-out";
        testOut.textContent = "testing…";
        setTimeout(() => {
          test.disabled = false;
          testOut.className = "test-out ok";
          testOut.textContent = "✓ " + meta.test_ms + " ms · " + p.selected_model;
        }, 900);
      });
      head.appendChild(testOut);
      head.appendChild(test);
      box.appendChild(head);
      const key = el("p", "key-line");
      key.append("key " + (meta.mask ? "configured · " : "not configured — set "), el("code", "", meta.env));
      if (meta.mask) key.append(" · ", el("code", "", meta.mask));
      else key.append(" in .env");
      box.appendChild(key);
      const row = el("div", "prov-row");
      const sel = modelSelect(pid, p.selected_model, { allowEmpty: !p.selected_model });
      sel.disabled = !p.enabled;
      sel.addEventListener("change", () => { p.selected_model = sel.value || null; renderSettings(); });
      row.appendChild(sel);
      box.appendChild(row);
      disp.appendChild(box);
    });

    const rb = el("div", "route-block");
    rb.appendChild(el("h3", "set-h3", "Route"));
    const grid = el("div", "route-grid");
    ["primary", "fallback"].forEach((pos) => {
      const cell = el("div", "route-cell");
      cell.appendChild(el("b", "", pos));
      const cur = aiSettings.routing[pos];
      const ps = el("select", "set-select");
      ps.setAttribute("aria-label", pos + " provider");
      if (pos === "fallback") {
        const none = el("option", "", "— none");
        none.value = "";
        ps.appendChild(none);
      }
      Object.entries(PROVIDERS).forEach(([pid, meta]) => {
        const o = el("option", "", meta.name + (aiSettings.providers[pid].enabled ? "" : " · disabled"));
        o.value = pid;
        ps.appendChild(o);
      });
      ps.value = cur ? cur.provider : "";
      ps.addEventListener("change", () => {
        if (!ps.value) { aiSettings.routing.fallback = null; renderSettings(); return; }
        const cat = MODELS[ps.value] || [];
        const selModel = aiSettings.providers[ps.value].selected_model;
        const model = cat.some((m) => m.id === selModel && m.tools) ? selModel : (cat.find((m) => m.tools) || { id: "" }).id;
        aiSettings.routing[pos] = { provider: ps.value, model };
        renderSettings();
      });
      cell.appendChild(ps);
      if (cur) {
        const ms = modelSelect(cur.provider, cur.model, { routeOnly: true });
        ms.addEventListener("change", () => { cur.model = ms.value; renderSettings(); });
        cell.appendChild(ms);
        if (aiSettings.providers[cur.provider].selected_model && cur.model !== aiSettings.providers[cur.provider].selected_model)
          cell.appendChild(el("p", "set-warn", "⚠ differs from the provider's selected model"));
      }
      grid.appendChild(cell);
    });
    rb.appendChild(grid);
    const err = routeError();
    if (err) rb.appendChild(el("p", "set-err", "⛔ " + err));
    rb.appendChild(el("p", "set-note",
      "Fallback fires only on provider-level failures — timeout, 429, 5xx. 400 / contract / policy / cost-cap errors never fall back, so the reserve route must be a different provider."));
    disp.appendChild(rb);
    body.appendChild(disp);

    // Kora — defaults for new runs; each run snapshots them into an immutable RunSpec
    const kora = setCard("Code (Kora)", "Defaults for new runs; every run copies them into an immutable RunSpec.");
    const kp = el("div", "prov-row");
    kp.appendChild(el("span", "ro-pill", "runtime: Claude Agent SDK · read-only"));
    const km = el("select", "set-select");
    km.setAttribute("aria-label", "Kora default model");
    KORA_MODELS.forEach((m) => { const o = el("option", "", m); o.value = m; km.appendChild(o); });
    km.value = aiSettings.kora.default_model;
    km.addEventListener("change", () => { aiSettings.kora.default_model = km.value; renderSettings(); });
    kp.appendChild(km);
    kora.appendChild(kp);
    const nums = el("div", "num-grid");
    [["max_turns", "max turns", 1, 1], ["max_budget_usd", "budget · USD", 0.1, 0.1], ["deadline_s", "deadline · s", 60, 30]].forEach(([field, label, min, step]) => {
      const cell = el("div", "num-cell");
      const lab = el("label", "", label);
      lab.htmlFor = "set-" + field;
      const inp = el("input", "set-input");
      inp.id = "set-" + field;
      inp.type = "number";
      inp.min = String(min);
      inp.step = String(step);
      inp.value = String(aiSettings.kora[field]);
      inp.addEventListener("change", () => {
        const v = Number(inp.value);
        if (Number.isFinite(v)) aiSettings.kora[field] = Math.max(min, v);
        renderSettings();
      });
      cell.appendChild(lab);
      cell.appendChild(inp);
      nums.appendChild(cell);
    });
    kora.appendChild(nums);
    body.appendChild(kora);

    // Voice — v1 read-only, env-backed; the editor is owned by Settings → Voice (M+1)
    const vo = setCard("Voice", "Read-only in v1 — voices come from .env. Editing arrives in Settings → Voice (M+1).");
    VOICES.forEach((v) => {
      const row = el("div", "voice-row");
      row.appendChild(el("b", "", v.role));
      row.appendChild(el("code", "", v.env + (v.mask ? " · " + v.mask : "")));
      row.appendChild(el("small", "", v.note));
      vo.appendChild(row);
    });
    body.appendChild(vo);

    // production: PUT /api/settings/ai/* with the expected revision; a 409 shows
    // “changed on another device” + a server-vs-draft diff — never an auto-merge
    if (settingsDirty()) {
      const bar = el("div", "save-bar");
      bar.appendChild(el("span", "", "unsaved changes · revision " + aiSettings.revision + " → " + (aiSettings.revision + 1)));
      const revert = el("button", "ghost-button", "Revert");
      revert.type = "button";
      revert.addEventListener("click", () => { aiSettings = JSON.parse(savedSettings); renderSettings(); });
      bar.appendChild(revert);
      const save = el("button", "primary-button", "Save");
      save.type = "button";
      save.disabled = !!err;
      save.addEventListener("click", () => {
        aiSettings.revision += 1;
        savedSettings = JSON.stringify(aiSettings);
        setConn("AI settings saved · revision " + aiSettings.revision);
        renderSettings();
      });
      bar.appendChild(save);
      body.appendChild(bar);
    }
  }

  // ---------- composer: first message from home creates a thread in the active project ----
  function setConn(text) {
    $("conn-status").textContent = text;
    $("conn-status").hidden = !text;
    if (text) setTimeout(() => { if ($("conn-status").textContent === text) setConn(""); }, 3000);
  }
  function resizeInput() {
    const input = $("msg-input");
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 148) + "px";
  }
  function sendMessage() {
    const input = $("msg-input");
    const text = input.value.trim();
    if (!text) return;
    const r = route();
    let id = r.view === "thread" ? r.id : null;
    if (!id) {
      // production: POST /api/threads {title, project_id} → navigate, then POST /message
      id = uid("t");
      threads.unshift({ id, title: text.slice(0, 60), project_id: activeProject, stage: "collect", last_outcome: null, ago: "just now" });
      feeds[id] = [];
      location.hash = "#/thread/" + encodeURIComponent(id);
    }
    if (!feeds[id]) return; // unknown thread (not-found view)
    feeds[id].push({ kind: "user", text });
    input.value = "";
    resizeInput();
    render();
    $("typing").hidden = false;
    setTimeout(() => {
      $("typing").hidden = true;
      feeds[id].push({ kind: "assistant", text: "Got it. I’ll clarify the scope and hand a brief to Code — one moment." });
      render();
    }, 1200);
  }
  $("msg-send").addEventListener("click", sendMessage);
  $("msg-input").addEventListener("input", resizeInput);
  $("msg-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendMessage();
    }
  });
  const focusComposer = () => requestAnimationFrame(() => $("msg-input").focus());
  $("new-task").addEventListener("click", () => { location.hash = "#/"; closeDrawer(); focusComposer(); });
  $("hero-new").addEventListener("click", focusComposer);

  // ---------- voice: mic button states idle→connecting→on; overlay; toggle gates start ----
  let voiceOn = false;
  let connecting = false;
  let liveMuted = false;
  let speakTimer = null;

  function setMicState(state) {
    $("mic-btn").dataset.state = state;
    setConn(state === "connecting" ? "connecting voice…" : state === "on" ? "🎙 speak — I'm listening" : "");
  }
  function setLiveStatus(text, speaking) {
    $("live-status").textContent = text;
    $("live-overlay").classList.toggle("speaking", !!speaking);
  }
  function openLive() {
    $("live-overlay").classList.add("open");
    $("live-overlay").setAttribute("aria-hidden", "false");
    setLiveStatus("Flow is listening…", false);
    // demo of the two production states driven by onBotStarted/StoppedSpeaking
    let speaking = false;
    speakTimer = setInterval(() => {
      if (liveMuted) return;
      speaking = !speaking;
      setLiveStatus(speaking ? "Flow is replying" : "Flow is listening…", speaking);
    }, 3600);
  }
  function closeLive() {
    clearInterval(speakTimer);
    $("live-overlay").classList.remove("open");
    $("live-overlay").setAttribute("aria-hidden", "true");
  }
  function disconnectVoice(navigate = true) {
    if (!voiceOn) return;
    voiceOn = false;
    closeLive();
    setMicState("idle");
    if (!navigate) return; // radio start hangs up silently, without leaving the file's thread
    // production: navigates to the call's thread read from /client/session-alive
    const tid = koraStatus.thread_id;
    if (tid && route().id !== tid) location.hash = "#/thread/" + encodeURIComponent(tid);
  }
  $("mic-btn").addEventListener("click", () => {
    if (connecting) return;
    if (voiceOn) { disconnectVoice(); return; } // mic is always the hang-up
    if (!dispOn) return;                        // toggle gates the START only
    pauseRadio();                               // mic and radio are mutually exclusive
    connecting = true;
    setMicState("connecting");
    setTimeout(() => {
      connecting = false;
      voiceOn = true;
      setMicState("on");
      openLive();
    }, 700);
  });
  $("live-mute").addEventListener("click", () => {
    liveMuted = !liveMuted;
    $("live-mute").querySelector("small").textContent = liveMuted ? "Unmute" : "Mute";
    $("live-mute").setAttribute("aria-pressed", String(liveMuted));
    $("live-mute").classList.toggle("muted", liveMuted);
    setLiveStatus(liveMuted ? "Microphone paused" : "Flow is listening…", false);
    $("live-wave").style.opacity = liveMuted ? ".18" : "1";
  });
  $("live-end").addEventListener("click", disconnectVoice);

  // ---------- folder picker for “＋ project” (production: GET /api/browse) ----------
  const FAKE_FS = {
    "/Users/you": ["Projects", "Documents", "Desktop"],
    "/Users/you/Projects": ["luma-mobile", "atlas-api", "orbit-site"],
    "/Users/you/Projects/luma-mobile": ["Sources", "Tests"],
    "/Users/you/Projects/atlas-api": ["app", "tests"],
    "/Users/you/Projects/orbit-site": ["public", "src"],
    "/Users/you/Documents": [],
    "/Users/you/Desktop": [],
  };
  let pickerCur = null;
  function openPicker() {
    $("picker").classList.add("open");
    $("picker").setAttribute("aria-hidden", "false");
    $("picker-error").textContent = "";
    browse("/Users/you");
    $("picker-choose").focus();
  }
  function closePicker() {
    $("picker").classList.remove("open");
    $("picker").setAttribute("aria-hidden", "true");
  }
  function browse(path) {
    pickerCur = path;
    $("picker-path").textContent = path;
    $("picker-error").textContent = "";
    const ul = $("picker-dirs");
    ul.replaceChildren();
    const row = (label, go) => {
      const li = el("li", "", label);
      li.tabIndex = 0;
      li.setAttribute("role", "button");
      li.addEventListener("click", go);
      li.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); }
      });
      return li;
    };
    const parent = path.split("/").slice(0, -1).join("/");
    if (FAKE_FS[parent]) ul.appendChild(row("‹ back", () => browse(parent)));
    (FAKE_FS[path] || []).forEach((name) => {
      const child = path + "/" + name;
      ul.appendChild(row("📁 " + name, () => browse(child)));
    });
  }
  $("add-project").addEventListener("click", openPicker);
  $("picker-cancel").addEventListener("click", closePicker);
  $("picker-choose").addEventListener("click", () => {
    if (!pickerCur) return;
    if (projects.some((p) => p.path === pickerCur)) {
      $("picker-error").textContent = "⛔ project already exists";
      return;
    }
    projects.push({ id: uid("p"), name: pickerCur.split("/").pop(), path: pickerCur });
    closePicker();
    render();
  });

  // ---------- drawer (mobile) + Escape closes the top modal ----------
  function openDrawer() { $("sidebar").classList.add("open"); $("scrim").classList.add("open"); }
  function closeDrawer() { $("sidebar").classList.remove("open"); $("scrim").classList.remove("open"); }
  $("menu-button").addEventListener("click", openDrawer);
  $("close-sidebar").addEventListener("click", closeDrawer);
  $("scrim").addEventListener("click", closeDrawer);
  $$(".modal-layer").forEach((layer) => layer.addEventListener("click", (ev) => {
    if (ev.target !== layer) return;
    if (layer.id === "live-overlay") disconnectVoice(); else closePicker();
  }));
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if ($("live-overlay").classList.contains("open")) { disconnectVoice(); return; }
    if ($("picker").classList.contains("open")) { closePicker(); return; }
    closeDrawer();
  });

  // ---------- theme: Night Atlas ⇄ Hero Drive ----------
  function applyTheme(theme, persist = true) {
    const drive = theme === "drive";
    document.body.classList.toggle("hero-mode", drive);
    $("brand-theme").textContent = drive ? "Hero Drive" : "Night Atlas";
    $("theme-toggle-icon").textContent = drive ? "☾" : "⚡";
    $("theme-toggle-label").textContent = drive ? "Night Atlas" : "Hero Drive";
    $("theme-toggle").setAttribute("aria-label", drive ? "Switch to Night Atlas mode" : "Switch to Hero Drive mode");
    document.querySelector('meta[name="theme-color"]').content = drive ? "#07111f" : "#090b24";
    document.title = `CodeFlow — ${drive ? "Hero Drive" : "Night Atlas"}`;
    if (persist) localStorage.setItem("codeflow-theme", theme);
  }
  applyTheme(localStorage.getItem("codeflow-theme") === "drive" ? "drive" : "night", false);
  $("theme-toggle").addEventListener("click", () =>
    applyTheme(document.body.classList.contains("hero-mode") ? "night" : "drive"));

  // ---------- init ----------
  validateActiveProject();
  applyDispToggleUI();
  setTab("chat");
  render();
})();
