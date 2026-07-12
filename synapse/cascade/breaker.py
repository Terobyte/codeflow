"""CircuitBreaker — pure, Clock-driven (Р-14). No I/O, no pipecat imports: a quota'd tier
mutes itself out of rotation instead of eating a 429+retry RTT on every subsequent turn.

Window typing: RPM/TIMEOUT/ERROR mute for `rpm_mute_s` (or the provider's own retry-after);
RPD mutes until the next `rpd_reset_hour_utc` UTC, rolling to tomorrow if that hour already
passed today; AUTH (401/403) and a tripped CostCap both hard-mute until a manual
`reset_tier()` (R5/R9) — there is no automatic recovery from a bad key or an exhausted cost
cap.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum


class ErrorKind(str, Enum):
    RPM = "RPM"
    RPD = "RPD"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"
    AUTH = "AUTH"
    # B33: a non-rate-limit client error (400/404/413 — bad/oversized request, not a tier-health
    # signal). It must NOT mute an otherwise-healthy tier; the strategy fails the turn instead.
    CLIENT = "CLIENT"


class CircuitBreaker:
    def __init__(self, tier_count: int, rpm_mute_s: float, rpd_reset_hour_utc: int) -> None:
        self._tier_count = tier_count
        self._rpm_mute_s = rpm_mute_s
        self._rpd_reset_hour_utc = rpd_reset_hour_utc
        self._muted_until: dict[int, float] = {}
        self._hard_muted: set[int] = set()

    def mute(self, tier_idx: int, kind: ErrorKind, now: float, retry_after_s: float | None = None) -> None:
        if kind == ErrorKind.CLIENT:
            return  # B33: a client-side bad request is not a tier-health signal — never mute.
        if kind == ErrorKind.AUTH:
            self.hard_mute(tier_idx)
            return
        if kind == ErrorKind.RPD:
            until = self._next_rpd_reset(now)
        else:  # RPM, TIMEOUT, ERROR
            # B32: a Retry-After of 0 (or negative) would set mute_until == now → the tier is
            # immediately re-selectable → a failover-to-self livelock on the dead tier that drains
            # the cost cap. Floor a non-positive retry-after to the default window.
            effective = retry_after_s if (retry_after_s is not None and retry_after_s > 0) else self._rpm_mute_s
            until = now + effective
        current = self._muted_until.get(tier_idx)
        if current is None or until > current:
            self._muted_until[tier_idx] = until

    def hard_mute(self, tier_idx: int) -> None:
        """Permanent mute until manual reset_tier() — AUTH failures and CostCap trips (R5/R9)."""
        self._hard_muted.add(tier_idx)

    def reset_tier(self, tier_idx: int) -> None:
        self._muted_until.pop(tier_idx, None)
        self._hard_muted.discard(tier_idx)

    def is_muted(self, tier_idx: int, now: float) -> bool:
        if tier_idx in self._hard_muted:
            return True
        until = self._muted_until.get(tier_idx)
        return until is not None and now < until

    def first_available(self, now: float) -> int | None:
        for idx in range(self._tier_count):
            if not self.is_muted(idx, now):
                return idx
        return None

    def mask(self, now: float) -> dict[int, float | str | None]:
        """{tier: muted_until|"AUTH"|None} — JSON-safe (no infinities) for the journal."""
        result: dict[int, float | str | None] = {}
        for idx in range(self._tier_count):
            if idx in self._hard_muted:
                result[idx] = "AUTH"
            elif self.is_muted(idx, now):
                result[idx] = self._muted_until[idx]
            else:
                result[idx] = None
        return result

    def _next_rpd_reset(self, now: float) -> float:
        current = datetime.fromtimestamp(now, tz=timezone.utc)
        reset_today = current.replace(hour=self._rpd_reset_hour_utc, minute=0, second=0, microsecond=0)
        if current >= reset_today:
            reset_today += timedelta(days=1)
        return reset_today.timestamp()
