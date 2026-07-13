"""UI v2 слайс UI-2: RunSpec — один снапшот для cwd/промпта/гейта (спека §3, находка B)."""
import asyncio

from synapse.bridge.kora import KoraRunner
from synapse.bridge.runspec import RunSpec
from synapse.bridge.state import SpeakLedger, TaskStatus, TaskStore
from synapse.config import SynapseConfig
from synapse.journal import TurnJournal


class FakeClock:
    def __init__(self, t=0.0): self.t = t
    def now(self): return self.t


def _runner(tmp_path, captured):
    cfg = SynapseConfig(kora_workspace_dir=str(tmp_path / "default-ws"))
    clock = FakeClock()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)

    class FakeClient:
        def __init__(self, opts): captured["opts"] = opts
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        async def query(self, text): pass
        async def receive_response(self):
            r = captured["runner"]
            captured["gate_in_project"] = r._gate_decision(
                "Write", {"file_path": str(captured["proj"] / "a.txt")}
            )
            captured["gate_in_default_ws"] = r._gate_decision(
                "Write", {"file_path": str(tmp_path / "default-ws" / "b.txt")}
            )
            if False:
                yield None

    runner = KoraRunner(cfg, store, SpeakLedger(), clock, journal, None,
                        client_factory=lambda opts: FakeClient(opts))
    captured["runner"] = runner
    return runner, store


async def test_runspec_project_root_reaches_cwd_prompt_and_gate(tmp_path):
    proj = tmp_path / "proj"; proj.mkdir()
    captured = {"proj": proj}
    runner, store = _runner(tmp_path, captured)
    store.start_task("t1", "задача", TaskStatus.RUNNING, 0.0)

    await runner._run("t1", "задача", RunSpec(thread_id="th1", project_root=str(proj)))

    opts = captured["opts"]
    resolved = str(proj.resolve())
    assert opts.cwd == resolved                      # голова 1: cwd опций
    assert resolved in opts.system_prompt            # голова 2: путь в ТЕКСТЕ промпта
    allowed, _, _ = captured["gate_in_project"]
    assert allowed                                   # голова 3: гейт-клетка = проект
    denied, _, cat = captured["gate_in_default_ws"]
    assert not denied and cat == "outside_workspace" # дефолт-воркспейс теперь ЧУЖОЙ


async def test_runspec_none_project_root_falls_back_to_default_workspace(tmp_path):
    captured = {"proj": tmp_path / "unused"}
    captured["proj"].mkdir()
    runner, store = _runner(tmp_path, captured)
    store.start_task("t2", "задача", TaskStatus.RUNNING, 0.0)

    await runner._run("t2", "задача", RunSpec(thread_id="th1", project_root=None))
    assert captured["opts"].cwd == str((tmp_path / "default-ws").resolve())


async def test_snapshot_cleared_after_run_with_identity_guard(tmp_path):
    captured = {"proj": tmp_path / "p"}; captured["proj"].mkdir()
    runner, store = _runner(tmp_path, captured)
    store.start_task("t3", "з", TaskStatus.RUNNING, 0.0)
    await runner._run("t3", "з", RunSpec(thread_id="th1", project_root=str(captured["proj"])))
    assert runner._run_root is None and runner._run_owner is None
