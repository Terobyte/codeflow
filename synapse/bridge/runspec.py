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
    # None-сентинел как у соседей (B46): None → права дефолтные ("full" нормализует kora),
    # но вид рана честный — «прямая диспетчеризация, НЕ гейт-ран стадии». Гейт-запуски
    # (_launch_run) ВСЕГДА передают явный "docs_only"/"full"; on_run_finished по этому полю
    # отличает стадийный ран (двигает freshness/стадию треда) от прямой задачи (невидима
    # для гейт-стейта). Дефолт "full" здесь конфлейтил прямую задачу с гейт-раном.
    gate_mode: str | None = None
    model: str | None = None         # None → cfg.kora_model
    run_kind: str = "code"           # "code" | "docs"; consult arrives in МЕШ-2
