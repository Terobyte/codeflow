"""WebRTC demo server (Р-6). Serves the Синапс voice agent over pipecat SmallWebRTCTransport
so the browser's own WebRTC stack does acoustic echo cancellation (getUserMedia defaults
echoCancellation=true). That cancels Kora's TTS out of the mic capture and kills the
local-speaker->mic feedback loop that made her hear, interrupt, and talk over herself.
`build_session_pipeline()` stays transport-agnostic; we only wrap it per browser session,
referencing the one long-lived `SynapseHost` (M1 host-singleton) so task/ledger/confirm-flow
state survives a reconnect and only the per-connection transport+processors are rebuilt.
aiortc/cv2/fastapi/pipecat_ai_prebuilt are imported here (behind the `voice` extra), never at
app.py top (S4).
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat_ai_prebuilt.frontend import PipecatPrebuiltUI

from synapse.bridge.state import Liveness, TaskStatus
from synapse.pipeline.app import SynapseHost, build_session_pipeline

# B8: hard cap on pending (started-but-not-yet-offered) handshake sessions — bounds the
# memory a bare-/start flood can claim. Generous for a single-client demo.
_MAX_PENDING_SESSIONS = 128

# M1 slice 5 (§2.2): our own committed PWA assets (manifest/icons/watchdog script) — separate
# from the prebuilt bundle's own dist/ directory, which we read from but never write to.
_STATIC_DIR = Path(__file__).parent / "static"

# M1 slice 5 (§2.2): meta injected into the prebuilt bundle's <head> so /client installs as a
# PWA (manifest + apple-touch-icon + A2HS meta) and picks up the reconnect watchdog, without
# forking pipecat_ai_prebuilt's dist/ (a fork would need re-diffing on every pipecat upgrade).
_PWA_HEAD = (
    '<link rel="manifest" href="./manifest.webmanifest">'
    '<link rel="apple-touch-icon" href="./apple-touch-icon.png">'
    '<meta name="apple-mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
    '<meta name="theme-color" content="#0b0f14">'
    '<script defer src="./reconnect.js"></script>'
    '<script defer src="./status-widget.js"></script>'
)


def _status_color(liveness: Liveness, task_status: TaskStatus | None, awaiting: bool) -> str:
    """Светофор Коры — kora status UI (tero run 2026-07-12). red > yellow > green;
    дефолт-маппинг из ран-файла §1, Теро подкрутит на глаз. Терминал/нет-задачи проверяются
    ПЕРВЫМИ (R2): после task_completed стрим кончается и heartbeat'ов больше нет, так что
    возраст в liveness() растёт вечно — успешно завершённая задача обязана оставаться
    зелёной, а не гнить в жёлтый/красный. liveness опрашивается только при живом ране."""
    if task_status is TaskStatus.FAILED:
        return "red"
    if task_status is None or task_status in (TaskStatus.IDLE, TaskStatus.COMPLETED):
        return "green"  # нет живого рана — liveness не о чем
    if liveness is Liveness.UNREACHABLE:
        return "red"
    if (
        liveness is Liveness.STALE
        or awaiting
        or task_status in (TaskStatus.PENDING_CONFIRMATION, TaskStatus.CANCEL_REQUESTED)
    ):
        return "yellow"
    return "green"


def build_web_app(host: SynapseHost) -> FastAPI:
    """FastAPI app: POST/PATCH /api/offer drive pipecat's SmallWebRTC signaling, the prebuilt
    browser client is mounted at /client. `host` is the ONE long-lived `SynapseHost` (built once
    by `run()`, or by the caller); this function does no key validation or network I/O itself,
    so a stub host works fine for route-only tests. Each new browser offer spins one fresh
    per-connection `SynapseSession` (`run_session`) wired to `host`, and preempts whichever
    session was previously active (DoD-2: exactly one live client)."""
    app = FastAPI()

    # B28: the journal fd lives as long as the host; close it when uvicorn shuts down (the
    # only live-path close — console.py closes its own). Looked up lazily inside the handler:
    # unit tests build this app around stub hosts with no real journal, and must not trip at
    # build time. Late writes after close are silent no-ops (B28 guard in TurnJournal._write).
    # Registered on app.router: Starlette 1.x removed the app-level add_event_handler alias;
    # FastAPI keeps the identical API on APIRouter, run by its default lifespan at shutdown.
    async def _close_journal() -> None:
        host.journal.close()

    app.router.add_event_handler("shutdown", _close_journal)

    handler = SmallWebRTCRequestHandler()
    # Р-6: the prebuilt RTVI client (pipecat-ai-prebuilt 1.0.3) uses the "start bot, then connect"
    # handshake -- it POSTs /start FIRST to open a session, THEN sends the SDP offer to
    # /sessions/{sessionId}/api/offer (its smallwebrtc startBotParams: endpoint=/start, offer URL =
    # start_url.replace("/start", "/sessions/<id>/api/offer")). A server exposing only /api/offer
    # 404s that first /start, so the browser hangs at "authenticating -> Unable to connect". We
    # mirror pipecat's own runner contract: mint a sessionId on /start, gate the offer proxy on it.
    active_sessions: dict[str, dict] = {}
    # M1: host holds exactly one active client -- a new offer preempts whichever PipelineTask is
    # currently running, torn down under `lock` so two concurrent offers can't race the
    # check-cancel-replace (Risk-M5).
    current: dict[str, PipelineTask | None] = {"task": None}
    lock = asyncio.Lock()

    async def run_session(connection: SmallWebRTCConnection, session_id: str | None = None) -> None:
        session = build_session_pipeline(host)
        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        full = Pipeline([transport.input(), session.pipeline, transport.output()])
        task = PipelineTask(full, idle_timeout_secs=None)  # M3: never auto-drop a connected demo session

        greeted = False

        @transport.event_handler("on_client_connected")
        async def _on_client_connected(_transport, _client):
            # M1 slice 5 (§2.7): resync greeting — once per run_session. pipecat re-emits
            # "connected" on ICE self-heals of the SAME connection (no dedup upstream), and the
            # host-level arbiter would let each re-greet truncate Kora's live turn — hence the latch.
            # Deterministic, no LLM (R2-крит).
            nonlocal greeted
            if greeted:
                return
            greeted = True
            greeting = host.store.resync_greeting(
                host.clock.now(), host.cfg.stale_after_s, host.cfg.unreachable_after_s
            )
            if greeting:
                await host.push_speak_frame(greeting)
            # Undelivered criticals replay via speak() (Р-15г ledger stays honest); min_age keeps a
            # just-emitted critical (organic speak still in flight) from double-voicing.
            for ev in host.speak_ledger.unspoken(host.clock.now(), min_age_s=5.0):
                host.speak(ev.speak_text)

        @transport.event_handler("on_client_disconnected")
        async def _on_client_disconnected(_transport, _client):
            # M1: browser close/refresh pushes no EndFrame -> cancel so Flux/Fish/LLM sockets for
            # THIS connection's transport tear down instead of leaking until the process exits.
            # host state (store/speak_ledger/confirm_flow/breaker/cost_cap) is untouched.
            await task.cancel(reason="webrtc client disconnected")
            async with lock:
                if current["task"] is task:
                    current["task"] = None
                host.unbind_output(task)  # M1 slice 2: stop the SPEAK injector targeting a dead task

        async with lock:
            old = current["task"]
            current["task"] = task
            # M1 slice 2: bind the SPEAK injector to THIS task under the same lock that
            # publishes it as current, so a racing offer can't leave the injector pointed at a
            # preempted task. A preempting connection's later bind supersedes this one.
            host.bind_output(task)
        # B24: old.cancel + monitor spawn moved INSIDE the try — a raise in this setup window used
        # to skip the finally, leaking the bind slot, the current["task"] publish, and the
        # active_sessions entry. `monitor` is None-guarded so a raise before it spawns is safe.
        monitor = None
        try:
            if old is not None:
                await old.cancel(reason="preempted by new connection")
            monitor = asyncio.ensure_future(host.monitor_forever())
            await PipelineRunner(handle_sigint=False).run(task)  # M2: leave SIGINT to uvicorn
        finally:
            if monitor is not None:
                monitor.cancel()
                # B29: consume the cancellation — a cancelled-but-never-awaited task leaks a
                # pending exception ("Task was destroyed but it is pending" on teardown).
                try:
                    await monitor
                except asyncio.CancelledError:
                    pass
            async with lock:
                if current["task"] is task:
                    current["task"] = None
                host.unbind_output(task)  # M1 slice 2: no-op if a preempting task already rebound
            if session_id is not None:
                active_sessions.pop(session_id, None)

    async def _handle_offer(
        request: SmallWebRTCRequest, background_tasks: BackgroundTasks, session_id: str | None = None
    ):
        async def on_connection(connection: SmallWebRTCConnection) -> None:
            background_tasks.add_task(run_session, connection, session_id)

        return await handler.handle_web_request(
            request=request, webrtc_connection_callback=on_connection
        )

    @app.post("/start")
    async def start_bot(request: Request):
        # RTVI connect handshake: open a session, hand the browser its ICE config, and (via the
        # returned sessionId) tell it which /sessions/<id>/api/offer to POST the SDP offer to next.
        # B25: distinguish an EMPTY body (bare /start is a legitimate handshake — the RTVI
        # prebuilt client always POSTs a flat JSON object, but curl/manual flows may not) from a
        # MALFORMED one: garbage must be a diagnosable 400, not a silent empty handshake.
        # json.loads raises more than JSONDecodeError on hostile input (UnicodeDecodeError on bad
        # bytes, RecursionError on deep nesting) — catch exactly that set; endpoint is unauthenticated.
        raw = await request.body()
        if raw:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
                return JSONResponse({"error": "malformed JSON body"}, status_code=400)
            if not isinstance(data, dict):
                return JSONResponse({"error": "JSON body must be an object"}, status_code=400)
        else:
            data = {}
        session_id = str(uuid.uuid4())
        active_sessions[session_id] = data.get("body", {})
        # B8: a /start with no follow-up offer is only popped via run_session's finally (reached
        # only by a completed offer), so a bare-/start flood (tab-close, ICE fail, curl loop)
        # would grow this unbounded. Cap it, evicting the oldest pending handshake (dict preserves
        # insertion order). The demo has ~1 concurrent client, so a legit /start→offer is never
        # evicted before its offer arrives.
        while len(active_sessions) > _MAX_PENDING_SESSIONS:
            active_sessions.pop(next(iter(active_sessions)))
        result: dict = {"sessionId": session_id}
        if data.get("enableDefaultIceServers"):
            result["iceConfig"] = {"iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}]}
        return result

    @app.post("/sessions/{session_id}/api/offer")
    async def session_offer(
        session_id: str, request: SmallWebRTCRequest, background_tasks: BackgroundTasks
    ):
        if session_id not in active_sessions:
            return Response(content="Invalid or not-yet-ready session_id", status_code=404)
        return await _handle_offer(request, background_tasks, session_id)

    @app.patch("/sessions/{session_id}/api/offer")
    async def session_ice_candidate(session_id: str, request: SmallWebRTCPatchRequest):
        await handler.handle_patch_request(request)
        return {"status": "success"}

    # Direct (session-less) offer routes: unused by the prebuilt client (it always goes through
    # /sessions/<id>/...), kept so the endpoint stays curl-testable and API-symmetric.
    @app.post("/api/offer")
    async def offer(request: SmallWebRTCRequest, background_tasks: BackgroundTasks):
        return await _handle_offer(request, background_tasks)

    @app.patch("/api/offer")
    async def ice_candidate(request: SmallWebRTCPatchRequest):
        await handler.handle_patch_request(request)
        return {"status": "success"}

    @app.get("/")
    async def index():
        return RedirectResponse(url="/client/")

    # M1 slice 5 (§2.2): PWA wrapper around the prebuilt bundle. `PipecatPrebuiltUI` (mounted
    # below) is a `StaticFiles(directory=dist, html=True)` whose asset paths are RELATIVE
    # (`./assets/...`), so serving a patched index.html from a route registered BEFORE the mount
    # doesn't break asset loading — Starlette matches routes in registration order, so these win
    # over the mount's own handling of the same paths. We wrap instead of forking dist/ (a fork
    # would need re-diffing on every pipecat_ai_prebuilt upgrade). `html=True` StaticFiles serves
    # BOTH "/client/" (its own index-serving fallback) and "/client/index.html" (the literal
    # path) for the same file, so both are patched here or the second would still serve the
    # unpatched original.
    dist_index_path = Path(PipecatPrebuiltUI.directory) / "index.html"
    dist_html = dist_index_path.read_text(encoding="utf-8")
    patched_index = dist_html.replace("</head>", _PWA_HEAD + "</head>")
    if patched_index == dist_html:
        # B6: a silent no-op anchor replace must never pass as "patched" — dist/index.html's
        # shape changing out from under us (a pipecat_ai_prebuilt upgrade) must fail loudly at
        # build time, not silently ship a client with no manifest link.
        raise RuntimeError("PWA head injection failed: no </head> anchor in dist/index.html")
    _patched_index_bytes = patched_index.encode("utf-8")
    _manifest_bytes = (_STATIC_DIR / "manifest.webmanifest").read_bytes()
    _reconnect_js_bytes = (_STATIC_DIR / "reconnect.js").read_bytes()
    _icon_192_bytes = (_STATIC_DIR / "icon-192.png").read_bytes()
    _icon_512_bytes = (_STATIC_DIR / "icon-512.png").read_bytes()
    _apple_touch_icon_bytes = (_STATIC_DIR / "apple-touch-icon.png").read_bytes()
    # kora status UI (tero run 2026-07-12): страница логов + виджет-светофор, тот же
    # pre-read-bytes идиом, что и PWA-ассеты выше.
    _logs_html_bytes = (_STATIC_DIR / "logs.html").read_bytes()
    _status_widget_js_bytes = (_STATIC_DIR / "status-widget.js").read_bytes()

    @app.get("/client/")
    async def client_index():
        return Response(content=_patched_index_bytes, media_type="text/html")

    @app.get("/client/index.html")
    async def client_index_html():
        return Response(content=_patched_index_bytes, media_type="text/html")

    @app.get("/client/manifest.webmanifest")
    async def client_manifest():
        return Response(content=_manifest_bytes, media_type="application/manifest+json")

    @app.get("/client/reconnect.js")
    async def client_reconnect_js():
        return Response(content=_reconnect_js_bytes, media_type="text/javascript")

    @app.get("/client/icon-192.png")
    async def client_icon_192():
        return Response(content=_icon_192_bytes, media_type="image/png")

    @app.get("/client/icon-512.png")
    async def client_icon_512():
        return Response(content=_icon_512_bytes, media_type="image/png")

    @app.get("/client/apple-touch-icon.png")
    async def client_apple_touch_icon():
        return Response(content=_apple_touch_icon_bytes, media_type="image/png")

    # M1 slice 5 (§2.7): truth-based signal for reconnect.js — NOT a wall clock (R3/R4: iOS
    # suspends page timers while locked/backgrounded, so elapsed-time heuristics false-positive
    # on every ordinary wake). `current["task"]` is None the instant no client is actively bound
    # (torn down or preempted), so this reflects real server state instead of a guess.
    @app.get("/client/session-alive")
    async def session_alive():
        return JSONResponse({"active": current["task"] is not None})

    # kora status UI (tero run 2026-07-12): все четыре роута ниже — как session-alive,
    # ДО app.mount (Starlette матчит в порядке регистрации, роут выигрывает у StaticFiles).

    @app.get("/client/kora-status")
    async def kora_status():
        now = host.clock.now()
        live = host.store.liveness(now, host.cfg.stale_after_s, host.cfg.unreachable_after_s)
        task = host.store.task
        status = task.status if task is not None else None
        # Зеркалит RUNNING-гейт snapshot'а (state.py:310): флаг «ждёт ответа» показывается
        # только пока задача реально бежит.
        awaiting = bool(
            host.store.awaiting_answer and task is not None and task.status == TaskStatus.RUNNING
        )
        return JSONResponse(
            {
                "color": _status_color(live, status, awaiting),
                "liveness": live.value,
                "task_status": status.value if status is not None else None,
                "awaiting_answer": awaiting,
                "task_text": task.text[:60] if task is not None else None,
            }
        )

    @app.get("/client/kora-log")
    async def kora_log_feed():
        # Хост-стаб без проведённой ленты (kora_log=None, паттерн kora_runner) → пустой фид.
        entries = list(host.kora_log) if host.kora_log is not None else []
        return JSONResponse({"entries": entries})

    @app.get("/client/logs")
    async def client_logs():
        return Response(content=_logs_html_bytes, media_type="text/html")

    @app.get("/client/status-widget.js")
    async def client_status_widget_js():
        return Response(content=_status_widget_js_bytes, media_type="text/javascript")

    app.mount("/client", PipecatPrebuiltUI, name="client")
    return app
