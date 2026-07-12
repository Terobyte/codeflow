"""Bug-hunt wave-5 RED regression tests — dispatcher bounded tool loop, awaiting-liveness gate,
critical-registration hoist, secret-path coverage, WebRTC /start body parsing, config empty-env
handling, MockLLM affirm/deny word-set overreach, journal close-guard, monitor-task leak.

Frozen tree per bugs.md (Wave 4 done at 196 tests). One (or a small cluster of) test(s) per bug
ID; each asserts the *post-fix* behavior, written to fail AT ITS OWN ASSERTION on unpatched code
— never on import/collection/fixture.

  B10 (dispatcher/loop.py `ingest_user_turn`): the old shape was a strict two-pass turn — a
     second completion after tool dispatch, then `text, _ = ...` silently dropping any further
     tool_calls. A chaining LLM (get_task_status -> request_cancel) lost the follow-up call.
     Post-fix: bounded while-loop, capped by `_MAX_TOOL_PASSES`. MockLLM cannot exercise this
     (`_respond_to_tool_result` always returns `[]`) — these tests define their OWN stub LLM.

  B19 (bridge/state.py `liveness`): `_awaiting_answer` reported OK unconditionally, unlike
     render_state/snapshot which gate on `task.status == RUNNING`. After `request_cancel` flips
     status synchronously (the flag is cleared later, in `_handle_question`'s finally), a stale
     awaiting flag kept reporting OK through the cancel window.
     Post-fix: gated on `task is None or task.status == RUNNING` (no-task+awaiting stays OK —
     mirrors the frozen `test_answer_kora.py::test_liveness_ok_while_awaiting_even_when_stale`).

  B20 (bridge/kora.py `apply_event_to_store`): critical registration lived INSIDE the
     lifecycle-only branch — a non-lifecycle CRITICAL event only heartbeats the store and never
     arms the SpeakLedger, so a forgotten SPEAK on such an event silently vanishes instead of
     tripping Р-15г's CRITICAL_WITHOUT_SPEAK watchdog. Post-fix: critical registration hoisted
     OUT of the lifecycle gate; SPEAK dispatch stays lifecycle-only (NO-EXFIL backstop intact).

  B22 (bridge/kora.py `_is_secret_path`): the secret-containment denylist missed `prod.env`
     (not `.env`/`.env.*`), `secrets.yaml`-family names, `token.txt`-family names, `.pgpass`,
     `settings.local.json`/`local.settings.json`, and `.keystore`/`.jks`. Post-fix: denylist
     widened (deny-only, no new allow) with an explicit anti-overreach negative (`secrets.py`,
     `.env.example`, `id_rsa.pub`, `tokenizer.py` stay allowed).

  B25 (pipeline/webrtc_server.py `start_bot`): `except Exception: data = {}` swallowed EVERY
     JSON parse failure into a silent empty handshake, `data.get("body")` on a non-dict JSON
     (e.g. a list) could also 500. Post-fix: empty body stays a legitimate bare-/start handshake
     (200), but a non-empty malformed/non-object body is a diagnosable 400.

  B26 (config.py `SynapseConfig.from_env`): `KORA_ENABLED=""` was treated as an ACTIVE `False`
     (`"".strip().lower() not in (...) ` includes `""`), unlike every other env override in the
     file (fish_tts_model/_num), where an unset/empty value keeps the dataclass default.
     Post-fix: empty value = unset -> dataclass default; non-empty still parses normally.

  B27 (dispatcher/mock_llm.py `MockLLM.complete`): affirm/deny routed on `words & _*_WORDS`
     (intersection) like status/cancel/submit — but confirm/deny are single-word decisions, so
     "да, скачай отчёт" matched `_AFFIRM_WORDS` and was swallowed as a bare confirm instead of
     reaching submit_task. Post-fix: affirm/deny require the WHOLE utterance to be confirmation
     words (subset check); status/cancel/submit remain on intersection (regression-tested).

  B28 (journal.py `TurnJournal` + pipeline/webrtc_server.py `build_web_app`): the fd is a host
     singleton, never closed on the live webrtc path; `close()` exists but nothing calls it
     there, and — more urgently — a late write (monitor/KoraRunner task outside the ASGI
     lifecycle) after ANY close() raises `ValueError: I/O operation on closed file`, which
     `alert()`'s narrow `except OSError` does NOT catch and `record_kora_event`/`end_turn`
     don't catch at all. Post-fix: (8a) `_write` becomes a silent no-op once `close()` has run;
     `close()` is idempotent. (8b, plan v2.1) `build_web_app` registers a shutdown handler that
     closes the host's journal — looked up LAZILY inside an inner `_close_journal` closure, not
     eagerly at build time (an eager `host.journal.close` attribute read was the plan's original
     v2 shape and crashed every frozen test that builds the app around a stub host with no real
     journal; those stub-host tests never fire the ASGI shutdown event, so lazy is safe).

  B29 (pipeline/webrtc_server.py `run_session`): `monitor.cancel()` on teardown was never
     awaited — none of run_session's remaining awaits (uncontended `Lock.acquire`, dict ops)
     yield control back to the loop, so the scheduled cancellation callback had not run by the
     time run_session returned, leaving a pending task ("Task was destroyed but it is pending").
     Post-fix: the cancellation is awaited (CancelledError swallowed) before returning.

"""
from __future__ import annotations

import asyncio
import itertools
import types

import pytest

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.kora import (
    _LIFECYCLE_TYPES,
    _is_secret_path,
    _message_to_events,
    apply_event_to_store,
)
from synapse.bridge.state import EventClass, KoraEvent, Liveness, TaskStatus, TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.dispatcher.loop import DispatcherTurnLoop
from synapse.dispatcher.mock_llm import MockLLM
from synapse.dispatcher.tools import KoraBridge, ToolCall, ToolHandlers
from synapse.journal import AlertKind, TurnJournal


# ============================================================================================
# B10 — bounded tool-pass loop (dispatcher/loop.py `ingest_user_turn`)
# ============================================================================================
def _make_loop_with_llm(journal_dir: str, llm) -> tuple[DispatcherTurnLoop, ToolHandlers, TaskStore]:
    cfg = SynapseConfig()
    clock = FakeClock(start=0.0)
    journal = TurnJournal(journal_dir, clock, session_id="b10")
    store = TaskStore(clock, journal_dir=None)
    classifier = KeywordClassifier(cfg.destructive_keywords)
    confirm_flow = ConfirmFlow(
        store, clock, classifier, journal,
        cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s,
    )
    bridge = KoraBridge(store=store, confirm_flow=confirm_flow, clock=clock, cfg=cfg)
    handlers = ToolHandlers(bridge, journal)
    loop = DispatcherTurnLoop(llm, handlers, confirm_flow, store, journal, clock, cfg)
    return loop, handlers, store


class _ChainingLLM:
    """Chains get_task_status -> request_cancel across THREE completions — exercises the B10
    bounded-loop shape (a strict two-pass loop calls `_complete()` only once after the first
    tool_calls batch and would drop request_cancel). MockLLM's `_respond_to_tool_result` always
    returns `[]`, so it structurally cannot chain a second tool call (R-MAJOR disposition) —
    this stub is this test's OWN LLM, not MockLLM."""

    async def complete(self, messages, tools):
        last = messages[-1] if messages else {}
        role = last.get("role")
        if role == "user":
            return "", [ToolCall("get_task_status", {})]
        if role == "tool" and last.get("name") == "get_task_status":
            return "", [ToolCall("request_cancel", {})]
        return "готово", []


class _AlwaysToolsLLM:
    """A pathological LLM that ALWAYS returns a tool_call, regardless of what it's shown —
    without a cap the bounded loop would spin forever."""

    async def complete(self, messages, tools):
        return "", [ToolCall("get_task_status", {})]


async def test_b10_chained_tool_calls_beyond_pass_two_are_not_dropped(tmp_path):
    loop, handlers, store = _make_loop_with_llm(str(tmp_path), _ChainingLLM())
    store.start_task("tk", "задача", TaskStatus.RUNNING, 0.0)

    record, text = await loop.ingest_user_turn("статус потом отмени")

    names = [tc["name"] for tc in record.tool_calls]
    # POST-FIX: both calls dispatch across 3 completions. CURRENTLY: the strict 2-pass shape
    # calls `_complete()` only ONCE after the first tool_calls batch, discarding request_cancel.
    assert names == ["get_task_status", "request_cancel"], (
        f"chained tool_calls beyond pass 2 were dropped (B10): {names!r}"
    )
    assert text == "готово"
    assert store.task.status == TaskStatus.CANCEL_REQUESTED


async def test_b10_pathological_llm_capped_at_max_tool_passes(tmp_path):
    # Deferred import (not module-level): pre-fix `_MAX_TOOL_PASSES` doesn't exist at all, and an
    # ImportError at module scope would fail EVERY test in this file at collection, not just this
    # one at its own assertion (the wave-4 convention this file otherwise follows throughout).
    from synapse.dispatcher.loop import _MAX_TOOL_PASSES

    loop, handlers, store = _make_loop_with_llm(str(tmp_path), _AlwaysToolsLLM())

    record, text = await loop.ingest_user_turn("статус")

    # POST-FIX: exactly `_MAX_TOOL_PASSES` dispatch rounds, never unbounded.
    assert len(record.tool_calls) == _MAX_TOOL_PASSES, (
        f"expected exactly {_MAX_TOOL_PASSES} dispatch rounds against an always-tool_calls LLM, "
        f"got {len(record.tool_calls)}"
    )


# ============================================================================================
# B19 — liveness() gated on RUNNING while awaiting an answer (bridge/state.py)
# ============================================================================================
def test_b19_awaiting_gated_off_after_cancel_request_reports_degraded():
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    store.start_task("tk", "t", TaskStatus.RUNNING, 0.0)
    store.heartbeat(0.0)
    store.set_awaiting()
    assert store.request_cancel() is True
    assert store.task.status == TaskStatus.CANCEL_REQUESTED
    # the flag is cleared later (in KoraRunner._handle_question's finally) — it is still True here.
    assert store.awaiting_answer is True

    # far past the unreachable threshold — the unpatched code returns OK unconditionally on
    # `_awaiting_answer` alone, masking a dead/cancelled Kora through the cancel window.
    live = store.liveness(10_000.0, 120, 300)
    assert live != Liveness.OK, (
        f"liveness reported {live!r} while awaiting a stale, cancel-requested task — B19: "
        "awaiting is not gated on RUNNING, unlike render_state/snapshot"
    )
    assert live == Liveness.UNREACHABLE


def test_b19_awaiting_running_stays_ok_regression():
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    store.start_task("tk", "t", TaskStatus.RUNNING, 0.0)
    store.heartbeat(0.0)
    store.set_awaiting()
    # far past unreachable — but RUNNING+awaiting must still read OK (E5 MAJOR-R1 happy path).
    assert store.liveness(10_000.0, 120, 300) == Liveness.OK


def test_b19_awaiting_no_task_stays_ok_mirrors_frozen_answer_kora_test():
    # Duplicate regression of the frozen test_answer_kora.py::
    # test_liveness_ok_while_awaiting_even_when_stale — a literal `task.status == RUNNING`
    # gate (without the `task is None or` carve-out) would flip this to UNREACHABLE and break it.
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    store.heartbeat(0.0)
    assert store.task is None
    assert store.liveness(10_000.0, 120, 300) == Liveness.UNREACHABLE
    store.set_awaiting()
    assert store.liveness(10_000.0, 120, 300) == Liveness.OK


# ============================================================================================
# B20 — critical registration hoisted out of the lifecycle-only gate (bridge/kora.py)
# ============================================================================================
def _b20_collaborators(tmp_path):
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    store.start_task("tk", "t", TaskStatus.RUNNING, 0.0)
    ledger_journal = TurnJournal(str(tmp_path), clock, session_id="b20")
    return store, ledger_journal


async def test_b20_non_lifecycle_critical_arms_ledger_but_never_speaks(tmp_path):
    from synapse.bridge.state import SpeakLedger

    store, journal = _b20_collaborators(tmp_path)
    ledger = SpeakLedger()
    speaks: list[str] = []

    # Synthetic: type NOT in _LIFECYCLE_TYPES, but cls=CRITICAL and speak_text set (a
    # hypothetical future producer bug/regression) — must never reach on_speak (NO-EXFIL is
    # STRUCTURAL: speak dispatch stays inside the lifecycle branch), but the ledger must still
    # arm so a silently-dropped critical fact is caught by the Р-15г watchdog instead of vanishing.
    event = KoraEvent("e1", "kora_system", EventClass.CRITICAL, {}, "секретный текст", 1.0)
    apply_event_to_store(event, store, ledger, speaks.append, journal)

    assert speaks == [], "non-lifecycle speak_text reached on_speak — NO-EXFIL backstop broken"
    assert store.task.events == [], "non-lifecycle event was appended to task.events (heartbeat-only expected)"

    # POST-FIX: register_critical armed the ledger for this event -> unresolved after the
    # window -> CRITICAL_WITHOUT_SPEAK. CURRENTLY: register_critical lives INSIDE the
    # lifecycle-only branch, so a non-lifecycle CRITICAL never arms the ledger -> silent drop, no
    # alert ever fires -> this list stays empty -> RED.
    alerts = ledger.check(now=1.0 + 999.0, window_s=5.0)
    assert alerts and alerts[0][0] == "CRITICAL_WITHOUT_SPEAK", (
        f"non-lifecycle CRITICAL event never armed the SpeakLedger (B20): {alerts!r}"
    )


# --- fake SDK messages (class NAME is what the duck-typed mapper keys on — NO underscore
# prefix: `_message_to_events` matches on `type(msg).__name__` literally; mirrors test_kora.py) --
class SystemMessage:
    def __init__(self, subtype, data=None):
        self.subtype = subtype
        self.data = data or {}


class AssistantMessage:
    def __init__(self, content):
        self.content = content


class UserMessage:
    def __init__(self, content):
        self.content = content


class ResultMessage:
    def __init__(self, is_error, num_turns=1, total_cost_usd=0.001):
        self.is_error = is_error
        self.num_turns = num_turns
        self.total_cost_usd = total_cost_usd


class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class ThinkingBlock:
    def __init__(self, thinking, signature=""):
        self.thinking = thinking
        self.signature = signature


class ToolResultBlock:
    def __init__(self, tool_use_id, content, is_error=False):
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class RateLimitEvent:  # exercises the "unknown -> kora_<snake>" branch
    pass


def test_b20_message_mapper_invariant_non_lifecycle_never_critical_or_speak():
    seq = itertools.count()
    msgs = [
        SystemMessage("init", {"session_id": "s1", "model": "m"}),  # lifecycle: task_started
        SystemMessage("compact_boundary", {}),  # non-lifecycle: kora_system
        AssistantMessage(
            [TextBlock("h" * 500), ToolUseBlock("u1", "Write", {"file_path": "a"}), ThinkingBlock("secret")]
        ),  # non-lifecycle: assistant_text, tool_use, thinking
        UserMessage([ToolResultBlock("u1", "ok", is_error=False)]),  # non-lifecycle: tool_result
        ResultMessage(is_error=False, num_turns=2, total_cost_usd=0.01),  # lifecycle: task_completed
        ResultMessage(is_error=True),  # lifecycle: task_failed
        RateLimitEvent(),  # non-lifecycle: kora_rate_limit_event
    ]
    events: list[KoraEvent] = []
    for m in msgs:
        events += _message_to_events(m, "tk", "задача", 1.5, seq)

    non_lifecycle = [e for e in events if e.type not in _LIFECYCLE_TYPES]
    lifecycle = [e for e in events if e.type in _LIFECYCLE_TYPES]
    assert non_lifecycle, "premise: at least one non-lifecycle event was produced"
    assert lifecycle, "premise: at least one lifecycle event was produced (init/ResultMessage)"

    for e in non_lifecycle:
        assert e.speak_text is None, f"non-lifecycle event {e.type!r} unexpectedly carries speak_text"
        assert e.cls != EventClass.CRITICAL, f"non-lifecycle event {e.type!r} is unexpectedly CRITICAL"


# ============================================================================================
# B22 — secret-path denylist coverage (bridge/kora.py `_is_secret_path`)
# ============================================================================================
@pytest.mark.parametrize(
    "name",
    ["prod.env", "dev.env", "secrets.yaml", "token.txt", ".pgpass", "local.settings.json", "x.keystore", "x.jks"],
)
def test_b22_new_secret_patterns_are_denied(tmp_path, name):
    assert _is_secret_path(tmp_path / name) is True, f"{name!r} should be classified as a secret path"


@pytest.mark.parametrize("name", ["secrets.py", ".env.example", "id_rsa.pub", "tokenizer.py"])
def test_b22_lookalikes_are_not_denied(tmp_path, name):
    assert _is_secret_path(tmp_path / name) is False, f"{name!r} should NOT be classified as a secret path"


# ============================================================================================
# B25 — /start distinguishes empty (legit) from malformed (400) body (pipeline/webrtc_server.py)
# ============================================================================================
def _endpoint(app, name):
    for route in app.routes:
        ep = getattr(route, "endpoint", None)
        if ep is not None and getattr(ep, "__name__", None) == name:
            return ep
    raise AssertionError(f"route endpoint {name!r} not found")


def _cells(fn) -> dict:
    return dict(zip(fn.__code__.co_freevars, [c.cell_contents for c in (fn.__closure__ or ())]))


class _FakeStartRequest:
    def __init__(self, raw: bytes):
        self._raw = raw

    async def body(self) -> bytes:
        return self._raw


def _webrtc_server_or_skip():
    pytest.importorskip("aiortc")
    pytest.importorskip("cv2")
    pytest.importorskip("fastapi")
    try:
        from synapse.pipeline import webrtc_server
    except (ImportError, RuntimeError) as e:
        pytest.skip(f"webrtc deps/prebuilt UI unavailable: {e}")
    return webrtc_server


async def test_b25_start_bot_empty_body_is_a_legitimate_bare_handshake():
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    start_bot = _endpoint(app, "start_bot")

    result = await start_bot(_FakeStartRequest(b""))
    assert "sessionId" in result, f"bare /start (empty body) must still succeed: {result!r}"


async def test_b25_start_bot_malformed_json_is_400():
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    start_bot = _endpoint(app, "start_bot")

    resp = await start_bot(_FakeStartRequest(b"{bad"))
    # POST-FIX: a diagnosable 400. CURRENTLY: `except Exception: data = {}` swallows the parse
    # error into the same silent-success path as a bare /start -> RED (no .status_code / not 400).
    status = getattr(resp, "status_code", None)
    assert status == 400, f"malformed JSON body should 400, got {resp!r} (status={status!r})"


async def test_b25_start_bot_non_object_json_is_400():
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    start_bot = _endpoint(app, "start_bot")

    resp = await start_bot(_FakeStartRequest(b"[1,2]"))
    status = getattr(resp, "status_code", None)
    assert status == 400, f"a JSON array body should 400 (not a dict), got {resp!r} (status={status!r})"


async def test_b25_start_bot_valid_object_json_is_200():
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    start_bot = _endpoint(app, "start_bot")

    result = await start_bot(_FakeStartRequest(b'{"x":1}'))
    assert "sessionId" in result, f"a valid flat JSON object body should still 200: {result!r}"


# ============================================================================================
# B26 — KORA_ENABLED="" is unset (keeps the dataclass default), not an active False (config.py)
# ============================================================================================
def test_b26_empty_kora_enabled_keeps_dataclass_default():
    cfg = SynapseConfig.from_env({"KORA_ENABLED": ""})
    # POST-FIX: empty = unset -> dataclass default (True). CURRENTLY: "".strip().lower() ==
    # "" which IS in the falsy-string tuple check's complement -> active False -> RED.
    assert cfg.kora_enabled == SynapseConfig().kora_enabled
    assert cfg.kora_enabled is True


def test_b26_false_kora_enabled_still_disables_regression():
    cfg = SynapseConfig.from_env({"KORA_ENABLED": "false"})
    assert cfg.kora_enabled is False


def test_b26_true_kora_enabled_still_enables_regression():
    cfg = SynapseConfig.from_env({"KORA_ENABLED": "true"})
    assert cfg.kora_enabled is True


# ============================================================================================
# B27 — MockLLM affirm/deny only on a whole-utterance match (dispatcher/mock_llm.py)
# ============================================================================================
async def test_b27_affirm_word_plus_payload_routes_to_submit_not_confirm():
    llm = MockLLM()
    text, calls = await llm.complete([{"role": "user", "content": "да, скачай отчёт"}], [])
    # POST-FIX: the whole utterance is not JUST confirmation words -> falls through to submit.
    # CURRENTLY: "да" & _AFFIRM_WORDS is truthy (intersection) -> swallowed as a bare confirm -> RED.
    assert len(calls) == 1 and calls[0].name == "submit_task", (
        f"«да, скачай отчёт» should route to submit_task, got {calls!r}"
    )


async def test_b27_bare_affirm_routes_to_confirm_regression():
    llm = MockLLM()
    text, calls = await llm.complete([{"role": "user", "content": "да"}], [])
    assert calls == [ToolCall("confirm_task", {"decision": "confirm"})]


async def test_b27_bare_deny_routes_to_confirm_deny_regression():
    llm = MockLLM()
    text, calls = await llm.complete([{"role": "user", "content": "нет"}], [])
    assert calls == [ToolCall("confirm_task", {"decision": "deny"})]


# ============================================================================================
# B28 — 8a: closed journal is a silent no-op, not a crash (journal.py); 8b (plan v2.1): the
# live webrtc path closes the journal on ASGI shutdown, looked up lazily (webrtc_server.py)
# ============================================================================================
def test_b28_closed_journal_late_writes_are_silent_noop_and_close_is_idempotent(tmp_path):
    clock = FakeClock(0.0)
    journal = TurnJournal(str(tmp_path), clock, session_id="b28")
    journal.begin_turn("hi")
    journal.close()
    journal.close()  # idempotent — must not raise on an already-closed file

    # record_kora_event -> _write(fsync=False), previously had NO exception guard at all.
    ev = KoraEvent("e1", "kora_system", EventClass.NARRATABLE, {}, None, 1.0)
    journal.record_kora_event(ev)  # POST-FIX: silent no-op. CURRENTLY: ValueError -> RED.

    # alert -> _write(fsync=True); its OWN try/except only catches OSError, not ValueError.
    journal.alert(AlertKind.KORA_RUN_FAILED, {"x": 1})  # POST-FIX: silent no-op. CURRENTLY: RED.

    # end_turn -> _write via the same guarded path.
    journal.begin_turn("second")
    journal.end_turn()  # POST-FIX: silent no-op. CURRENTLY: RED.


def test_b28b_build_web_app_with_stub_host_does_not_raise():
    # 8b v2.1 anti-regression: the journal must be looked up LAZILY inside the shutdown handler
    # — an eager `host.journal.close` attribute read at build time (the plan's original v2
    # shape) would AttributeError right here, exactly like the frozen stub-host tests
    # (test_webrtc_server.py / w1 b8 / w4 b24) it would have broken.
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    assert app is not None


def test_b28b_app_registers_exactly_one_shutdown_handler():
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    # POST-FIX: build_web_app wires the journal close via add_event_handler("shutdown", ...).
    # CURRENTLY: nothing on the live path ever closes the journal fd -> empty list -> RED.
    assert len(app.router.on_shutdown) == 1, (
        f"expected exactly one on_shutdown handler (the B28 journal close), "
        f"got {app.router.on_shutdown!r}"
    )


async def test_b28b_shutdown_handler_closes_the_hosts_journal(tmp_path):
    webrtc_server = _webrtc_server_or_skip()
    journal = TurnJournal(str(tmp_path), FakeClock(0.0), session_id="b28b")
    host = types.SimpleNamespace(journal=journal)
    app = webrtc_server.build_web_app(host=host)

    assert journal._file.closed is False, "premise: the journal fd is open until shutdown"
    # Run every registered shutdown handler (exactly one post-fix; an empty list pre-fix runs
    # nothing, so this test still fails AT ITS OWN ASSERTION below, not on an IndexError).
    for handler in list(app.router.on_shutdown):
        await handler()

    assert journal._file.closed is True, (
        "the shutdown handler(s) ran but the host journal's file is still open (B28b)"
    )


# ============================================================================================
# B29 — monitor-task cancellation is awaited before run_session returns (pipeline/webrtc_server.py)
# ============================================================================================
async def test_b29_monitor_cancellation_awaited_before_run_session_returns(monkeypatch):
    webrtc_server = _webrtc_server_or_skip()

    async def _forever():
        await asyncio.sleep(3600)

    host = types.SimpleNamespace(
        monitor_forever=_forever,
        bind_output=lambda task: None,
        unbind_output=lambda task: None,
    )
    app = webrtc_server.build_web_app(host=host)
    offer_ep = _endpoint(app, "offer")
    handle_offer = _cells(offer_ep)["_handle_offer"]
    run_session = _cells(handle_offer)["run_session"]

    new_task = types.SimpleNamespace(has_finished=lambda: False, queue_frame=lambda *a, **k: None)

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

    class _FakeRunner:
        def __init__(self, handle_sigint=False):
            pass

        async def run(self, task):
            return None  # completes immediately, no suspension — like a finished session

    monkeypatch.setattr(
        webrtc_server, "build_session_pipeline", lambda _host: types.SimpleNamespace(pipeline=object())
    )
    monkeypatch.setattr(webrtc_server, "SmallWebRTCTransport", _FakeTransport)
    monkeypatch.setattr(webrtc_server, "TransportParams", lambda **k: object())
    monkeypatch.setattr(webrtc_server, "Pipeline", lambda *a, **k: object())
    monkeypatch.setattr(webrtc_server, "PipelineTask", lambda *a, **k: new_task)
    monkeypatch.setattr(webrtc_server, "PipelineRunner", _FakeRunner)

    created: list[asyncio.Task] = []
    real_ensure_future = asyncio.ensure_future

    def _tracking_ensure_future(coro_or_future, **kw):
        t = real_ensure_future(coro_or_future, **kw)
        created.append(t)
        return t

    monkeypatch.setattr(asyncio, "ensure_future", _tracking_ensure_future)

    await run_session(object(), session_id="sess-b29")

    assert len(created) == 1, f"expected exactly one monitor task, got {len(created)}"
    monitor = created[0]
    # POST-FIX: run_session's finally awaits the cancelled monitor before returning, so by the
    # time we regain control it is fully done (CancelledError consumed). CURRENTLY: a bare
    # `monitor.cancel()` only REQUESTS cancellation — none of run_session's remaining awaits
    # (uncontended Lock.acquire, dict ops) yield control back to the loop, so the scheduled
    # cancellation callback never runs before we get here -> done() is False -> RED.
    assert monitor.done() is True, (
        "monitor task not done immediately after run_session returned — B29: cancel() without "
        'await leaves a pending task ("Task was destroyed but it is pending" on teardown)'
    )
    assert monitor.cancelled() is True
