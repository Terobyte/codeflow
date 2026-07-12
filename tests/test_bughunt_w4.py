"""Bug-hunt wave-4 RED regression tests — monitor liveness alert, HTTP-date Retry-After,
WebRTC setup-window cleanup leak.

Frozen tree per bugs.md (Wave 3 done at 193 tests). One test per bug ID; each asserts the
*post-fix* behavior, so all three FAIL RED against the current (unfixed) code, and each is
written to fail AT ITS OWN ASSERTION — never on import/collection/fixture.

  B12 (pipeline/app.py `monitor_forever`): Р-11 between-turns liveness is decorative — the
     monitor calls `store.liveness(...)` and THROWS THE RESULT AWAY; there is no AlertKind /
     SPEAK for a STALE/UNREACHABLE Kora anywhere. A Kora that dies between turns emits nothing.
     Post-fix: when liveness degrades in the monitor loop, an alert is emitted on the journal.

  B41 (cascade/classify.py `_retry_after_header`): an HTTP-date-form `Retry-After` (RFC 1123,
     e.g. "Wed, 21 Oct 2026 07:28:00 GMT") hits `float(value)` → ValueError → silently None →
     the caller degrades to the 60s default, ignoring the provider's real window.
     Post-fix: the date form is parsed (email.utils) into a positive delay-in-seconds.

  B24 (pipeline/webrtc_server.py `run_session`): `current["task"] = task` + `host.bind_output`
     are published OUTSIDE the cleanup try/finally, and `old.cancel()` / the monitor-future
     setup run before the `try` too. A raise in that setup window skips the finally → the bind
     slot, the `current` publish, and the `active_sessions` entry all leak.
     Post-fix: cleanup runs even when a setup-window operation raises.
"""
from __future__ import annotations

import asyncio
import types
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from synapse.bridge.state import Liveness, TaskStore
from synapse.cascade.breaker import ErrorKind
from synapse.cascade.classify import classify_error
from synapse.clock import FakeClock
from synapse.pipeline.app import SynapseHost


# ============================================================================================
# B12 — monitor_forever discards liveness(); no stale/unreachable alert exists (app.py:159)
# ============================================================================================
class _RecordingJournal:
    """Duck-types just `alert(kind, detail=None)` and records every call verbatim (no AlertKind
    validation, so the fix is free to introduce a new kind without this fake needing to know it)."""

    def __init__(self) -> None:
        self.alerts: list[tuple] = []

    def alert(self, kind, detail=None) -> None:
        self.alerts.append((kind, detail))


class _EmptyLedger:
    """`check()` returns no critical-without-speak alerts, so the ONLY thing that can put an
    alert on the journal during a monitor iteration is the liveness-degradation path (B12)."""

    def check(self, now, window_s):
        return []


class _NoopCostCap:
    def maybe_reset(self, now) -> None:
        pass


def _b12_host() -> tuple[SynapseHost, _RecordingJournal]:
    journal = _RecordingJournal()
    # Real TaskStore so liveness() genuinely computes UNREACHABLE from real thresholds: a signal
    # at ts=0.0, monitor "now" at 10_000 → age 10_000 ≥ unreachable_after_s (300) → UNREACHABLE.
    store = TaskStore(FakeClock(0.0), journal_dir=None)
    store.heartbeat(0.0)
    cfg = types.SimpleNamespace(
        heartbeat_interval_s=30.0,   # unused: asyncio.sleep is patched below
        critical_speak_window_s=5.0,
        stale_after_s=120.0,
        unreachable_after_s=300.0,
    )
    host = SynapseHost(
        clock=FakeClock(start=10_000.0),  # monitor "now" → store reports UNREACHABLE
        cfg=cfg,
        journal=journal,
        store=store,
        speak_ledger=_EmptyLedger(),
        classifier=None,
        confirm_flow=None,
        arbiter_policy=None,
        bridge=None,
        handlers=None,
        breaker=None,
        cost_cap=_NoopCostCap(),
    )
    return host, journal


async def test_b12_monitor_emits_alert_when_kora_liveness_is_unreachable(monkeypatch):
    host, journal = _b12_host()

    # Premise: the store really is UNREACHABLE at the monitor's "now" — so a live monitor sees a
    # degraded Kora on its very first iteration. (Guards against a silently-OK fixture false pass.)
    assert host.store.liveness(10_000.0, 120.0, 300.0) == Liveness.UNREACHABLE

    # Drive monitor_forever deterministically: first sleep returns (→ one full loop-body run that
    # observes the UNREACHABLE liveness), second sleep raises CancelledError to end the loop.
    sleep_calls = {"n": 0}

    async def fake_sleep(_seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr("synapse.pipeline.app.asyncio.sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await host.monitor_forever()

    # POST-FIX: a degraded (stale/unreachable) liveness in the monitor emits a journal alert.
    # CURRENTLY: liveness()'s result is discarded and no such AlertKind exists → zero alerts → RED.
    assert journal.alerts, (
        "monitor_forever observed an UNREACHABLE Kora but emitted NO alert — Р-11 between-turns "
        "liveness is decorative (B12): liveness() result discarded, no stale/unreachable alert"
    )
    # Tie the emitted alert to the liveness-degradation cause (not some unrelated alert): its kind
    # name or detail must reference the degraded state or Kora liveness.
    blob = " ".join(f"{kind}|{detail}" for kind, detail in journal.alerts).lower()
    assert any(term in blob for term in ("unreachable", "stale", "kora", "liveness")), (
        f"an alert fired but none references the degraded Kora liveness: {journal.alerts!r}"
    )


# ============================================================================================
# B41 — HTTP-date-form Retry-After silently degrades to None (classify.py:79-86)
# ============================================================================================
def test_b41_http_date_retry_after_is_parsed_not_dropped_to_none():
    # A well-formed RFC-1123 / HTTP-date Retry-After a few minutes in the future. Providers are
    # allowed to send this form instead of a delta-seconds integer (RFC 9110 §10.2.3).
    future = datetime.now(timezone.utc) + timedelta(seconds=300)
    http_date = format_datetime(future, usegmt=True)  # e.g. "Sun, 12 Jul 2026 05:30:00 GMT"

    kind, retry_after = classify_error(429, body={}, headers={"Retry-After": http_date})

    # kind is RPM either way (OpenRouter-style header-only 429); the regression is retry_after.
    assert kind == ErrorKind.RPM
    # POST-FIX: the date is parsed into a positive delay. CURRENTLY: float("Sun, 12 Jul ...")
    # raises ValueError → _retry_after_header returns None → the provider's real window is lost. RED.
    assert retry_after is not None, (
        "HTTP-date-form Retry-After was silently dropped to None (B41) — the caller now uses the "
        "60s default and ignores the provider's real ~300s window"
    )
    assert retry_after > 0, f"parsed retry_after must be a positive delay, got {retry_after!r}"
    assert retry_after < 3600, (
        f"retry_after should reflect the date DELTA (~300s), not an absolute epoch: {retry_after!r}"
    )


# ============================================================================================
# B24 — run_session publishes bind/current OUTSIDE the cleanup try/finally (webrtc_server.py)
# ============================================================================================
def _endpoint(app, name):
    for route in app.routes:
        ep = getattr(route, "endpoint", None)
        if ep is not None and getattr(ep, "__name__", None) == name:
            return ep
    raise AssertionError(f"route endpoint {name!r} not found")


def _cells(fn) -> dict:
    return dict(zip(fn.__code__.co_freevars, [c.cell_contents for c in (fn.__closure__ or ())]))


def _bare_host() -> SynapseHost:
    # A real SynapseHost with all-None collaborators (like tests/test_push.py::_host): only its
    # real bind_output/unbind_output/_output_task semantics matter here.
    return SynapseHost(
        clock=None, cfg=None, journal=None, store=None, speak_ledger=None, classifier=None,
        confirm_flow=None, arbiter_policy=None, bridge=None, handlers=None, breaker=None,
        cost_cap=None,
    )


class _SetupBoom(RuntimeError):
    """Raised by the preempted task's cancel() to simulate a failure in run_session's setup window
    (any raise before the try/finally — old.cancel, ensure_future, PipelineRunner — has the same
    leak signature)."""


async def test_b24_setup_window_raise_still_runs_session_cleanup(monkeypatch):
    pytest.importorskip("aiortc")
    pytest.importorskip("cv2")
    pytest.importorskip("fastapi")
    try:
        from synapse.pipeline import webrtc_server
    except (ImportError, RuntimeError) as e:
        pytest.skip(f"webrtc deps/prebuilt UI unavailable: {e}")

    host = _bare_host()
    app = webrtc_server.build_web_app(host=host)

    # Reach the run_session closure and its build_web_app-local state (current/active_sessions).
    offer_ep = _endpoint(app, "offer")
    handle_offer = _cells(offer_ep)["_handle_offer"]
    run_session = _cells(handle_offer)["run_session"]
    rs_cells = _cells(run_session)
    current = rs_cells["current"]
    active_sessions = rs_cells["active_sessions"]
    assert rs_cells["host"] is host, "premise: run_session closes over the host we built the app with"

    # Stub the pipecat surface so run_session reaches the setup window with a freshly-bound task,
    # without a live transport/runner. None of these can fail spuriously → the ONLY deterministic
    # raise is old.cancel() in the setup window.
    new_task = types.SimpleNamespace(
        has_finished=lambda: False,
        queue_frame=lambda *a, **k: None,
    )

    class _FakeTransport:
        def __init__(self, **kwargs):
            pass

        def input(self):
            return object()

        def output(self):
            return object()

        def event_handler(self, _name):
            def deco(fn):
                return fn
            return deco

    monkeypatch.setattr(
        webrtc_server, "build_session_pipeline", lambda _host: types.SimpleNamespace(pipeline=object())
    )
    monkeypatch.setattr(webrtc_server, "SmallWebRTCTransport", _FakeTransport)
    monkeypatch.setattr(webrtc_server, "TransportParams", lambda **k: object())
    monkeypatch.setattr(webrtc_server, "Pipeline", lambda *a, **k: object())
    monkeypatch.setattr(webrtc_server, "PipelineTask", lambda *a, **k: new_task)

    # A preempted "old" task already published/bound, whose cancel() blows up in the setup window
    # (this is `await old.cancel(...)`, which sits BEFORE run_session's try).
    class _OldTask:
        async def cancel(self, reason=None):
            raise _SetupBoom("old.cancel failed in the setup window")

    old_task = _OldTask()
    current["task"] = old_task
    session_id = "sess-b24"
    active_sessions[session_id] = {"body": {}}

    # Run the session: the setup-window raise should propagate (or, if the fix swallows it, return).
    # Either way the cleanup must have run — that is what we assert below.
    try:
        await run_session(object(), session_id=session_id)
    except _SetupBoom:
        pass

    # POST-FIX: the bind/publish live inside the protected region, so a setup-window raise still
    # unbinds the output task, clears `current`, and pops the session. CURRENTLY they are OUTSIDE
    # the try/finally → the raise skips cleanup and every slot leaks → RED (first assert below).
    assert host._output_task is None, (
        "SPEAK injector left bound to a task whose session never ran — setup-window raise skipped "
        "host.unbind_output (B24: bind published outside the cleanup try/finally)"
    )
    assert current["task"] is None, (
        f"current['task'] left published after a failed setup window: {current['task']!r}"
    )
    assert session_id not in active_sessions, (
        "active_sessions entry leaked — run_session's finally never popped it after a setup-window raise"
    )
