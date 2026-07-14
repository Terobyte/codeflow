"""UI-5 слайс «гигиена»: чистый контекст треда, компакт, rename/авто-title, архив, удаление проекта.

Task 8 — формальные якоря поверх UI-3-механики: код уже удовлетворяет инвариантам (история
ключуется по треду; регидрация берёт только kind=user/assistant; `_complete` принимает thread_id
явно, без мутируемого current_thread_id). Здесь — фиксация/регрессия.
"""
import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.state import TaskStore
from synapse.clock import FakeClock  # noqa: F401  (re-exported pattern from test_text_turn)
from synapse.config import SynapseConfig
from synapse.dispatcher.loop import DispatcherTurnLoop
from synapse.dispatcher.tools import KoraBridge, ToolHandlers
from synapse.journal import TurnJournal
from synapse.threads import ThreadStore


class ScriptedLLM:
    """Эхо последней user-реплики + числа user-сообщений; копит каждое увиденное сообщение.

    Различает компакт-вызов (system содержит «Сожми») от обычного хода: на компакт возвращает
    фиксированную выжимку и пишет её в `compacted`, на обычный ход — эхо, как в Task 8."""

    def __init__(self, summary: str = "ВЫЖИМКА: было 3 темы") -> None:
        self.seen: list[list[dict]] = []
        self.summary = summary
        self.compacted: list[list[dict]] = []

    async def complete(self, messages, tools):
        self.seen.append(messages)
        sys_text = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        if "Сожми" in sys_text:
            self.compacted.append(messages)
            return self.summary, []
        users = [m for m in messages if m.get("role") == "user"]
        return f"ok:{users[-1]['content']}:{len(users)}", []


def _loop(tmp_path, feed_reader=None, stage_block_for=None, on_compact=None, cfg=None):
    clock = FakeClock()
    cfg = cfg or SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
                          cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    handlers = ToolHandlers(KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg), journal)
    llm = ScriptedLLM()
    loop = DispatcherTurnLoop(
        llm, handlers, confirm, store, journal, clock, cfg,
        thread_feed_reader=feed_reader, stage_block_for=stage_block_for,
        on_compact=on_compact,
    )
    return loop, llm


# --- Task 8: чистый контекст нового треда ----------------------------------------------


async def test_two_threads_do_not_leak_history(tmp_path):
    """История Б не содержит user/assistant реплик А (и наоборот)."""
    loop, llm = _loop(tmp_path)
    await loop.ingest_user_turn("первая реплика А", thread_id="thA")
    await loop.ingest_user_turn("вторая реплика А", thread_id="thA")
    # Б — отдельный тред, своя история
    await loop.ingest_user_turn("реплика Б", thread_id="thB")
    await loop.ingest_user_turn("ещё реплика Б", thread_id="thB")

    a_msgs = [m for m in llm.seen if any(m.get("role") == "user" and m.get("content") == "первая реплика А" for m in m)]
    # в любом сообщении, виденном на ходах А, не должно быть «реплика Б»
    for msgs in llm.seen:
        contents = str([m.get("content") for m in msgs])
        if "первая реплика А" in contents:
            assert "реплика Б" not in contents
        if "реплика Б" in contents:
            assert "первая реплика А" not in contents


async def test_thread_b_history_count_is_independent(tmp_path):
    """Ход в Б видит ровно свою историю (1 user), не накрученную ходами А."""
    loop, llm = _loop(tmp_path)
    await loop.ingest_user_turn("а1", thread_id="thA")
    await loop.ingest_user_turn("а2", thread_id="thA")
    _, reply = await loop.ingest_user_turn("б1", thread_id="thB")
    # ScriptedLLM эхо ok:<content>:<user-count> — у Б один user
    assert reply == "ok:б1:1"


async def test_cold_rehydration_reads_only_user_assistant(tmp_path):
    """Холодная регидрация тащит ТОЛЬКО kind=user/assistant; кора-виды и лента-события — нет."""
    feed = {"thX": [
        {"kind": "user", "text": "старый вопрос"},
        {"kind": "assistant", "text": "старый ответ"},
        {"kind": "gate_card", "stage": "propose", "action": "send_to_kora"},
        {"kind": "event", "text": "правки → сбор"},
        {"kind": "task", "text": "запуск задачи"},
        {"kind": "system", "text": "старт сессии"},
        {"kind": "thinking", "text": "размышление Коры"},
        {"kind": "tool_use", "text": "Write: ..."},
        {"kind": "tool_result", "text": "ок"},
        {"kind": "result", "text": "завершено"},
    ]}
    loop, llm = _loop(tmp_path, feed_reader=lambda tid: feed.get(tid, []))
    await loop.ingest_user_turn("новая", thread_id="thX")
    msgs = llm.seen[-1]
    # регидрированные реплики — на месте
    assert any(m.get("role") == "user" and m.get("content") == "старый вопрос" for m in msgs)
    assert any(m.get("role") == "assistant" and m.get("content") == "старый ответ" for m in msgs)
    # история = только user/assistant (после system-сообщения); НИ один display/kora-kind не
    # стал сообщением. Проверяем по составу ролей и по точному множеству контентов истории.
    history_msgs = [m for m in msgs if m.get("role") in ("user", "assistant")]
    assert {m["role"] for m in history_msgs} <= {"user", "assistant"}
    # ни один «запрещённый» текст из feed-видов не должен появиться как самостоятельное сообщение
    forbidden_texts = {
        "правки → сбор", "запуск задачи", "старт сессии",
        "размышление Коры", "Write: ...", "ок", "завершено",
    }
    actual_contents = {m.get("content") for m in history_msgs}
    leaked = forbidden_texts & actual_contents
    assert not leaked, f"NO-EXFIL нарушен: в историю попали feed-виды {leaked}"
    # gate_card — dict-запись, не строка; убеждаемся что её action-текст тоже не просочился
    assert not any(isinstance(m.get("content"), dict) for m in history_msgs)


async def test_state_block_is_global_across_threads(tmp_path):
    """Глобальный [СОСТОЯНИЕ]-блок одинаков для обоих тредов (синглтон store, тот же clock)."""
    loop, llm = _loop(tmp_path)
    await loop.ingest_user_turn("ход А", thread_id="thA")
    await loop.ingest_user_turn("ход Б", thread_id="thB")
    a_system = llm.seen[-2][0]  # первое сообщение хода А — system
    b_system = llm.seen[-1][0]  # первое сообщение хода Б — system
    assert a_system["role"] == "system" and b_system["role"] == "system"
    # state_block — общий хвост после промпта; часы не тикали (FakeClock) → идентичен
    assert a_system["content"] == b_system["content"]


async def test_complete_receives_thread_id_explicitly(tmp_path):
    """thread_id передаётся явно в _complete; нет мутируемого current_thread_id на loop."""
    loop, llm = _loop(tmp_path)
    # нет атрибута current_thread_id/_current_thread_id — инвариант «не вводи мутируемое поле»
    assert not hasattr(loop, "_current_thread_id")
    assert not hasattr(loop, "current_thread_id")
    await loop.ingest_user_turn("x", thread_id="thA")
    await loop.ingest_user_turn("y", thread_id="thB")
    # истории изолированы — значит thread_id дошёл до _history_for корректно на каждом ходе.
    # После хода история = [user, assistant]; user-реплика каждого треда своя и не протекла.
    a_user = [m for m in loop._histories["thA"] if m["role"] == "user"]
    b_user = [m for m in loop._histories["thB"] if m["role"] == "user"]
    assert [m["content"] for m in a_user] == ["x"]
    assert [m["content"] for m in b_user] == ["y"]


async def test_stage_block_dispatched_per_thread(tmp_path):
    """stage_block_for зовётся с правильным thread_id для каждого хода (без глобального стейта)."""
    seen_ids: list[str | None] = []
    loop, llm = _loop(
        tmp_path,
        stage_block_for=lambda tid: (seen_ids.append(tid), "СТАДИЙНЫЙ БЛОК")[1],
    )
    await loop.ingest_user_turn("ход А", thread_id="thA")
    await loop.ingest_user_turn("ход Б", thread_id="thB")
    assert seen_ids[-2:] == ["thA", "thB"]
    # системное сообщение каждого хода содержит стадийный блок
    assert "СТАДИЙНЫЙ БЛОК" in llm.seen[-2][0]["content"]
    assert "СТАДИЙНЫЙ БЛОК" in llm.seen[-1][0]["content"]


# --- Task 9: компакт длинного треда (S10) ----------------------------------------------


def _cfg(threshold: int) -> SynapseConfig:
    return dataclasses.replace(SynapseConfig(), dispatcher_compact_after=threshold)


async def _seed_history(loop, thread_id, msgs):
    """Заполнить историю треда напрямую (минуя ingest) — фикстура для теста границы."""
    hist = loop._history_for(thread_id)
    hist.clear()
    hist.extend(msgs)


async def test_no_compact_below_threshold(tmp_path):
    cfg = _cfg(threshold=40)
    loop, llm = _loop(tmp_path, cfg=cfg)
    await _seed_history(loop, "th", [{"role": "user", "content": f"u{i}"} for i in range(20)])
    pre = len(loop._histories["th"])
    await loop.ingest_user_turn("новый", thread_id="th")
    assert not llm.compacted, "компакт не должен срабатывать ниже порога"
    # история только выросла (+1 user, +1 assistant), ничего не сжато
    assert len(loop._histories["th"]) == pre + 2


async def test_compact_fires_once_above_threshold(tmp_path):
    cfg = _cfg(threshold=6)
    loop, llm = _loop(tmp_path, cfg=cfg)
    # 8 user-сообщений — больше порога 6. cut = 8//2 = 4 → history[4] это user (u4) → сжимаем
    await _seed_history(loop, "th", [{"role": "user", "content": f"u{i}"} for i in range(8)])
    await loop.ingest_user_turn("новый", thread_id="th")
    assert len(llm.compacted) == 1, "ровно один компакт-вызов LLM"


async def test_compact_preserves_tail_and_starts_with_user(tmp_path):
    cfg = _cfg(threshold=6)
    loop, llm = _loop(tmp_path, cfg=cfg)
    seed = [{"role": "user", "content": f"u{i}"} for i in range(8)]
    await _seed_history(loop, "th", seed)
    await loop.ingest_user_turn("новый", thread_id="th")
    hist = loop._histories["th"]
    # первый элемент истории после компакта — [КОМПАКТ], затем хвост с u4..u7 + новый ход
    assert hist[0]["role"] == "user"
    assert "[КОМПАКТ]" in hist[0]["content"]
    # хвост (u4..u7) сохранён дословно
    tail_contents = [m["content"] for m in hist[1:] if m.get("role") == "user"]
    for i in range(4, 8):
        assert f"u{i}" in tail_contents, f"хвост потерял u{i}"
    # НИКОГДА не оставлять начало хвоста assistant/tool без предшествующего user: после
    # [КОМПАКТ] (user) идёт tail[0] который обязан быть user (cut продвинут до user)
    assert hist[1]["role"] == "user"


async def test_compact_cut_advances_to_next_user_through_tool_group(tmp_path):
    """Многопроходная tool-call-группа: cut падает ВНУТРИ группы (assistant+tool),
    продвинуть до следующего user. Фикстура обязанна содержать такую группу ниже cut."""
    cfg = _cfg(threshold=5)
    loop, llm = _loop(tmp_path, cfg=cfg)
    # 6 seed-сообщений (> порога 5). cut = 6//2 = 3 → history[3] это assistant(не user)
    # → продвигаем до history[4]=u2 (ПЕРВЫЙ user на/после cut).
    seed = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0",
         "tool_calls": [{"id": "c1", "name": "get_task_status", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "get_task_status", "content": "{}"},
        {"role": "assistant", "content": "a1"},          # index 3 — НЕ user (cut сюда)
        {"role": "user", "content": "u2"},               # index 4 — ПЕРВЫЙ user на/после cut
        {"role": "assistant", "content": "a2"},
    ]
    await _seed_history(loop, "th", seed)
    await loop.ingest_user_turn("новый", thread_id="th")
    hist = loop._histories["th"]
    # compact сработал
    assert len(llm.compacted) == 1
    # хвост начинается с u2 (user), а не с a1 (assistant без предшествующего user)
    assert hist[1]["role"] == "user"
    assert hist[1]["content"] == "u2"
    # оборванной tool-группы в хвосте нет (assistant a1 уехал в выжимку)
    assert not any(m.get("role") == "tool" for m in hist)


async def test_compact_no_user_after_cut_means_no_compact(tmp_path):
    cfg = _cfg(threshold=2)
    loop, llm = _loop(tmp_path, cfg=cfg)
    # один user + один assistant: cut=1, после cut нет user → не жать
    await _seed_history(loop, "th", [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
    ])
    await loop.ingest_user_turn("новый", thread_id="th")
    assert not llm.compacted, "нет user после cut → компакт не срабатывает"


async def test_compact_is_inplace_not_rebind(tmp_path):
    """АНТИ-rebind-якорь: второй ход видит УЖЕ сжатую историю в self._histories.

    Ребинд локальной `history` дошёл бы до _complete ЭТОГО хода, но self._histories[thread_id]
    остался бы несжатым → второй ход снова увидел бы сырые u0..u7 и сжал бы ИХ, а не [КОМПАКТ].
    Здесь же второй компакт работает над результатом первого — значит мутация была in-place."""
    cfg = _cfg(threshold=6)
    loop, llm = _loop(tmp_path, cfg=cfg)
    await _seed_history(loop, "th", [{"role": "user", "content": f"u{i}"} for i in range(8)])
    # первый ход сжимает u0..u3 → [КОМПАКТ]
    await loop.ingest_user_turn("ход1", thread_id="th")
    assert len(llm.compacted) == 1
    assert "[КОМПАКТ]" in loop._histories["th"][0]["content"]
    # второй ход: история (7 после хода1) > порога → снова компакт, но теперь над СЖАТОЙ
    # историей — второй payload содержит [КОМПАКТ] как часть сжимаемой старшей половины.
    await loop.ingest_user_turn("ход2", thread_id="th")
    assert len(llm.compacted) == 2
    second_payload = str(llm.compacted[1])
    assert "[КОМПАКТ]" in second_payload, (
        "in-place нарушен: второй ход не видит [КОМПАКТ] первого — мутация была ребиндом"
    )
    # старые сырые u0..u3 во втором сжатии отсутствуют (их уже нет в истории)
    assert "u0" not in second_payload and "u1" not in second_payload


async def test_compact_second_turn_sees_compacted_history(tmp_path):
    """Если порог низкий, повторный компакт на том же треде видит [КОМПАКТ] в истории."""
    cfg = _cfg(threshold=2)
    loop, llm = _loop(tmp_path, cfg=cfg)
    # 4 сообщения — сжимаем на ходе; после компакта history ~ [КОМПАКТ, tail...]
    await _seed_history(loop, "th", [{"role": "user", "content": f"u{i}"} for i in range(4)])
    await loop.ingest_user_turn("ход", thread_id="th")
    hist = loop._histories["th"]
    # система хода (последний вызов llm.seen) видит [КОМПАКТ] в пользовательских сообщениях
    last_turn_msgs = llm.seen[-1]
    user_contents = [m["content"] for m in last_turn_msgs if m.get("role") == "user"]
    assert any("[КОМПАКТ]" in c for c in user_contents), "ход ответил с учётом выжимки"


async def test_on_compact_callback_fires(tmp_path):
    fired: list[str] = []
    cfg = _cfg(threshold=6)
    loop, llm = _loop(tmp_path, cfg=cfg, on_compact=lambda tid: fired.append(tid))
    await _seed_history(loop, "th7", [{"role": "user", "content": f"u{i}"} for i in range(8)])
    await loop.ingest_user_turn("новый", thread_id="th7")
    assert fired == ["th7"], "on_compact зовётся ровно раз с thread_id"


async def test_on_compact_callback_not_fired_below_threshold(tmp_path):
    fired: list[str] = []
    cfg = _cfg(threshold=40)
    loop, llm = _loop(tmp_path, cfg=cfg, on_compact=lambda tid: fired.append(tid))
    await _seed_history(loop, "th", [{"role": "user", "content": f"u{i}"} for i in range(10)])
    await loop.ingest_user_turn("новый", thread_id="th")
    assert fired == []


async def test_compact_excludes_kora_kinds_via_construction(tmp_path):
    """NO-EXFIL: в компакт-промпт не попадают кора-виды. История диспетчера содержит только
    user/assistant (регидрация фильтрует kinds; tool-хвосты откатываются), поэтому компакт-вызов
    по построению оперирует чистым user/assistant. Проверим это напрямую: в сообщениях,
    переданных на компакт, нет role=tool или assistant-with-tool_calls."""
    cfg = _cfg(threshold=5)
    loop, llm = _loop(tmp_path, cfg=cfg)
    seed = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    await _seed_history(loop, "th", seed)
    await loop.ingest_user_turn("новый", thread_id="th")
    compact_msgs = llm.compacted[-1]
    # compact-вызов: system + один user(сериализованная старшая половина). Проверим сериализацию.
    user_payload = next(m for m in compact_msgs if m.get("role") == "user")
    payload_str = str(user_payload["content"])
    # старшая половина (u0..u1, a0..a1) сжата в JSON; НИ tool, НИ чужих kinds
    assert "u0" in payload_str and "a0" in payload_str
    # role=tool в сериализованной старшей половине нет (её там и не было по построению)
    assert "tool_call_id" not in payload_str
    assert '"role": "tool"' not in payload_str


# --- config.py: dispatcher_compact_after env ------------------------------------------


def test_config_default_compact_threshold_is_40():
    assert SynapseConfig().dispatcher_compact_after == 40


def test_config_compact_threshold_from_env(monkeypatch):
    monkeypatch.setenv("DISPATCHER_COMPACT_AFTER", "7")
    assert SynapseConfig.from_env().dispatcher_compact_after == 7


def test_config_compact_threshold_explicit_zero_preserved(monkeypatch):
    monkeypatch.setenv("DISPATCHER_COMPACT_AFTER", "0")
    assert SynapseConfig.from_env().dispatcher_compact_after == 0


def test_config_compact_threshold_malformed_env_keeps_default(monkeypatch):
    monkeypatch.setenv("DISPATCHER_COMPACT_AFTER", "not-a-number")
    assert SynapseConfig.from_env().dispatcher_compact_after == 40


# --- feed event wiring (app.py on_compact → лента) -------------------------------------


def _webrtc_or_skip():
    pytest.importorskip("aiortc"); pytest.importorskip("cv2"); pytest.importorskip("fastapi")
    try:
        from synapse.pipeline import webrtc_server
        return webrtc_server
    except (ImportError, RuntimeError) as e:
        pytest.skip(f"webrtc deps unavailable: {e}")


async def test_on_compact_writes_feed_event(tmp_path):
    """on_compact, привязанный в build_host, пишет kind=event «контекст сжат» в ленту треда."""
    webrtc_server = _webrtc_or_skip()
    from synapse.threads import ThreadStore
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    th = threads.create("тред")
    # Воспроизведём замыкание _on_compact из build_host (живёт там, недоступно напрямую).
    def _on_compact(thread_id):
        threads.append_feed(thread_id, {"ts": clock.now(), "kind": "event", "text": "контекст сжат"})
    _on_compact(th.id)
    feed = threads.read_feed(th.id)
    assert len(feed) == 1
    assert feed[0]["kind"] == "event"
    assert feed[0]["text"] == "контекст сжат"


# --- Task 10: авто-title + rename (S30) ------------------------------------------------


def test_maybe_autotitle_renames_sentinel_thread(tmp_path):
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    th = threads.create("новый тред")
    changed = threads.maybe_autotitle(th.id, "добавь функцию логирования в модуль core")
    assert changed
    assert threads.get(th.id).title == "добавь функцию логирования в модуль core"


def test_maybe_autotitle_does_not_rename_meaningful_title(tmp_path):
    """commit-путь создаёт тред с осмысленным title — auto-title не должен его затирать."""
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    th = threads.create("уже осмысленный title")
    changed = threads.maybe_autotitle(th.id, "совсем другой текст второй реплики")
    assert not changed
    assert threads.get(th.id).title == "уже осмысленный title"


def test_maybe_autotitle_second_turn_no_op(tmp_path):
    """Второй user-ход не переименовывает: title уже не сентинель после первого хода."""
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    th = threads.create("новый тред")
    assert threads.maybe_autotitle(th.id, "первая реплика")  # первый ход переименовал
    changed = threads.maybe_autotitle(th.id, "вторая реплика, другая")
    assert not changed
    assert threads.get(th.id).title == "первая реплика"


def test_maybe_autotitle_truncates_to_80(tmp_path):
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    th = threads.create("новый тред")
    long_text = "ё" * 200
    threads.maybe_autotitle(th.id, long_text)
    assert len(threads.get(th.id).title) == 80


def test_maybe_autotitle_empty_text_no_op(tmp_path):
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    th = threads.create("новый тред")
    assert not threads.maybe_autotitle(th.id, "   ")
    assert threads.get(th.id).title == "новый тред"


def test_maybe_autotitle_unknown_thread_no_op(tmp_path):
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    assert not threads.maybe_autotitle("ghost", "текст")


def test_rename_persists_new_title(tmp_path):
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    th = threads.create("старое название")
    assert threads.rename(th.id, "новое название")
    assert threads.get(th.id).title == "новое название"


def test_rename_unknown_thread_returns_false(tmp_path):
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    assert not threads.rename("ghost", "x")


def test_rename_survives_restart():
    import tempfile
    d = tempfile.mkdtemp()
    c1 = FakeClock()
    store1 = ThreadStore(c1, d)
    th = store1.create("новый тред")
    store1.rename(th.id, "переименовано и сохранено")
    # рестарт: новый стор читает тот же каталог
    store2 = ThreadStore(FakeClock(), d)
    assert store2.get(th.id).title == "переименовано и сохранено"


# --- PATCH /api/threads/{id} route -----------------------------------------------------


def _endpoint(app, name):
    return next(r.endpoint for r in app.routes if getattr(getattr(r, "endpoint", None), "__name__", "") == name)


class FakeRequest:
    def __init__(self, body=None, json_ct=True, origin="http://testserver", host="testserver", method="PATCH"):
        self._body = body or {}
        self.method = method
        self.headers = {"content-type": "application/json" if json_ct else "text/plain",
                        "host": host}
        if origin:
            self.headers["origin"] = origin
    async def json(self): return self._body


def _api_host_for_rename(tmp_path):
    """SimpleNamespace-хост с настоящим ThreadStore + минимально нужными роуту полями."""
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    return SimpleNamespace(clock=clock, threads=threads, text_loop=object(),
                           turn_lock=asyncio.Lock())


async def test_patch_thread_rename_csrf_rejects_non_json(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host_for_rename(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("новый тред")
    ep = _endpoint(app, "api_thread_patch")
    resp = await ep(th.id, FakeRequest({"title": "x"}, json_ct=False))
    assert resp.status_code == 403


async def test_patch_thread_rename_unknown_thread_404(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host_for_rename(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    ep = _endpoint(app, "api_thread_patch")
    resp = await ep("ghost", FakeRequest({"title": "x"}))
    assert resp.status_code == 404


async def test_patch_thread_rename_empty_title_400(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host_for_rename(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("новый тред")
    ep = _endpoint(app, "api_thread_patch")
    resp = await ep(th.id, FakeRequest({"title": "   "}))
    assert resp.status_code == 400


async def test_patch_thread_rename_success_persists(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host_for_rename(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("старое")
    ep = _endpoint(app, "api_thread_patch")
    resp = await ep(th.id, FakeRequest({"title": "новое название"}))
    assert resp.status_code == 200
    import json as _json
    body = _json.loads(resp.body)
    assert body["title"] == "новое название"
    # персист: перечитываем стор
    assert host.threads.get(th.id).title == "новое название"


async def test_patch_thread_rename_truncates_long_title(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host_for_rename(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("x")
    ep = _endpoint(app, "api_thread_patch")
    resp = await ep(th.id, FakeRequest({"title": "я" * 200}))
    assert resp.status_code == 200
    import json as _json
    assert len(_json.loads(resp.body)["title"]) == 80


async def test_message_route_autotitles_sentinel_thread(tmp_path):
    """Message-роут зовёт maybe_autotitle на user-ходе: композерный тред-сентинель получает
    title из первой реплики, не меняя stage/request."""
    webrtc_server = _webrtc_or_skip()
    # Нужен host с настоящим text_loop + threads + message-роутом. Берём полный _loop из Task 8.
    clock = FakeClock()
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
                          cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    handlers = ToolHandlers(KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg), journal)
    from synapse.dispatcher.loop import DispatcherTurnLoop
    llm = ScriptedLLM()
    text_loop = DispatcherTurnLoop(llm, handlers, confirm, store, journal, clock, cfg)
    threads = ThreadStore(clock, tmp_path / "threads")
    host = SimpleNamespace(clock=clock, threads=threads, text_loop=text_loop,
                           store=store, turn_lock=asyncio.Lock(),
                           current_http_thread={"id": None})
    app = webrtc_server.build_web_app(host=host)
    th = threads.create("новый тред")  # композерный сентинель
    ep = _endpoint(app, "api_thread_message")
    resp = await ep(th.id, FakeRequest({"text": "сделай быстрый фикс в auth.py"}))
    assert resp.status_code == 200
    # авто-title сработал из реплики
    assert threads.get(th.id).title == "сделай быстрый фикс в auth.py"
    # stage/request не тронуты
    assert threads.get(th.id).stage == "collect"
    assert threads.get(th.id).request_text is None


# --- UI lexical: rename handlers в app.js ----------------------------------------------


def test_rename_ui_handlers_present_and_xss_safe():
    """Лексический тест: app.js содержит patchJSON + renameCurrentThread + commitRename,
    тап по #view-title, inline input (НЕ window.prompt — он выкинут проектной дисциплиной),
    и НЕ содержит innerHTML (XSS-дисциплина)."""
    app_path = Path(__file__).resolve().parent.parent / "synapse" / "pipeline" / "client" / "app.js"
    app = app_path.read_text(encoding="utf-8")
    for token in ("patchJSON", "renameCurrentThread", "commitRename",
                  'addEventListener("click", renameCurrentThread)', "/api/threads/"):
        assert token in app, f"rename UI missing {token!r}"
    assert "innerHTML" not in app
    # проектная дисциплина запретила window.prompt — rename идёт через inline input
    assert "prompt(" not in app
    # тап-таргет — именно #view-title, не #thread-badge
    assert '$("view-title").addEventListener' in app


# --- Task 11: архив тредов + удаление проекта (S31) ------------------------------------
# Заимствуем _endpoint/FakeRequest, определённые выше (Task 10 секция).


def _api_host_archive(tmp_path):
    """Хост с ThreadStore + ProjectStore + store для per-thread busy-чека."""
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    from synapse.projects import ProjectStore
    return SimpleNamespace(
        clock=clock, threads=threads, store=TaskStore(clock),
        projects=ProjectStore(tmp_path / "projects.json"),
        turn_lock=asyncio.Lock(),
    )


# --- store: set_archived / list(include_archived) --------------------------------------


def test_set_archived_excludes_from_default_list(tmp_path):
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    a = threads.create("живой")
    b = threads.create("на архив")
    threads.set_archived(b.id, True)
    visible = [t.id for t in threads.list()]
    assert a.id in visible and b.id not in visible


def test_list_include_archived_returns_all(tmp_path):
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    a = threads.create("живой")
    b = threads.create("на архив")
    threads.set_archived(b.id, True)
    all_ids = {t.id for t in threads.list(include_archived=True)}
    assert all_ids == {a.id, b.id}


def test_set_archived_unknown_thread_returns_false(tmp_path):
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    assert not threads.set_archived("ghost", True)


def test_set_archived_preserves_feed_and_metadata(tmp_path):
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    th = threads.create("тред")
    threads.append_feed(th.id, {"kind": "user", "text": "реплика"})
    threads.set_archived(th.id, True)
    # рестарт: новый стор читает тот же каталог — лента и archived сохранены
    store2 = ThreadStore(FakeClock(), tmp_path / "threads")
    assert store2.get(th.id).archived is True
    feed = store2.read_feed(th.id)
    assert any(e.get("kind") == "user" for e in feed)


# --- unbind_project -------------------------------------------------------------------


async def test_unbind_project_clears_project_id_and_writes_event(tmp_path):
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    from synapse.projects import ProjectStore
    projects = ProjectStore(tmp_path / "projects.json")
    (tmp_path / "proj").mkdir()
    proj = await projects.add("п", str(tmp_path / "proj"))
    th = threads.create("в проекте")
    threads.bind_project(th.id, proj["id"])
    orphan = threads.create("без проекта")
    count = threads.unbind_project(proj["id"])
    assert count == 1
    assert threads.get(th.id).project_id is None  # тред НЕ удалён
    assert threads.get(orphan.id) is not None  # чужой тред не тронут
    # event «проект удалён» добавлен в ленту затронутого треда
    feed = threads.read_feed(th.id)
    assert any(e.get("kind") == "event" and "проект удалён" in e.get("text", "") for e in feed)


# --- ProjectStore.remove --------------------------------------------------------------


async def test_project_remove_deletes_and_returns_true(tmp_path):
    from synapse.projects import ProjectStore
    store = ProjectStore(tmp_path / "projects.json")
    (tmp_path / "p").mkdir()
    proj = await store.add("п", str(tmp_path / "p"))
    assert await store.remove(proj["id"]) is True
    assert store.get(proj["id"]) is None


async def test_project_remove_unknown_returns_false(tmp_path):
    from synapse.projects import ProjectStore
    store = ProjectStore(tmp_path / "projects.json")
    assert await store.remove("ghost") is False


async def test_project_remove_survives_restart(tmp_path):
    from synapse.projects import ProjectStore
    store1 = ProjectStore(tmp_path / "projects.json")
    (tmp_path / "keep").mkdir(); (tmp_path / "gone").mkdir()
    keep = await store1.add("keep", str(tmp_path / "keep"))
    gone = await store1.add("gone", str(tmp_path / "gone"))
    await store1.remove(gone["id"])
    store2 = ProjectStore(tmp_path / "projects.json")
    assert store2.get(keep["id"]) is not None
    assert store2.get(gone["id"]) is None


# --- POST /api/threads/{id}/archive ---------------------------------------------------


async def test_archive_route_csrf_rejects_non_json(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host_archive(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("тред")
    ep = _endpoint(app, "api_thread_archive")
    resp = await ep(th.id, FakeRequest({}, json_ct=False))
    assert resp.status_code == 403


async def test_archive_route_unknown_thread_404(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host_archive(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    ep = _endpoint(app, "api_thread_archive")
    resp = await ep("ghost", FakeRequest({}))
    assert resp.status_code == 404


async def test_archive_route_success_sets_archived(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host_archive(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("тред")
    ep = _endpoint(app, "api_thread_archive")
    resp = await ep(th.id, FakeRequest({}))
    assert resp.status_code == 200
    import json as _json
    assert _json.loads(resp.body)["archived"] is True


async def test_archive_route_409_only_for_live_thread(tmp_path):
    """per-thread busy: архив ИМЕННО живого треда → 409; архив ДРУГОГО пока первый
    исполняется → OK. Голый глобальный busy-чек ложно-409-нул бы второй."""
    webrtc_server = _webrtc_or_skip()
    host = _api_host_archive(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    from synapse.bridge.state import TaskStatus
    live = host.threads.create("живой тред с задачей")
    other = host.threads.create("другой тред")
    host.threads.append_task(live.id, "t1")
    host.store.start_task("t1", "з", TaskStatus.RUNNING, 0.0)
    ep = _endpoint(app, "api_thread_archive")
    # архив живого треда → 409
    assert (await ep(live.id, FakeRequest({}))).status_code == 409
    assert host.threads.get(live.id).archived is False  # стадия/архив не сдвинуты
    # архив ДРУГОГО треда пока первый исполняется → OK
    resp = await ep(other.id, FakeRequest({}))
    assert resp.status_code == 200
    assert host.threads.get(other.id).archived is True


# --- GET /api/threads hides archived, ?archived=1 shows them --------------------------


async def test_threads_list_route_hides_archived(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host_archive(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    a = host.threads.create("живой")
    b = host.threads.create("архив")
    host.threads.set_archived(b.id, True)
    ep = _endpoint(app, "api_threads_list")
    import json as _json
    visible = [t["id"] for t in _json.loads((await ep()).body)["threads"]]
    assert a.id in visible and b.id not in visible


async def test_threads_list_route_archived_param(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host_archive(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    a = host.threads.create("живой")
    b = host.threads.create("архив")
    host.threads.set_archived(b.id, True)
    ep = _endpoint(app, "api_threads_list")
    import json as _json
    # ?archived=1 → только архив
    archived = _json.loads((await ep(archived="1")).body)["threads"]
    assert [t["id"] for t in archived] == [b.id]


# --- DELETE /api/projects/{id} --------------------------------------------------------


async def test_delete_project_route_csrf(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host_archive(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    ep = _endpoint(app, "api_projects_delete")
    resp = await ep("x", FakeRequest({}, json_ct=False))
    assert resp.status_code == 403


async def test_delete_project_route_unknown_404(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host_archive(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    ep = _endpoint(app, "api_projects_delete")
    resp = await ep("ghost", FakeRequest({}))
    assert resp.status_code == 404


async def test_delete_project_route_success_unbinds_threads(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host_archive(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    (tmp_path / "p").mkdir()
    proj = await host.projects.add("п", str(tmp_path / "p"))
    th = host.threads.create("в проекте")
    host.threads.bind_project(th.id, proj["id"])
    ep = _endpoint(app, "api_projects_delete")
    resp = await ep(proj["id"], FakeRequest({}))
    assert resp.status_code == 200
    import json as _json
    # проект удалён из возвращённого списка
    assert all(p["id"] != proj["id"] for p in _json.loads(resp.body)["projects"])
    # тред жив, но потерял привязку
    assert host.threads.get(th.id) is not None
    assert host.threads.get(th.id).project_id is None
    # event в ленте
    feed = host.threads.read_feed(th.id)
    assert any(e.get("kind") == "event" and "проект удалён" in e.get("text", "") for e in feed)


# --- UI lexical: archive/delete handlers ----------------------------------------------


def test_archive_delete_ui_handlers_present_and_xss_safe():
    """app.js: кнопка «архив» в карточке треда, «×» у проекта, confirm(), deleteJSON;
    без innerHTML; карточка остаётся ссылкой (#/thread/...) для SPA-роутера."""
    app_path = Path(__file__).resolve().parent.parent / "synapse" / "pipeline" / "client" / "app.js"
    app = app_path.read_text(encoding="utf-8")
    for token in ("archiveThread", "deleteProject", "deleteJSON",
                  "tc-archive", "pr-delete", "/archive", "/api/projects/",
                  "window.confirm"):
        assert token in app, f"archive/delete UI missing {token!r}"
    assert "innerHTML" not in app
    # archived-фильтрация идёт через сервер (loadLists), но UI не рендерит archived-треды



