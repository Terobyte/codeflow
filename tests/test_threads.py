"""UI v2 слайс UI-2: ThreadStore — персист метаданных (находка G) и ленты (S3)."""
import json

from synapse.threads import ThreadStore


class FakeClock:
    def __init__(self, t=0.0): self.t = t
    def now(self): return self.t


def test_create_persist_reload_and_task_index(tmp_path):
    clock = FakeClock(10.0)
    ts = ThreadStore(clock, tmp_path, feed_max=100)
    th = ts.create("сделай лендинг")
    ts.append_task(th.id, "task-1")
    clock.t = 20.0
    ts.set_outcome(th.id, "completed")

    reloaded = ThreadStore(FakeClock(), tmp_path, feed_max=100)  # рестарт хоста
    got = reloaded.get(th.id)
    assert got is not None and got.title == "сделай лендинг"
    assert got.task_ids == ["task-1"] and got.last_outcome == "completed"
    assert got.updated_ts == 20.0
    assert reloaded.thread_for_task("task-1").id == th.id
    assert reloaded.thread_for_task("nope") is None


def test_list_sorted_by_updated_desc(tmp_path):
    clock = FakeClock(1.0)
    ts = ThreadStore(clock, tmp_path, feed_max=100)
    a = ts.create("a"); clock.t = 2.0
    b = ts.create("b"); clock.t = 3.0
    ts.append_task(a.id, "t1")  # a обновился позже
    assert [t.id for t in ts.list()] == [a.id, b.id]


def test_feed_appends_survive_restart_and_cap(tmp_path):
    ts = ThreadStore(FakeClock(), tmp_path, feed_max=5)
    th = ts.create("x")
    for i in range(13):
        ts.append_feed(th.id, {"ts": float(i), "kind": "text", "text": f"e{i}"})
    tail = ThreadStore(FakeClock(), tmp_path, feed_max=5).read_feed(th.id)
    assert len(tail) <= 6                      # кап держит файл ~feed_max (допуск на фактор 1.2)
    assert tail[-1]["text"] == "e12"            # хвост свежий
    assert all(isinstance(e, dict) for e in tail)


def test_corrupt_thread_json_is_skipped_not_fatal(tmp_path):
    (tmp_path / "broken.json").write_text("{oops", encoding="utf-8")
    ts = ThreadStore(FakeClock(), tmp_path, feed_max=5)
    assert ts.list() == []


import asyncio  # noqa: E402

from synapse.bridge.kora import KoraRunner  # noqa: E402
from synapse.bridge.runspec import RunSpec  # noqa: E402
from synapse.bridge.state import SpeakLedger, TaskStatus, TaskStore  # noqa: E402
from synapse.config import SynapseConfig  # noqa: E402
from synapse.journal import TurnJournal  # noqa: E402


class _OkClient:
    """Скриптованный клиент: один ResultMessage-подобный no-op — ран завершается сам."""
    def __init__(self, opts): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    async def query(self, text): pass
    async def receive_response(self):
        if False:
            yield None


async def test_run_finished_reports_thread_outcome(tmp_path):
    clock = FakeClock()
    store = TaskStore(clock)
    outcomes = []
    cfg = SynapseConfig(kora_workspace_dir=str(tmp_path / "ws"))
    runner = KoraRunner(cfg, store, SpeakLedger(), clock, TurnJournal(str(tmp_path / "j"), clock),
                        None, client_factory=_OkClient,
                        on_run_finished=lambda thread_id, outcome, gate_mode=None:
                            outcomes.append((thread_id, outcome)))
    store.start_task("t1", "задача", TaskStatus.RUNNING, 0.0)
    await runner._run("t1", "задача", RunSpec(thread_id="th9"))
    # пустой стрим без task_completed → терминализация в FAILED → исход failed
    assert outcomes == [("th9", "failed")]


from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier  # noqa: E402
from synapse.dispatcher.tools import KoraBridge, ToolHandlers  # noqa: E402


async def test_voice_submit_gets_auto_thread(tmp_path):
    clock = FakeClock()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    cfg = SynapseConfig()
    classifier = KeywordClassifier(cfg.destructive_keywords)
    confirm = ConfirmFlow(store, clock, classifier, journal, cfg.affirm_words,
                          cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    threads = ThreadStore(clock, tmp_path / "threads")
    voice_thread = {"id": None}
    started = []

    def _committed(task_id, text):
        th = threads.get(voice_thread["id"]) if voice_thread["id"] else None
        if th is None:
            th = threads.create(title=text)
            voice_thread["id"] = th.id
        threads.append_task(th.id, task_id)
        started.append((task_id, th.id))

    bridge = KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg,
                        on_task_committed=_committed)
    handlers = ToolHandlers(bridge, journal)
    handlers.begin_turn("turn-1")
    res = await handlers.submit_task(text="создай файл заметок")
    assert res["outcome"] == "committed"
    task_id, thread_id = started[0]
    assert threads.thread_for_task(task_id).id == thread_id
    assert threads.get(thread_id).title.startswith("создай файл")


async def test_feed_writer_persists_kora_entries_by_task(tmp_path):
    clock = FakeClock()
    store = TaskStore(clock)
    threads = ThreadStore(clock, tmp_path / "threads")
    th = threads.create("тред")
    threads.append_task(th.id, "t1")

    def sink(entry: dict) -> None:  # копия wiring-а build_host
        tid = entry.get("task_id")
        target = threads.thread_for_task(tid) if tid else None
        if target is not None:
            threads.append_feed(target.id, entry)

    cfg = SynapseConfig(kora_workspace_dir=str(tmp_path / "ws"))
    runner = KoraRunner(cfg, store, SpeakLedger(), clock,
                        TurnJournal(str(tmp_path / "j"), clock), None,
                        client_factory=_OkClient, log_sink=sink)
    store.start_task("t1", "задача", TaskStatus.RUNNING, 0.0)
    await runner._run("t1", "задача", RunSpec(thread_id=th.id))

    feed = ThreadStore(FakeClock(), tmp_path / "threads").read_feed(th.id)  # рестарт
    assert feed and feed[0]["kind"] == "task" and feed[0]["task_id"] == "t1"


# --- UI v3 иерархия «проекты → треды»: настоящие замыкания build_host ---------------------
# build_host собирается с фейковыми ключами (паттерн test_host_singleton) — тестируем
# НАСТОЯЩИЙ _on_task_committed/_resolve_project_root, а не копию wiring-а.


def _fake_host(tmp_path):
    from synapse.pipeline.app import build_host
    cfg = SynapseConfig(
        google_api_key="fake-google-key",
        openrouter_api_key="fake-openrouter-key",
        anthropic_api_key="fake-anthropic-key",
        deepgram_api_key="fake-deepgram-key",
        fish_audio_api_key="fake-fish-key",
        fish_reference_id="fake-fish-ref",
        journal_dir=str(tmp_path),
    )
    host = build_host(cfg)
    specs = []
    host.kora_runner.start = lambda tid, text, spec: specs.append(spec)
    return host, specs


async def test_voice_auto_thread_born_in_active_project(tmp_path):
    host, specs = _fake_host(tmp_path)
    proj_dir = tmp_path / "proj"; proj_dir.mkdir()
    proj = await host.projects.add("мой проект", str(proj_dir))
    host.voice_project["id"] = proj["id"]

    host.bridge.on_task_committed("t1", "сделай файл")

    th = host.threads.thread_for_task("t1")
    assert th.project_id == proj["id"]           # авто-тред — ветка активного проекта
    assert specs[0].thread_id == th.id
    assert specs[0].project_root == proj["path"] # Кора идёт в папку проекта, не в дефолт


async def test_voice_auto_thread_dead_project_degrades_to_loose(tmp_path):
    host, specs = _fake_host(tmp_path)
    host.voice_project["id"] = "ghost"           # проект удалили, а дом ещё помнит id

    host.bridge.on_task_committed("t2", "текст задачи")

    th = host.threads.thread_for_task("t2")
    assert th.project_id is None
    assert specs[0].project_root is None


async def test_voice_task_in_project_thread_resolves_project_root(tmp_path):
    # Регресс-пин бага: раньше голосовой путь слал жёсткое project_root=None —
    # задача в проектном треде игнорировала папку проекта.
    host, specs = _fake_host(tmp_path)
    proj_dir = tmp_path / "p2"; proj_dir.mkdir()
    proj = await host.projects.add("п2", str(proj_dir))
    th = host.threads.create("тред проекта", project_id=proj["id"])
    host.voice_thread["id"] = th.id

    host.bridge.on_task_committed("t3", "задача")

    assert specs[0].thread_id == th.id
    assert specs[0].project_root == proj["path"]


async def test_http_task_in_project_thread_resolves_project_root(tmp_path):
    host, specs = _fake_host(tmp_path)
    proj_dir = tmp_path / "p3"; proj_dir.mkdir()
    proj = await host.projects.add("п3", str(proj_dir))
    th = host.threads.create("тред проекта", project_id=proj["id"])
    host.current_http_thread["id"] = th.id

    # HTTP-мост живёт внутри text_loop (S7: свой ToolHandlers) — общий резолвер тот же
    host.text_loop._handlers.bridge.on_task_committed("t4", "задача")

    assert specs[0].thread_id == th.id
    assert specs[0].project_root == proj["path"]
