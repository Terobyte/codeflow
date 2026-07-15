"""TurnContext — единый контекст хода для голосового и HTTP каналов (Ф0.1, слайс С1).

Раньше `[СОСТОЯНИЕ]` собирался инлайн в двух местах:
- HTTP: `DispatcherTurnLoop._complete` → `system_prompt + "\\n\\n" + state_block`;
- голос: `build_host._on_end_of_turn` → только `build_system_prompt(...)`, БЕЗ `[СОСТОЯНИЕ]`.

Голос не видел состояние вообще — роутинг `answer_kora` держался на догадке модели. Эта
фабрика извлекает сборку в одно место: оба канала получают одинаковый snapshot на старте
хода, включая `should_hide_task`-скоуп (терминальная задача чужого треда) и `awaiting_answer`
— теперь LLM видит основание позвать `answer_kora` без догадок.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from synapse.bridge.state import TaskStore, should_hide_task
from synapse.clock import Clock
from synapse.config import SynapseConfig
from synapse.prompt import build_system_prompt


@dataclass(frozen=True)
class TurnContext:
    """Контекст одного хода: базовый промпт (со stage-правилами) + снапшот состояния.

    `system_message` — то, что кладётся в role=system сообщение LLM: база + состояние,
    разделённые пустой строкой. Полностью детерминирован входами (нет LLM в сборке)."""
    system_prompt: str   # база промпта + stage-правила (+ словарь задачи, если задан)
    state_block: str     # [СОСТОЯНИЕ] snapshot (render_state с правильным hide-скоупом)

    @property
    def system_message(self) -> str:
        return self.system_prompt + "\n\n" + self.state_block


def build_turn_context(
    *,
    cfg: SynapseConfig,
    store: TaskStore,
    clock: Clock,
    thread_id: str | None,
    task_dictionary: dict[str, str] | None = None,
    stage_block_for: Callable[[str | None], str] | None = None,
    owner_thread_for: Callable[[str], str | None] | None = None,
) -> TurnContext:
    """Собрать TurnContext для хода в треде `thread_id`.

    Все резолверы опциональны — голос собирает их на хосте, HTTP держит в loop-е. Снапшот
    снимается ОДИН раз на старте хода; свежесть симметрична между каналами. Терминальная
    задача чужого треда прячется (`should_hide_task`) — голос раньше вообще не видел
    состояния, теперь видит правильно отскоупленное."""
    stage_block = stage_block_for(thread_id) if stage_block_for is not None else ""
    prompt = build_system_prompt(cfg, task_dictionary or {}, stage_block=stage_block)
    task = store.task
    hide = (task is not None and owner_thread_for is not None
            and should_hide_task(task, thread_id, owner_thread_for(task.id)))
    state = store.render_state(clock.now(), cfg.stale_after_s,
                               cfg.unreachable_after_s, hide_task=hide)
    return TurnContext(system_prompt=prompt, state_block=state)
