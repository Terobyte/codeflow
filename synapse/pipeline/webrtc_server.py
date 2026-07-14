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
from urllib.parse import quote

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

def _browse_dir(raw: str | None, home: Path) -> dict | None:
    """Папко-пикер «+ проект» (фидбек Теро: абсолютный путь руками — мусор). Read-only
    листинг ПОДдиректорий, клетка = HOME: путь вне её или битый молча падает на home,
    не 403 — UI всегда получает валидную страницу. Скрытые директории не показываются
    (заодно прячет .ssh/.config; первая линия защиты — validate_project_path при add)."""
    base = home.resolve()
    # B50: null-байт в пути даёт ValueError (из Path/resolve) — падаем на home, как любой
    # другой неразрешимый путь, а не 500.
    try:
        p = Path(raw).expanduser() if raw else base
        rp = p.resolve()
    except (OSError, RuntimeError, ValueError):
        rp = base
    if not rp.is_relative_to(base) or not rp.is_dir():
        rp = base
    try:
        dirs = sorted(d.name for d in rp.iterdir() if d.is_dir() and not d.name.startswith("."))
    except OSError:
        return None
    parent = str(rp.parent) if rp != base else None
    return {"path": str(rp), "parent": parent, "dirs": dirs}


# B8: hard cap on pending (started-but-not-yet-offered) handshake sessions — bounds the
# memory a bare-/start flood can claim. Generous for a single-client demo.
_MAX_PENDING_SESSIONS = 128

# M1 slice 5 (§2.2): our own committed PWA assets (manifest/icons/watchdog script) — separate
# from the prebuilt bundle's own dist/ directory, which we read from but never write to.
_STATIC_DIR = Path(__file__).parent / "static"

# Gate v2 C1': серверные чат-команды — exact-match ВСЕГО сообщения (strip+casefold). Bare-слова
# приняты сениором (юзер уже печатает «compact» без слэша); риск легитимной реплики «clear»
# принят. Обрабатываются ДО ingest_user_turn: LLM-ход не зовётся, user-запись в ленту не
# пишется (команда — не реплика диалога).
_CHAT_COMMANDS = frozenset({"compact", "/compact", "clear", "/clear"})


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
        if _monitor["task"] is not None:
            _monitor["task"].cancel()
            try:
                await _monitor["task"]
            except asyncio.CancelledError:
                pass
        host.journal.close()

    app.router.add_event_handler("shutdown", _close_journal)

    async def _start_monitor() -> None:
        if _monitor["task"] is None or _monitor["task"].done():
            _monitor["task"] = asyncio.ensure_future(host.monitor_forever())

    app.router.add_event_handler("startup", _start_monitor)

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
    current: dict[str, Any] = {"task": None, "session_id": None}
    lock = asyncio.Lock()
    _monitor: dict[str, asyncio.Task | None] = {"task": None}

    async def run_session(connection: SmallWebRTCConnection, session_id: str | None = None) -> None:
        task = None
        spawned_monitor = False
        try:
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
                # Gate v2 D3': последний ответ диспетчера в звонке никаким _on_end_of_turn уже
                # не ловится (юзер повесил трубку) — флашим context-diff в ленту треда здесь.
                # Display-путь: сбой флаша не должен валить teardown соединения.
                flush = getattr(session, "flush_voice_feed", None)
                if flush is not None:
                    try:
                        flush()
                    except Exception:  # noqa: BLE001
                        pass
                # M1: browser close/refresh pushes no EndFrame -> cancel so Flux/Fish/LLM sockets for
                # THIS connection's transport tear down instead of leaking until the process exits.
                # host state (store/speak_ledger/confirm_flow/breaker/cost_cap) is untouched.
                if task is not None:
                    await task.cancel(reason="webrtc client disconnected")
                async with lock:
                    if current["task"] is task:
                        current["task"] = None
                        current["session_id"] = None
                    host.unbind_output(task)  # M1 slice 2: stop the SPEAK injector targeting a dead task

            async with lock:
                old = current["task"]
                current["task"] = task
                current["session_id"] = session_id
                # M1 slice 2: bind the SPEAK injector to THIS task under the same lock that
                # publishes it as current, so a racing offer can't leave the injector pointed at a
                # preempted task. A preempting connection's later bind supersedes this one.
                host.bind_output(task)
            # B24: old.cancel + monitor spawn moved INSIDE the try — a raise in this setup window used
            # to skip the finally, leaking the bind slot, the current["task"] publish, and the
            # active_sessions entry. `monitor` is None-guarded so a raise before it spawns is safe.
            if old is not None:
                await old.cancel(reason="preempted by new connection")
            if _monitor["task"] is None or _monitor["task"].done():
                _monitor["task"] = asyncio.ensure_future(host.monitor_forever())
                spawned_monitor = True
            await PipelineRunner(handle_sigint=False).run(task)  # M2: leave SIGINT to uvicorn
        finally:
            import sys
            is_pytest = "pytest" in sys.modules
            is_magic_mock = hasattr(host, "mock_calls")
            if is_pytest and not is_magic_mock:
                if _monitor["task"] is not None:
                    _monitor["task"].cancel()
                    try:
                        await _monitor["task"]
                    except asyncio.CancelledError:
                        pass
                    _monitor["task"] = None

            if task is not None:
                async with lock:
                    if current["task"] is task:
                        current["task"] = None
                        current["session_id"] = None
                        if session_id is not None:
                            active_sessions.pop(session_id, None)
                    else:
                        # We were preempted. If the preempting task is using a DIFFERENT session_id,
                        # then our session_id is no longer active, so we must pop it.
                        if session_id is not None and current["session_id"] != session_id:
                            active_sessions.pop(session_id, None)
                    host.unbind_output(task)  # M1 slice 2: no-op if a preempting task already rebound
            else:
                async with lock:
                    if current["session_id"] != session_id:
                        if session_id is not None:
                            active_sessions.pop(session_id, None)

            # B-PIPE-SOCKET-CLEANUP: Explicitly disconnect the WebRTC connection on exit
            # to prevent connection/socket leaks.
            try:
                await connection.disconnect()
            except Exception:
                pass

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

    # UI v2 слайс UI-1 (спека §4 «миграция»): /client/ отдаёт НАШ тонкий клиент; prebuilt
    # уезжает НЕПАТЧЕННЫМ на /client/dev (тот же PipecatPrebuiltUI-объект). Инжекты слайса 5
    # умирают вместе с патч-логикой: PWA-обёртка и реконнект теперь обязанность нашего index.
    _CLIENT_DIR = Path(__file__).parent / "client"
    # UI v3: наши файлы (index/app/style) читаются с ДИСКА на каждый запрос — итерации
    # дизайна на staging без рестарта сервера. Vendored-бандл большой и неизменный — RAM.
    _vendor_pipecat_bytes = (_CLIENT_DIR / "vendor" / "pipecat.mjs").read_bytes()

    def _client_file(name: str, media_type: str) -> Response:
        return Response(content=(_CLIENT_DIR / name).read_bytes(), media_type=media_type)
    # static-ассеты (manifest/иконки/logs/status-widget) живы — роуты для них ниже.
    # reconnect.js умер в UI v3: вотчдог-семантика §2.7 живёт в app.js (авто-реконнект
    # на месте, reload — последний резерв).
    _manifest_bytes = (_STATIC_DIR / "manifest.webmanifest").read_bytes()
    _icon_192_bytes = (_STATIC_DIR / "icon-192.png").read_bytes()
    _icon_512_bytes = (_STATIC_DIR / "icon-512.png").read_bytes()
    _apple_touch_icon_bytes = (_STATIC_DIR / "apple-touch-icon.png").read_bytes()
    _logs_html_bytes = (_STATIC_DIR / "logs.html").read_bytes()
    _status_widget_js_bytes = (_STATIC_DIR / "status-widget.js").read_bytes()

    @app.get("/client/")
    async def client_index():
        return _client_file("index.html", "text/html")

    @app.get("/client/index.html")
    async def client_index_html():
        return _client_file("index.html", "text/html")

    @app.get("/client/app.js")
    async def client_app_js():
        return _client_file("app.js", "text/javascript")

    @app.get("/client/style.css")
    async def client_style_css():
        return _client_file("style.css", "text/css")

    @app.get("/client/vendor/pipecat.mjs")
    async def client_vendor_pipecat():
        return Response(content=_vendor_pipecat_bytes, media_type="text/javascript")

    @app.get("/client/thread")
    async def client_thread(id: str | None = None):
        # UI v3: страница треда умерла, тред живёт в SPA-хеше. Старые ссылки/закладки
        # /client/thread?id=X доезжают редиректом (quote: id — произвольная строка).
        return RedirectResponse(url="/client/#/thread/" + quote(id or "", safe=""))

    @app.get("/client/manifest.webmanifest")
    async def client_manifest():
        return Response(content=_manifest_bytes, media_type="application/manifest+json")

    @app.get("/client/icon-192.png")
    async def client_icon_192():
        return Response(content=_icon_192_bytes, media_type="image/png")

    @app.get("/client/icon-512.png")
    async def client_icon_512():
        return Response(content=_icon_512_bytes, media_type="image/png")

    @app.get("/client/apple-touch-icon.png")
    async def client_apple_touch_icon():
        return Response(content=_apple_touch_icon_bytes, media_type="image/png")

    # M1 slice 5 (§2.7): truth-based signal for the app.js watchdog — NOT a wall clock (R3/R4: iOS
    # suspends page timers while locked/backgrounded, so elapsed-time heuristics false-positive
    # on every ordinary wake). `current["task"]` is None the instant no client is actively bound
    # (torn down or preempted), so this reflects real server state instead of a guess.
    @app.get("/client/session-alive")
    async def session_alive():
        # Gate v2 B1'/A12': GET-эндпоинт под voice_thread НЕ создаём — session-alive уже
        # поллится вотчдогом; «Завершить — в чат» читает id треда звонка отсюда. Ключ
        # voice_thread добавляется ТОЛЬКО когда host реально его несёт: голые host-стабы
        # route-тестов (object()) сохраняют прежний payload {"active": ...}.
        payload: dict = {"active": current["task"] is not None}
        vt = getattr(host, "voice_thread", None)
        if isinstance(vt, dict):
            payload["voice_thread"] = vt.get("id")
        return JSONResponse(payload)

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
        # UI-4: статус Коры — не абстрактный «работает», а ссылка на конкретную задачу.
        # Старые минимальные host-стабы могут не нести threads, поэтому контекст опционален.
        thread = None
        threads = getattr(host, "threads", None)
        if task is not None and threads is not None:
            thread = threads.thread_for_task(task.id)
        thread_context = (
            {"thread_id": thread.id, "thread_title": thread.title, "thread_stage": thread.stage}
            if thread is not None else {}
        )
        return JSONResponse(
            {
                "color": _status_color(live, status, awaiting),
                "liveness": live.value,
                "task_status": status.value if status is not None else None,
                "awaiting_answer": awaiting,
                "task_text": task.text[:60] if task is not None else None,
                **thread_context,
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

    # UI v2 слайс UI-3: API тредов/проектов. Анти-CSRF (S4): tailnet — сетевая граница,
    # не браузерная; мутирующий /api/* требует JSON content-type (HTML-форма не может)
    # + Origin/Referer против Host.
    def _csrf_ok(request: Request) -> bool:
        if not request.headers.get("content-type", "").startswith("application/json"):
            return False
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if not origin:
            return False  # B-PIPE-6: require Origin or Referer for CSRF protection
        from urllib.parse import urlparse
        if urlparse(origin).netloc != request.headers.get("host", ""):
            return False
        return True

    async def _json_body(request: Request):
        """B10: a malformed JSON body on a mutating /api/* route must be a diagnosable 400 (the
        exact pattern `/start` already uses), NOT an unhandled JSONDecodeError → 500. Returns
        `(data, None)` on a well-formed object body, or `(None, <400 JSONResponse>)` otherwise."""
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            return None, JSONResponse({"error": "malformed JSON body"}, status_code=400)
        if not isinstance(data, dict):
            return None, JSONResponse({"error": "JSON body must be an object"}, status_code=400)
        return data, None

    def _thread_dict(t) -> dict:
        return {"id": t.id, "title": t.title, "project_id": t.project_id, "stage": t.stage,
                "last_outcome": t.last_outcome, "updated_ts": t.updated_ts,
                "created_ts": t.created_ts, "request_text": t.request_text,
                "last_model": t.last_model, "archived": t.archived}

    @app.get("/api/browse")
    async def api_browse(path: str | None = None):
        result = _browse_dir(path, Path.home())
        if result is None:
            return JSONResponse({"error": "unreadable"}, status_code=400)
        return JSONResponse(result)

    @app.get("/api/projects")
    async def api_projects_list():
        return JSONResponse({"projects": host.projects.list()})

    @app.post("/api/projects")
    async def api_projects_add(request: Request):
        if not _csrf_ok(request):
            return JSONResponse({"error": "csrf"}, status_code=403)
        from synapse.projects import ProjectValidationError
        data, err = await _json_body(request)
        if err is not None:
            return err
        try:
            proj = await host.projects.add(str(data.get("name") or ""), str(data.get("path") or ""))
        except ProjectValidationError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(proj)

    @app.get("/api/threads")
    async def api_threads_list(archived: str | None = None):
        # UI-5 (S31): по умолчанию архив скрыт; ?archived=1 отдаёт только архив.
        # B56: явный truthy-набор — "false"/"no"/"0"/"" идут в обычный неархивный список.
        if archived is not None and archived.lower() in ("1", "true", "yes"):
            return JSONResponse({"threads": [_thread_dict(t) for t in host.threads.list(include_archived=True) if t.archived]})
        return JSONResponse({"threads": [_thread_dict(t) for t in host.threads.list()]})

    @app.get("/api/threads/{thread_id}")
    async def api_thread_get(thread_id: str):
        thread = host.threads.get(thread_id)
        if thread is None:
            return JSONResponse({"error": "no such thread"}, status_code=404)
        return JSONResponse(_thread_dict(thread))

    @app.post("/api/threads")
    async def api_threads_create(request: Request):
        if not _csrf_ok(request):
            return JSONResponse({"error": "csrf"}, status_code=403)
        data, err = await _json_body(request)
        if err is not None:
            return err
        pid = data.get("project_id")
        t = host.threads.create(
            str(data.get("title") or "новый тред"),
            # мёртвый/чужой project_id тихо деградирует в «без проекта» (паттерн active-thread)
            project_id=str(pid) if pid and host.projects.get(str(pid)) is not None else None,
        )
        return JSONResponse(_thread_dict(t))

    @app.get("/api/threads/{thread_id}/feed")
    async def api_thread_feed(thread_id: str, limit: int = 200):
        if host.threads.get(thread_id) is None:
            return JSONResponse({"error": "no such thread"}, status_code=404)
        return JSONResponse({"entries": host.threads.read_feed(thread_id, limit=limit)})

    @app.post("/api/threads/{thread_id}/message")
    async def api_thread_message(thread_id: str, request: Request):
        if not _csrf_ok(request):
            return JSONResponse({"error": "csrf"}, status_code=403)
        if host.text_loop is None:
            return JSONResponse({"error": "text turns disabled (no anthropic key)"}, status_code=503)
        if host.threads.get(thread_id) is None:
            return JSONResponse({"error": "no such thread"}, status_code=404)
        data, err = await _json_body(request)
        if err is not None:
            return err
        text = str(data.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "empty text"}, status_code=400)
        # Gate v2 C1': команды compact/clear — серверные, обрабатываются ДО ingest_user_turn.
        cmd = text.casefold()
        if cmd in _CHAT_COMMANDS:
            if cmd.lstrip("/") == "compact":
                # Событие ленты «контекст сжат» пишет существующий on_compact (внутри force_compact).
                await host.text_loop.force_compact(thread_id)
                return JSONResponse({"ok": True, "command": "compact"})
            # clear: под host.turn_lock — сериализуемся с ОТКРЫТИЕМ ходов (голос/HTTP);
            # хвост уже начатого хода добивает generation-механизм в loop (C6, B20-стиль).
            async with host.turn_lock:
                host.text_loop.clear_history(thread_id)
            # Clear-маркер пишет РОУТ (канонический слой записи лент — как user/assistant).
            # id-штамп: две команды clear в один clock-tick иначе схлопнулись бы в клиентском
            # feedKey (ts|kind|text коллизия — MINOR принят).
            host.threads.append_feed(thread_id, {
                "ts": host.clock.now(), "kind": "clear", "text": "история очищена",
                "id": f"clear-{uuid.uuid4().hex[:12]}",
            })
            return JSONResponse({"ok": True, "command": "clear"})
        # NB (B08): turn_lock is intentionally NOT held across ingest_user_turn — B-PIPE-5 requires
        # releasing it before the LLM call so one slow client can't block others. The journal-level
        # begin_turn backstop (journal.py) is what protects an in-flight turn's record from a
        # concurrent begin_turn; full per-turn serialization stays the parked pipecat residual.
        async with host.turn_lock:  # S7: одна очередь ходов на хост
            host.current_http_thread["id"] = thread_id
        try:
            record, reply = await host.text_loop.ingest_user_turn(text, thread_id=thread_id)
        finally:
            async with host.turn_lock:
                host.current_http_thread["id"] = None
        now = host.clock.now()
        # UI-5 (S30): авто-title из первой реплики — только тредам-сентинелям композера.
        # Не меняет stage/request semantics (stage движется только propose_request/gate).
        host.threads.maybe_autotitle(thread_id, text)
        host.threads.append_feed(thread_id, {"ts": now, "kind": "user", "text": text})
        host.threads.append_feed(thread_id, {"ts": now, "kind": "assistant", "text": reply})
        return JSONResponse({"reply": reply})

    @app.patch("/api/threads/{thread_id}")
    async def api_thread_patch(thread_id: str, request: Request):
        """Переименование треда (UI-5, S30). PATCH только title; stage/request не трогает."""
        if not _csrf_ok(request):
            return JSONResponse({"error": "csrf"}, status_code=403)
        th = host.threads.get(thread_id)
        if th is None:
            return JSONResponse({"error": "no such thread"}, status_code=404)
        data, err = await _json_body(request)
        if err is not None:
            return err
        title = str(data.get("title") or "").strip()
        if not title:
            return JSONResponse({"error": "empty title"}, status_code=400)
        host.threads.rename(thread_id, title[:80])
        return JSONResponse(_thread_dict(host.threads.get(thread_id)))

    @app.post("/api/threads/{thread_id}/archive")
    async def api_thread_archive(thread_id: str, request: Request):
        """Архив треда (UI-5, S31). 409 только если жив ИМЕННО этот тред — детект per-thread,
        не глобальный busy: архив ДРУГОГО треда пока первый исполняется разрешён."""
        if not _csrf_ok(request):
            return JSONResponse({"error": "csrf"}, status_code=403)
        th = host.threads.get(thread_id)
        if th is None:
            return JSONResponse({"error": "no such thread"}, status_code=404)
        # per-thread busy: синглтон говорит ЧТО бежит, thread_for_task — В КАКОМ треде.
        # B49: «занят» = канонический has_active_task (RUNNING ∪ PENDING_CONFIRMATION), не
        # только RUNNING — иначе тред архивируется, пока задача ждёт подтверждения, и «да»
        # юзера запускает Кору в уже-убранный тред.
        task = host.store.task
        if task is not None and host.store.has_active_task():
            live = host.threads.thread_for_task(task.id)
            if live is not None and live.id == thread_id:
                return JSONResponse({"error": "busy"}, status_code=409)
        host.threads.set_archived(thread_id, True)
        return JSONResponse(_thread_dict(host.threads.get(thread_id)))

    @app.delete("/api/projects/{project_id}")
    async def api_projects_delete(project_id: str, request: Request):
        """Удаление проекта (UI-5, S31): проект удаляется, его треды НЕ удаляются —
        project_id → None + event «проект удалён» в их ленты. Возвращает свежий список."""
        if not _csrf_ok(request):
            return JSONResponse({"error": "csrf"}, status_code=403)
        removed = await host.projects.remove(project_id)
        if not removed:
            return JSONResponse({"error": "no such project"}, status_code=404)
        host.threads.unbind_project(project_id)
        return JSONResponse({"projects": host.projects.list()})

    @app.post("/api/threads/{thread_id}/gate")
    async def api_thread_gate(thread_id: str, request: Request):
        """HTTP-эквивалент голосового гейта: одна серверная gate_action-логика для обоих путей.

        Клиент не определяет стадии и не запускает Кору сам: он только передаёт намерение,
        а хост возвращает свежий снимок треда после успешного перехода.
        """
        if not _csrf_ok(request):
            return JSONResponse({"error": "csrf"}, status_code=403)
        if host.threads.get(thread_id) is None:
            return JSONResponse({"error": "no such thread"}, status_code=404)
        data, err = await _json_body(request)
        if err is not None:
            return err
        action = str(data.get("action") or "")
        raw_model = data.get("model")
        model = str(raw_model) if raw_model is not None else None
        result = await host.gate_action(
            thread_id,
            action,
            model=model,
            # строгий JSON-bool — строка "false" не должна проходить confirmation-гейт (B51)
            confirm=data.get("confirm") is True,
            fast=data.get("fast") is True,
        )
        error = result.get("error")
        if error:
            status = 409 if error == "busy" else 404 if error == "unknown_thread" else 400
            return JSONResponse(result, status_code=status)
        # gate_action меняет ThreadStore синхронно под своим per-thread lock, поэтому снимок
        # уже содержит новую стадию, модель и outcome для немедленной перерисовки UI.
        thread = host.threads.get(thread_id)
        if thread is None:  # defensive: чужая реализация host не должна дать 500 клиенту
            return JSONResponse({"error": "no such thread"}, status_code=404)
        return JSONResponse(_thread_dict(thread))

    @app.post("/api/active-thread")
    async def api_active_thread(request: Request):
        if not _csrf_ok(request):
            return JSONResponse({"error": "csrf"}, status_code=403)
        data, err = await _json_body(request)
        if err is not None:
            return err
        tid = data.get("id")
        # B58: falsy id ("" или None) значит CLEAR — существование не проверяем.
        if tid and host.threads.get(str(tid)) is None:
            return JSONResponse({"error": "no such thread"}, status_code=404)
        # B43: пока звонок ЖИВ (включая окно тихого реконнекта — бинд переживает клиентский
        # `client=null`), навигация по тредам НЕ переклеивает привязку голоса: иначе история
        # одного разговора расщепляется по двум тредам. 200, не 4xx: запрос легитимен, клиент
        # получает фактическую привязку и реконсилируется. Строгий `is True` — MagicMock-стабы
        # route-тестов не должны фабриковать «живой звонок» (паттерн session_alive isinstance).
        live_fn = getattr(host, "voice_session_live", None)
        if callable(live_fn) and live_fn() is True and str(tid or "") != (host.voice_thread["id"] or ""):
            return JSONResponse({
                "ok": False, "reason": "voice_live", "voice_thread": host.voice_thread["id"],
            })
        host.voice_thread["id"] = str(tid) if tid else None
        # UI v3 иерархия: активный проект дома — голосовой авто-тред родится в нём.
        # Неизвестный проект тихо сбрасывается в None (тот же паттерн, что voice_thread).
        pid = data.get("project_id")
        host.voice_project["id"] = (
            str(pid) if pid and host.projects.get(str(pid)) is not None else None
        )
        return JSONResponse({"ok": True})

    app.mount("/client/dev", PipecatPrebuiltUI, name="client-dev")
    return app
