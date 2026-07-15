# -*- coding: utf-8-sig -*-
"""Red tests proving bugs B50, B51, B56, B57, B58 from docs/bugs.md (hunt 2026-07-14,
routes round). One test per bug ID, driving the REAL `/api/*` routes via
`starlette.testclient.TestClient` against `build_web_app(host)`
(`synapse/pipeline/webrtc_server.py`) -- same pattern as
`tests/test_hunt0714_a.py::test_B10_malformed_json_post_returns_400_not_500` and
`tests/test_webrtc_server.py`.

Every test asserts the CORRECT (documented) post-fix behavior, so each is expected to
FAIL against the current (unfixed) tree at its OWN assertion and flip green once the
corresponding bug is fixed, with no change to the assertion itself. `strict=True` xfail
keeps the normal suite green (xfailed) while `--runxfail` shows the raw red.

Touches no production code. Hosts here are either a real `ThreadStore` (B56/B57/B58,
lightweight -- no need for the full `SynapseHost`/Kora machinery) or a minimal stub host
object recording what it receives (B51, per the brief's suggested route-level proof).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("aiortc")
pytest.importorskip("cv2")
pytest.importorskip("fastapi")

from synapse.clock import FakeClock
from synapse.threads import ThreadStore

try:
    from synapse.pipeline.webrtc_server import build_web_app
except (ImportError, RuntimeError) as e:  # prebuilt frontend raises RuntimeError on missing dist
    pytest.skip(f"webrtc deps/prebuilt UI unavailable: {e}", allow_module_level=True)

from starlette.testclient import TestClient

# CSRF-satisfying headers shared by every mutating POST (matches test_hunt0714_a.py's
# _csrf_ok pattern: JSON content-type + Origin whose netloc matches TestClient's default
# Host "testserver").
_CSRF_HEADERS = {"content-type": "application/json", "origin": "http://testserver"}


def _threads_host(tmp_path):
    """Minimal host stub carrying a REAL ThreadStore -- enough for the /api/threads*
    routes under test, without needing the full SynapseHost/Kora wiring."""
    host = MagicMock()
    host.threads = ThreadStore(FakeClock(), str(tmp_path / "threads"))
    return host


# ---------------------------------------------------------------------------------------
# B50 -- GET /api/browse?path=%00 (embedded null byte) 500s: `_browse_dir`
# (webrtc_server.py:44-47) only catches OSError/RuntimeError from `Path.resolve()`, not the
# `ValueError: embedded null character` a null byte triggers. Prove: the request returns a
# sane status (fallback-to-home 200, or a diagnosable 400), never an unhandled 500.
# ---------------------------------------------------------------------------------------


def test_B50_browse_null_byte_path_must_not_500():
    host = MagicMock()
    app = build_web_app(host)
    # raise_server_exceptions=False: observe the real HTTP status the server would send a
    # client instead of pytest re-raising the escaped ValueError as a Python exception.
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/api/browse?path=%00")

    assert resp.status_code in (200, 400), (
        "B50: a null-byte path must fall back to home (200) or 400, not crash -- "
        f"got status={resp.status_code!r} body={resp.text!r}"
    )


# ---------------------------------------------------------------------------------------
# B51 -- confirm/fast coerced via `bool(data.get(...))` (webrtc_server.py:663-664);
# `bool("false") == True`, so a JSON body `{"confirm": "false"}` is treated as confirmed.
# Prove via a stub host that records the `confirm` kwarg `gate_action` actually receives:
# it must be False for a string "false", not True.
# ---------------------------------------------------------------------------------------


def test_B51_string_false_confirm_must_not_be_treated_as_confirmed():
    captured: dict = {}

    class _DummyThread:
        """Just enough shape for the route's post-gate `_thread_dict(thread)` snapshot."""

        id = "th1"
        title = "t"
        project_id = None
        stage = "propose"
        last_outcome = None
        request_text = "do the thing"
        last_model = None
        archived = False
        updated_ts = 0.0
        created_ts = 0.0

    class _StubHost:
        def __init__(self) -> None:
            self.threads = MagicMock()
            self.threads.get.return_value = _DummyThread()  # non-None -> route proceeds

        async def gate_action(self, thread_id, action, model=None, confirm=False, fast=False,
                              user_initiated=True):
            captured["confirm"] = confirm
            captured["fast"] = fast
            return {"ok": True, "stage": "spec_plan"}

    host = _StubHost()
    app = build_web_app(host)
    client = TestClient(app, raise_server_exceptions=False)

    client.post(
        "/api/threads/th1/gate",
        json={"action": "send_to_kora", "confirm": "false"},
        headers=_CSRF_HEADERS,
    )

    assert captured.get("confirm") is False, (
        "B51: POST .../gate with JSON confirm='false' must reach gate_action as "
        f"confirm=False, not bool('false')=True -- captured={captured!r}"
    )


# ---------------------------------------------------------------------------------------
# B56 -- GET /api/threads?archived=false is treated truthy (webrtc_server.py:511
# `if archived and archived != "0"`), so it returns the ARCHIVED-only (here empty) list
# instead of the real non-archived list. Prove: with one non-archived thread present,
# `?archived=false` must return that thread.
# ---------------------------------------------------------------------------------------


def test_B56_archived_false_string_must_return_non_archived_threads(tmp_path):
    host = _threads_host(tmp_path)
    t = host.threads.create("some thread")
    assert t.archived is False  # sanity: freshly created threads are not archived

    app = build_web_app(host)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/api/threads?archived=false")

    assert resp.status_code == 200
    ids = [row["id"] for row in resp.json()["threads"]]
    assert ids == [t.id], (
        "B56: ?archived=false must behave like the false-y it names, returning the "
        f"real non-archived thread list -- got threads={resp.json()['threads']!r} "
        "(bug: `if archived and archived != \"0\"` treats the string 'false' as truthy, "
        "so this returns archived-only == [])"
    )


# ---------------------------------------------------------------------------------------
# B57 -- GET /api/threads/{id}/feed?limit=0 returns ALL entries because
# `splitlines()[-0:] == [0:]` (threads.py:266). Prove: limit=0 must return zero entries.
# ---------------------------------------------------------------------------------------


def test_B57_feed_limit_zero_must_return_zero_entries(tmp_path):
    host = _threads_host(tmp_path)
    t = host.threads.create("some thread")
    for i in range(3):
        host.threads.append_feed(t.id, {"ts": float(i), "kind": "event", "text": f"e{i}"})

    app = build_web_app(host)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get(f"/api/threads/{t.id}/feed?limit=0")

    assert resp.status_code == 200
    assert resp.json()["entries"] == [], (
        "B57: limit=0 must return zero feed entries -- got "
        f"{resp.json()['entries']!r} (bug: `lines[-0:]` == `lines[0:]` == the whole list, "
        "since Python's `-0 == 0`)"
    )


# ---------------------------------------------------------------------------------------
# B58 -- POST /api/active-thread {"id": ""} 404s instead of clearing the active thread:
# the guard at webrtc_server.py:685 only excludes `None` (`tid is not None`), so an
# empty-string id is looked up as a real thread id and fails the existence check. Prove:
# an empty-string id must clear voice_thread and return ok, not 404.
# ---------------------------------------------------------------------------------------


def test_B58_active_thread_empty_string_id_must_clear(tmp_path):
    host = _threads_host(tmp_path)
    t = host.threads.create("some thread")
    host.voice_thread = {"id": t.id}
    host.voice_project = {"id": None}
    host.projects = MagicMock()

    app = build_web_app(host)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/api/active-thread", json={"id": ""}, headers=_CSRF_HEADERS)

    assert resp.status_code == 200, (
        "B58: an empty-string id must clear the active thread (200 ok), not 404 -- "
        f"got status={resp.status_code!r} body={resp.text!r} "
        "(bug: `tid is not None` guard admits '' as a real id to look up)"
    )
    assert host.voice_thread["id"] is None, (
        "B58: empty-string id must clear voice_thread -- "
        f"got voice_thread={host.voice_thread!r}"
    )
