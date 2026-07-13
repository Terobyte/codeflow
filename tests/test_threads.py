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
