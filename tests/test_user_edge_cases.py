# -*- coding: utf-8 -*-
"""Пользовательские edge-case сценарии как regression guards.

Каждый тест = реальный сценарий юзера CodeFlow/Synapse (голосовой кодинг-агент: «диспетчер»
Flow уточняет намерение, «Кора» Kora пишет код). Все гварды, которые здесь проверяются,
РЕАЛЬНО существуют в коде, но на этих конкретных пользовательских траекториях не были покрыты.

Это НЕ доказанные баги (красных среди них нет) — это зелёные regression guards на тонкие места
границы пользовательского взаимодействия. Подробности и категоризация — в описании каждого теста.
"""
import asyncio
import json

import pytest

# Эти гварды — зеркало test_*_failing.py: cascade.services тянет pipecat, голосовые модули —
# aiortc/cv2/fastapi. Без них модуль просто пропускается (importorskip), а не падает на сборке.
pytest.importorskip("pipecat")
pytest.importorskip("aiortc")
pytest.importorskip("cv2")
pytest.importorskip("fastapi")

from synapse.bridge.confirm import (
    ConfirmDecisionOutcome,
    ConfirmFlow,
    ConfirmOutcome,
    KeywordClassifier,
)
from synapse.bridge.state import Liveness, TaskStatus, TaskStore
from synapse.cascade.services import CostCap
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.dispatcher.tools import KoraBridge, ToolHandlers
from synapse.journal import TurnJournal
from synapse.prompt import COMMANDS_NOTE, build_system_prompt
from synapse.threads import Thread, ThreadStore


# --- общие фабрики -----------------------------------------------------------------------

AFFIRM = frozenset({"да", "подтверждаю", "делай"})
DENY = frozenset({"нет", "отмена", "стоп"})


def _flow(tmp_path, *, max_rereadbacks=2, confirm_timeout_s=30.0):
    """ConfirmFlow + TaskStore на чистом tmp_path (без state.json)."""
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock, session_id="s")
    clf = KeywordClassifier({"удали", "снеси"})
    flow = ConfirmFlow(
        store, clock, clf, journal, AFFIRM, DENY, max_rereadbacks, confirm_timeout_s
    )
    return flow, store, clock, journal


def _handlers(tmp_path, *, on_answer=None, on_gate=None, thread_id=None, channel="voice"):
    """ToolHandlers с настроечным bridge для staging/answer инструментов."""
    clock = FakeClock(0.0)
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock, session_id="s")
    clf = KeywordClassifier(cfg.destructive_keywords)
    confirm_flow = ConfirmFlow(
        store, clock, clf, journal, cfg.affirm_words, cfg.deny_words,
        cfg.max_rereadbacks, cfg.confirm_timeout_s,
    )
    bridge = KoraBridge(
        store=store, confirm_flow=confirm_flow, clock=clock, cfg=cfg,
        on_answer=on_answer, on_gate=on_gate,
        thread_id_for=(lambda: thread_id), channel=channel,
    )
    return ToolHandlers(bridge, journal), store, clock, journal


# =========================================================================================
# 1. «Я подтвердил голосом, но передумал, пока диспетчер дочитывал» — deny обязан победить
#    даже после того, как LLM решил confirm (disagreement → reject).
# =========================================================================================


def test_ec1_user_says_no_but_llm_says_confirm_rejects(tmp_path):
    """Юзер: поставил «удали бэкапы» → зачитка → «нет». Диспетчер (LLM) ошибочно решил
    confirm. Double-key (b): транскрипт ('нет' ∈ DENY) и LLM-decision не сходятся →
    RESET, задача НЕ запускается. Подтверждение необратимой задачи не на воле LLM."""
    flow, store, clock, journal = _flow(tmp_path)
    flow.submit("удали старые бэкапы", now=0.0, thread_id="voice")
    flow.note_user_turn("нет, отмена", now=1.0, thread_id="voice")

    result = flow.confirm("confirm", now=1.0, thread_id="voice")  # LLM says confirm

    assert result.outcome == ConfirmDecisionOutcome.RESET
    assert store.task is None  # задача отложена, ничего не запущено


# =========================================================================================
# 2. Cancel посреди PENDING_CONFIRMATION, затем приходящий confirm — не воскресить отменённое.
# =========================================================================================


def test_ec2_cancel_then_confirm_does_not_resurrect(tmp_path):
    """Юзер поставил необратимую задачу, передумал → request_cancel. Диспетчер всё ещё
    пытается confirm_task. B1 (CRIT): стор уже в CANCEL_REQUESTED, staged-задача не должна
    воскреснуть от запоздалого confirm. Диспетчер скажет «уже не ждёт подтверждения»."""
    flow, store, clock, journal = _flow(tmp_path)
    flow.submit("удали бэкапы", now=0.0, thread_id="voice")
    assert store.task.status == TaskStatus.PENDING_CONFIRMATION

    store.request_cancel()  # юзер отменил
    assert store.task.status == TaskStatus.CANCEL_REQUESTED

    flow.note_user_turn("да", now=1.0, thread_id="voice")
    result = flow.confirm("confirm", now=1.0, thread_id="voice")

    assert result.outcome == ConfirmDecisionOutcome.REJECTED
    # задача НЕ вернулась в RUNNING
    assert store.task.status != TaskStatus.RUNNING


# =========================================================================================
# 3. «Кора спросила, а я ответил слишком поздно — будущего уже нет» — no_pending_question.
# =========================================================================================


async def test_ec3_answer_after_question_gone_reports_no_pending(tmp_path):
    """Юзер за рулём, Кора спросила mid-run. К тому моменту, как ответ дошёл, вопрос уже
    снят (deadline/cancel/supersede). answer_kora обязан ЧЕСТНО вернуть no_pending_question,
    а не молча «проглотить» ответ — иначе юзер думает, что ответ доставлен, а Кора его не
    получила. on_answer-колбэк решает исход; когда он False, инструмент обязан это отразить."""
    # on_answer имитирует «будущего уже нет» — provide_answer вернул False.
    handlers, store, clock, journal = _handlers(
        tmp_path, on_answer=lambda t: False, thread_id="voice"
    )
    handlers.begin_turn("turn-1")
    res = await handlers.answer_kora(text="используй postgres")
    assert res["outcome"] == "no_pending_question"


# =========================================================================================
# 4. «Напиши код сразу» в PWA без подтверждения — опасный путь требует явного confirm.
# =========================================================================================


async def test_ec4_write_code_fast_without_confirm_refused(tmp_path):
    """Юзер ткнул «Write code now» (fast=true), но confirm не дошёл/не поставил. gate_action
    обязан отказать: dangerous-путь прямо-в-код требует второго явного подтверждения. on_gate
    — это серверный лаунчер; здесь он проверяет, что confirm=False не пропускает fast-ран.
    (Сама логика no_plan_file/stale_plan живёт в app.py; здесь — контракт границы инструмента:
    dangerous-флаг доходит до лаунчера, и тот отказал.)"""
    seen = {}

    def on_gate(action, *, model=None, confirm=False, fast=False):
        seen.update(action=action, confirm=confirm, fast=fast)
        if action == "write_code" and fast and not confirm:
            return {"error": "confirm_required"}
        return {"ok": True}

    handlers, *_ = _handlers(tmp_path, on_gate=on_gate, thread_id="t1")
    handlers.begin_turn("turn-1")
    res = await handlers.gate_action(action="write_code", fast=True, confirm=False)

    assert seen == {"action": "write_code", "confirm": False, "fast": True}
    assert res["error"] == "confirm_required"


# =========================================================================================
# 5. «Кора, очисти историю» голосом — диспетчер НЕ изображает выполнение серверной команды.
# =========================================================================================


def test_ec5_prompt_forbids_pretending_compact_clear(tmp_path):
    """Юзер за рулём говорит «очисти контекст». /compact и /clear — серверные команды,
    работают ТОЛЬКО в текстовом чате. Диспетчер не имеет права обещать их выполнение
    (анти-галлюцинационный гейт C3'). Проверяем, что промпт несёт этот запрет всегда —
    независимо от owed-киллсвича (это операционная правда, не OWED-правило)."""
    for owed in (True, False):
        prompt = build_system_prompt(SynapseConfig(include_owed_prompt_rules=owed))
        assert COMMANDS_NOTE.strip() in prompt
    assert "только в текстовом чате" in prompt


# =========================================================================================
# 6. «Поставил задачу, Кора ещё кодит — хочу вторую» — REJECTED_ACTIVE на второй submit.
# =========================================================================================


def test_ec6_second_task_while_kora_running_rejected(tmp_path):
    """Юзер завёл задачу (Кора уже RUNNING), тут же диктует вторую. Синглтон «одна активная
    задача» (§1): второй submit обязан вернуться REJECTED_ACTIVE с понятным текстом, иначе
    Кора получит две задачи одновременно. Юзер услышит «у меня уже есть активная задача»."""
    flow, store, clock, journal = _flow(tmp_path)
    first = flow.submit("реализуй фичу А", now=0.0)
    assert first.outcome == ConfirmOutcome.COMMITTED
    assert store.has_active_task()

    second = flow.submit("теперь сделай фичу Б", now=1.0)

    assert second.outcome == ConfirmOutcome.REJECTED_ACTIVE
    assert second.reject_text  # диспетчер зачитает причину голосом
    assert store.task.text == "реализуй фичу А"  # первая задача цела


# =========================================================================================
# 7. Cost cap: ровно на границе лимита — N-й вызов (N == max) ещё проходит, N+1 уже нет.
# =========================================================================================


def test_ec7_cost_cap_boundary_inclusive_then_blocks(tmp_path):
    """Юзер гоняет Кору весь день. Лимит max_paid_calls_per_day = 3. record_paid_attempt
    инклюзивен: 3-й вызов (== max) срабатывает И тут же взводит trip — 4-й уже False.
    Это «бюджет ровно исчерпан»: последний разрешённый вызов + жёсткий стоп после."""
    cap = CostCap(max_paid_calls_per_day=3)
    now = 1_000_000.0  # фиксированный момент, далеко от epoch

    assert cap.record_paid_attempt(now) is True   # 1
    assert cap.record_paid_attempt(now) is True   # 2
    assert cap.record_paid_attempt(now) is True   # 3 — ровно лимит, ещё разрешено, но trip взведён
    assert cap.tripped is True
    assert cap.record_paid_attempt(now) is False  # 4 — бюджет исчерпан, жёсткий стоп


# =========================================================================================
# 8. Тред-стадия: revise из CODING откатывает в collect и обнуляет прошлый outcome.
# =========================================================================================


def test_ec8_revise_from_code_resets_outcome(tmp_path):
    """Юзер: Кора кодит (stage=code, last_outcome=completed от ПРЕДЫДУЩЕГО рана). Юзер говорит
    «перепиши иначе» → revise. B07: регрессия в collect обязана обнулить last_outcome, иначе
    write_code сочтёт старый completed свежим и пустит CODE по устаревшему плану. FSM разрешает
    code → collect; set_outcome(None) — отдельная операция, которую хост обязан позвать рядом.
    Тест фиксирует ОБА шага revise-траектории (FSM + обнуление)."""
    clock = FakeClock(0.0)
    threads = ThreadStore(clock, str(tmp_path / "threads"))
    th = threads.create("прототип", project_id="proj")

    # Честная линейная траектория по таблице _STAGE_TRANSITIONS:
    # collect → propose → spec_plan → code
    threads.begin_run(th.id, "propose", "task-0", "m")
    threads.begin_run(th.id, "spec_plan", "task-1", "m")
    threads.begin_run(th.id, "code", "task-2", "m")
    threads.finish_run(th.id, "completed", expected_stage="code")
    assert threads.get(th.id).stage == "code"
    assert threads.get(th.id).last_outcome == "completed"

    # revise из code → collect (легальный переход)
    threads.set_stage(th.id, "collect")
    assert threads.get(th.id).stage == "collect"
    # и хост обязан обнулить outcome рядом (B07) — без этого write_code соврёт
    threads.set_outcome(th.id, None)
    assert threads.get(th.id).last_outcome is None


# =========================================================================================
# 9. Liveness: завершённая задача не превращается в UNREACHABLE от «стареющего» сердцебиения.
# =========================================================================================


def test_ec9_completed_task_stays_ok_even_with_stale_heartbeat(tmp_path):
    """Юзер: Кора завершила задачу 10 минут назад. last_event_ts давно не обновлялся, и
    наивная логика сказала бы STALE→UNREACHABLE → диспетчер врал бы «Кора не в сети» после
    УСПЕХА. B23: COMPLETED → OK безусловно (возраст последнего события — НЕ liveness-сигнал
    для законченной работы). FAILED не сворачивается (зомби-реконсиляция неотличима)."""
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    store.start_task("tk", "сборка прототипа", TaskStatus.RUNNING, now=0.0)
    store.heartbeat(10.0)
    # доводим до COMPLETED через событие
    from synapse.bridge.state import EventClass, KoraEvent

    store.apply_event(
        KoraEvent(id="e1", type="task_completed", cls=EventClass.NARRATABLE,
                  payload={}, speak_text="готово", ts=20.0)
    )
    assert store.task.status == TaskStatus.COMPLETED

    # далеко за обоими порогами — но завершённая задача остаётся OK
    assert store.liveness(now=9_999.0, stale_after_s=120, unreachable_after_s=300) == Liveness.OK


# =========================================================================================
# 10. bind_project отказывает после первого же запуска — нельзя сменить проект на бегу.
# =========================================================================================


async def test_ec10_bind_project_after_run_refused(tmp_path):
    """Юзер: запустил Кору (task_ids непустой), потом голосом «привяжи проект Y».
    bind_project (находка F): ок ТОЛЬКО при null→значение И пустых task_ids. После первого
    рана повторная привязка/смена проекта → False. Иначе Кора пишет не туда, куда юзер думает.
    Здесь проверяем НИЖНИЙ слой (ThreadStore.bind_project) — граница, которая реально решает."""
    clock = FakeClock(0.0)
    threads = ThreadStore(clock, str(tmp_path / "threads"))
    th = threads.create("тред", project_id=None)

    # до запуска — привязка ок
    assert threads.bind_project(th.id, "proj-A") is True
    # первый «ран» — append_task имитирует запуск Коры
    threads.append_task(th.id, "task-1")
    # теперь смена проекта обязана отказать (даже повтор той же — значение→значение)
    assert threads.bind_project(th.id, "proj-B") is False
    assert threads.get(th.id).project_id == "proj-A"  # не сменился
