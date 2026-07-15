"""Probe P3b (§14.2.4, automatable half) — TTS context_id presence + uniqueness.

Go/no-go for KV-1 and KV-3b. The spec's whole presence mechanism (reply-window, dedupe,
terminal-vs-milestone) rests on one platform assumption:

    every TTSSpeakFrame gets its OWN context_id, and that id arrives in TTSStartedFrame

If it were false, `TTSCorrelationRegistry.pop_started()` would never resolve an exact
(context_id -> utterance_id) pair, no reply-window would ever open, and interactive Kora
would silently degrade to "hard question only" — the spec forbids a FIFO fallback, so this
is a real no-go gate, not a preference.

The assumption is NOT obviously true: pipecat's TTSService takes `reuse_context_id_within_turn`
which DEFAULTS TO TRUE, and `create_context_id()` returns the SAME id for every sentence while
a turn context is set. This probe pins down which frames escape that reuse.

Run:  .venv/bin/python probes/p3b_context_id.py
The live half of P3b (Fish reconnect latency p95 <= 500 ms) needs a real WS and is separate.
"""

import asyncio
import sys
from collections.abc import AsyncGenerator

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
)
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.tests.utils import SleepFrame, run_test

SAMPLE_RATE = 24000


class FakeTTSService(TTSService):
    """Minimal concrete TTSService standing in for FishAudioTTSService.

    The fake must mirror the REAL service in exactly the settings this probe measures,
    or it measures itself. `push_start_frame` is the one that matters: it defaults to
    False in the base class, and with the default no TTSStartedFrame is emitted at all —
    a fake left on defaults reports a false NO-GO. FishAudioTTSService passes
    push_start_frame=True (pipecat/services/fish/tts.py:195), so the base class creates the
    audio context and emits TTSStartedFrame(context_id=context_id) itself
    (tts_service.py:1097-1103). That is the code path Synapse actually runs.

    Everything else is stubbed: no network, no key.
    """

    def __init__(self, **kwargs):
        super().__init__(
            sample_rate=SAMPLE_RATE,
            push_start_frame=True,  # mirrors fish/tts.py:195 — see docstring
            # store mode requires every field to be initialized; this fake supports none.
            settings=TTSSettings(model=None, voice=None, language=None),
            **kwargs,
        )
        # (text -> context_id) as the service was actually asked to synthesize it. This is the
        # ground truth for "which id belongs to which utterance" — inferring it by counting
        # started frames guesses; recording it at the source does not.
        self.requests: list[tuple[str, str]] = []

    def can_generate_metrics(self) -> bool:
        return False

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        self.requests.append((text, context_id))
        yield TTSAudioRawFrame(
            audio=b"\x00\x00" * 160, sample_rate=SAMPLE_RATE, num_channels=1
        )


async def drive(frames_to_send) -> tuple[list[TTSStartedFrame], FakeTTSService]:
    tts = FakeTTSService()
    # TTSStartedFrame is not pushed inline: the base class parks it on a per-context audio
    # queue that a separate task drains downstream. Without a trailing settle the EndFrame
    # closes the pipeline first and the probe sees zero started frames — an artifact of the
    # harness, not of the id logic. The sleeps are the drain, not a timing guess at the SUT.
    frames = [*frames_to_send, SleepFrame(sleep=1.0)]
    received_down, _ = await run_test(tts, frames_to_send=frames)
    return [f for f in received_down if isinstance(f, TTSStartedFrame)], tts


async def collect_started(frames_to_send) -> list[TTSStartedFrame]:
    started, _ = await drive(frames_to_send)
    return started


async def case_speak_frames_are_unique(n: int = 20) -> tuple[bool, str]:
    """20 alternating disp/kora SPEAK utterances — the spec's exact probe shape."""
    frames = [TTSSpeakFrame(text=f"реплика {i} от {'коры' if i % 2 else 'диспетчера'}.")
              for i in range(n)]
    started = await collect_started(frames)
    ids = [f.context_id for f in started]

    if len(started) != n:
        return False, f"ожидалось {n} TTSStartedFrame, получено {len(started)}"
    if any(i is None for i in ids):
        return False, f"context_id is None у {sum(1 for i in ids if i is None)} из {n} фреймов"
    if len(set(ids)) != n:
        dupes = len(ids) - len(set(ids))
        return False, f"context_id НЕ уникален: {dupes} повторов на {n} реплик"
    return True, f"{n}/{n} TTSSpeakFrame получили уникальный непустой context_id"


async def case_llm_turn_groups_into_one_id() -> tuple[bool, str]:
    """Control case — the one that makes the result above mean something.

    If reuse were simply OFF, every frame would trivially get its own id and the main case
    would prove nothing about TTSSpeakFrame specifically. So assert the CONTRAST: three
    sentences inside ONE dispatcher turn must collapse into a SINGLE audio context (one
    TTSStartedFrame, one id), while three standalone SPEAK utterances produce three.

    Asserting "all turn ids are equal" would pass vacuously on a single frame — count the
    frames instead.
    """
    n_sentences = 3
    frames = [
        LLMFullResponseStartFrame(),
        LLMTextFrame(text="Первое предложение. "),
        LLMTextFrame(text="Второе предложение. "),
        LLMTextFrame(text="Третье предложение. "),
        LLMFullResponseEndFrame(),
    ]
    turn_started = await collect_started(frames)
    speak_started = await collect_started(
        [TTSSpeakFrame(text=f"Реплика {i}.") for i in range(n_sentences)]
    )
    turn_ids = {f.context_id for f in turn_started}
    speak_ids = {f.context_id for f in speak_started}

    grouped = len(turn_started) == 1 and len(turn_ids) == 1
    split = len(speak_started) == n_sentences and len(speak_ids) == n_sentences
    if not grouped:
        return False, (
            f"{n_sentences} предложений одного хода дали {len(turn_started)} started-фреймов "
            f"({len(turn_ids)} id) — группировки нет, reuse не активен, "
            f"уникальность SPEAK ничего не доказывает"
        )
    if not split:
        return False, (
            f"{n_sentences} SPEAK-реплик дали {len(speak_started)} started-фреймов "
            f"({len(speak_ids)} id) — ожидалось {n_sentences}"
        )
    return True, (
        f"контраст подтверждён: {n_sentences} предложений хода → 1 общий контекст, "
        f"{n_sentences} SPEAK → {n_sentences} разных id. SPEAK обходит reuse адресно"
    )


async def case_speak_amid_llm_turn_keeps_own_id() -> tuple[bool, str]:
    """The realistic shape: Kora's SPEAK lands in the middle of a dispatcher turn.

    This is the case the reply-window depends on — if the SPEAK inherited the dispatcher
    turn's context_id, a dispatcher run could claim Kora's utterance_id and the window would
    open for the wrong speaker. Identify ids by the text the service was asked to synthesize,
    not by counting.
    """
    kora_text = "Кора вклинилась со своей репликой."
    frames = [
        LLMFullResponseStartFrame(),
        LLMTextFrame(text="Диспетчер говорит первое. "),
        TTSSpeakFrame(text=kora_text),
        LLMTextFrame(text="Диспетчер говорит второе. "),
        LLMFullResponseEndFrame(),
    ]
    started, tts = await drive(frames)
    started_ids = {f.context_id for f in started}

    kora_ids = {cid for text, cid in tts.requests if kora_text in text}
    disp_ids = {cid for text, cid in tts.requests if kora_text not in text}
    if not kora_ids:
        return False, "реплика Коры вообще не дошла до синтеза"
    if not disp_ids:
        return False, "ни одно предложение диспетчера не дошло до синтеза — нечего сравнивать"

    overlap = kora_ids & disp_ids
    if overlap:
        return False, (
            f"SPEAK Коры унаследовал context_id хода диспетчера ({len(overlap)} общих) — "
            f"exact-корреляция невозможна, диспетчерский ран может забрать окно Коры"
        )
    if not kora_ids <= started_ids:
        return False, "id реплики Коры не дошёл до TTSStartedFrame"
    return True, (
        f"id Коры ({len(kora_ids)}) и id диспетчера ({len(disp_ids)}) не пересекаются; "
        f"id Коры присутствует в TTSStartedFrame"
    )


CASES = [
    ("SPEAK-реплики уникальны (20 чередований)", case_speak_frames_are_unique),
    ("контраст: ход группируется, SPEAK — нет", case_llm_turn_groups_into_one_id),
    ("SPEAK посреди хода не наследует id хода", case_speak_amid_llm_turn_keeps_own_id),
]


async def main() -> int:
    import importlib.metadata as md

    print(f"probe P3b — context_id presence/uniqueness · pipecat {md.version('pipecat-ai')}")
    print(f"TTSService.reuse_context_id_within_turn default = "
          f"{FakeTTSService()._reuse_context_id_within_turn}\n")

    results = []
    for title, case in CASES:
        try:
            ok, detail = await case()
        except Exception as e:  # a probe that errors is a probe that failed
            ok, detail = False, f"{type(e).__name__}: {e}"
        results.append(ok)
        print(f"  [{'OK ' if ok else 'FAIL'}] {title}\n         {detail}")

    verdict = all(results)
    print(f"\nВЕРДИКТ P3b (автоматическая половина): {'GO' if verdict else 'NO-GO'}")
    if verdict:
        print("  context_id присутствует и уникален на каждый TTSSpeakFrame →")
        print("  exact-корреляция context_id ↔ utterance_id реализуема, FIFO не нужен.")
        print("  Остаётся живая половина: Fish switch latency p95 ≤ 500 мс.")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
