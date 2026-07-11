"""M1 slice 2 (2026-07-11 run): proactive push channel -- making Kora's SPEAK audible.

`SynapseHost.speak()` registers the ledger ALWAYS, then either injects a TTSSpeakFrame straight
into the live per-connection output task (out-of-band, no input frame needed) or falls back to
the frame-driven arbiter queue. These tests exercise that surface (push_speak_frame / speak /
bind_output / unbind_output) against tiny duck-typed fakes -- no pipecat task, no live server.
"""
import asyncio

from pipecat.frames.frames import TTSSpeakFrame

from synapse.pipeline.app import SynapseHost


class FakeTask:
    """Duck-types just the two members the injector touches: has_finished() + async queue_frame."""

    def __init__(self, finished: bool = False) -> None:
        self._finished = finished
        self.frames: list = []  # records (frame, direction) per queue_frame call

    def has_finished(self) -> bool:
        return self._finished

    async def queue_frame(self, frame, direction=None) -> None:
        self.frames.append((frame, direction))


class FakeLedger:
    def __init__(self) -> None:
        self.calls: list = []

    def register_speak_text(self, text: str, ts: float) -> None:
        self.calls.append((text, ts))


class FakeArbiter:
    def __init__(self) -> None:
        self.spoken: list = []

    def push_speak(self, text: str) -> None:
        self.spoken.append(text)


class FakeClock:
    def now(self) -> float:
        return 42.0


def _host() -> SynapseHost:
    # Build the host directly with fakes for only the collaborators the SPEAK surface reads
    # (clock/speak_ledger/arbiter_policy); the rest are irrelevant here. _output_task starts None.
    return SynapseHost(
        clock=FakeClock(),
        cfg=None,
        journal=None,
        store=None,
        speak_ledger=FakeLedger(),
        classifier=None,
        confirm_flow=None,
        arbiter_policy=FakeArbiter(),
        bridge=None,
        handlers=None,
        breaker=None,
        cost_cap=None,
    )


# --- push_speak_frame (the out-of-band injector) ------------------------------------------------

async def test_push_speak_frame_injects_ttsspeak_when_live():
    host = _host()
    task = FakeTask()
    host.bind_output(task)

    await host.push_speak_frame("готово")

    assert len(task.frames) == 1
    frame, _direction = task.frames[0]
    assert isinstance(frame, TTSSpeakFrame)
    assert frame.text == "готово"
    # m3 regression: SPEAK is Kora's readback, never LLM-authored -- must stay out of context.
    assert frame.append_to_context is False


async def test_push_speak_frame_noop_when_no_output():
    host = _host()
    task = FakeTask()
    host.bind_output(task)
    host.unbind_output(task)  # back to None -- nothing bound
    assert host._output_task is None

    await host.push_speak_frame("привет")  # must not raise

    assert task.frames == []  # None guard -> no injection even into a once-bound task


async def test_push_speak_frame_noop_when_task_finished():
    # The M1 fix: queue_frame on a finished task is a SILENT DROP (unbounded put never raises,
    # the drain task is gone), so a non-None check alone would lose the SPEAK into the void.
    host = _host()
    task = FakeTask(finished=True)
    host.bind_output(task)

    await host.push_speak_frame("привет")

    assert task.frames == []  # has_finished() guard blocks the doomed injection


# --- speak (the sync on_speak entry point) ------------------------------------------------------

async def test_speak_registers_ledger_always():
    # Invariant: the ledger is registered synchronously, first thing, on EVERY speak -- even on
    # the live+loop path where the actual utterance is only scheduled (ensure_future), not awaited.
    host = _host()
    host.bind_output(FakeTask())

    host.speak("сохранил")

    assert host.speak_ledger.calls == [("сохранил", 42.0)]


async def test_speak_live_with_loop_schedules_injection():
    host = _host()
    task = FakeTask()
    host.bind_output(task)

    host.speak("билд готов")
    # ensure_future only schedules -- nothing has run yet until we yield the loop.
    assert task.frames == []
    await asyncio.sleep(0)

    assert len(task.frames) == 1
    frame, _direction = task.frames[0]
    assert isinstance(frame, TTSSpeakFrame)
    assert frame.text == "билд готов"
    assert frame.append_to_context is False
    # Must NOT also push through the arbiter -- that would double the utterance.
    assert host.arbiter_policy.spoken == []


def test_speak_no_output_falls_back_to_arbiter():
    host = _host()  # nothing bound -> frame-driven arbiter fallback

    host.speak("нет соединения")

    assert host.arbiter_policy.spoken == ["нет соединения"]
    assert host.speak_ledger.calls == [("нет соединения", 42.0)]


def test_speak_live_but_no_running_loop_falls_back_to_arbiter():
    # m1 loop-safety: a live output task but no running loop (a sync caller) can't ensure_future,
    # so speak() must fall back to the arbiter rather than blow up on get_running_loop().
    host = _host()
    task = FakeTask()
    host.bind_output(task)

    host.speak("синхронный путь")  # a plain sync test = no running event loop

    assert host.arbiter_policy.spoken == ["синхронный путь"]
    assert task.frames == []  # never injected out-of-band
    assert host.speak_ledger.calls == [("синхронный путь", 42.0)]


# --- bind / unbind lifecycle --------------------------------------------------------------------

def test_bind_and_unbind_set_and_clear():
    host = _host()
    task = FakeTask()

    host.bind_output(task)
    assert host._output_task is task

    host.unbind_output(task)
    assert host._output_task is None


def test_unbind_of_non_current_task_is_noop():
    # Preempt: a new connection's bind supersedes the old one, so the superseded task's later
    # unbind (in its finally) must NOT clear the live task's slot -- the `is` check saves it.
    host = _host()
    live = FakeTask()
    superseded = FakeTask()
    host.bind_output(live)

    host.unbind_output(superseded)

    assert host._output_task is live
