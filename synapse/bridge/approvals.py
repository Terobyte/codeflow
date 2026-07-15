"""ApprovalService — двухключевой контракт для gate_action (Ф0.3, слайс С3).

Обобщает ConfirmFlow (`synapse/bridge/confirm.py`) на запуск Коры через гейт: `confirm=true`
из tool call перестаёт быть властью — единственный путь запустить — пройти оба ключа:
  (a) между readback (stage) и consume обязан пройти user turn;
  (b) транскрипт этого turn-а обязан пройти affirm-проверку (affirm.py);
  (c) digest свода в момент consume обязан совпасть со staged. digest несёт И стадию треда:
      любое движение стадии между stage() и consume() инвалидирует pending структурно, по
      несовпадению, — без обратной зависимости threads→bridge.

Хранение v1 — in-memory: рестарт теряет pending, пользователь подтверждает заново (персист —
вместе с audit-хранилищем Фазы 1).
"""
from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass

from synapse.bridge.affirm import classify_affirm
from synapse.clock import Clock

_approval_seq = itertools.count(1)


def _new_approval_id(now: float) -> str:
    """Одноразовый id `apr-{ms}-{seq}`, disjoint от task-…/gate-… пространств имён."""
    return f"apr-{int(now * 1000)}-{next(_approval_seq)}"


def gate_digest(request_text: str | None, action: str, model: str | None,
                fast: bool, stage: str | None) -> str:
    """Детерминированный digest свода запуска: sha256(request_text | action | model | fast | stage).
    Несёт И стадию треда — смена стадии между stage() и consume() инвалидирует pending по
    несовпадению digest, без перечисления всех set_stage-путей и без обратной зависимости."""
    payload = "|".join((
        request_text or "",
        action,
        model or "",
        "1" if fast else "0",
        stage or "",
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Pending:
    approval_id: str
    thread_id: str
    action: str
    digest: str
    staged_turn_seq: int
    issued_at: float
    expires_at: float


@dataclass(frozen=True)
class Approval:
    """Результат успешного consume — одноразовый токен запуска; уходит в журнал вместе с фактом."""
    approval_id: str
    thread_id: str
    action: str
    digest: str


class ApprovalService:
    """Double-key контракт ConfirmFlow, обобщённый на gate_action.

    Каналы различаются источником подтверждения:
    - HTTP `/api/threads/{id}/gate` с confirm:true несёт КЛИК живого пользователя —
      ApprovalService не требуется (клик = второй ключ). Хост передаёт user_initiated=True.
    - Голосовой tool-путь (gate_action из LLM) — user_initiated=False: без ApprovalService
      модель сама выставила бы confirm=true (self-approval запуска кода).
    """

    def __init__(
        self,
        clock: Clock,
        ttl_s: float,
        affirm_words: frozenset[str],
        deny_words: frozenset[str],
    ) -> None:
        self._clock = clock
        self._ttl_s = ttl_s
        self._affirm_words = affirm_words
        self._deny_words = deny_words
        # per-thread: один pending на тред (одна активная задача — синглтон).
        self._pending: dict[str, _Pending] = {}
        # последний user-транскрипт (та же точка входа, что ConfirmFlow.note_user_turn).
        self._last_user_turn: dict[str, tuple[int, str]] = {}
        # Monotonic per-thread watermark.  stage() snapshots it; consume() accepts only a
        # transcript from a strictly newer turn.  Timestamps are not enough: FakeClock and real
        # clocks may legitimately give stage/user events the same value.
        self._user_turn_seq: dict[str, int] = {}

    def stage(self, thread_id: str, action: str, digest: str, now: float) -> str:
        """Запомнить pending, вернуть readback-текст для озвучки.Pending ждёт user turn + affirm."""
        aid = _new_approval_id(now)
        self._pending[thread_id] = _Pending(
            approval_id=aid, thread_id=thread_id, action=action, digest=digest,
            staged_turn_seq=self._user_turn_seq.get(thread_id, 0),
            issued_at=now, expires_at=now + self._ttl_s,
        )
        return (f"Запускаю выполнение. Подтверди: «да», или отклони: «нет». "
                f"Действие: {action}.")

    def note_user_turn(self, thread_id: str, transcript: str, now: float) -> None:
        """Та же точка входа, что ConfirmFlow.note_user_turn — хост делает один fan-out
        на оба сервиса. Ключится по thread_id: ответ из треда Б не подтверждает pending треда А."""
        seq = self._user_turn_seq.get(thread_id, 0) + 1
        self._user_turn_seq[thread_id] = seq
        self._last_user_turn[thread_id] = (seq, transcript)

    def invalidate(self, thread_id: str) -> None:
        """Явная инвалидация с call site-ов app.py (смена СВОДА: set_request, revise-ветка).
        Смену СТАДИИ ловит digest сам — invalidate для неё не нужен."""
        self._pending.pop(thread_id, None)

    def consume(self, thread_id: str, action: str, digest: str, now: float) -> Approval | None:
        """None ⇔ нет staged / не было user turn / не affirm / digest сменился / TTL истёк.
        Успех — одноразовый: pending гасится сразу (повторный consume потребует новой стадии)."""
        pending = self._pending.get(thread_id)
        if pending is None:
            return None
        # TTL
        if now > pending.expires_at:
            self._pending.pop(thread_id, None)
            return None
        # action/digest обязаны совпасть (digest несёт stage — смена стадии инвалидирует)
        if pending.action != action or pending.digest != digest:
            self._pending.pop(thread_id, None)
            return None
        # (a) intervening user turn
        noted = self._last_user_turn.get(thread_id)
        if noted is None:
            return None
        turn_seq, transcript = noted
        if turn_seq <= pending.staged_turn_seq:
            return None
        # (b) affirm-проверка транскрипта
        response = classify_affirm(transcript, self._affirm_words, self._deny_words)
        if response != "affirm":
            # deny / unclear → pending НЕ гасится (unclear → повторный readback в gate_action)
            return None
        # успех — одноразовый: гасим pending, возвращаем approval
        self._pending.pop(thread_id, None)
        self._last_user_turn.pop(thread_id, None)
        return Approval(
            approval_id=pending.approval_id,
            thread_id=thread_id, action=action, digest=digest,
        )
