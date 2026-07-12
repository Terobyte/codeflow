"""classify_error — turns a provider error response into a typed (ErrorKind, retry_after_s)
pair the CircuitBreaker understands. Pure function, no I/O (Р-14).

- Google 429: `error.details[]` may carry a `RetryInfo.retryDelay` (e.g. "34s") and/or a
  `QuotaFailure` whose violation metric names contain "PerDay" (→ RPD) or "PerMinute" (→ RPM).
- OpenRouter 429: only a `Retry-After` header — the quota class is not distinguishable from
  the response, so it defaults to RPM (an asymmetry vs. Google, documented in the design doc
  as a parking-lot item, not silently assumed away here).
- 401/403 → AUTH. No response (client-side timeout) or 5xx → TIMEOUT/ERROR.
"""
from __future__ import annotations

import re
from typing import Any

from synapse.cascade.breaker import ErrorKind

_RETRY_DELAY_RE = re.compile(r"(\d+(?:\.\d+)?)")


def classify_error(
    status: int | None,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[ErrorKind, float | None]:
    headers = headers or {}
    body = body or {}

    if status is None:
        return ErrorKind.TIMEOUT, None
    if status in (401, 403):
        return ErrorKind.AUTH, None
    if status == 429:
        kind, retry_after = _classify_google_429(body)
        if kind is not None:
            return kind, retry_after
        return ErrorKind.RPM, _retry_after_header(headers)
    if status >= 500:
        return ErrorKind.ERROR, None
    # B33: a non-rate-limit 4xx (400/404/413 — malformed/oversized request, e.g. context-window
    # exceeded) is a deterministic client error, not a tier-health signal. Muting the tier 60s
    # would blind a healthy tier (and failover to a tier that returns the same 4xx), so classify
    # it CLIENT: the breaker won't mute it and the strategy fails the turn instead of looping.
    if 400 <= status < 500:
        return ErrorKind.CLIENT, None
    return ErrorKind.ERROR, None


def _classify_google_429(body: dict[str, Any]) -> tuple[ErrorKind | None, float | None]:
    error = body.get("error")
    details = error.get("details", []) if isinstance(error, dict) else body.get("details", [])
    retry_after: float | None = None
    quota_kind: ErrorKind | None = None
    for detail in details or []:
        detail_type = detail.get("@type", "")
        if detail_type.endswith("RetryInfo"):
            delay = detail.get("retryDelay")
            if isinstance(delay, str):
                match = _RETRY_DELAY_RE.search(delay)
                if match:
                    retry_after = float(match.group(1))
        elif detail_type.endswith("QuotaFailure"):
            for violation in detail.get("violations", []):
                metric = "".join(str(v) for v in violation.values())
                if "PerDay" in metric:
                    quota_kind = ErrorKind.RPD
                elif "PerMinute" in metric:
                    quota_kind = ErrorKind.RPM
    if quota_kind is None:
        # RetryInfo present but no QuotaFailure to name the class: we still know the provider's
        # own delay -- keep it and default the class to RPM rather than discarding a valid
        # retryDelay via the header fallback (Bug 5).
        if retry_after is not None:
            return ErrorKind.RPM, retry_after
        return None, None
    return quota_kind, retry_after


def _retry_after_header(headers: dict[str, str]) -> float | None:
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return float(value)
            except ValueError:
                return None
    return None
