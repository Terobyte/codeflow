"""DispatcherTurnLoop — the chat loop: builds messages, calls the LLM, dispatches tool
calls through ToolHandlers, and returns the final text for the caller (console.py / a future
pipecat adapter) to route into ArbiterPolicy. Journaling and the grounding check happen
here; `end_turn()` is left to the caller so `tts_texts` can be filled in AFTER the caller
drains the arbiter (R2/R6 evidence-ordering: the alert-durability guarantee is about
`alert()`, not the turn-close line, so it's fine for end_turn() to happen after TTS output
is known).
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from typing import Any, Callable, Protocol

from synapse.bridge.confirm import ConfirmFlow
from synapse.bridge.state import TaskStore
from synapse.clock import Clock
from synapse.config import SynapseConfig
from synapse.dispatcher.tools import ALL_SCHEMAS, ToolCall, ToolHandlers
from synapse.dispatcher.turn_context import build_turn_context
from synapse.journal import TurnJournal, TurnRecord

# B5: the authoritative set of dispatchable tool names — dispatch never resolves anything else.
_VALID_TOOL_NAMES = frozenset(s.name for s in ALL_SCHEMAS)


class LLMClient(Protocol):
    async def complete(self, messages: list[dict[str, Any]], tools: list[Any]) -> tuple[str, list[ToolCall]]:
        ...


# B10: tool passes per user turn are BOUNDED. The old shape was strictly two passes and
# silently dropped any tool_calls the second completion returned; a chaining LLM
# (get_task_status -> request_cancel) lost the follow-up. Loop until the model stops
# calling tools, capped so a pathological LLM can't spin forever (industry default 5-20).
_MAX_TOOL_PASSES = 5
_MAX_CACHED_THREADS = 64


def _append_coalesced(hist: list[dict[str, Any]], role: str, text: str) -> None:
    """Gate v2 C2' (MINOR): подряд идущие same-role реплики склеиваются в одно сообщение —
    войс-путь (D1') пишет user-транскрипты в ленту и без ответной пары, а Anthropic-шейп
    ждёт чередования ролей. Общая точка для регидрации и note_external_turn."""
    if hist and hist[-1].get("role") == role and isinstance(hist[-1].get("content"), str):
        hist[-1]["content"] += "\n" + text
    else:
        hist.append({"role": role, "content": text})


def history_from_feed(entries: list[dict]) -> list[dict[str, Any]]:
    """ЕДИНАЯ точка «лента треда → LLM-история»: HTTP-путь (`_history_for`, холодный
    кэш-мисс) и войс-путь (B44: build_session_pipeline сидит свежий контекст реконнекта из
    той же ленты) обязаны регидрироваться одинаково, иначе каналы расходятся в памяти.
    В историю идут ТОЛЬКО реплики (kind user/assistant) — кора-виды display-only и в
    LLM-контекст не попадают НИКОГДА (NO-EXFIL); срез по ПОСЛЕДНЕМУ kind=="clear" (каveat
    R5: очищенная командой «clear» история не должна воскресать из feed-архива)."""
    entries = list(entries)
    for i in range(len(entries) - 1, -1, -1):
        if isinstance(entries[i], dict) and entries[i].get("kind") == "clear":
            entries = entries[i + 1:]
            break
    hist: list[dict[str, Any]] = []
    for e in entries:
        # B-DISP-6: a corrupted feed (a bare string/None/list, or a future schema drift) must not
        # crash rehydration with AttributeError — skip non-dict entries instead. The clear-search
        # above guards the same way so a malformed entry never masquerades as the clear marker.
        if not isinstance(e, dict):
            continue
        kind = e.get("kind")
        if kind in ("user", "assistant"):
            _append_coalesced(hist, kind, str(e.get("text", "")))
    return hist


class DispatcherTurnLoop:
    def __init__(
        self,
        llm: LLMClient,
        handlers: ToolHandlers,
        confirm_flow: ConfirmFlow,
        store: TaskStore,
        journal: TurnJournal,
        clock: Clock,
        cfg: SynapseConfig,
        task_dictionary: dict[str, str] | None = None,
        thread_feed_reader: Callable[[str], list[dict]] | None = None,
        stage_block_for: Callable[[str], str] | None = None,
        on_compact: Callable[[str], None] | None = None,
        owner_thread_for: Callable[[str], str | None] | None = None,
        on_user_turn: Callable[[str, str, float], None] | None = None,
    ) -> None:
        self._llm = llm
        self._handlers = handlers
        self._confirm_flow = confirm_flow
        self._store = store
        self._journal = journal
        self._clock = clock
        self._cfg = cfg
        self._task_dictionary = task_dictionary or {}
        # UI-3 (спека §4, находка A): пер-тред контекст. История LLM ключуется по треду.
        self._thread_feed_reader = thread_feed_reader
        self._stage_block_for = stage_block_for
        # UI-5 (S10): колбэк на факт компакта истории треда (лента пишет event «контекст сжат»).
        self._on_compact = on_compact
        # С3: fan-out user turn на ApprovalService (опционально — стабы/консоль без approvals).
        self._on_user_turn = on_user_turn
        # Резолвер «id треда-владельца задачи» — скоуп терминальной задачи к её треду в
        # [СОСТОЯНИЕ] (иначе завершённая задача течёт во все треды, see should_hide_task).
        self._owner_thread_for = owner_thread_for
        self._histories: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        # Async turns normally share one event loop, but HTTP/voice adapters can call the
        # synchronous history surfaces from different threads. The lock is deliberately held
        # only for in-memory commits, never across an LLM await.
        self._history_locks: dict[str, threading.RLock] = {}
        self._history_locks_guard = threading.Lock()
        self._history_index_lock = threading.RLock()
        # Gate v2 C6 (sec-6): пер-тред поколение истории. clear_history инкрементит его; ход,
        # начатый ДО clear, при коммите видит несовпадение и НЕ воскрешает очищенную историю.
        self._generations: dict[str, int] = {}

    # Делегат на модульную функцию — общая точка с регидрацией (B44 вынес логику наверх).
    _append_coalesced = staticmethod(_append_coalesced)

    def _history_lock_for(self, thread_id: str) -> threading.RLock:
        with self._history_locks_guard:
            return self._history_locks.setdefault(thread_id, threading.RLock())

    def _history_for(self, thread_id: str) -> list[dict[str, Any]]:
        """Пер-тред контекст (спека §4, находка A): история LLM ключуется по треду.
        Холодный тред регидрируется из персиста через history_from_feed (единая точка
        «feed → history» с войс-каналом, B44)."""
        with self._history_lock_for(thread_id):
            with self._history_index_lock:
                hist = self._histories.get(thread_id)
            if hist is None:
                hist = []
                if self._thread_feed_reader is not None:
                    hist = history_from_feed(list(self._thread_feed_reader(thread_id)))
                with self._history_index_lock:
                    self._histories[thread_id] = hist
                    while len(self._histories) > _MAX_CACHED_THREADS:
                        self._histories.popitem(last=False)
            else:
                with self._history_index_lock:
                    self._histories.move_to_end(thread_id)
            return hist

    def note_external_turn(self, thread_id: str, role: str, text: str) -> None:
        """Gate v2 D4' (sec-5): реплика, прошедшая МИМО ingest_user_turn (войс-путь пишет её в
        feed напрямую), доливается в ТЁПЛУЮ LLM-историю треда, чтобы кэшированный тред увидел
        разговор без рестарта. Кэш-мисс — no-op: холодная регидрация подхватит её из feed сама
        (writer уже положил запись в ленту до вызова)."""
        with self._history_lock_for(thread_id):
            with self._history_index_lock:
                hist = self._histories.get(thread_id)
            if hist is None:
                return
            self._append_coalesced(hist, role, text)

    def clear_history(self, thread_id: str) -> None:
        """Gate v2 C1': команда «clear» — LLM-история треда чистится IN-PLACE (живая ссылка,
        дисциплина _maybe_compact) + поколение инкрементится (C6). Feed-файл НЕ трогается
        (лента = архив); clear-маркер в ленту пишет РОУТ (канонический слой записи —
        webrtc_server, как у user/assistant)."""
        with self._history_lock_for(thread_id):
            with self._history_index_lock:
                hist = self._histories.get(thread_id)
            if hist is not None:
                hist[:] = []
            self._generations[thread_id] = self._generations.get(thread_id, 0) + 1

    async def force_compact(self, thread_id: str) -> None:
        """Gate v2 C1': команда «compact» — немедленный компакт истории треда, минуя порог
        (threshold_override=1: жмём, если есть что резать). LLM-ХОД диспетчера не зовётся —
        только внутренний вызов сжатия; событие ленты пишет существующий on_compact."""
        history = self._history_for(thread_id)
        await self._maybe_compact(thread_id, history, threshold_override=1)

    async def ingest_user_turn(self, transcript: str, thread_id: str = "voice") -> tuple[TurnRecord, str]:
        now = self._clock.now()
        record = self._journal.begin_turn(transcript)
        record.thread_id = thread_id
        self._handlers.begin_turn(record.turn_id)

        # R3: MUST run before the LLM call — half (a) of Р-16's double-key confirm check.
        self._confirm_flow.note_user_turn(transcript, now)
        # С3: fan-out на ApprovalService (gate_action) — тот же user turn кормит approval-flow.
        # Хост передаёт опциональный колбэк; для стабов/консоли без approvals он None.
        if self._on_user_turn is not None:
            self._on_user_turn(thread_id, transcript, now)

        had_active_task = self._store.has_active_task()
        history = self._history_for(thread_id)
        # Gate v2 C6: снимок поколения ДО await'ов — сверяется на коммите (см. ниже).
        generation = self._generations.get(thread_id, 0)
        # UI-5 (S10): компакт ПЕРЕД ходом — старшая половина жмётся отдельным LLM-вызовом,
        # если история длиннее порога. Мутирует список IN-PLACE (history[:] = ...): ребинд
        # локальной `history` дошёл бы до _complete ЭТОГО хода, но self._histories[thread_id]
        # остался бы несжатым → на следующем ходу всё всплыло бы обратно.
        await self._maybe_compact(thread_id, history)

        # B02: run the WHOLE turn (LLM call + tool loop) on a LOCAL snapshot of the shared history,
        # never mutating the shared list across the `await self._complete(...)` suspension. Two
        # concurrent turns on the SAME thread otherwise interleave their appends into the one shared
        # list — user messages stack with no separating assistant turn, each _complete sees the
        # other's in-flight messages, and the error-path rollback (`del history[snapshot-1:]`) cut
        # from an index the other turn had already grown, discarding its data too. Only the final
        # (user, assistant) pair is committed to the shared history, atomically, after this turn's
        # own completion returns. The shared history holds only user/assistant across turns —
        # consistent with feed rehydration and compaction (both user/assistant only); intra-turn
        # tool messages live in `working` and are discarded, exactly as a cold rehydrate would.
        working = list(history)
        working.append({"role": "user", "content": transcript})

        text = ""
        try:
            text, tool_calls = await self._complete(working, thread_id)
            record.llm_output = text
            passes = 0
            # Р-2: a tool turn needs at least one more completion with the tool results in context —
            # that call produces the text the dispatcher actually says. B10: keep going while the
            # model keeps chaining tools, bounded by _MAX_TOOL_PASSES; on cap exhaustion the tail
            # tool_calls are dropped (same behavior the old 2-pass shape had on pass 2).
            while tool_calls and passes < _MAX_TOOL_PASSES:
                # UI-3: канонический шейп — tool-результату предшествует assistant-ход с
                # tool_use-анонсом (без него Anthropic Messages API отклоняет историю).
                working.append({
                    "role": "assistant",
                    "content": text or "",
                    "tool_calls": [
                        {"id": c.id, "name": c.name, "arguments": c.arguments} for c in tool_calls
                    ],
                })
                for call in tool_calls:
                    await self._dispatch_tool(call, working)
                text, tool_calls = await self._complete(working, thread_id)
                if text:
                    record.llm_output = text
                passes += 1
        except Exception:
            # Nothing was committed to the shared history yet (the turn ran on `working`), so
            # there is nothing to roll back — just close the journal turn (B-PIPE-4: the caller
            # won't get a chance since the exception propagates).
            self._journal.end_turn()
            raise
        finally:
            record.latency_ms = (self._clock.now() - now) * 1000.0
            self._journal.check_grounding(record, had_active_task)
        # Commit this turn's user msg + final assistant reply to the SHARED history in one
        # synchronous burst (no await between the two appends → no concurrent turn can interleave
        # and stack a second user with no assistant between).
        # Gate v2 C6 (B20-стиль: правда на момент коммита, не до-await снимок): «clear»,
        # прилетевший во время нашего await, инкрементит поколение — поздний коммит этой
        # (user, assistant)-пары молча воскресил бы только что очищенную историю. Скип, не ошибка.
        with self._history_lock_for(thread_id):
            if self._generations.get(thread_id, 0) == generation:
                history.append({"role": "user", "content": transcript})
                if text:
                    history.append({"role": "assistant", "content": text})
        return record, text

    async def _complete(
        self, history: list[dict[str, Any]], thread_id: str
    ) -> tuple[str, list[ToolCall]]:
        # С1: единая фабрика контекста хода (раньше — инлайн сборка system_prompt+state_block).
        # Голос теперь собирает через ту же фабрику, поэтому [СОСТОЯНИЕ] симметричен между
        # каналами; `_render_state` умер (тело переехало в build_turn_context).
        ctx = build_turn_context(
            cfg=self._cfg, store=self._store, clock=self._clock, thread_id=thread_id,
            task_dictionary=self._task_dictionary,
            stage_block_for=self._stage_block_for,
            owner_thread_for=self._owner_thread_for,
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": ctx.system_message},
            *history,
        ]
        return await self._llm.complete(messages, ALL_SCHEMAS)

    async def _dispatch_tool(self, call: ToolCall, history: list[dict[str, Any]]) -> Any:
        # B5: dispatch ONLY the declared tools. A hallucinated/adversarial name that collides with
        # a real ToolHandlers method (e.g. `begin_turn`) must NOT be `getattr`'d and invoked —
        # validate against the ALL_SCHEMAS allowlist first, not just "is it an attribute".
        handler = getattr(self._handlers, call.name, None) if call.name in _VALID_TOOL_NAMES else None
        if handler is None:
            result: Any = {"error": f"unknown tool {call.name}"}
        else:
            try:
                result = await handler(**call.arguments)
            except TypeError as exc:
                result = {"error": f"invalid arguments for {call.name}: {exc}"}
        history.append(
            {"role": "tool", "tool_call_id": call.id, "name": call.name,
             "content": json.dumps(result, ensure_ascii=False)}
        )
        return result

    def _render_state(self, now: float, thread_id: str | None = None) -> str:
        # С1: метод оставлен для обратной совместимости (его зовут существующие тесты и,
        # возможно, внешние вызовы), но сборка делегирована в build_turn_context. Параметр now
        # принят, но игнорируется — snapshot времени снимается clock-ом фабрики (единственный
        # источник правды, как и в голосовом пути). Терминальная задача чужого треда прячется.
        ctx = build_turn_context(
            cfg=self._cfg, store=self._store, clock=self._clock, thread_id=thread_id,
            stage_block_for=self._stage_block_for,
            owner_thread_for=self._owner_thread_for,
        )
        return ctx.state_block

    # --- UI-5 (S10): компакт длинной истории -------------------------------------------

    async def _maybe_compact(
        self, thread_id: str, history: list[dict[str, Any]], threshold_override: int | None = None
    ) -> None:
        """Сжать старшую половину истории, если она длиннее порога, ПЕРЕД ходом.

        Gate v2 C1' (MINOR): `threshold_override` — явный параметр для force_compact (команда
        «compact» жмёт немедленно, порог=1); None → конфиг-порог dispatcher_compact_after.

        Мутирует `history` IN-PLACE (history[:] = ...), а не ребиндит локальную ссылку:
        `_history_for` отдаёт ЖИВУЮ ссылку на `self._histories[thread_id]`, поэтому только
        inplace-мутация переживает следующий ход (анти-rebind-якорь в тесте).

        Граница разреза — МЕХАНИЧЕСКАЯ: `cut = len//2`, продвинутый вперёд до первого
        role==user на/после cut (user всегда начинает свежую turn-группу). Жать только
        целые группы — оборванная tool_use/tool_result-пара ломает Anthropic API. Поскольку
        история здесь содержит только user/assistant (регидрация + этот метод), роль user —
        корректный срез-маркер; tool-хвостов в `history` нет (они удаляются на откате хода).
        """
        threshold = threshold_override if threshold_override is not None else self._cfg.dispatcher_compact_after
        if threshold <= 0 or len(history) <= threshold:
            return
        cut = len(history) // 2
        # продвинуть cut до первого role==user (на или после cut) — не оставлять начало
        # хвоста assistant-репликой без предшествующего user
        while cut < len(history) and history[cut].get("role") != "user":
            cut += 1
        if cut >= len(history):
            # после cut нет user-сообщения — хвост одна группа, резать нечего чисто
            return
        older = history[:cut]
        # NO-EXFIL: в историю компакта попадают ТОЛЬКО user/assistant (по построению здесь
        # другого и нет); кора-виды/лента сюда не входят. tools=[] — компакт без инструментов.
        compact_messages = [
            {"role": "system", "content": (
                "Сожми диалог диспетчера ниже в краткую выжимку. Сохрани решения, имена, пути "
                "файлов и договорённости дословно. Не добавляй ничего от себя."
            )},
            {"role": "user", "content": json.dumps(older, ensure_ascii=False)},
        ]
        try:
            summary, _ = await self._llm.complete(compact_messages, [])
        except Exception:  # noqa: BLE001 — сбой компакта не должен валить ход диспетчера
            return
        summary = (summary or "").strip() or "[история сжата]"
        # B20: сплайсим по ТЕКУЩЕМУ списку, НЕ по до-await снимку `tail`. Другой ход этого же
        # треда во время нашего `await self._llm.complete` дописывает свою (user,assistant)-пару
        # в КОНЕЦ общего списка (turn_lock отпущен до ingest — B-PIPE-5); ребинд из устаревшего
        # `tail` молча их терял (B20). Старшая половина, которую мы сжали, append-иммутабельна
        # (ходы дописывают только хвост, голову не трогают) — если она всё ещё голова списка,
        # заменяем РОВНО её и сохраняем всё, что теперь идёт следом (исходный хвост + чужие
        # коммиты). Если параллельный ход уже сам пересобрал/сжал список (голова не совпала) —
        # no-op, а не затирание его результата.
        compacted = False
        with self._history_lock_for(thread_id):
            if history[:len(older)] == older:
                history[:len(older)] = [{"role": "user", "content": f"[КОМПАКТ] {summary}"}]
                compacted = True
        if compacted and self._on_compact is not None:
            try:
                self._on_compact(thread_id)
            except Exception:  # noqa: BLE001 — колбэк ленты не валит ход
                pass
