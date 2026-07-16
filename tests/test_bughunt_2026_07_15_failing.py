# -*- coding: utf-8 -*-
"""Красные (негативные) тесты для дефектов, подтверждённых в багханте 2026-07-15 по
диффу origin/main..HEAD. Каждый тест утверждает ЖЕЛАЕМНОЕ поведение и падает сегодня.

- B-CORE-4: TTSCache.__init__ не вычищает осиротевшие .tmp, оставшиеся от рухнувшего
  _atomic_write, — они накапливаются после жёстких убийств процесса.
- B-CORE-6: KoraRunner.start() при RuntimeError из asyncio.create_task (нет event loop)
  оставляет self._active указывать на ранее отменённый таск вместо None.

Третья находка багханта — note_external_turn пишет в общую LLM-историю треда БЕЗ сверки
поколения (B-DISP-7), асимметрия с C6/B20, которыми ingest_user_turn защищён от того же
самого: голосовой flush-путь дописывает assistant-реплику после clear_history и воскрешает
очищенную историю. Красный тест ниже воспроизводяет эффект напрямую.

Эти тесты — канонические для свипа 2026-07-15; одноимённые утверждения в
tests/test_new_reported_bugs_failing.py (WIP-сборищще, где часть тестов падает в собственном
сетапе) следует удалить при консолидации, чтобы не дублировать.
"""
from unittest.mock import MagicMock, patch

from synapse.bridge.kora import KoraRunner
from synapse.clock import SystemClock
from synapse.config import SynapseConfig
from synapse.dispatcher.loop import DispatcherTurnLoop
from synapse.pipeline.tts_cache import TTSCache
from unittest.mock import MagicMock, patch

from synapse.bridge.kora import KoraRunner
from synapse.clock import SystemClock
from synapse.config import SynapseConfig
from synapse.pipeline.tts_cache import TTSCache


# B-CORE-4 — orphaned .tmp survives TTSCache construction (no startup sweep)
def test_b_core_4_tts_cache_init_does_not_sweep_orphaned_tmp(tmp_path):
    """RED: файл .*.tmp, осиротевший рухнувшим _atomic_write, переживает构造 TTSCache.
    Desired: __init__ вычищает stale `.*.tmp` в своём root (тот же паттерн, что создаёт
    _atomic_write), чтобы жёсткие килли не засоряли кэш."""
    orphan = tmp_path / ".deadbeefwav.aaaaaaaa.tmp"
    orphan.write_bytes(b"orphan from a crashed put_wav")
    # паттерн имени совпадает с _atomic_write: f".{path.name}.{uuid.hex}.tmp"
    # (path.name для wav == "{key}.wav" → ".{key}.wav.{uuid}.tmp")
    assert orphan.name.startswith(".") and orphan.name.endswith(".tmp")
    TTSCache(tmp_path, "model", "voice")
    assert not orphan.exists(), "B-CORE-4: orphaned .tmp not swept on TTSCache init"


# B-CORE-6 — _active not reset to None when create_task raises RuntimeError
def test_b_core_6_runner_active_not_cleared_when_create_task_raises():
    """RED: при RuntimeError из asyncio.create_task (нет event loop — консольный/sync-путь)
    except-ветка start() закрывает корутину и терминализирует задачу, но оставляет
    self._active указывать на ранее отменённый таск. Desired: _active is None — после
    провала запуска никакого живого рана нет."""
    runner = KoraRunner(
        SynapseConfig(), MagicMock(), MagicMock(), SystemClock(), MagicMock(), None
    )
    stale_active = MagicMock()
    stale_active.done.return_value = False  # start() вызовет .cancel(), затем не создаст таск
    runner._active = stale_active
    with patch("asyncio.create_task", side_effect=RuntimeError("no running event loop")):
        runner.start("task_1", "do something")
    assert runner._active is None, (
        "B-CORE-6: _active not reset to None after create_task RuntimeError"
    )


# B-DISP-7 — note_external_turn revives history cleared by clear_history (C6 asymmetry)
def test_b_disp_7_note_external_turn_revives_cleared_history():
    """RED: note_external_turn дописывает в общую LLM-историю треда БЕЗ сверки поколения.
    clear_history инкрементит _generations (C6); ingest_user_turn сверяет поколение на
    коммите, чтобы «clear» во время await не воскресил очищенное. note_external_turn делает
    ровно то же (дописывает в shared history) но без проверки — голосовой flush-путь
    дописывает assistant-реплику после clear и воскрешает историю. Desired: после clear
    последующий note_external_turn — no-op, как холодная регидрация (writer уже в ленте)."""
    cfg = SynapseConfig()
    clock = SystemClock()
    loop = DispatcherTurnLoop(
        MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), clock, cfg
    )
    # прогреваем кэш — иначе note_external_turn уйдёт в no-op по `hist is None`
    hist = loop._history_for("t1")
    loop.note_external_turn("t1", "user", "hello")
    assert len(hist) == 1
    loop.clear_history("t1")
    assert len(hist) == 0
    # голосовая assistant-реплика прилетает после clear — должна НЕ воскресить историю
    loop.note_external_turn("t1", "assistant", "i revive the cleared history")
    assert len(hist) == 0, "B-DISP-7: note_external_turn revived cleared history (no generation check)"
