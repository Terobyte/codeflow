"""kora status UI (tero run 2026-07-12) — светофор Коры + вкладка логов «размышлений».

Покрывает Plan v2 ран-файла `2026-07-12-synapse-kora-status-ui.md`:
- `_message_to_log_entries` — display-only маппер-близнец (values в tool_use видны — осознанная,
  задокументированная диспозиция R4/P-D; thinking-ТЕКСТ присутствует, в отличие от journal'ного {});
- log_sink в KoraRunner: порядок entries, изоляция исключений (R1: падающий sink НЕ роняет
  живую задачу), stale-guard, sink=None;
- `_status_color`: терминал/нет-задачи ПЕРВЫМИ (R2: завершённая задача не гниёт в жёлтый/красный
  от вечно растущего liveness-возраста), полная матрица;
- роуты /client/kora-status, /client/kora-log, /client/logs, /client/status-widget.js —
  все ДО app.mount (P-G); инжект виджета в оба index-пути; лексика static-файлов.

Конвенции — test_slice5_pwa.py (прямой await роутов, `.body`, `_endpoint()`, никакого TestClient);
fake-клиент ПРОДУБЛИРОВАН из test_kora.py:38-116 (НЕ импортирован: в репо нет cross-import
прецедента между тест-модулями — P-E).
"""
from __future__ import annotations

import json
import types
from collections import deque

import pytest

from synapse.bridge.kora import KoraRunner, _message_to_log_entries
from synapse.bridge.state import Liveness, SpeakLedger, TaskStatus, TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.journal import TurnJournal


def _webrtc_server_or_skip():
    pytest.importorskip("aiortc")
    pytest.importorskip("cv2")
    pytest.importorskip("fastapi")
    try:
        from synapse.pipeline import webrtc_server
    except (ImportError, RuntimeError) as e:
        pytest.skip(f"webrtc deps/prebuilt UI unavailable: {e}")
    return webrtc_server


def _endpoint(app, name):
    for route in app.routes:
        ep = getattr(route, "endpoint", None)
        if ep is not None and getattr(ep, "__name__", None) == name:
            return ep
    raise AssertionError(f"route endpoint {name!r} not found")


# --- fake SDK messages (дубликат идиомы test_kora.py — маппер дак-тайпит по имени класса) ----


class SystemMessage:
    def __init__(self, subtype, data=None):
        self.subtype = subtype
        self.data = data or {}


class AssistantMessage:
    def __init__(self, content):
        self.content = content


class UserMessage:
    def __init__(self, content):
        self.content = content


class ResultMessage:
    def __init__(self, is_error, num_turns=1, total_cost_usd=0.001):
        self.is_error = is_error
        self.num_turns = num_turns
        self.total_cost_usd = total_cost_usd


class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class ThinkingBlock:
    def __init__(self, thinking, signature=""):
        self.thinking = thinking
        self.signature = signature


class ToolResultBlock:
    def __init__(self, tool_use_id, content, is_error=False):
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class RateLimitEvent:  # неизвестный тип: дисплей-лента молчит (journal ловит его отдельно)
    pass


# --- fake async-context client (дубликат идиомы test_kora.py) -------------------------------


def _static(messages):
    async def gen():
        for m in messages:
            yield m

    return gen


def _client_factory(gen_func):
    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, prompt, session_id="default"):
            pass

        def receive_response(self):
            return gen_func()

    return lambda opts: _FakeClient()


def make_runner(tmp_path, client_factory=None, log_sink=None):
    clock = FakeClock(0.0)
    ws = tmp_path / "ws"
    cfg = SynapseConfig(kora_workspace_dir=str(ws))
    store = TaskStore(clock)
    ledger = SpeakLedger()
    journal = TurnJournal(str(tmp_path / "journal"), clock, session_id="s")
    runner = KoraRunner(
        cfg, store, ledger, clock, journal, None, client_factory=client_factory, log_sink=log_sink
    )
    return runner, store, journal


def _journal_rows(journal):
    return [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines() if line.strip()]


# =========================================================================================
# 1. _message_to_log_entries — display-only маппер
# =========================================================================================


def test_log_mapper_full_stream_kinds_and_texts():
    msgs = [
        SystemMessage("init", {"session_id": "s1", "model": "m"}),
        AssistantMessage(
            [
                TextBlock("привет"),
                ThinkingBlock("тайная мысль"),
                ToolUseBlock("u1", "Write", {"file_path": "a.txt", "content": "b"}),
            ]
        ),
        UserMessage([ToolResultBlock("u1", "ok", is_error=False)]),
        ResultMessage(is_error=False, num_turns=2, total_cost_usd=0.01),
    ]
    entries = []
    for m in msgs:
        entries += _message_to_log_entries(m, 1.5)

    assert [e["kind"] for e in entries] == ["system", "text", "thinking", "tool_use", "tool_result", "result"]
    assert entries[0]["text"] == "старт сессии, модель m"
    assert entries[1]["text"] == "привет"
    # thinking-ТЕКСТ присутствует — в отличие от journal-маппера, где payload={} заморожен.
    assert entries[2]["text"] == "тайная мысль"
    assert entries[4]["text"] == "ок"
    assert entries[5]["text"] == "задача завершена · ходов: 2 · $0.0100"
    assert all(e["ts"] == 1.5 for e in entries)


def test_log_mapper_tool_use_values_visible():
    # Осознанная диспозиция R4/P-D: display-only лента показывает ЗНАЧЕНИЯ tool-input
    # (владелец читает логи своей машины) — политика keys-only защищает другого потребителя
    # (journal/store/LLM-контекст) и не тронута.
    (entry,) = _message_to_log_entries(
        AssistantMessage([ToolUseBlock("u1", "Write", {"file_path": "a.txt", "content": "b"})]), 1.0
    )
    assert entry["kind"] == "tool_use"
    assert entry["text"].startswith("Write: ")
    assert "a.txt" in entry["text"]
    assert '"content": "b"' in entry["text"]


def test_log_mapper_caps():
    entries = _message_to_log_entries(
        AssistantMessage(
            [
                TextBlock("h" * 5000),
                ThinkingBlock("t" * 5000),
                ToolUseBlock("u1", "Write", {"content": "c" * 1000}),
            ]
        ),
        1.0,
    )
    assert len(entries[0]["text"]) == 4000
    assert len(entries[1]["text"]) == 4000
    assert len(entries[2]["text"]) == 300


def test_log_mapper_result_failure():
    (entry,) = _message_to_log_entries(ResultMessage(is_error=True, num_turns=3, total_cost_usd=0.5), 1.0)
    assert entry["kind"] == "result"
    assert entry["text"] == "задача упала · ходов: 3 · $0.5000"


def test_log_mapper_result_without_metadata():
    (entry,) = _message_to_log_entries(ResultMessage(is_error=False, num_turns=None, total_cost_usd=None), 1.0)
    assert entry["text"] == "задача завершена"


def test_log_mapper_non_init_system_shows_subtype():
    (entry,) = _message_to_log_entries(SystemMessage("compact_boundary", {}), 1.0)
    assert entry == {"ts": 1.0, "kind": "system", "text": "compact_boundary"}


def test_log_mapper_unknown_type_is_skipped():
    assert _message_to_log_entries(RateLimitEvent(), 1.0) == []


def test_log_mapper_tool_result_error():
    (entry,) = _message_to_log_entries(UserMessage([ToolResultBlock("u1", "x", is_error=True)]), 1.0)
    assert entry["kind"] == "tool_result"
    assert entry["text"] == "ошибка"


# =========================================================================================
# 2. KoraRunner + log_sink — порядок, изоляция (R1), stale-guard, sink=None
# =========================================================================================


async def test_runner_sink_receives_header_then_entries_in_stream_order(tmp_path):
    msgs = [
        SystemMessage("init", {"model": "m"}),
        AssistantMessage([TextBlock("делаю"), ThinkingBlock("мысль")]),
        ResultMessage(is_error=False),
    ]
    entries: list[dict] = []
    runner, store, _ = make_runner(
        tmp_path, client_factory=_client_factory(_static(msgs)), log_sink=entries.append
    )
    store.start_task("tk", "задача", TaskStatus.RUNNING, 0.0)

    await runner._run("tk", "задача")

    assert [e["kind"] for e in entries] == ["task", "system", "text", "thinking", "result"]
    assert entries[0]["text"] == "задача"  # заголовок ленты — сам текст задачи (Изм.1d)
    assert store.task.status == TaskStatus.COMPLETED


async def test_runner_without_sink_streams_fine(tmp_path):
    msgs = [SystemMessage("init", {}), ResultMessage(is_error=False)]
    runner, store, _ = make_runner(tmp_path, client_factory=_client_factory(_static(msgs)))
    store.start_task("tk", "задача", TaskStatus.RUNNING, 0.0)

    await runner._run("tk", "задача")

    assert store.task.status == TaskStatus.COMPLETED


async def test_runner_raising_sink_never_fails_the_task(tmp_path):
    # R1 BLOCKER: исключение display-only кода не имеет права утечь в _run'ов except Exception
    # (это дало бы KORA_RUN_FAILED + terminalize реально успешной задачи).
    def bad_sink(entry):
        raise RuntimeError("display-only boom")

    msgs = [SystemMessage("init", {}), ResultMessage(is_error=False)]
    runner, store, journal = make_runner(
        tmp_path, client_factory=_client_factory(_static(msgs)), log_sink=bad_sink
    )
    store.start_task("tk", "задача", TaskStatus.RUNNING, 0.0)

    await runner._run("tk", "задача")

    assert store.task.status == TaskStatus.COMPLETED
    alerts = [r for r in _journal_rows(journal) if r["kind"] == "alert"]
    assert not any(a["alert_kind"] == "KORA_RUN_FAILED" for a in alerts)


async def test_runner_stale_run_writes_only_its_header_to_sink(tmp_path):
    # Store занят ДРУГОЙ задачей → stale-guard в _stream баилит до маппинга: в sink попадает
    # только заголовок kind="task" (он пишется до первого сообщения), ни одной entry потока.
    msgs = [SystemMessage("init", {}), AssistantMessage([TextBlock("x")]), ResultMessage(is_error=False)]
    entries: list[dict] = []
    runner, store, _ = make_runner(
        tmp_path, client_factory=_client_factory(_static(msgs)), log_sink=entries.append
    )
    store.start_task("OTHER", "другая", TaskStatus.RUNNING, 0.0)

    await runner._run("tk", "задача")

    assert [e["kind"] for e in entries] == ["task"]


# =========================================================================================
# 3. _status_color — терминал/нет-задачи ПЕРВЫМИ (R2), полная матрица
# =========================================================================================


@pytest.mark.parametrize(
    "liveness,task_status,awaiting,expected",
    [
        (Liveness.OK, None, False, "green"),
        (Liveness.OK, TaskStatus.IDLE, False, "green"),
        (Liveness.OK, TaskStatus.RUNNING, False, "green"),
        (Liveness.OK, TaskStatus.COMPLETED, False, "green"),
        (Liveness.STALE, TaskStatus.RUNNING, False, "yellow"),
        (Liveness.OK, TaskStatus.RUNNING, True, "yellow"),
        (Liveness.OK, TaskStatus.PENDING_CONFIRMATION, False, "yellow"),
        (Liveness.OK, TaskStatus.CANCEL_REQUESTED, False, "yellow"),
        (Liveness.UNREACHABLE, TaskStatus.RUNNING, False, "red"),
        (Liveness.OK, TaskStatus.FAILED, False, "red"),
        # прецеденс: red бьёт awaiting-yellow
        (Liveness.UNREACHABLE, TaskStatus.RUNNING, True, "red"),
        # R2: терминал/нет-задачи выигрывают у протухшего liveness (heartbeat'ов больше нет,
        # возраст растёт вечно — завершённая задача не должна гнить в жёлтый/красный).
        (Liveness.STALE, TaskStatus.COMPLETED, False, "green"),
        (Liveness.UNREACHABLE, TaskStatus.COMPLETED, False, "green"),
        (Liveness.UNREACHABLE, None, False, "green"),
        (Liveness.STALE, TaskStatus.FAILED, False, "red"),
    ],
)
def test_status_color_matrix(liveness, task_status, awaiting, expected):
    webrtc_server = _webrtc_server_or_skip()
    assert webrtc_server._status_color(liveness, task_status, awaiting) == expected


# =========================================================================================
# 4. Роуты kora-status / kora-log — содержательный стаб: реальный TaskStore + FakeClock
# =========================================================================================


def _stub_host(clock, store, kora_log=None):
    cfg = types.SimpleNamespace(stale_after_s=120.0, unreachable_after_s=300.0)
    return types.SimpleNamespace(clock=clock, cfg=cfg, store=store, kora_log=kora_log)


async def test_kora_status_route_running_fresh_is_green(tmp_path):
    webrtc_server = _webrtc_server_or_skip()
    clock = FakeClock(1.0)
    store = TaskStore(FakeClock(0.0))
    store.start_task("t1", "скачать отчёт", TaskStatus.RUNNING, now=0.0)
    store.heartbeat(0.0)
    app = webrtc_server.build_web_app(host=_stub_host(clock, store))

    resp = await _endpoint(app, "kora_status")()
    data = json.loads(resp.body)

    assert data == {
        "color": "green",
        "liveness": "ok",
        "task_status": "running",
        "awaiting_answer": False,
        "task_text": "скачать отчёт",
    }


async def test_kora_status_route_stale_running_is_yellow(tmp_path):
    webrtc_server = _webrtc_server_or_skip()
    clock = FakeClock(200.0)  # heartbeat в 0.0 → возраст 200 ≥ stale 120, < unreachable 300
    store = TaskStore(FakeClock(0.0))
    store.start_task("t1", "задача", TaskStatus.RUNNING, now=0.0)
    store.heartbeat(0.0)
    app = webrtc_server.build_web_app(host=_stub_host(clock, store))

    data = json.loads((await _endpoint(app, "kora_status")()).body)

    assert data["color"] == "yellow"
    assert data["liveness"] == "stale"


async def test_kora_status_route_awaiting_running_gate(tmp_path):
    webrtc_server = _webrtc_server_or_skip()
    clock = FakeClock(1.0)
    store = TaskStore(FakeClock(0.0))
    store.start_task("t1", "задача", TaskStatus.RUNNING, now=0.0)
    store.heartbeat(0.0)
    store.set_awaiting()
    app = webrtc_server.build_web_app(host=_stub_host(clock, store))

    data = json.loads((await _endpoint(app, "kora_status")()).body)
    assert data["awaiting_answer"] is True
    assert data["color"] == "yellow"

    # RUNNING-гейт (зеркалит snapshot, state.py:310): та же awaiting-отметка при терминальном
    # статусе НЕ показывается и не красит светофор.
    store.set_task_status(TaskStatus.COMPLETED)
    data2 = json.loads((await _endpoint(app, "kora_status")()).body)
    assert data2["awaiting_answer"] is False
    assert data2["color"] == "green"


async def test_kora_status_route_no_task_is_green_with_nulls():
    webrtc_server = _webrtc_server_or_skip()
    store = TaskStore(FakeClock(0.0))
    app = webrtc_server.build_web_app(host=_stub_host(FakeClock(10_000.0), store))

    data = json.loads((await _endpoint(app, "kora_status")()).body)

    assert data["color"] == "green"  # R2: нет задачи — протухший _last_event_ts не о чем
    assert data["task_status"] is None
    assert data["task_text"] is None


async def test_kora_status_route_truncates_task_text_to_60():
    webrtc_server = _webrtc_server_or_skip()
    store = TaskStore(FakeClock(0.0))
    store.start_task("t1", "а" * 61, TaskStatus.RUNNING, now=0.0)
    store.heartbeat(0.0)
    app = webrtc_server.build_web_app(host=_stub_host(FakeClock(1.0), store))

    data = json.loads((await _endpoint(app, "kora_status")()).body)
    assert data["task_text"] == "а" * 60


async def test_kora_log_route_serves_entries_in_insertion_order():
    webrtc_server = _webrtc_server_or_skip()
    log = deque(maxlen=3)
    for i in range(5):  # переполнение ring-buffer: остаются 3 последних, старейшие первыми
        log.append({"ts": float(i), "kind": "text", "text": f"e{i}"})
    app = webrtc_server.build_web_app(host=_stub_host(FakeClock(0.0), TaskStore(FakeClock(0.0)), kora_log=log))

    data = json.loads((await _endpoint(app, "kora_log_feed")()).body)
    assert [e["text"] for e in data["entries"]] == ["e2", "e3", "e4"]


async def test_kora_log_route_unwired_host_serves_empty():
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=_stub_host(FakeClock(0.0), TaskStore(FakeClock(0.0)), kora_log=None))

    data = json.loads((await _endpoint(app, "kora_log_feed")()).body)
    assert data == {"entries": []}


# =========================================================================================
# 5. Инжект + static-роуты + лексика + порядок регистрации (P-G)
# =========================================================================================


@pytest.mark.parametrize("endpoint_name", ["client_index", "client_index_html"])
async def test_index_routes_inject_status_widget(endpoint_name):
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    body = (await _endpoint(app, endpoint_name)()).body.decode("utf-8")
    assert "status-widget.js" in body


async def test_logs_route_serves_safe_html():
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    resp = await _endpoint(app, "client_logs")()

    assert resp.status_code == 200
    assert resp.media_type == "text/html"
    body = resp.body.decode("utf-8")
    for token in ("kora-log", "textContent", "← назад", "visibilitychange"):
        assert token in body, f"logs.html missing expected token {token!r}"
    # XSS-дисциплина: только textContent, никакой вставки сырого HTML.
    assert "innerHTML" not in body


async def test_status_widget_route_serves_safe_js():
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    resp = await _endpoint(app, "client_status_widget_js")()

    assert resp.status_code == 200
    assert resp.media_type == "text/javascript"
    body = resp.body.decode("utf-8")
    for token in ("kora-status", "zIndex", "2147483647", "location.href", "visibilitychange"):
        assert token in body, f"status-widget.js missing expected token {token!r}"
    assert "innerHTML" not in body
    # R3: standalone iOS PWA без вкладок — навигация, не новое окно.
    assert "window.open" not in body


def test_new_routes_registered_before_client_mount():
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    routes = app.router.routes
    mount_i = next(i for i, r in enumerate(routes) if r.__class__.__name__ == "Mount")
    idx = {
        getattr(getattr(r, "endpoint", None), "__name__", None): i for i, r in enumerate(routes)
    }
    for name in ("kora_status", "kora_log_feed", "client_logs", "client_status_widget_js"):
        assert idx[name] < mount_i, f"{name} must be registered BEFORE the /client mount"


# =========================================================================================
# 6. SynapseHost — сырой None (паттерн kora_runner, P-B) + конфиг-поле (P-A)
# =========================================================================================


def test_synapse_host_without_kora_log_stores_raw_none():
    from synapse.pipeline.app import SynapseHost

    host = SynapseHost(
        clock=FakeClock(0.0),
        cfg=None,
        journal=None,
        store=None,
        speak_ledger=None,
        classifier=None,
        confirm_flow=None,
        arbiter_policy=None,
        bridge=None,
        handlers=None,
        breaker=None,
        cost_cap=None,
    )
    assert host.kora_log is None


def test_kora_log_max_is_config_not_constant():
    assert SynapseConfig().kora_log_max == 500
