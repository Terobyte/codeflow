"""Destructive-task confirmation (Р-12, protocol Р-16, §4/§11.2).

Double-key confirm: (a) a user turn must have happened between the read-back and
`confirm()` — a `confirm_task` call with no intervening user turn is a self-attempt and is
rejected + alerted (CONFIRM_SELF_ATTEMPT); (b) the transcript of that user turn must pass a
narrow, deterministic affirm/deny check — the LLM's own decision and the transcript's
affirm-check must agree, disagreement is a reject.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Iterable, Protocol

from synapse.bridge.state import TaskStatus, TaskStore
from synapse.clock import Clock
from synapse.journal import AlertKind, TurnJournal

_task_id_counter = itertools.count(1)


def _new_task_id(now: float) -> str:
    return f"task-{int(now * 1000)}-{next(_task_id_counter)}"


_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _normalize(text: str) -> str:
    return _PUNCT_RE.sub("", text.lower()).strip()


def _words(text: str) -> set[str]:
    return set(_normalize(text).split())


def _classify_response(text: str, affirm_words: frozenset[str], deny_words: frozenset[str]) -> str:
    words = _words(text)
    if words & deny_words:
        return "deny"
    if words & affirm_words:
        return "affirm"
    return "unclear"


class DestructiveClassifier(Protocol):
    def is_destructive(self, text: str) -> bool: ...


class KeywordClassifier:
    """Keyword-substring destructive-intent classifier. Fail-safe: any match, however weak,
    resolves to True — a missed keyword is a silent safety hole, a false positive just costs
    one extra voice confirmation."""

    def __init__(self, keywords: Iterable[str]) -> None:
        self._keywords = tuple(k.lower() for k in keywords)

    def is_destructive(self, text: str) -> bool:
        normalized = text.lower()
        return any(kw in normalized for kw in self._keywords)


class ConfirmOutcome(str, Enum):
    COMMITTED = "committed"
    STAGED = "staged"
    REJECTED_ACTIVE = "rejected_active"


@dataclass
class SubmitResult:
    outcome: ConfirmOutcome
    task_id: str | None = None
    readback_text: str | None = None
    reject_text: str | None = None


class ConfirmDecisionOutcome(str, Enum):
    COMMITTED = "committed"
    REJECTED = "rejected"
    REREADBACK = "rereadback"
    RESET = "reset"


@dataclass
class ConfirmResult:
    outcome: ConfirmDecisionOutcome
    text: str | None = None
    task_id: str | None = None


@dataclass
class _Staged:
    task_id: str
    text: str
    readback_text: str
    rereadback_count: int = 0
    awaiting_user_turn: bool = True
    last_readback_ts: float = 0.0


class ConfirmFlow:
    def __init__(
        self,
        store: TaskStore,
        clock: Clock,
        classifier: DestructiveClassifier,
        journal: TurnJournal,
        affirm_words: frozenset[str],
        deny_words: frozenset[str],
        max_rereadbacks: int,
        confirm_timeout_s: float,
    ) -> None:
        self._store = store
        self._clock = clock
        self._classifier = classifier
        self._journal = journal
        self._affirm_words = affirm_words
        self._deny_words = deny_words
        self._max_rereadbacks = max_rereadbacks
        self._confirm_timeout_s = confirm_timeout_s
        self._last_user_turn_transcript = ""
        self._last_user_turn_ts: float | None = None

        self._staged: _Staged | None = None
        persisted = store.staged
        if persisted:
            # B37: a staged blob written by a different schema version must be DROPPED, not crash
            # boot — `_Staged(**persisted)` TypeErrors on an unexpected/missing key. Best-effort
            # restore, same contract as the store's own B18 persistence hardening.
            try:
                self._staged = _Staged(**persisted)
            except (TypeError, ValueError):
                self._staged = None
        # B12: reconcile the crash-between-writes scar. A PENDING_CONFIRMATION task with NO staged
        # blob (submit persisted start_task but died before set_staged, pre-atomic-stage_task) can
        # never be resolved by the normal flow — has_active_task() blocks every submit while
        # confirm() rejects («Подтверждать нечего», self._staged is None). Drop the dangling task on
        # construction so the flow un-wedges. Unreachable now that submit stages atomically, but a
        # state.json written by an older build can still carry the scar across a restart.
        if (self._staged is None
                and store.task is not None
                and store.task.status == TaskStatus.PENDING_CONFIRMATION):
            store.clear_task()

    @property
    def staged(self) -> _Staged | None:
        return self._staged

    def submit(self, text: str, now: float) -> SubmitResult:
        if self._store.has_active_task():
            return SubmitResult(
                outcome=ConfirmOutcome.REJECTED_ACTIVE,
                reject_text="У меня уже есть активная задача, новую пока принять не могу.",
            )
        if self._classifier.is_destructive(text):
            task_id = _new_task_id(now)
            readback = f'Подтверди необратимую задачу: "{text}"'
            self._staged = _Staged(
                task_id=task_id, text=text, readback_text=readback,
                rereadback_count=0, awaiting_user_turn=True, last_readback_ts=now,
            )
            # B12: ONE atomic persist for task + staged. The old two-write pair (start_task then
            # set_staged) had a crash window that wedged the flow forever if it hit between them.
            self._store.stage_task(task_id, text, asdict(self._staged), now)
            return SubmitResult(outcome=ConfirmOutcome.STAGED, task_id=task_id, readback_text=readback)
        task_id = _new_task_id(now)
        self._store.start_task(task_id, text, TaskStatus.RUNNING, now)
        return SubmitResult(outcome=ConfirmOutcome.COMMITTED, task_id=task_id)

    def note_user_turn(self, transcript: str, now: float) -> None:
        """R3: the dispatcher loop MUST call this for every user turn, before the LLM runs
        — this is half (a) of the double-key check in confirm()."""
        self._last_user_turn_transcript = transcript
        self._last_user_turn_ts = now
        if self._staged is not None:
            self._staged.awaiting_user_turn = False

    def confirm(self, llm_decision: str, now: float) -> ConfirmResult:
        if self._staged is None:
            return ConfirmResult(
                outcome=ConfirmDecisionOutcome.REJECTED,
                text="Подтверждать нечего — нет задачи, ожидающей подтверждения.",
            )
        # B1 (CRIT): a `request_cancel` while a destructive task is PENDING_CONFIRMATION flips the
        # STORE to CANCEL_REQUESTED but leaves this dangling `_staged`. A confirm must NEVER
        # resurrect a task the user cancelled — verify the store still holds THIS staged task in
        # PENDING_CONFIRMATION; any divergence (cancelled, gone, superseded) drops the stale confirm.
        task = self._store.task
        if task is None or task.id != self._staged.task_id or task.status != TaskStatus.PENDING_CONFIRMATION:
            self._staged = None
            self._store.set_staged(None)
            return ConfirmResult(
                outcome=ConfirmDecisionOutcome.REJECTED,
                text="Эта задача уже не ждёт подтверждения.",
            )
        if self._staged.awaiting_user_turn:
            self._journal.alert(AlertKind.CONFIRM_SELF_ATTEMPT, {"task_id": self._staged.task_id})
            return ConfirmResult(
                outcome=ConfirmDecisionOutcome.REJECTED,
                text="Не могу подтвердить без твоего ответа.",
            )
        if now - self._staged.last_readback_ts >= self._confirm_timeout_s:
            return self._reset("подтверждение не разобрал, задача отложена")

        response = _classify_response(self._last_user_turn_transcript, self._affirm_words, self._deny_words)
        if response == "deny":
            return self._reset("хорошо, задачу отменяю")
        if response == "unclear":
            return self._rereadback()

        # response == "affirm": LLM decision and the transcript's affirm-check must agree.
        if llm_decision.strip().lower() != "confirm":
            return ConfirmResult(
                outcome=ConfirmDecisionOutcome.REJECTED,
                text="Не разобрался с подтверждением, уточни ещё раз.",
            )
        task_id = self._staged.task_id
        self._store.set_task_status(TaskStatus.RUNNING)
        self._store.set_staged(None)
        self._staged = None
        return ConfirmResult(outcome=ConfirmDecisionOutcome.COMMITTED, task_id=task_id)

    def _rereadback(self) -> ConfirmResult:
        assert self._staged is not None
        self._staged.rereadback_count += 1
        if self._staged.rereadback_count > self._max_rereadbacks:
            return self._reset("подтверждение не разобрал, задача отложена")
        self._staged.awaiting_user_turn = True
        self._staged.last_readback_ts = self._clock.now()
        self._store.set_staged(asdict(self._staged))
        return ConfirmResult(outcome=ConfirmDecisionOutcome.REREADBACK, text=self._staged.readback_text)

    def _reset(self, phrase: str) -> ConfirmResult:
        task_id = self._staged.task_id if self._staged else None
        self._staged = None
        self._store.set_staged(None)
        self._store.clear_task()
        return ConfirmResult(outcome=ConfirmDecisionOutcome.RESET, text=phrase, task_id=task_id)
