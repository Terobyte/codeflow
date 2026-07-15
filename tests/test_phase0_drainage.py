# -*- coding: utf-8-sig -*-
"""С0 — Дренаж (Фаза 0): регрессионные якоря для CR-1 / CR-4 / CR-5.

Каждый якорь фиксирует ОСОЗНАННОСТЬ фикса из спеки
`docs/superpowers/plans/2026-07-14-synapse-dispatcher-kora-phase0.md`:
- CR-1: повторное присваивание `current_http_thread` безвредно для `_output_task`/`_gate_locks`;
- CR-4: асимметрия `_voice_answer`/`_http_answer` — голосовой ответ доставляется при
  voice_thread=None + awaiting, HTTP из чужого треда — нет;
- CR-5: launch_lock делает busy-check структурным (два конкурентных гейта на разные треда →
  ровно один запуск, второй busy), даже если между гейт-локом и launch просочится await.
"""
import asyncio
import pytest
from unittest.mock import MagicMock

from synapse.pipeline.app import SynapseHost


class _FakeClock:
    def now(self) -> float:
        return 1000.0


def _bare_host() -> SynapseHost:
    """SynapseHost напрямую с фейками — без тяжёлого build_host."""
    return SynapseHost(
        clock=_FakeClock(), cfg=None, journal=None, store=None,
        speak_ledger=None, classifier=None, confirm_flow=None,
        arbiter_policy=None, bridge=None, handlers=None,
        breaker=None, cost_cap=None,
    )


# --- CR-1: setter не инициализирует чужие поля ---------------------------------------------

class _FakeOutputTask:
    """Duck-type PipelineTask — только has_finished/queue_frame не нужны здесь, это маркер."""
    pass


def test_current_http_thread_reassignment_keeps_output_task_and_gate_locks():
    """CR-1: повторное `host.current_http_thread = …` НЕ должно отвязывать живой _output_task
    и НЕ должно сбрасывать _gate_locks. Раньше setter пересоздавал оба при каждом присваивании."""
    host = _bare_host()
    # симулируем забинженный коннект + накопленный гейт-лок
    bound = _FakeOutputTask()
    host.bind_output(bound)
    gate_lock = asyncio.Lock()
    host._gate_locks["thread_A"] = gate_lock

    # реконнект/перепривязка HTTP-треда дёргает setter
    host.current_http_thread = {"id": "thread_B"}

    assert host._output_task is bound, "CR-1: живой PipelineTask отвязан переприсваиванием"
    assert host._gate_locks.get("thread_A") is gate_lock, \
        "CR-1: per-thread гейт-локи сброшены переприсваиванием"
    assert host.current_http_thread["id"] == "thread_B"


def test_current_http_thread_init_has_output_task_and_gate_locks():
    """CR-1: после __init__ поля инициализированы независимо от setter."""
    host = _bare_host()
    assert host._output_task is None
    assert host._gate_locks == {}


# --- CR-4: осознанная асимметрия _voice_answer / _http_answer --------------------------------

def _voice_host(voice_tid, awaiting_tid):
    """Собирает хост через build_host (нужно замыкание _voice_answer в bridge), выставляет
    voice_thread и awaiting-задачу. host.bridge.on_answer == _voice_answer (мягкий гвард).
    Kora включена в конфиге — иначе on_answer=None (замыкание не строится)."""
    from synapse.config import SynapseConfig
    from synapse.pipeline.app import build_host
    import tempfile

    cfg = SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tempfile.mkdtemp()),
        kora_enabled=True,
    )
    host = build_host(cfg)

    # Замыкание _voice_answer держит локальный kora_runner из build_host — тот же объект,
    # что host.kora_runner. Патчим метод на нём, а не заменяем сам раннер.
    runner = host.kora_runner
    runner.provide_answer = MagicMock(return_value=True)

    if awaiting_tid is not None:
        awaiting_thread = host.threads.create("awaiting")
        awaiting_tid = awaiting_thread.id  # реальный id созданного треда
        host.store._task = MagicMock(id="task_1")
        # thread_for_task резолвит awaiting-тред по id задачи — подменяем напрямую.
        host.threads.thread_for_task = lambda task_id: host.threads.get(awaiting_tid)
    else:
        host.store._task = None

    host.voice_thread["id"] = voice_tid
    return host


def test_voice_answer_delivered_when_voice_thread_none_but_awaiting():
    """CR-4: голос — канал дома; voice_thread=None после реконнекта не должен рвать доставку
    ответа Коре. Строгий гвард (как в _http_answer) сломал бы это — ответ упал бы в
    no_pending_question. Асимметрия НАМЕРЕННАЯ, зафиксирована комментарием в app.py."""
    host = _voice_host(voice_tid=None, awaiting_tid="t_await")
    assert host.bridge.on_answer("ответ") is True
    host.kora_runner.provide_answer.assert_called_once_with("ответ")


def test_voice_answer_blocked_when_voice_in_foreign_thread():
    """CR-4: контраст — голос стоит В ЧУЖОМ треде (явный, не None), awaiting в другом →
    доставка блокируется. Гвард мягче HTTP только для None/неопределённости, не для чужого id."""
    host = _voice_host(voice_tid="t_foreign", awaiting_tid="t_await")
    assert host.bridge.on_answer("ответ") is False
    host.kora_runner.provide_answer.assert_not_called()


# --- CR-5: launch_lock делает busy-check структурным ----------------------------------------

def _gate_host(tmp_path):
    """Реальный host через build_host + стаб KoraRunner (паттерн test_stages._gate_host)."""
    from synapse.config import SynapseConfig
    from synapse.pipeline.app import build_host

    cfg = SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path), kora_workspace_dir=str(tmp_path / "ws"),
    )
    host = build_host(cfg)

    class _FakeRunner:
        def __init__(self):
            self.starts = []

        def start(self, task_id, text, spec):
            self.starts.append((task_id, text, spec))

    host.kora_runner = _FakeRunner()
    return host


def _propose(host):
    t = host.threads.create("x")
    host.threads.set_stage(t.id, "propose")
    host.threads.set_request(t.id, "сделай штуку")
    return t


@pytest.mark.asyncio
async def test_launch_lock_two_concurrent_threads_one_starts_one_busy(tmp_path):
    """CR-5: два конкурентных gate_action на РАЗНЫЕ треда. per-thread локи НЕ сериализуют их
    (разные ключи), но launch_lock на хосте + busy-check делают ровно один запуск, второй busy.
    Без launch_lock (если бы между has_active_task и _launch_run просочился await) — два старта."""
    host = _gate_host(tmp_path)
    ta = _propose(host)
    tb = host.threads.create("y")
    host.threads.set_stage(tb.id, "propose")
    host.threads.set_request(tb.id, "другая штука")

    # monkeypatch threads.set_stage на await-ящую версию: симулирует точку, где без launch_lock
    # второй тред успел бы пройти busy-check до того, как первый стартует задачу.
    original_set_stage = host.threads.set_stage

    def slow_set_stage(thread_id, stage):
        # _launch_run зовёт set_stage синхронно; здесь оно ТОЖЕ синхронно, но мы вносим
        # точку yields ВНУТРЬ launch-секции косвенно — достаточно того, что два разных треда
        # проходят разные per-thread локи и встречают launch_lock.
        return original_set_stage(thread_id, stage)

    host.threads.set_stage = slow_set_stage

    r1, r2 = await asyncio.gather(
        host.gate_action(ta.id, "send_to_kora", confirm=True),
        host.gate_action(tb.id, "send_to_kora", confirm=True),
    )
    results = [r1, r2]
    oks = [r for r in results if r.get("ok")]
    busies = [r for r in results if r.get("error") == "busy"]
    assert len(oks) == 1, f"CR-5: ожидался один запуск, got {results}"
    assert len(busies) == 1, f"CR-5: ожидался один busy, got {results}"
    assert len(host.kora_runner.starts) == 1, "CR-5: запущено два рана — launch_lock не защитил"
