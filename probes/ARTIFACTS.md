# Probe-артефакты (спека `2026-07-15-synapse-kora-voice-files-radio-design.md` §14.2)

Спека запрещает implementation до закрытия probes. Здесь — фактические результаты
прогонов, а не намерения. Каждый probe перезапускается одной командой.

Окружение на 2026-07-15: `claude-agent-sdk 0.2.116` (pin из `pyproject.toml` совпадает
с установленным), `pipecat-ai 1.5.0`, Claude CLI `2.1.210`, Python 3.14.0.

---

## P3b (§14.2.4) — `context_id` presence + uniqueness — **GO** (автоматическая половина)

Команда: `.venv/bin/python probes/p3b_context_id.py` · дата: 2026-07-15 · pipecat 1.5.0

Спека называет это единственной точкой отказа всего presence-механизма: без уникального
`context_id` на каждый `TTSSpeakFrame` окно ответа не откроется ни для одной реплики, и
KV-3b молча деградирует до «только hard question». FIFO-fallback спекой запрещён.

**Результат: допущение верно.** 3 из 3 кейсов зелёные.

| Кейс | Результат |
| --- | --- |
| 20 чередующихся SPEAK-реплик | 20/20 получили уникальный непустой `context_id` |
| Контраст: ход группируется, SPEAK — нет | 3 предложения одного хода → 1 общий контекст; 3 SPEAK → 3 разных id |
| SPEAK посреди хода диспетчера | id Коры и id диспетчера не пересекаются; id Коры доезжает до `TTSStartedFrame` |

**Механика (проверено по исходникам, затем эмпирически).**

- `TTSService.reuse_context_id_within_turn` **по умолчанию `True`**, и `create_context_id()`
  возвращает один и тот же id всем предложениям, пока стоит turn-контекст
  (`tts_service.py:469-478`). Само по себе это как раз тот сценарий, которого спека боялась.
- Спасает то, что `TTSSpeakFrame` — привилегированный путь: перед генерацией id pipecat
  принудительно обнуляет `_turn_context_id`, вынуждая свежий `uuid4()`
  (`tts_service.py:758-769`, комментарий «TTSSpeakFrame is independent»).
- `SynapseSpeakFrame` из §4.1 наследует `TTSSpeakFrame` → уникальность достаётся даром.
  **Флаг `reuse_context_id_within_turn` трогать не нужно**: уникальность реплик Коры от него
  не зависит, а выключение сломало бы группировку предложений диспетчера.
- id доезжает до фрейма: `TTSStartedFrame(context_id=context_id)` (`tts_service.py:1103`).

**Ловушка, на которой probe едва не соврал.** `push_start_frame` в базовом классе —
`False` (`tts_service.py:155`), и с дефолтом `TTSStartedFrame` не эмитится вообще: первый
прогон дал 0 фреймов и «NO-GO». Это был артефакт фейка, а не платформы —
`FishAudioTTSService` передаёт `push_start_frame=True` (`fish/tts.py:195`). Фейк обязан
зеркалить боевой сервис в том параметре, который probe измеряет, иначе он измеряет себя.

**Что НЕ закрыто.** Живая половина: Fish switch latency p95 ≤ 500 мс на 20 чередованиях,
barge-in, cache-key. Требует реального Fish WS. KV-1 остаётся заблокированным до неё;
KV-3b по части корреляции разблокирован.

---

## P1 (§14.2.1) — SDK bidirectional — **OPEN**
## P2 (§14.2.2) — SDK MCP `deliver_file` — **OPEN**
## P3a (§14.2.3) — Pipecat route bypass — **OPEN**
## P4 (§14.2.5) — Fish MP3 segments — **OPEN** (нужен HTTPS staging)
## P5 (§14.2.6) — PWA media auth на iPhone Safari — **OPEN** (нужен HTTPS staging + телефон)
