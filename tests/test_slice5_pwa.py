"""M1 slice 5 — PWA manifest for /client (§2.2) + deterministic reconnect resync (§2.7).

/client/ = наш клиент (UI v2 слайс UI-1); патч-инжект в prebuilt удалён, prebuilt непатченный
на /client/dev. PWA-статика (manifest/иконки/reconnect-роуты) и §2.7-логика (session-alive,
resync_greeting, SpeakLedger.unspoken, on_client_connected-wiring) живы — роуты не изменились.

Plan v2 (run file `2026-07-12-synapse-slice5-pwa-reconnect.md` §5): the prebuilt bundle at
/client is wrapped (not forked) to inject PWA meta into its index.html; a new
`/client/session-alive` truth-endpoint replaces any wall-clock reload heuristic in
`reconnect.js` (R3/R4 dispositions — iOS suspends page timers, so elapsed-time guesses false-
positive on every ordinary wake); `TaskStore.resync_greeting`/`SpeakLedger.unspoken` back a
once-per-`run_session` resync greeting on `on_client_connected` (R1: pipecat re-emits
"connected" on same-connection ICE self-heals with no upstream dedup, so a `greeted` latch is
required or every wifi blip would truncate Kora's live turn).

Conventions follow tests/test_bughunt_w4.py / w5.py / test_bughunt_w1_dispatch_webrtc.py:
`_endpoint`/`_cells` closure introspection, `build_web_app(host=object())` for host-independent
routes, a bespoke `_FakeTransport` (`event_handler` stores the handler instead of discarding
it — plain w4/w5 fakes throw the decorated function away), monkeypatching webrtc_server module
globals before invoking `run_session`. No TestClient anywhere (route functions are called
directly, `Response`/`JSONResponse` bodies read off `.body`, not `.content`/`.text`).
"""
from __future__ import annotations

import asyncio
import io
import json
import types

import pytest
from PIL import Image

from synapse.bridge.state import EventClass, KoraEvent, SpeakLedger, TaskStatus, TaskStore
from synapse.clock import FakeClock
from synapse.prompt import CANON_PHRASE_STALE_KORA


def _webrtc_server_or_skip():
    pytest.importorskip("aiortc")
    pytest.importorskip("cv2")
    pytest.importorskip("fastapi")
    try:
        from synapse.pipeline import webrtc_server
    except (ImportError, RuntimeError) as e:
        pytest.skip(f"webrtc deps/prebuilt UI unavailable: {e}")
    return webrtc_server


def _endpoint(app, name):
    for route in app.routes:
        ep = getattr(route, "endpoint", None)
        if ep is not None and getattr(ep, "__name__", None) == name:
            return ep
    raise AssertionError(f"route endpoint {name!r} not found")


def _cells(fn) -> dict:
    return dict(zip(fn.__code__.co_freevars, [c.cell_contents for c in (fn.__closure__ or ())]))


# ================================================================================================
# §2.2 — /client/manifest.webmanifest
# ================================================================================================
async def test_manifest_route_serves_pwa_fields():
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    resp = await _endpoint(app, "client_manifest")()

    assert resp.status_code == 200
    assert resp.media_type == "application/manifest+json"
    data = json.loads(resp.body)
    assert data["name"] == "Синапс"
    assert data["start_url"] == "/client/"
    assert data["display"] == "standalone"
    sizes = {icon["sizes"] for icon in data["icons"]}
    assert sizes == {"192x192", "512x512"}


# ================================================================================================
# §2.2 — icon routes: valid PNG bytes at the declared sizes
# ================================================================================================
@pytest.mark.parametrize(
    "endpoint_name,size",
    [("client_icon_192", 192), ("client_icon_512", 512), ("client_apple_touch_icon", 180)],
)
async def test_icon_routes_serve_correctly_sized_png(endpoint_name, size):
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    resp = await _endpoint(app, endpoint_name)()

    assert resp.status_code == 200
    assert resp.media_type == "image/png"
    assert resp.body[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(resp.body))
    assert img.size == (size, size)


# ================================================================================================
# §2.7 — вотчдог: truth-poll, no wall-clock reload logic. UI v3: носитель — app.js (reconnect.js
# умер; свой клиент реконнектится на месте, reload — последний резерв по прежним правилам).
# ================================================================================================
async def test_app_js_carries_watchdog_primitives():
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    resp = await _endpoint(app, "client_app_js")()

    assert resp.status_code == 200
    assert resp.media_type == "text/javascript"
    body = resp.body.decode("utf-8")
    for token in ("session-alive", "location.reload", "sessionStorage", "visibilitychange",
                  "probeSession"):
        assert token in body, f"app.js watchdog missing expected token {token!r}"
    names = {getattr(getattr(r, "endpoint", None), "__name__", "") for r in app.routes}
    assert "client_reconnect_js" not in names


# ================================================================================================
# §2.7 — /client/session-alive reflects `current["task"]` truth (no wall clock)
# ================================================================================================
async def test_session_alive_reflects_current_task_truth():
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    offer_ep = _endpoint(app, "offer")
    handle_offer = _cells(offer_ep)["_handle_offer"]
    run_session = _cells(handle_offer)["run_session"]
    current = _cells(run_session)["current"]

    resp = await _endpoint(app, "session_alive")()
    assert json.loads(resp.body) == {"active": False}

    current["task"] = object()
    resp2 = await _endpoint(app, "session_alive")()
    assert json.loads(resp2.body) == {"active": True}


# ================================================================================================
# §2.7 — TaskStore.resync_greeting
# ================================================================================================
def test_resync_greeting_is_none_without_a_task():
    store = TaskStore(FakeClock(0.0))
    assert store.resync_greeting(0.0, 120, 300) is None


def test_resync_greeting_running_task():
    store = TaskStore(FakeClock(0.0))
    store.start_task("t1", "скачать отчёт", TaskStatus.RUNNING, now=0.0)
    store.heartbeat(0.0)
    greeting = store.resync_greeting(1.0, 120, 300)
    assert greeting == (
        "С возвращением. Задача «скачать отчёт»: "
        "Задача выполняется, сигнала о завершении пока не было."
    )


def test_resync_greeting_truncates_long_task_text():
    store = TaskStore(FakeClock(0.0))
    long_text = "а" * 61
    store.start_task("t1", long_text, TaskStatus.RUNNING, now=0.0)
    store.heartbeat(0.0)
    greeting = store.resync_greeting(1.0, 120, 300)
    assert f"«{'а' * 60}…»" in greeting
    assert long_text not in greeting


def test_resync_greeting_awaiting_suffix():
    store = TaskStore(FakeClock(0.0))
    store.start_task("t1", "задача", TaskStatus.RUNNING, now=0.0)
    store.heartbeat(0.0)
    store.set_awaiting()
    greeting = store.resync_greeting(1.0, 120, 300)
    assert greeting.endswith("Кора ждёт твоего ответа на свой вопрос.")


def test_resync_greeting_stale_suffix_is_canon_phrase():
    store = TaskStore(FakeClock(0.0))
    store.start_task("t1", "задача", TaskStatus.RUNNING, now=0.0)
    store.heartbeat(0.0)
    greeting = store.resync_greeting(10_000.0, 120, 300)
    assert greeting.endswith(CANON_PHRASE_STALE_KORA)


# ================================================================================================
# §2.7 — SpeakLedger.unspoken
# ================================================================================================
def test_unspoken_returns_old_unspoken_critical_with_speak_text():
    ledger = SpeakLedger()
    ev = KoraEvent(id="e1", type="task_completed", cls=EventClass.CRITICAL, payload={}, speak_text="готово", ts=0.0)
    ledger.register_critical(ev)
    assert ledger.unspoken(now=100.0, min_age_s=5.0) == [ev]


def test_unspoken_excludes_already_spoken():
    ledger = SpeakLedger()
    ev = KoraEvent(id="e1", type="task_completed", cls=EventClass.CRITICAL, payload={}, speak_text="готово", ts=0.0)
    ledger.register_critical(ev)
    ledger.register_speak("e1", ts=1.0)
    assert ledger.unspoken(now=100.0, min_age_s=5.0) == []


def test_unspoken_excludes_events_without_speak_text():
    ledger = SpeakLedger()
    ev = KoraEvent(id="e1", type="task_completed", cls=EventClass.CRITICAL, payload={}, speak_text=None, ts=0.0)
    ledger.register_critical(ev)
    assert ledger.unspoken(now=100.0, min_age_s=5.0) == []


def test_unspoken_excludes_events_younger_than_min_age():
    ledger = SpeakLedger()
    ev = KoraEvent(id="e1", type="task_completed", cls=EventClass.CRITICAL, payload={}, speak_text="готово", ts=98.0)
    ledger.register_critical(ev)
    # age = 100 - 98 = 2s < min_age_s=5.0 — organic on_speak may still be in flight (R2).
    assert ledger.unspoken(now=100.0, min_age_s=5.0) == []


def test_unspoken_includes_alerted_but_still_unspoken():
    ledger = SpeakLedger()
    ev = KoraEvent(id="e1", type="task_completed", cls=EventClass.CRITICAL, payload={}, speak_text="готово", ts=0.0)
    ledger.register_critical(ev)
    ledger.check(now=1000.0, window_s=5.0)  # marks alerted=True, still not spoken
    assert ledger.unspoken(now=1000.0, min_age_s=5.0) == [ev]


# ================================================================================================
# §2.7 — run_session wiring: on_client_connected drives resync_greeting/unspoken through host
# ================================================================================================
class _RecordingHost:
    """Stub host: real TaskStore/SpeakLedger (so resync_greeting/unspoken run for real), but
    push_speak_frame/speak/bind_output/unbind_output/monitor_forever are stubs — this test is
    about the WIRING (what run_session calls, how many times), not about a live PipelineTask."""

    def __init__(self, clock, store, speak_ledger, cfg):
        self.clock = clock
        self.store = store
        self.speak_ledger = speak_ledger
        self.cfg = cfg
        self.push_speak_calls: list[str] = []
        self.speak_calls: list[str] = []

    async def push_speak_frame(self, text: str) -> None:
        self.push_speak_calls.append(text)

    def speak(self, text: str) -> None:
        self.speak_calls.append(text)

    def bind_output(self, task) -> None:
        pass

    def unbind_output(self, task) -> None:
        pass

    async def monitor_forever(self) -> None:
        await asyncio.sleep(3600)


class _FakeTransport:
    """Unlike the plain w4/w5 fakes (whose `event_handler` decorator discards the function),
    this one RECORDS every registered handler by event name (P2 disposition) so the test can
    invoke `on_client_connected` directly, the way a real ICE self-heal or reconnect would."""

    def __init__(self, **kwargs):
        self.handlers: dict[str, object] = {}

    def input(self):
        return object()

    def output(self):
        return object()

    def event_handler(self, name):
        def deco(fn):
            self.handlers[name] = fn
            return fn
        return deco


class _FakeRunner:
    def __init__(self, handle_sigint=False):
        pass

    async def run(self, task):
        return None  # completes immediately, like a session with no live transport


async def _run_session_and_capture_handlers(monkeypatch, webrtc_server, host, session_id):
    created: list[_FakeTransport] = []

    class _CapturingTransport(_FakeTransport):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            created.append(self)

    new_task = types.SimpleNamespace(has_finished=lambda: False, queue_frame=lambda *a, **k: None)

    monkeypatch.setattr(
        webrtc_server, "build_session_pipeline", lambda _host: types.SimpleNamespace(pipeline=object())
    )
    monkeypatch.setattr(webrtc_server, "SmallWebRTCTransport", _CapturingTransport)
    monkeypatch.setattr(webrtc_server, "TransportParams", lambda **k: object())
    monkeypatch.setattr(webrtc_server, "Pipeline", lambda *a, **k: object())
    monkeypatch.setattr(webrtc_server, "PipelineTask", lambda *a, **k: new_task)
    monkeypatch.setattr(webrtc_server, "PipelineRunner", _FakeRunner)

    app = webrtc_server.build_web_app(host=host)
    offer_ep = _endpoint(app, "offer")
    handle_offer = _cells(offer_ep)["_handle_offer"]
    run_session = _cells(handle_offer)["run_session"]

    await run_session(object(), session_id=session_id)
    assert created, "SmallWebRTCTransport was never constructed by run_session"
    return created[-1].handlers


async def test_on_client_connected_greets_exactly_once_per_run_session(monkeypatch):
    webrtc_server = _webrtc_server_or_skip()
    clock = FakeClock(100.0)
    store = TaskStore(clock)
    store.start_task("t1", "задача", TaskStatus.RUNNING, now=0.0)
    store.heartbeat(0.0)
    cfg = types.SimpleNamespace(stale_after_s=120.0, unreachable_after_s=300.0)
    host = _RecordingHost(clock, store, SpeakLedger(), cfg)

    handlers = await _run_session_and_capture_handlers(monkeypatch, webrtc_server, host, "sess-connected-1")
    on_connected = handlers["on_client_connected"]

    # Two "connected" fires (e.g. an ICE self-heal re-firing on the SAME connection, R1) must
    # only greet once — the latch is per run_session, not per event.
    await on_connected(None, None)
    await on_connected(None, None)

    assert len(host.push_speak_calls) == 1, (
        f"on_client_connected re-greeted more than once per run_session: {host.push_speak_calls!r}"
    )
    assert host.push_speak_calls[0].startswith("С возвращением. Задача «задача»:")


async def test_on_client_connected_no_task_no_greeting(monkeypatch):
    webrtc_server = _webrtc_server_or_skip()
    clock = FakeClock(100.0)
    store = TaskStore(clock)  # no task started -- virgin host
    cfg = types.SimpleNamespace(stale_after_s=120.0, unreachable_after_s=300.0)
    host = _RecordingHost(clock, store, SpeakLedger(), cfg)

    handlers = await _run_session_and_capture_handlers(monkeypatch, webrtc_server, host, "sess-connected-2")
    await handlers["on_client_connected"](None, None)

    assert host.push_speak_calls == []


async def test_on_client_connected_replays_undelivered_aged_critical(monkeypatch):
    webrtc_server = _webrtc_server_or_skip()
    clock = FakeClock(100.0)
    store = TaskStore(clock)  # no task -- isolates the replay path from the greeting path
    ledger = SpeakLedger()
    ev = KoraEvent(
        id="e1", type="task_completed", cls=EventClass.CRITICAL, payload={},
        speak_text="Готово: файл удалён.", ts=0.0,  # age 100s at clock.now() >> min_age_s=5.0
    )
    ledger.register_critical(ev)
    cfg = types.SimpleNamespace(stale_after_s=120.0, unreachable_after_s=300.0)
    host = _RecordingHost(clock, store, ledger, cfg)

    handlers = await _run_session_and_capture_handlers(monkeypatch, webrtc_server, host, "sess-connected-3")
    await handlers["on_client_connected"](None, None)

    assert host.speak_calls == ["Готово: файл удалён."]
