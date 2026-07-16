"""Staging smoke driver for Task 7 — drives :7861 via httpx.

Deterministic checks first; real-Kora checks gated behind --kora flag (spends credits).
Live voice (#9) is NOT here — needs a phone.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

BASE = "http://localhost:7861"
_TOKEN = os.environ.get("SYNAPSE_API_TOKEN", "")
if not _TOKEN:
    raise RuntimeError("SYNAPSE_API_TOKEN is required for the authenticated staging smoke")
H = {
    "content-type": "application/json",
    "origin": "http://localhost:7861",
    "authorization": f"Bearer {_TOKEN}",
}


def show(label: str, r: httpx.Response) -> dict:
    try:
        body = r.json()
    except Exception:
        body = r.text
    print(f"[{r.status_code}] {label}: {json.dumps(body, ensure_ascii=False)[:300]}")
    return body if isinstance(body, dict) else {}


def wait_run_done(client: httpx.Client, tid: str, max_s: float = 600.0, poll: float = 3.0) -> dict:
    """Poll thread detail until stage moved past the launched stage or run terminal."""
    deadline = time.time() + max_s
    last = {}
    while time.time() < deadline:
        r = client.get(f"{BASE}/api/threads/{tid}", timeout=10)
        last = r.json()
        kora = client.get(f"{BASE}/client/kora-status", timeout=10).json()
        ts = kora.get("task_status")
        if ts in ("completed", "failed") or kora.get("awaiting_answer"):
            return last
        time.sleep(poll)
    return last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kora", action="store_true", help="run real-Kora checks (#4-#6)")
    args = ap.parse_args()

    fails: list[str] = []
    client = httpx.Client(timeout=30, headers={"authorization": f"Bearer {_TOKEN}"})

    # health
    show("health /api/threads", client.get(f"{BASE}/api/threads"))

    # --- #1 create thread in collect ---
    r = client.post(f"{BASE}/api/threads", json={"title": "smoke"}, headers=H)
    t = show("#1 create thread", r)
    tid = t.get("id")
    assert tid, "no thread id"
    d = show("#1 thread detail", client.get(f"{BASE}/api/threads/{tid}"))
    if d.get("stage") != "collect":
        fails.append(f"#1 stage={d.get('stage')} != collect")

    # --- CSRF: gate without JSON content-type → 403 ---
    r = client.post(f"{BASE}/api/threads/{tid}/gate", data={"action": "revise"})
    show("#CSRF gate without json", r)
    if r.status_code != 403:
        fails.append(f"#CSRF status={r.status_code} != 403")

    # --- #3 propose_request path: drive via dispatcher? We test the host gate directly.
    #     collect→propose is a dispatcher tool (propose_request). The HTTP gate only handles
    #     send_to_kora/write_code/revise. So set request_text + stage via the dispatcher message
    #     route (real LLM) is heavy; instead we assert the stage transition logic by using the
    #     message route is LLM-driven. For deterministic coverage we go collect→propose via the
    #     voice tool is not HTTP-exposed. We verify propose indirectly: gate from collect with
    #     send_to_kora must be illegal (no request) until stage is propose.
    # first, send_to_kora from collect without request → illegal_stage (collect not in source)
    r = client.post(f"{BASE}/api/threads/{tid}/gate",
                    json={"action": "send_to_kora", "confirm": True}, headers=H)
    body = show("#3 send_to_kora from collect (illegal_stage)", r)
    if body.get("error") != "illegal_stage":
        fails.append(f"#3 collect→send_to_kora error={body.get('error')} != illegal_stage")

    # --- #2/#3 set stage to propose + request_text by simulating the dispatcher tool outcome.
    #     Since propose_request is a dispatcher tool (not an HTTP route), and the message route
    #     drives the real LLM, we use a tiny in-process helper: directly set via the message route
    #     is LLM-bound. Instead we skip to the documented HTTP surface and exercise it after
    #     placing the thread into propose with request_text via the test seam below.
    #     (These checks are about the gate_action host helper + docs_only + busy, which is the
    #      real Task 7 integration target — see in-process test_stages.py for FSM exhaustiveness.)

    # Put thread into propose+request_text via the dispatcher text loop is LLM-driven & costly;
    # the staging harness focuses on the KORA path (#4-#6) which needs the propose precondition.
    # We set the precondition through the public message route ONLY when --kora (it calls the LLM).
    if not args.kora:
        print("\n[deterministic checks complete; --kora not set — skipping real Kora #4-#6]")
        client.close()
        print("\nFAILS:" if fails else "\nALL DETERMINISTIC CHECKS PASSED")
        return 1 if fails else 0

    # ---- real Kora path (#4-#6) ----
    # Use the dispatcher message route to move collect→propose (LLM calls propose_request).
    # This is the genuine voice/text integration path and the only public way to reach propose.
    print("\n=== real Kora path: driving collect→propose via dispatcher message route ===")
    r = client.post(f"{BASE}/api/threads/{tid}/message",
                    json={"text": "Свод: добавь в файл notes.md строку 'hello from staging'. Это вся задача. Готово, отправляй."},
                    headers=H)
    show("#dispatcher message (collect→propose)", r)
    d = show("#post-message thread detail", client.get(f"{BASE}/api/threads/{tid}"))
    propose_ok = d.get("stage") == "propose" and bool(d.get("request_text"))
    if not propose_ok:
        fails.append(f"#propose stage={d.get('stage')} request={d.get('request_text')!r}")
        # cannot continue meaningfully
        client.close()
        print("\nFAILS:", *fails, sep="\n  ")
        return 1

    # --- #4 send_to_kora (non-fast) → spec_plan + docs_only run, writes plan file ---
    r = client.post(f"{BASE}/api/threads/{tid}/gate",
                    json={"action": "send_to_kora", "confirm": True}, headers=H)
    body = show("#4 send_to_kora → spec_plan", r)
    if body.get("stage") != "spec_plan":
        fails.append(f"#4 stage={body.get('stage')} != spec_plan")

    # --- #8 busy: a SECOND gate while the run is live → 409 ---
    r = client.post(f"{BASE}/api/threads/{tid}/gate",
                    json={"action": "send_to_kora", "confirm": True}, headers=H)
    show("#8 busy (second gate while run live)", r)
    if r.status_code != 409:
        fails.append(f"#8 status={r.status_code} != 409")

    print("\n[waiting for SPEC_PLAN run to complete (docs_only)...]")
    d = wait_run_done(client, tid, max_s=900, poll=4)
    show("#4 after run", client.get(f"{BASE}/api/threads/{tid}"))
    plan_path = "/Users/terobyte/synapse-kora-workspace/docs/plans/%s.md" % tid
    import os.path as _op
    print(f"[plan file {plan_path} exists={_op.exists(plan_path)}]")

    # --- #6 write_code → code stage + full gate ---
    r = client.post(f"{BASE}/api/threads/{tid}/gate",
                    json={"action": "write_code", "confirm": True}, headers=H)
    body = show("#6 write_code → code", r)
    if body.get("stage") != "code":
        fails.append(f"#6 stage={body.get('stage')} != code")
    print("\n[waiting for CODE run to complete (full gate)...]")
    d = wait_run_done(client, tid, max_s=1200, poll=4)
    final = show("#6 after code run", client.get(f"{BASE}/api/threads/{tid}"))
    if final.get("stage") != "done":
        fails.append(f"#done stage={final.get('stage')} != done")

    # --- #7 fast path without confirm → 400 (on a fresh thread) ---
    r2 = client.post(f"{BASE}/api/threads", json={"title": "fast"}, headers=H)
    t2 = show("#7 create second thread", r2)["id"]
    # need propose+request for fast path
    client.post(f"{BASE}/api/threads/{t2}/message",
                json={"text": "Свод: напиши 'x' в f.txt. Готово, сразу код."}, headers=H)
    r = client.post(f"{BASE}/api/threads/{t2}/gate",
                    json={"action": "send_to_kora", "fast": True, "confirm": False}, headers=H)
    show("#7 fast without confirm (confirm_required)", r)
    if r.status_code != 400:
        fails.append(f"#7 status={r.status_code} != 400")

    client.close()
    print("\nFAILS:" if fails else "\nALL CHECKS PASSED")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
