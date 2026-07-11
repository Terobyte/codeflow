"""Voice pipeline assembly (Р-6, item 20; split host/session — M1 host-singleton).
`build_host` constructs the long-lived logical state (task store, speak ledger, confirm
flow, arbiter policy, breaker, cost cap, ...) ONCE, so it survives a WebRTC reconnect.
`build_session_pipeline` constructs the transport-agnostic per-connection processing chain --
Flux STT -> context aggregator -> cascade LLMSwitcher (Р-14 failover) -> TTS arbiter (Р-5) ->
Fish TTS -- referencing the host's long-lived objects by reference; a fresh one is built on
every connection because a pipecat FrameProcessor instance is only good for one PipelineRunner
run. `run()` — the `python -m synapse.pipeline.app` entrypoint — lazily imports LocalAudioTransport
(S4: it requires pyaudio/portaudio at *module import time*, which would otherwise make this whole
module unimportable without the optional `voice` extra, breaking every environment that never
runs live voice, including test_pipeline_smoke).
"""
from __future__ import annotations

import asyncio

from pipecat.pipeline.llm_switcher import LLMSwitcher
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMAssistantAggregator, LLMUserAggregator
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.services.fish.tts import FishAudioTTSService

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.state import SpeakLedger, TaskStore
from synapse.cascade.breaker import CircuitBreaker
from synapse.cascade.services import CostCap, build_tier_services
from synapse.cascade.strategy import build_strategy_type
from synapse.clock import Clock, SystemClock
from synapse.config import SynapseConfig
from synapse.dispatcher.tools import ALL_SCHEMAS, KoraBridge, ToolHandlers, register_all
from synapse.journal import AlertKind, TurnJournal
from synapse.pipeline.arbiter import ArbiterPolicy, TTSArbiterProcessor
from synapse.pipeline.context_guard import GenerationGuard, GenerationStartHook, make_guarded_assistant_aggregator


class SynapseHost:
    """Long-lived logical state, built ONCE by `build_host()` and shared by reference across
    every WebRTC reconnect (M1 host-singleton): task store, speak ledger, confirm flow, arbiter
    policy, breaker, cost cap. None of these are pipecat FrameProcessors, so nothing here is
    tied to a single PipelineRunner run -- `SynapseSession` (per-connection) is."""

    def __init__(
        self,
        clock: Clock,
        cfg: SynapseConfig,
        journal: TurnJournal,
        store: TaskStore,
        speak_ledger: SpeakLedger,
        classifier: KeywordClassifier,
        confirm_flow: ConfirmFlow,
        arbiter_policy: ArbiterPolicy,
        bridge: KoraBridge,
        handlers: ToolHandlers,
        breaker: CircuitBreaker,
        cost_cap: CostCap,
    ) -> None:
        self.clock = clock
        self.cfg = cfg
        self.journal = journal
        self.store = store
        self.speak_ledger = speak_ledger
        self.classifier = classifier
        self.confirm_flow = confirm_flow
        self.arbiter_policy = arbiter_policy
        self.bridge = bridge
        self.handlers = handlers
        self.breaker = breaker
        self.cost_cap = cost_cap

    async def monitor_forever(self) -> None:
        """R8: periodically drives speak_ledger.check()/store.liveness() so the Р-15г/Р-11
        invariants fire even between turns, not only incidentally when a turn happens to run."""
        while True:
            await asyncio.sleep(self.cfg.heartbeat_interval_s)
            now = self.clock.now()
            for kind, detail in self.speak_ledger.check(now, self.cfg.critical_speak_window_s):
                self.journal.alert(AlertKind(kind), detail)
            self.store.liveness(now, self.cfg.stale_after_s, self.cfg.unreachable_after_s)


def build_host(cfg: SynapseConfig, clock: Clock | None = None) -> SynapseHost:
    """Hard-fails via cfg.validate_voice_keys() before touching the network if a required
    key is missing (R5) — never a silently half-configured host. Builds the long-lived logical
    state exactly ONCE; call `build_session_pipeline(host)` per WebRTC connection for the
    per-connection processors."""
    cfg.validate_voice_keys()
    clock = clock or SystemClock()

    journal = TurnJournal(cfg.journal_dir, clock)
    store = TaskStore(clock, journal_dir=cfg.journal_dir)
    speak_ledger = SpeakLedger()
    classifier = KeywordClassifier(cfg.destructive_keywords)
    confirm_flow = ConfirmFlow(
        store, clock, classifier, journal,
        cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s,
    )

    arbiter_policy = ArbiterPolicy()

    def on_speak(text: str) -> None:
        # SPEAK path (Р-5/Р-15): Kora's ready text goes straight to the TTS queue, no LLM.
        # Р-15г (Bug 2): also satisfy the ledger so a critical event that DID get its SPEAK
        # stops counting as an unpaired-critical alert. register_critical itself is wired only
        # in the console runner until the WebSocket Kora bridge lands.
        arbiter_policy.push_speak(text)
        speak_ledger.register_speak_text(text, clock.now())

    bridge = KoraBridge(store=store, confirm_flow=confirm_flow, clock=clock, on_speak=on_speak, cfg=cfg)
    handlers = ToolHandlers(bridge, journal)

    # breaker needs only the tier COUNT, not the service instances themselves -- those are
    # per-connection (build_session_pipeline rebuilds them fresh every reconnect, since a
    # pipecat FrameProcessor instance belongs to exactly one PipelineRunner run). This pair is
    # discarded immediately after counting.
    _tier_probe, _ = build_tier_services(cfg)
    breaker = CircuitBreaker(len(_tier_probe), cfg.rpm_mute_s, cfg.rpd_reset_hour_utc)
    cost_cap = CostCap(cfg.max_paid_calls_per_day)

    return SynapseHost(
        clock=clock,
        cfg=cfg,
        journal=journal,
        store=store,
        speak_ledger=speak_ledger,
        classifier=classifier,
        confirm_flow=confirm_flow,
        arbiter_policy=arbiter_policy,
        bridge=bridge,
        handlers=handlers,
        breaker=breaker,
        cost_cap=cost_cap,
    )


class SynapseSession:
    """Bundles the assembled per-connection pipeline with the collaborators `run()` (and tests)
    need direct handles to — the pipeline itself only sees frames, never these objects. Built
    fresh by `build_session_pipeline()` on every WebRTC connection; wired to the long-lived
    `SynapseHost` by reference."""

    def __init__(self, pipeline: Pipeline, llm_switcher: LLMSwitcher, generation_guard: GenerationGuard) -> None:
        self.pipeline = pipeline
        self.llm_switcher = llm_switcher
        self.generation_guard = generation_guard


def build_session_pipeline(host: SynapseHost) -> SynapseSession:
    """Builds the transport-agnostic per-connection processing chain, wired to `host`'s
    long-lived state by reference. Safe to call again on every WebRTC reconnect -- each call
    returns fresh FrameProcessor instances (pipecat constraint: an instance is good for exactly
    one PipelineRunner run, so these can never be shared across connections like the host is)."""
    services, labels = build_tier_services(host.cfg)
    generation_guard = GenerationGuard()
    strategy_type = build_strategy_type(host.breaker, labels, host.cost_cap, generation_guard, host.clock)
    llm_switcher = LLMSwitcher(services, strategy_type=strategy_type)
    register_all(llm_switcher, host.handlers)

    # Cascade observability (Bug 3): the strategy exposes on_retry/on_tail_tier/on_all_failed
    # so this module owns the journal wiring without strategy.py depending on the journal.
    strategy = llm_switcher.strategy

    @strategy.event_handler("on_retry")
    async def _journal_retry(_strategy, next_idx):
        if host.journal.current is not None:
            host.journal.current.retry = True

    @strategy.event_handler("on_tail_tier")
    async def _journal_tail_tier(_strategy):
        host.journal.alert(AlertKind.TAIL_TIER_ENTRY)

    @strategy.event_handler("on_all_failed")
    async def _journal_all_failed(_strategy, reason=None):
        host.journal.alert(
            AlertKind.COST_CAP if reason == "cost_cap" else AlertKind.ALL_TIERS_FAILED,
            {"reason": reason},
        )

    stt = DeepgramFluxSTTService(api_key=host.cfg.deepgram_api_key or "", model="flux-general-multi")

    @stt.event_handler("on_end_of_turn")
    async def _on_end_of_turn(service, transcript: str) -> None:
        # R3: every user turn must reach confirm_flow.note_user_turn() before the LLM runs.
        host.confirm_flow.note_user_turn(transcript, host.clock.now())

    context = LLMContext(tools=ALL_SCHEMAS)
    # S1: replicate LLMContextAggregatorPair's own __init__ recipe (user first, then the
    # assistant with a back-reference to it) so the assistant half can be the guarded
    # subclass from make_guarded_assistant_aggregator -- LLMContextAggregatorPair itself
    # always builds a plain LLMAssistantAggregator internally with no hook to substitute a
    # subclass.
    user_aggregator = LLMUserAggregator(context)
    GuardedAssistantAggregator = make_guarded_assistant_aggregator(LLMAssistantAggregator, generation_guard)
    assistant_aggregator = GuardedAssistantAggregator(context, _paired_user_aggregator=user_aggregator)

    # Two GenerationStartHooks, not one, around llm_switcher (research §2.2): a user turn's
    # LLMContextFrame travels DOWNSTREAM out of user_aggregator, but a tool-call's
    # re-inference travels UPSTREAM out of assistant_aggregator instead -- LLM services
    # terminate LLMContextFrame rather than re-pushing it, so a switcher itself never relays
    # one either way. A DOWNSTREAM-only hook would miss every tool-loop turn, leaving
    # `current_generation` stale for it and letting a stale scrub cut off already-committed
    # tool-messages on the next error.
    pre_hook = GenerationStartHook(generation_guard, FrameDirection.DOWNSTREAM)
    post_hook = GenerationStartHook(generation_guard, FrameDirection.UPSTREAM)

    tts = FishAudioTTSService(
        api_key=host.cfg.fish_audio_api_key or "",
        settings=FishAudioTTSService.Settings(model=host.cfg.fish_tts_model, voice=host.cfg.fish_reference_id),
    )

    arbiter = TTSArbiterProcessor(host.arbiter_policy)

    # assistant_aggregator MUST sit AFTER tts, not before it: LLMAssistantAggregator.process_frame
    # terminates TextFrame/LLMTextFrame/LLMFullResponse* (it consumes them to build context and
    # does NOT push them onward), so placed upstream of TTS it swallows the LLM's spoken text
    # before arbiter/tts ever see it -> run_tts never fires -> silence. pipecat still forwards
    # what the aggregator needs from downstream of TTS: TTSTextFrame (a TextFrame subclass) via
    # push_text_frames (default True) rebuilds context from the spoken words, and
    # LLMFullResponseEndFrame (also forwarded by default) still fires the commit/tool-loop
    # re-inference trigger. This is pipecat's own canonical order (ref E6/E7 in run file).
    pipeline = Pipeline(
        [
            stt,
            user_aggregator,
            pre_hook,
            llm_switcher,
            post_hook,
            arbiter,
            tts,
            assistant_aggregator,
        ]
    )

    return SynapseSession(pipeline=pipeline, llm_switcher=llm_switcher, generation_guard=generation_guard)


def run() -> None:
    """`python -m synapse.pipeline.app` — boots the WebRTC demo server. The agent is now served
    over pipecat SmallWebRTCTransport (see synapse.pipeline.webrtc_server), so the browser's own
    WebRTC stack does acoustic echo cancellation and the old LocalAudioTransport (raw PortAudio,
    no AEC, echo-loop) is gone. uvicorn/webrtc deps are lazy-imported here (S4) so importing this
    module stays free of the `voice` extra."""
    import uvicorn
    from dotenv import load_dotenv

    from synapse.pipeline.webrtc_server import build_web_app

    load_dotenv()
    cfg = SynapseConfig.from_env()
    host = build_host(cfg)
    app = build_web_app(host)
    uvicorn.run(app, host="localhost", port=7860)


if __name__ == "__main__":
    run()
