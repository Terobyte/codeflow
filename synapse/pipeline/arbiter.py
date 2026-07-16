"""ArbiterPolicy — TTS queue arbitration (Р-5, уточнение §4). SPEAK jumps to the head of the
queue; the dispatcher sentence already at the front is allowed to finish, the rest of that
dispatcher turn is dropped (by construction, Р-15 redaction means it can't contain critical
facts — nothing lost). `flush_dispatcher()` clears only dispatcher-sourced buffered items on
an intra-turn cascade retry (§4), leaving any pending SPEAK alone.

S2 (accepted-narrow): this class is buffering/priority/drop-tail/flush ONLY — mid-utterance
audio splicing is pipecat's own TTSService job, not duplicated here.
S3: default sentence splitter reuses pipecat's own boundary matcher (`match_endofsentence`),
imported lazily (see `default_sentence_splitter`) so importing this module never touches the
network (nltk's punkt_tab data check) just to build a queue.

INVARIANT (S6): every mutating method on ArbiterPolicy is synchronous and await-free, so it
is safe to call re-entrantly from a single event loop without a lock. All I/O — actually
pushing frames downstream — belongs in `TTSArbiterProcessor`, never here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

Splitter = Callable[[str], list[str]]


def default_sentence_splitter(text: str) -> list[str]:
    from pipecat.utils.string import match_endofsentence

    sentences: list[str] = []
    remaining = text
    while remaining:
        idx = match_endofsentence(remaining)
        if idx <= 0:
            # incomplete streaming tail: keep verbatim so fragments re-concatenate
            # downstream (pipecat TTS) with their spaces intact -- stripping here mashes
            # adjacent fragments ("Задачапо рефакторингу"). Invariant: join(result) == text.
            sentences.append(remaining)
            break
        end = idx
        while end < len(remaining) and remaining[end].isspace():
            end += 1
        sentences.append(remaining[:end])  # sentence + its trailing whitespace, no strip
        remaining = remaining[end:]
    return sentences


@dataclass
class QueueItem:
    source: str  # "dispatcher" | "speak"
    text: str


class ArbiterPolicy:
    def __init__(self, splitter: Splitter | None = None) -> None:
        self._splitter = splitter or default_sentence_splitter
        self._queue: list[QueueItem] = []
        self._dispatcher_override: str | None = None

    def __len__(self) -> int:
        return len(self._queue)

    def push_dispatcher_text(self, text: str) -> None:
        if not text:
            return
        for sentence in self._splitter(text):
            self._queue.append(QueueItem(source="dispatcher", text=sentence))

    def push_speak(self, text: str) -> None:
        new = QueueItem(source="speak", text=text)
        # B36: SPEAK jumps ahead of the dispatcher TAIL (survivor = the sentence already playing
        # is kept), but multiple pending SPEAKs stay FIFO among themselves — the older critical
        # readback must not be delayed behind a newer one.
        if self._queue and self._queue[0].source == "dispatcher":
            survivor = self._queue[0]
            rest_speaks = [item for item in self._queue[1:] if item.source != "dispatcher"]
            self._queue = [survivor] + rest_speaks + [new]
        else:
            kept_speaks = [item for item in self._queue if item.source != "dispatcher"]
            self._queue = kept_speaks + [new]

    def flush_dispatcher(self) -> None:
        self._queue = [item for item in self._queue if item.source != "dispatcher"]

    def set_dispatcher_override(self, text: str | None) -> None:
        """Replace the next complete LLM response; used by narrow deterministic guards."""
        self._dispatcher_override = text

    def consume_dispatcher_override(self) -> str | None:
        text, self._dispatcher_override = self._dispatcher_override, None
        return text

    def pop_next(self) -> QueueItem | None:
        if not self._queue:
            return None
        return self._queue.pop(0)

    def drain_all(self) -> list[QueueItem]:
        items, self._queue = self._queue, []
        return items


class TTSArbiterProcessor(FrameProcessor):
    """Adapter: dispatcher TextFrames go through `policy.push_dispatcher_text()`, SPEAK
    (`TTSSpeakFrame`) through `policy.push_speak()`, and the drained queue is pushed
    downstream. All I/O lives here — see the ArbiterPolicy invariant docstring above."""

    def __init__(self, policy: ArbiterPolicy) -> None:
        super().__init__()
        self._policy = policy
        self._active_override: str | None = None
        self._override_emitted = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._active_override = self._policy.consume_dispatcher_override()
            self._override_emitted = False
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMFullResponseEndFrame):
            if self._active_override is not None and not self._override_emitted:
                self._policy.push_dispatcher_text(self._active_override)
                await self._drain()
            self._active_override = None
            self._override_emitted = False
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, TTSSpeakFrame):
            self._policy.push_speak(frame.text)
            await self._drain()
            return
        if isinstance(frame, TextFrame):
            if self._active_override is not None:
                if not self._override_emitted:
                    self._policy.push_dispatcher_text(self._active_override)
                    self._override_emitted = True
                # Suppress every provider-authored chunk in this generation.
            else:
                self._policy.push_dispatcher_text(frame.text)
            await self._drain()
            return
        await self.push_frame(frame, direction)

    async def _drain(self) -> None:
        for item in self._policy.drain_all():
            if item.source == "speak":
                # SPEAK is Kora's operational readback (Р-5/Р-15), never LLM-authored text --
                # keep it out of the LLM context (append_to_context=False) so it can't leak into
                # a later generation now that assistant_aggregator sits downstream of tts.
                await self.push_frame(
                    TTSSpeakFrame(text=item.text, append_to_context=False), FrameDirection.DOWNSTREAM
                )
            else:
                await self.push_frame(TextFrame(text=item.text), FrameDirection.DOWNSTREAM)
