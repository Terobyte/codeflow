"""RunSpec — единый носитель launch-параметров запуска Коры (спека UI v2 §3, раунды 4-5).
В начале `_run` атомарно кладётся в per-run снапшот; cwd опций, workspace в тексте
промпта и клетка гейта читают ОДИН источник — рассинхрон «трёх голов» невозможен по
построению (находка B). gate_mode/model потребляются слайсом UI-4; поля есть с рождения,
чтобы сигнатура больше не менялась."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunSpec:
    thread_id: str
    project_root: str | None = None  # None → дефолт-воркспейс (KORA_WORKSPACE_DIR)
    gate_mode: str = "full"          # "docs_only" появляется в UI-4
    model: str | None = None         # None → cfg.kora_model
