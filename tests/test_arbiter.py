import pytest
from pipecat.frames.frames import TextFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection

from synapse.pipeline.arbiter import ArbiterPolicy, TTSArbiterProcessor, default_sentence_splitter


def fixed_splitter(text):
    return [text]  # one "sentence" per push -- deterministic for these tests


def test_speak_jumps_to_head_when_queue_empty():
    a = ArbiterPolicy(splitter=fixed_splitter)
    a.push_speak("критично")
    items = a.drain_all()
    assert [i.text for i in items] == ["критично"]


def test_current_dispatcher_sentence_finishes_before_speak():
    a = ArbiterPolicy(splitter=fixed_splitter)
    a.push_dispatcher_text("первое.")
    a.push_dispatcher_text("второе.")
    a.push_speak("критично")
    items = a.drain_all()
    assert [i.text for i in items] == ["первое.", "критично"]


def test_speak_drops_undelivered_dispatcher_tail():
    a = ArbiterPolicy(splitter=fixed_splitter)
    a.push_dispatcher_text("первое.")
    a.push_dispatcher_text("второе.")
    a.push_dispatcher_text("третье.")
    a.push_speak("критично")
    items = a.drain_all()
    assert [i.text for i in items] == ["первое.", "критично"]


def test_flush_dispatcher_only_removes_dispatcher_items():
    a = ArbiterPolicy(splitter=fixed_splitter)
    a.push_speak("критично")
    a.push_dispatcher_text("болтовня.")
    a.flush_dispatcher()
    items = a.drain_all()
    assert [i.text for i in items] == ["критично"]


def test_multiple_speaks_all_survive_a_dispatcher_flush():
    a = ArbiterPolicy(splitter=fixed_splitter)
    a.push_speak("первый спик")
    a.push_speak("второй спик")
    a.push_dispatcher_text("болтовня.")
    a.flush_dispatcher()
    items = [i.text for i in a.drain_all()]
    assert "первый спик" in items
    assert "второй спик" in items
    assert "болтовня." not in items


def test_default_splitter_splits_sentences():
    a = ArbiterPolicy()
    a.push_dispatcher_text("Привет. Как дела?")
    items = a.drain_all()
    assert len(items) == 2


def test_drain_all_empties_the_queue():
    a = ArbiterPolicy(splitter=fixed_splitter)
    a.push_dispatcher_text("текст")
    a.drain_all()
    assert len(a) == 0
    assert a.pop_next() is None


@pytest.mark.asyncio
async def test_drain_pushes_speak_as_tts_speak_frame_out_of_context_and_dispatcher_as_plain_text():
    # No-audio-fix follow-up (Critic C, plan v2 item 3): assistant_aggregator now sits
    # downstream of tts, so a plain TextFrame for SPEAK would leak Kora's operational readback
    # into LLM context. _drain must push SPEAK as TTSSpeakFrame(append_to_context=False) and
    # leave dispatcher text as plain TextFrame.
    proc = TTSArbiterProcessor(ArbiterPolicy())
    captured = []

    async def fake_push_frame(frame, direction=FrameDirection.DOWNSTREAM):
        captured.append((frame, direction))

    proc.push_frame = fake_push_frame

    proc._policy.push_dispatcher_text("Привет.")
    proc._policy.push_speak("Подтверждаю.")
    await proc._drain()

    assert len(captured) == 2
    dispatcher_frame, _ = captured[0]
    speak_frame, _ = captured[1]
    assert isinstance(dispatcher_frame, TextFrame) and not isinstance(dispatcher_frame, TTSSpeakFrame)
    assert isinstance(speak_frame, TTSSpeakFrame) and speak_frame.append_to_context is False


def test_default_splitter_preserves_whitespace_no_mashing():
    # E10: streaming LLM fragments must re-concatenate with spaces intact -- the splitter must
    # never drop characters, or pipecat's TTS re-joins them mashed ("хорошем темпе" -> "хорошемтемпе").
    frags = ["Привет! ", "Это тест ", "синтеза речи."]
    pieces: list[str] = []
    for f in frags:
        r = default_sentence_splitter(f)
        assert "".join(r) == f  # per-fragment: no characters lost
        pieces += r
    assert "".join(pieces) == "".join(frags)  # cross-fragment: no mashing
    assert default_sentence_splitter(" по рефакторингу ") == [" по рефакторингу "]
