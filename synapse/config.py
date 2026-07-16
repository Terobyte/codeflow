"""SynapseConfig — one dataclass holding every M0 threshold/model-id/secret-name, so nothing
important is a hidden magic number scattered across modules. Thresholds marked "owed" in the
design doc (§7/§8) are config, not constants, precisely so they can be tuned without a code
change once real numbers land from испытание №4/№5.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SynapseConfig:
    # Cascade tiers (Р-14).
    tier1_model: str = "google/gemini-3.5-flash"  # OpenRouter primary
    tier2_model: str = "claude-haiku-4-5"  # Anthropic fallback

    google_api_key: str | None = None
    openrouter_api_key: str | None = None
    anthropic_api_key: str | None = None
    deepgram_api_key: str | None = None
    fish_audio_api_key: str | None = None
    fish_reference_id: str | None = None
    # С5: bearer-токен control plane (middleware в webrtc_server.py). None = deny — не
    # «authn выключен» (см. run()/_require_api_token, где пустой токен роняет старт сервера).
    api_token: str | None = None
    # KV-1a §4.1: собственный голос Коры на Play-пути (REST). Незаданный → резолвится в
    # fish_reference_id ДО обращения к кэшу (одинаковый реальный звук закономерно делит ключ),
    # поэтому это не required-секрет: фича деградирует, не ломается.
    kora_fish_reference_id: str | None = None
    # s2.1-pro-free is free through end of July 2026 (§11.4 M0 assumption); "s2-pro" (paid)
    # is pipecat's own FishAudioTTSService default.
    fish_tts_model: str = "s2.1-pro-free"

    # Play-озвучка ленты: санитайз текста Коры перед TTS — direct Gemini, НЕ OpenRouter
    # (отдельная квота от tier1-каскада; benchmarks/gemini-vs-openrouter-20260715). Таймаут
    # переиспользует request_timeout_s.
    speakify_model: str = "gemini-3.5-flash"
    # KV-2 §4.2: кап длины для speakable() — форматного фильтра «текст Коры звучит как речь,
    # а не как зачитанный markdown». Длиннее — не разговорная реплика, идёт через speakify.
    kora_speak_max_chars: int = 350
    kora_reply_speak_max_chars: int = 2000
    kora_reply_instruction_max_chars: int = 1200
    kora_reply_format_max_chars: int = 400

    request_timeout_s: float = 10.0

    # Circuit breaker windows (Р-14).
    rpm_mute_s: float = 60.0
    rpd_reset_hour_utc: int = 8  # M0-допущение: сброс free tier ~полночь Pacific (±1ч DST, owed)

    # Kora liveness (Р-11) — owed-пороги §7/§8, значения — конфиг, не константы.
    stale_after_s: float = 120.0
    unreachable_after_s: float = 300.0
    heartbeat_interval_s: float = 30.0

    # Destructive-task confirmation protocol (Р-16).
    confirm_timeout_s: float = 30.0
    max_rereadbacks: int = 2

    # SPEAK invariant window (Р-15г).
    critical_speak_window_s: float = 5.0

    # Cost cap (§11.5). None disables the cap.
    max_paid_calls_per_day: int | None = 500

    affirm_words: frozenset[str] = field(default_factory=lambda: frozenset({"да", "подтверждаю", "делай"}))
    deny_words: frozenset[str] = field(default_factory=lambda: frozenset({"нет", "отмена", "стоп"}))
    destructive_keywords: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "удали",
                "сотри",
                "перезапиши",
                "снеси",
                "отформатируй",
                "деплой",
                "drop",
                "rm",
                "delete",
                "overwrite",
            }
        )
    )

    journal_dir: str = "journals"
    include_owed_prompt_rules: bool = True

    # Kora — real producer via Claude Agent SDK (M1 slice 1). `kora_enabled=False` restores the
    # old hollow behavior for environments without the SDK/key. `kora_workspace_dir=None` →
    # ~/synapse-kora-workspace. The gate + system_prompt (not cwd) are the safety boundary
    # (§2.8/§2d): cwd is NOT a sandbox. `kora_cli_path=None` → SDK default; the nvm launcher is
    # broken (version-mismatch), so a live run must point this at the native binary.
    kora_enabled: bool = True
    kora_workspace_dir: str | None = None
    kora_model: str = "claude-sonnet-5"
    kora_cli_path: str | None = None
    kora_max_turns: int = 40
    kora_max_budget_usd: float | None = 1.0
    # Wall-clock watchdog (RISK-M7): asyncio.wait_for around the SDK stream so a hung CLI that
    # never emits a ResultMessage can't strand the task RUNNING forever (max_turns/max_budget
    # only fire on a ResultMessage).
    kora_deadline_s: float = 900.0
    # Дисплей-лента «размышлений Коры» /client/logs — размер ring-buffer на хосте
    # (kora status UI, tero run 2026-07-12).
    kora_log_max: int = 500
    # UI v2 (S3/S32): кап файла истории треда, аналог kora_log_max.
    thread_feed_max: int = 2000
    # UI-5 (S10): порог компакта истории диспетчера — ПЕРЕД ходом, если история длиннее,
    # старшая половина жмётся одним LLM-вызовом (только user/assistant, NO-EXFIL). 0 = выкл.
    dispatcher_compact_after: int = 40
    # ADV-2: дефолт персоны; Thread.persona перекрывает его для отдельного треда.
    default_persona: str = "техлид"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SynapseConfig":
        e = env if env is not None else os.environ
        kwargs = dict(
            google_api_key=e.get("GOOGLE_API_KEY") or None,
            openrouter_api_key=e.get("OPENROUTER_API_KEY") or None,
            anthropic_api_key=e.get("ANTHROPIC_API_KEY") or None,
            deepgram_api_key=e.get("DEEPGRAM_API_KEY") or None,
            fish_audio_api_key=e.get("FISH_AUDIO_API_KEY") or None,
            fish_reference_id=e.get("FISH_REFERENCE_ID") or None,
            api_token=e.get("SYNAPSE_API_TOKEN") or None,
            kora_fish_reference_id=e.get("KORA_FISH_REFERENCE_ID") or None,
            kora_workspace_dir=e.get("KORA_WORKSPACE_DIR") or None,
            kora_cli_path=e.get("KORA_CLI_PATH") or None,
        )
        # Unlike the api keys above, fish_tts_model has a real (non-None) default -- only
        # override it when the env var is actually set, so an unset FISH_TTS_MODEL keeps the
        # dataclass default instead of clobbering it with None.
        if e.get("FISH_TTS_MODEL"):
            kwargs["fish_tts_model"] = e["FISH_TTS_MODEL"]
        if e.get("SYNAPSE_DEFAULT_PERSONA"):
            kwargs["default_persona"] = e["SYNAPSE_DEFAULT_PERSONA"]
        # Same "override only when explicitly set" rule for the non-None Kora defaults.
        # B26: empty value = unset -> keep the dataclass default (same "override only when
        # explicitly set" rule as FISH_TTS_MODEL/_num above), never an active False.
        raw_enabled = e.get("KORA_ENABLED", "").strip().lower()
        if raw_enabled:
            kwargs["kora_enabled"] = raw_enabled not in ("false", "0", "no")
        if e.get("KORA_MODEL"):
            kwargs["kora_model"] = e["KORA_MODEL"]
        # B4: a malformed numeric env var must fall back to the dataclass default, never crash
        # the whole app at startup. Parse defensively; only override kwargs on a clean parse.
        def _num(key: str, parse):
            raw = e.get(key)
            if raw is None or not str(raw).strip():
                return None
            try:
                return parse(raw)
            except (ValueError, TypeError):
                return None

        turns = _num("KORA_MAX_TURNS", int)
        if turns is not None:
            kwargs["kora_max_turns"] = turns
        budget = _num("KORA_MAX_BUDGET_USD", float)
        if budget is not None:
            kwargs["kora_max_budget_usd"] = budget
        deadline = _num("KORA_DEADLINE_S", float)
        if deadline is not None:
            kwargs["kora_deadline_s"] = deadline
        # UI-5 (S10): тот же two-step, что kora_max_turns — _num на малформе/отсутствии
        # возвращает None (конструктор не падает), тогда остаётся дефолт датакласса.
        # ⚠️ НЕ `_num(...) or 40` — это затрёт валидный явный DISPATCHER_COMPACT_AFTER=0.
        compact = _num("DISPATCHER_COMPACT_AFTER", int)
        if compact is not None:
            kwargs["dispatcher_compact_after"] = compact
        # KV-2: тот же two-step, что у остальных kora_*-порогов (малформа/пусто → дефолт).
        speak_cap = _num("KORA_SPEAK_MAX_CHARS", int)
        if speak_cap is not None:
            kwargs["kora_speak_max_chars"] = speak_cap
        for env_key, field_name in (
            ("KORA_REPLY_SPEAK_MAX_CHARS", "kora_reply_speak_max_chars"),
            ("KORA_REPLY_INSTRUCTION_MAX_CHARS", "kora_reply_instruction_max_chars"),
            ("KORA_REPLY_FORMAT_MAX_CHARS", "kora_reply_format_max_chars"),
        ):
            value = _num(env_key, int)
            if value is not None:
                kwargs[field_name] = value
        return cls(**kwargs)

    def validate_voice_keys(self) -> None:
        """Hard-fail with the full list of missing keys (R5) — called when assembling the
        voice pipeline host (synapse.pipeline.app.build_host). The console/text path never
        calls this and works with no keys at all."""
        missing = [
            name
            for name, val in (
                ("OPENROUTER_API_KEY", self.openrouter_api_key),
                ("ANTHROPIC_API_KEY", self.anthropic_api_key),
                ("DEEPGRAM_API_KEY", self.deepgram_api_key),
                ("FISH_AUDIO_API_KEY", self.fish_audio_api_key),
                ("FISH_REFERENCE_ID", self.fish_reference_id),
            )
            if not val
        ]
        if missing:
            raise RuntimeError(
                "synapse: missing required keys for the voice pipeline: " + ", ".join(missing)
            )
