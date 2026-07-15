#!/usr/bin/env python3
"""Compare direct Gemini API with OpenRouter on identical text workloads.

The benchmark measures customer-visible availability and full-response latency.
It intentionally does not write prompt/response text, API keys, or HTTP bodies to
its reports.  The 10 tasks exercise small, structured, multilingual, and moderate
context requests; they are not a model-quality leaderboard.

Examples:
  python tools/bench_llm_providers.py
  python tools/bench_llm_providers.py --attempts 5 --retries 2 --timeout 20
  python tools/bench_llm_providers.py --openrouter-no-fallback
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

try:
    from dotenv import load_dotenv
except ImportError:  # Keep the utility usable in a bare Python environment.
    load_dotenv = None


@dataclass(frozen=True)
class Task:
    id: str
    category: str
    prompt: str
    validator: Callable[[str], bool]


def _nonempty(text: str) -> bool:
    return bool(text.strip())


def _contains(value: str) -> Callable[[str], bool]:
    return lambda text: value.lower() in text.lower()


def _json_object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except json.JSONDecodeError:
        return False


import re as _re


def _exact(value: str) -> Callable[[str], bool]:
    """Response text must be exactly `value` (stripped, case-insensitive)."""
    return lambda text: text.strip().lower() == value.lower()


def _only_number(value: str) -> Callable[[str], bool]:
    """Response must contain only the given number (stripped), no surrounding prose."""
    return lambda text: text.strip() == value


def _one_sentence_nonempty(text: str) -> bool:
    """Non-empty, single sentence (at most one sentence-ending punctuation mark)."""
    s = text.strip()
    if not s:
        return False
    # Count sentence-ending punctuation (.!?) — allow at most one.
    endings = len(_re.findall(r'[.!?]+', s))
    return endings <= 1


def _json_city_country(text: str) -> bool:
    """Valid JSON object with both 'city' and 'country' keys."""
    try:
        obj = json.loads(text.strip())
        return isinstance(obj, dict) and "city" in obj and "country" in obj
    except (json.JSONDecodeError, ValueError):
        return False


def _is_russian(text: str) -> bool:
    """Text must contain at least one Cyrillic character (i.e. translated to Russian)."""
    return bool(_re.search(r'[\u0400-\u04FF]', text))


def _one_word(text: str) -> bool:
    """Response must be exactly one word (stripped, no spaces)."""
    return len(text.strip().split()) == 1


def _code_under_20(text: str) -> bool:
    """Code expression under 20 characters (stripped of surrounding whitespace/backticks)."""
    s = text.strip().strip('`').strip()
    return 0 < len(s) < 20


def _hhmm_format(text: str) -> bool:
    """Response must match H:MM or HH:MM format exactly (stripped)."""
    return bool(_re.fullmatch(r'\d{1,2}:\d{2}', text.strip()))


def _three_comma_codes(text: str) -> bool:
    """Exactly three comma-separated numeric HTTP status codes."""
    parts = [p.strip() for p in text.strip().split(',')]
    return len(parts) == 3 and all(p.isdigit() and len(p) == 3 for p in parts)


TASKS: tuple[Task, ...] = (
    Task("t01", "tiny/exact", "Reply with exactly ACK-01 and nothing else.", _exact("ACK-01")),
    Task("t02", "arithmetic", "What is 17 * 23? Reply with only the number.", _only_number("391")),
    Task("t03", "russian", "Одним предложением объясни, что делает DNS.", _one_sentence_nonempty),
    Task("t04", "json", "Return only JSON: {\"city\":\"Paris\",\"country\":\"France\"}.", _json_city_country),
    Task("t05", "translation", "Translate into Russian: 'The build finished successfully.'", _is_russian),
    Task("t06", "classification", "Classify the sentiment of 'This is much better than yesterday' as one word: positive, neutral, or negative.", _one_word),
    Task("t07", "code", "Write a Python expression that returns the last item of a non-empty list. Keep it under 20 characters.", _code_under_20),
    Task("t08", "reasoning", "A train leaves at 09:15 and arrives at 11:42. How long was the trip? Reply as H:MM.", _hhmm_format),
    Task("t09", "instruction", "Give exactly three comma-separated HTTP status codes that normally indicate client errors.", _three_comma_codes),
    Task("t10", "context", "Context: alpha=red; beta=green; gamma=blue. Question: What is the value of beta? Reply with one word.", _one_word),
)


@dataclass
class Result:
    provider: str
    model: str
    task_id: str
    category: str
    attempt: int
    ok: bool
    contract_ok: bool
    status_code: int | None
    latency_ms: float
    retries_used: int
    recovered_by_retry: bool
    error_kind: str | None
    error_message: str | None
    output_chars: int


class Provider:
    name: str
    model: str

    def request(self, prompt: str, timeout_s: float) -> tuple[int, str]:
        raise NotImplementedError


class GeminiProvider(Provider):
    name = "gemini_direct"

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key, self.model = api_key, model

    def request(self, prompt: str, timeout_s: float) -> tuple[int, str]:
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 256},
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        response = _post_json(url, body, {"x-goog-api-key": self.api_key}, timeout_s)
        text = "".join(
            part.get("text", "")
            for candidate in response.get("candidates", [])
            for part in candidate.get("content", {}).get("parts", [])
            if isinstance(part, dict)
        )
        return 200, text


class OpenRouterProvider(Provider):
    name = "openrouter"

    def __init__(self, api_key: str, model: str, allow_fallbacks: bool) -> None:
        self.api_key, self.model, self.allow_fallbacks = api_key, model, allow_fallbacks

    def request(self, prompt: str, timeout_s: float) -> tuple[int, str]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 256,
        }
        if not self.allow_fallbacks:
            body["provider"] = {"allow_fallbacks": False}
        response = _post_json(
            "https://openrouter.ai/api/v1/chat/completions",
            body,
            {"Authorization": f"Bearer {self.api_key}"},
            timeout_s,
        )
        choices = response.get("choices", [])
        text = choices[0].get("message", {}).get("content", "") if choices else ""
        return 200, text if isinstance(text, str) else ""


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout_s: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.loads(response.read().decode())


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code <= 599
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


def _error_details(exc: BaseException) -> tuple[int | None, str, str]:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code, f"http_{exc.code}", "HTTP request failed"
    if isinstance(exc, urllib.error.URLError):
        return None, "network", "Network request failed"
    if isinstance(exc, TimeoutError):
        return None, "timeout", "Request timed out"
    if isinstance(exc, (ValueError, KeyError, TypeError, json.JSONDecodeError)):
        return None, "invalid_response", "Response could not be parsed"
    return None, type(exc).__name__.lower(), "Unexpected request failure"


def run_task(provider: Provider, task: Task, attempt: int, retries: int, timeout_s: float) -> Result:
    retries_used = 0
    had_failure = False
    overall_started = time.perf_counter()
    while True:
        try:
            status, text = provider.request(task.prompt, timeout_s)
            # Customer-visible latency includes an unsuccessful request, backoff, and recovery.
            latency_ms = (time.perf_counter() - overall_started) * 1000
            # P2: empty response text is not a transport success — the provider returned
            # HTTP 200 but produced no useful output. Mark as invalid_response.
            if not text or not text.strip():
                return Result(provider.name, provider.model, task.id, task.category, attempt, False,
                              False, status, latency_ms, retries_used, had_failure,
                              "invalid_response", "Empty response text", 0)
            contract_ok = task.validator(text)
            return Result(provider.name, provider.model, task.id, task.category, attempt, True,
                          contract_ok, status, latency_ms, retries_used, had_failure, None, None, len(text))
        except Exception as exc:  # Turn every provider failure into a report row.
            latency_ms = (time.perf_counter() - overall_started) * 1000
            status, kind, message = _error_details(exc)
            if retries_used < retries and _is_retryable(exc):
                had_failure = True
                retries_used += 1
                time.sleep(min(2 ** (retries_used - 1), 4))
                continue
            return Result(provider.name, provider.model, task.id, task.category, attempt, False,
                          False, status, latency_ms, retries_used, False, kind, message, 0)


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * quantile)
    return ordered[index]


def summarize(results: list[Result]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for provider in sorted({row.provider for row in results}):
        rows = [row for row in results if row.provider == provider]
        successes = [row for row in rows if row.ok]
        latencies = [row.latency_ms for row in successes]
        output[provider] = {
            "requests": len(rows),
            "transport_success_rate": _ratio(len(successes), len(rows)),
            "contract_success_rate": _ratio(sum(row.contract_ok for row in rows), len(rows)),
            "retry_recovery_rate": _ratio(sum(row.recovered_by_retry for row in rows), sum(row.retries_used > 0 for row in rows)),
            "requests_with_retry": sum(row.retries_used > 0 for row in rows),
            "median_latency_ms": _round(statistics.median(latencies)) if latencies else None,
            "p95_latency_ms": _round(_percentile(latencies, 0.95)),
            "max_latency_ms": _round(max(latencies)) if latencies else None,
            "error_kinds": _count_errors(rows),
        }
    return output


def _ratio(numerator: int, denominator: int) -> float | None:
    return _round(numerator / denominator) if denominator else None


def _round(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _count_errors(rows: list[Result]) -> dict[str, int]:
    kinds: dict[str, int] = {}
    for row in rows:
        if row.error_kind:
            kinds[row.error_kind] = kinds.get(row.error_kind, 0) + 1
    return kinds


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Gemini direct vs OpenRouter",
        "",
        f"UTC: {report['started_at_utc']}",
        f"Tasks: {report['config']['tasks']}; attempts per task: {report['config']['attempts']}; retries: {report['config']['retries']}",
        "",
        "| Provider | Model | Transport success | Contract success | Median | P95 | Retry recovery |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, data in report["summary"].items():
        model = report["models"][name]
        lines.append(
            f"| {name} | {model} | {_pct(data['transport_success_rate'])} | {_pct(data['contract_success_rate'])} | "
            f"{_ms(data['median_latency_ms'])} | {_ms(data['p95_latency_ms'])} | {_pct(data['retry_recovery_rate'])} |"
        )
    lines.extend([
        "",
        "Transport success = HTTP response parsed with non-empty generated text. Contract success additionally checks the task's simple expected form.",
        "Latency is end-to-end unary response latency; it is not time-to-first-token. Retry recovery is a success after at least one retry.",
        "",
        "## Errors",
        "",
    ])
    for name, data in report["summary"].items():
        lines.append(f"- {name}: {json.dumps(data['error_kinds'], ensure_ascii=False) if data['error_kinds'] else 'none'}")
    return "\n".join(lines) + "\n"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f} ms"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", type=int, default=3, help="runs per task and provider (default: 3)")
    parser.add_argument("--retries", type=int, default=1, help="retryable retries per run (default: 1)")
    parser.add_argument("--timeout", type=float, default=20.0, help="per HTTP request timeout, seconds (default: 20)")
    parser.add_argument("--out-dir", type=Path, default=Path("benchmarks"), help="directory for reports")
    parser.add_argument("--openrouter-model", default=os.getenv("OPENROUTER_MODEL", "google/gemini-3.5-flash"))
    parser.add_argument("--gemini-model", default=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"))
    parser.add_argument("--openrouter-no-fallback", action="store_true", help="disable OpenRouter provider fallbacks")
    return parser.parse_args()


def main() -> int:
    if load_dotenv:
        load_dotenv()
    args = parse_args()
    if args.attempts < 1 or args.retries < 0 or args.timeout <= 0:
        raise SystemExit("--attempts must be >= 1; --retries >= 0; --timeout > 0")
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    missing = [name for name, value in (("GOOGLE_API_KEY (or GEMINI_API_KEY)", gemini_key), ("OPENROUTER_API_KEY", openrouter_key)) if not value]
    if missing:
        raise SystemExit("Missing environment variable(s): " + ", ".join(missing))

    providers: tuple[Provider, ...] = (
        GeminiProvider(gemini_key, args.gemini_model),
        OpenRouterProvider(openrouter_key, args.openrouter_model, not args.openrouter_no_fallback),
    )
    started_at = datetime.now(UTC)
    results: list[Result] = []
    for attempt in range(1, args.attempts + 1):
        # Alternate endpoint order between rounds so warm connections / local load do not always
        # favour one path.
        ordered = providers if attempt % 2 else tuple(reversed(providers))
        for task in TASKS:
            for provider in ordered:
                result = run_task(provider, task, attempt, args.retries, args.timeout)
                results.append(result)
                print(f"{result.provider:13} {task.id} run={attempt} ok={result.ok} latency={result.latency_ms:.0f}ms", flush=True)

    report = {
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": datetime.now(UTC).isoformat(),
        "config": {"tasks": len(TASKS), "attempts": args.attempts, "retries": args.retries,
                   "timeout_s": args.timeout, "openrouter_fallbacks_allowed": not args.openrouter_no_fallback},
        "models": {provider.name: provider.model for provider in providers},
        "summary": summarize(results),
        "results": [asdict(result) for result in results],
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    json_path = args.out_dir / f"gemini-vs-openrouter-{stamp}.json"
    md_path = json_path.with_suffix(".md")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    md_path.write_text(markdown_report(report))
    print(f"\nReport: {json_path}\nSummary: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
