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
import uuid

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import RedirectResponse
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

from synapse.pipeline.app import SynapseHost, build_session_pipeline


def build_web_app(host: SynapseHost) -> FastAPI:
    """FastAPI app: POST/PATCH /api/offer drive pipecat's SmallWebRTC signaling, the prebuilt
    browser client is mounted at /client. `host` is the ONE long-lived `SynapseHost` (built once
    by `run()`, or by the caller); this function does no key validation or network I/O itself,
    so a stub host works fine for route-only tests. Each new browser offer spins one fresh
    per-connection `SynapseSession` (`run_session`) wired to `host`, and preempts whichever
    session was previously active (DoD-2: exactly one live client)."""
    app = FastAPI()
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
        if old is not None:
            await old.cancel(reason="preempted by new connection")

        monitor = asyncio.ensure_future(host.monitor_forever())
        try:
            await PipelineRunner(handle_sigint=False).run(task)  # M2: leave SIGINT to uvicorn
        finally:
            monitor.cancel()
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
        try:
            data = await request.json()
        except Exception:
            data = {}
        session_id = str(uuid.uuid4())
        active_sessions[session_id] = data.get("body", {})
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

    app.mount("/client", PipecatPrebuiltUI, name="client")
    return app
