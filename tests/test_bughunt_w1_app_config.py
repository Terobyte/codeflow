"""Wave-1 bug-hunt regression tests (FAILING/red until the fixes land).

One test per bug ID from bugs.md, each asserting POST-FIX behavior so it fails RED against
tree `a8dd919`:

  B4 -- config.from_env: a malformed KORA_MAX_TURNS must fall back to the default, not crash.
  B6 -- prompt._apply_owed_additions: an anchor that drifts must be SIGNALLED, not silently dropped.
  B2 -- app.monitor_forever: a raising loop body must not kill the (sole voice-path) invariant driver.
  B9 -- app.speak: a failed out-of-band injection must be OBSERVABLE, not silently swallowed.

These import only public/importable surfaces (SynapseHost, SynapseConfig, prompt helpers) and drive
them with tiny duck-typed fakes -- no pipecat task, no live server, mirroring tests/test_push.py.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from synapse.config import SynapseConfig
from synapse.pipeline.app import SynapseHost
from synapse.prompt import PROMPT_V3, _apply_owed_additions


# --------------------------------------------------------------------------------------------------
# Shared minimal fakes (only the members each surface actually touches)
# --------------------------------------------------------------------------------------------------

class _Clock:
    def now(self) -> float:
        return 42.0


class _RecordingLedger:
    """speak_ledger stand-in: records register_speak_text so speak() has something to call."""

    def __init__(self) -> None:
        self.registered: list = []

    def register_speak_text(self, text: str, ts: float) -> None:
        self.registered.append((text, ts))


class _RecordingJournal:
    """journal stand-in: records alert() calls -- the codebase's canonical observability hook."""

    def __init__(self) -> None:
        self.alerts: list = []

    def alert(self, kind, detail=None) -> None:
        self.alerts.append((kind, detail))


class _FakeArbiter:
    def __init__(self) -> None:
        self.spoken: list = []

    def push_speak(self, text: str) -> None:
        self.spoken.append(text)


def _bare_host(**overrides) -> SynapseHost:
    """SynapseHost with fakes for only the collaborators a test reads; None for the rest.
    Mirrors the `_host()` helper in tests/test_push.py. `_output_task` starts None."""
    kwargs = dict(
        clock=_Clock(),
        cfg=None,
        journal=None,
        store=None,
        speak_ledger=_RecordingLedger(),
        classifier=None,
        confirm_flow=None,
        arbiter_policy=_FakeArbiter(),
        bridge=None,
        handlers=None,
        breaker=None,
        cost_cap=None,
    )
    kwargs.update(overrides)
    return SynapseHost(**kwargs)


# --------------------------------------------------------------------------------------------------
# B4 -- config.from_env unguarded int()/float() crashes the whole app on a malformed env value
# --------------------------------------------------------------------------------------------------

def test_b4_from_env_malformed_kora_max_turns_falls_back_to_default():
    """POST-FIX: a garbage KORA_MAX_TURNS must NOT take down from_env(); it must keep the
    dataclass default (40). Currently `int("forty")` raises ValueError out of from_env -> red."""
    cfg = SynapseConfig.from_env(env={"KORA_MAX_TURNS": "forty"})

    # from_env survived (no ValueError) AND the malformed value was ignored in favour of the default.
    assert cfg.kora_max_turns == SynapseConfig().kora_max_turns == 40


# --------------------------------------------------------------------------------------------------
# B6 -- prompt anchor-insertion silently no-ops if PROMPT_V3 wording drifts -> OWED rules vanish
# --------------------------------------------------------------------------------------------------

def test_b6_owed_additions_signals_missing_anchor_instead_of_silent_noop():
    """POST-FIX: if an anchor the OWED-rule insertion relies on has drifted (is no longer present
    verbatim), _apply_owed_additions must SIGNAL it (raise) rather than silently drop the safety
    rules. Simulate drift by mutating one real anchor (semicolon -> period). Currently the
    str.replace is a no-op and nothing is raised -> red at the pytest.raises."""
    # Drift the possibility-«а» anchor exactly as an innocent PROMPT_V3 edit might.
    drifted = PROMPT_V3.replace(
        "а) принять новую задачу и передать её Коре;",
        "а) принять новую задачу и передать её Коре.",
    )
    # Fixture sanity: the anchor really was present, so `drifted` really is a drifted copy.
    assert drifted != PROMPT_V3, "test setup stale: possibility-«а» anchor not found in PROMPT_V3"

    with pytest.raises(Exception):
        _apply_owed_additions(drifted)


# --------------------------------------------------------------------------------------------------
# B2 -- monitor_forever dies permanently (silently) on any loop-body exception
# --------------------------------------------------------------------------------------------------

class _RaiseOnceLedger:
    """speak_ledger.check raises the first time (e.g. journal.alert -> os.fsync OSError), []after."""

    def __init__(self) -> None:
        self.check_calls = 0

    def check(self, now, window_s):
        self.check_calls += 1
        if self.check_calls == 1:
            raise RuntimeError("os.fsync boom in the loop body")
        return []


class _CountingStore:
    def __init__(self) -> None:
        self.liveness_calls = 0

    def liveness(self, now, stale_after_s, unreachable_after_s):
        self.liveness_calls += 1
        return None


class _MonitorCfg:
    heartbeat_interval_s = 0.0
    critical_speak_window_s = 5.0
    stale_after_s = 120.0
    unreachable_after_s = 300.0


async def test_b2_monitor_forever_survives_a_raising_loop_body(monkeypatch):
    """POST-FIX: a raising check() in one iteration must be caught/logged and the loop must keep
    driving speak_ledger.check()/store.liveness() (the SOLE voice-path Р-15г/Р-11 driver). A real
    cancellation (CancelledError) must still tear the loop down cleanly.

    Currently the unguarded `while True` lets the RuntimeError propagate out of monitor_forever
    -> the loop dies and store.liveness() is never reached again -> red at `assert raised is None`."""
    ledger = _RaiseOnceLedger()
    store = _CountingStore()
    host = _bare_host(
        cfg=_MonitorCfg(),
        journal=_RecordingJournal(),
        store=store,
        speak_ledger=ledger,
        arbiter_policy=None,
    )

    # Drive the `while True` deterministically: 1st/2nd sleeps return, the 3rd cancels (clean stop).
    calls = {"n": 0}

    async def fake_sleep(delay):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    raised: BaseException | None = None
    try:
        await host.monitor_forever()
    except asyncio.CancelledError:
        pass  # expected clean shutdown once the fix re-raises CancelledError
    except BaseException as exc:  # noqa: BLE001 -- we WANT to observe a leaked body exception
        raised = exc

    assert raised is None, f"monitor_forever died on a loop-body exception instead of surviving: {raised!r}"
    assert ledger.check_calls >= 2, "check() was not retried after it raised -> the loop did not survive"
    assert store.liveness_calls >= 1, "store.liveness() never ran after the raising iteration -> loop dead"


# --------------------------------------------------------------------------------------------------
# B9 -- speak() fire-and-forget ensure_future swallows exceptions + drops the SPEAK
# --------------------------------------------------------------------------------------------------

class _RaisingTask:
    """Live output task whose out-of-band injection fails (teardown/cancel race)."""

    def has_finished(self) -> bool:
        return False

    async def queue_frame(self, frame, direction=None) -> None:
        raise RuntimeError("queue_frame boom during injection (teardown race)")


async def test_b9_speak_injection_failure_is_observable(caplog):
    """POST-FIX: when the scheduled push_speak_frame injection raises, the failure must be
    OBSERVABLE -- surfaced via a done-callback that alerts (journal.alert) and/or logs at WARNING+
    -- rather than a silently swallowed ensure_future whose exception is never retrieved.

    Currently there is no done-callback, so the exception dies inside the orphaned task and nothing
    records it -> red at `assert observed`."""
    journal = _RecordingJournal()
    host = _bare_host(journal=journal)
    host.bind_output(_RaisingTask())

    with caplog.at_level(logging.DEBUG):
        host.speak("готово")            # schedules ensure_future(push_speak_frame(...))
        await asyncio.sleep(0)           # let the doomed injection run and raise
        await asyncio.sleep(0)           # let any done-callback fire

    # Any observable trace the fix's done-callback could leave: a journal alert, or a real
    # (non-asyncio) log record at WARNING or above. Exclude asyncio's own GC "Task exception was
    # never retrieved" noise so we test the fix's hook, not the interpreter's fallback.
    logged = any(r.levelno >= logging.WARNING and r.name != "asyncio" for r in caplog.records)
    observed = bool(journal.alerts) or logged

    assert observed, (
        "push_speak_frame injection failure was silently swallowed -- no done-callback "
        "observed it (no journal.alert, no WARNING+ log). The lost SPEAK is now invisible "
        "AND, because the ledger was marked spoken first, CRITICAL_WITHOUT_SPEAK can never fire."
    )
