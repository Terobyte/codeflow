import asyncio
import json
from pathlib import Path

from synapse.prompt import CANON_PHRASE_STALE_KORA
from synapse.runners.console import _read_scenario, run_scenario

DEMO_PATH = Path(__file__).parent / "scenarios" / "demo.jsonl"


def test_demo_scenario_exits_zero_and_speaks_kora_text_verbatim(tmp_path, capsys):
    steps = _read_scenario(str(DEMO_PATH))
    rc = asyncio.run(run_scenario(steps, journal_dir=str(tmp_path), session_id="e2e"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "TTS: Готово: глава 1 сохранена в chapter-1.md." in out
    assert f"TTS: {CANON_PHRASE_STALE_KORA}" in out
    assert "0 unexpected alerts" in out


def test_demo_scenario_journal_has_no_alert_rows(tmp_path):
    steps = _read_scenario(str(DEMO_PATH))
    rc = asyncio.run(run_scenario(steps, journal_dir=str(tmp_path), session_id="e2e2"))
    assert rc == 0
    path = tmp_path / "e2e2.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    alert_rows = [r for r in rows if r["kind"] == "alert"]
    assert alert_rows == []
    turn_rows = [r for r in rows if r["kind"] == "turn"]
    assert len(turn_rows) == 6  # 6 user turns in the demo scenario
