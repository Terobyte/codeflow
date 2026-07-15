# -*- coding: utf-8-sig -*-
"""С3 — ApprovalService: confirm=true от LLM перестаёт быть властью (Ф0.3).

Ядро спеки (из «Первого implementation slice» proposal-а):
- self-approval: gate_action(confirm=true) голосом без intervening turn → confirm_required;
- happy: readback → юзер «да» → повторный gate_action → запуск, approval одноразов;
- deny/unclear → confirm_required;
- invalidation: смена request_text между stage и consume → confirm_required;
- смена СТАДИИ между stage и consume → confirm_required (digest несёт stage);
- архивный тред с pending approval → {"error": "archived"} до consume;
- TTL истёк → confirm_required;
- HTTP-клик путь (user_initiated=True) без ApprovalService;
- замороженные: ConfirmFlow и стадийный гейт UI-4 не меняются.
"""
import pytest

from synapse.bridge.approvals import ApprovalService, gate_digest
from synapse.clock import FakeClock


_AFFIRM = frozenset({"да", "подтверждаю", "делай"})
_DENY = frozenset({"нет", "отмена", "стоп"})


def _service(ttl=30.0):
    return ApprovalService(FakeClock(0.0), ttl, _AFFIRM, _DENY)


def _digest(request_text="сделай X", action="send_to_kora", model=None, fast=False, stage="propose"):
    return gate_digest(request_text, action, model, fast, stage)


# --- unit: ApprovalService контракт --------------------------------------------------------

def test_consume_without_prior_stage_returns_none():
    """self-approval: consume без stage → None (нет pending)."""
    svc = _service()
    assert svc.consume("th", "send_to_kora", _digest(), now=1.0) is None


def test_consume_without_intervening_user_turn_returns_none():
    """С3 ядро: stage без последующего user turn → consume None (self-approval блокирован)."""
    svc = _service()
    svc.stage("th", "send_to_kora", _digest(), now=1.0)
    # НЕТ note_user_turn — пользователь не отвечал
    assert svc.consume("th", "send_to_kora", _digest(), now=2.0) is None


def test_happy_path_affirm_consumes_once():
    """readback → юзер «да» → consume отдаёт approval, повторный consume → None (одноразов)."""
    svc = _service()
    svc.stage("th", "send_to_kora", _digest(), now=1.0)
    svc.note_user_turn("th", "да, делай", now=2.0)
    approval = svc.consume("th", "send_to_kora", _digest(), now=3.0)
    assert approval is not None
    assert approval.action == "send_to_kora"
    # одноразовость: повторный consume — None
    assert svc.consume("th", "send_to_kora", _digest(), now=4.0) is None


def test_deny_does_not_consume():
    """«нет» → consume None."""
    svc = _service()
    svc.stage("th", "send_to_kora", _digest(), now=1.0)
    svc.note_user_turn("th", "нет, отмена", now=2.0)
    assert svc.consume("th", "send_to_kora", _digest(), now=3.0) is None


def test_unclear_does_not_consume_and_keeps_pending():
    """unclear → consume None, pending НЕ гасится (можно повторный readback)."""
    svc = _service()
    svc.stage("th", "send_to_kora", _digest(), now=1.0)
    svc.note_user_turn("th", "хмм непонятно", now=2.0)
    assert svc.consume("th", "send_to_kora", _digest(), now=3.0) is None
    # pending жив — после affirm можно consume
    svc.note_user_turn("th", "да", now=4.0)
    assert svc.consume("th", "send_to_kora", _digest(), now=5.0) is not None


def test_digest_change_request_text_invalidates():
    """invalidation: смена request_text между stage и consume → digest не совпал → None."""
    svc = _service()
    svc.stage("th", "send_to_kora", _digest(request_text="запрос А"), now=1.0)
    svc.note_user_turn("th", "да", now=2.0)
    assert svc.consume("th", "send_to_kora", _digest(request_text="запрос Б"), now=3.0) is None


def test_digest_change_stage_invalidates():
    """С3 ключ: смена СТАДИИ между stage и consume → None. digest несёт stage."""
    svc = _service()
    svc.stage("th", "send_to_kora", _digest(stage="propose"), now=1.0)
    svc.note_user_turn("th", "да", now=2.0)
    assert svc.consume("th", "send_to_kora", _digest(stage="spec_plan"), now=3.0) is None


def test_ttl_expired_returns_none():
    """TTL истёк → consume None, pending гасится."""
    svc = _service(ttl=10.0)
    svc.stage("th", "send_to_kora", _digest(), now=1.0)
    svc.note_user_turn("th", "да", now=2.0)
    assert svc.consume("th", "send_to_kora", _digest(), now=100.0) is None


def test_invalidate_clears_pending():
    """явный invalidate (смена СВОДА в app.py) чистит pending сразу."""
    svc = _service()
    svc.stage("th", "send_to_kora", _digest(), now=1.0)
    svc.invalidate("th")
    svc.note_user_turn("th", "да", now=2.0)
    assert svc.consume("th", "send_to_kora", _digest(), now=3.0) is None


def test_note_user_turn_is_thread_scoped():
    """ответ из треда Б не подтверждает pending треда А."""
    svc = _service()
    svc.stage("thA", "send_to_kora", _digest(), now=1.0)
    svc.note_user_turn("thB", "да", now=2.0)
    assert svc.consume("thA", "send_to_kora", _digest(), now=3.0) is None


# --- интеграция: gate_action голос vs HTTP ------------------------------------------------

def _gate_host(tmp_path):
    from synapse.config import SynapseConfig
    from synapse.pipeline.app import build_host

    cfg = SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path), kora_workspace_dir=str(tmp_path / "ws"),
        confirm_timeout_s=30.0,
    )
    host = build_host(cfg)

    class _FakeRunner:
        def __init__(self): self.starts = []
        def start(self, task_id, text, spec): self.starts.append((task_id, text, spec))
    host.kora_runner = _FakeRunner()
    return host


def _propose(host):
    t = host.threads.create("x")
    host.threads.set_stage(t.id, "propose")
    host.threads.set_request(t.id, "сделай штуку")
    return t


@pytest.mark.asyncio
async def test_voice_self_approval_blocked(tmp_path):
    """С3 DoD: gate_action(confirm=True) голосом (user_initiated=False) без intervening turn →
    confirm_required. Раньше confirm=true запускал сразу (self-approval)."""
    host = _gate_host(tmp_path)
    t = _propose(host)
    res = await host.gate_action(t.id, "send_to_kora", confirm=True, user_initiated=False)
    assert res.get("error") == "confirm_required"
    assert "readback" in res
    assert len(host.kora_runner.starts) == 0


@pytest.mark.asyncio
async def test_voice_happy_path_approval_consumes_once(tmp_path):
    """readback → юзер «да» → повторный gate_action → запуск; approval одноразов."""
    host = _gate_host(tmp_path)
    t = _propose(host)
    # первый голосовой гейт → stage + confirm_required
    r1 = await host.gate_action(t.id, "send_to_kora", user_initiated=False)
    assert r1.get("error") == "confirm_required"
    # юзер подтвердил голосом
    host.approvals.note_user_turn(t.id, "да, делай", host.clock.now())
    r2 = await host.gate_action(t.id, "send_to_kora", user_initiated=False)
    assert r2.get("ok") is True
    assert len(host.kora_runner.starts) == 1
    # одноразовость approval: повторный голосовой гейт даёт BUSY (задача активна — busy-check
    # стоит ДО approval), а не повторный запуск. Одноразовость самого pending проверена на
    # чистом сервисе (test_happy_path_affirm_consumes_once).
    r3 = await host.gate_action(t.id, "send_to_kora", user_initiated=False)
    assert r3.get("error") == "busy"


@pytest.mark.asyncio
async def test_http_click_path_bypasses_approval_service(tmp_path):
    """С3: HTTP-клик (user_initiated=True) несёт подтверждение живого пользователя —
    ApprovalService не требуется, confirm=True запускает сразу."""
    host = _gate_host(tmp_path)
    t = _propose(host)
    res = await host.gate_action(t.id, "send_to_kora", confirm=True, user_initiated=True)
    assert res.get("ok") is True
    assert len(host.kora_runner.starts) == 1


@pytest.mark.asyncio
async def test_archived_thread_with_pending_approval_errors_before_consume(tmp_path):
    """С3: архивный тред с pending approval → {"error": "archived"} до consume, approval не
    потребляется (archived-гвард gate_action стоит ДО launch-секции)."""
    host = _gate_host(tmp_path)
    t = _propose(host)
    # stage pending approval
    await host.gate_action(t.id, "send_to_kora", user_initiated=False)
    host.approvals.note_user_turn(t.id, "да", host.clock.now())
    # архивируем тред
    host.threads.set_archived(t.id, True)
    res = await host.gate_action(t.id, "send_to_kora", user_initiated=False)
    assert res.get("error") == "archived"
    assert len(host.kora_runner.starts) == 0
