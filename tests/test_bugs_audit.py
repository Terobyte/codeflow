"""Regression tests for bugs.md (Hunt 2026-07-13, B-UX namespace).

Static source audits: each test asserts the fix pattern is present and fails on reported bugs.
"""
from __future__ import annotations

import re
from pathlib import Path

CLIENT_DIR = Path(__file__).parent.parent / "synapse" / "pipeline" / "client"
STATIC_DIR = Path(__file__).parent.parent / "synapse" / "pipeline" / "static"
ROOT_DIR = Path(__file__).parent.parent


def read_client_file(name: str) -> str:
    return (CLIENT_DIR / name).read_text(encoding="utf-8")


def read_static_file(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def read_repo_file(rel_path: str) -> str:
    return (ROOT_DIR / rel_path).read_text(encoding="utf-8")


def extract_block_after_pattern(code: str, pattern: str) -> str:
    match = re.search(pattern, code)
    if not match:
        return ""
    start_idx = code.find("{", match.end())
    if start_idx == -1:
        return ""
    brace_count = 1
    i = start_idx + 1
    while i < len(code) and brace_count > 0:
        if code[i] == "{":
            brace_count += 1
        elif code[i] == "}":
            brace_count -= 1
        i += 1
    return code[start_idx:i]


def _index_before(haystack: str, needle: str, before: str) -> bool:
    """True if needle appears before before in haystack (both must exist)."""
    ni = haystack.find(needle)
    bi = haystack.find(before)
    return ni != -1 and bi != -1 and ni < bi


# ============================================================================================
# B-UX Tests (bugs.md Hunt 2026-07-13)
# ============================================================================================

def test_b_ux_1_probe_session_connecting_before_client_null():
    """B-UX-1: watchdog reconnect must set connecting=true before client=null/await disconnect."""
    body = extract_block_after_pattern(read_client_file("app.js"), r"async function probeSession")
    assert body, "Could not extract probeSession"
    reconnect = body.split("aliveMisses = 0")[-1] if "aliveMisses = 0" in body else body
    assert _index_before(reconnect, "connecting = true", "client = null"), (
        "B-UX-1: probeSession nulls client before connecting=true — race with mic button"
    )


def test_b_ux_2_enter_respects_send_guard():
    """B-UX-2: Enter keydown must not bypass msg-send.disabled / in-flight send guard."""
    js = read_client_file("app.js")
    send_body = extract_block_after_pattern(js, r"async function sendMessage")
    key_body = extract_block_after_pattern(js, r'\$\("msg-input"\)\.addEventListener\("keydown"')
    assert send_body and key_body, "Could not extract sendMessage or keydown handler"
    key_guard = (
        "disabled" in key_body
        or "sending" in key_body
        or "sendInFlight" in key_body
        or "msg-send" in key_body and "return" in key_body
    )
    send_guard = (
        "sending" in send_body
        or "sendInFlight" in send_body
        or re.search(r"if\s*\([^)]*disabled", send_body)
    )
    assert key_guard or send_guard, (
        "B-UX-2: Enter calls sendMessage() with no disabled/in-flight guard — double submit"
    )


def test_b_ux_3_gate_card_renders_structured_content():
    """B-UX-3: gate_card feed entries must not fall through to bare '· ' fallback."""
    app_js = read_client_file("app.js")
    app_py = read_repo_file("synapse/pipeline/app.py")
    add_body = extract_block_after_pattern(app_js, r"function addEntry")
    client_ok = (
        '"gate_card"' in add_body
        or "gate_card" in app_js.split("KIND_ICONS")[1][:200]
        or re.search(r'e\.kind\s*===\s*["\']gate_card["\']', add_body)
    )
    server_ok = re.search(
        r'kind["\']:\s*["\']gate_card["\'][^}]*["\']text["\']\s*:',
        app_py,
        re.DOTALL,
    )
    assert client_ok or server_ok, (
        "B-UX-3: gate_card has no addEntry branch / KIND_ICONS entry and server emits no text"
    )


def test_b_ux_4_feed_key_distinguishes_parallel_tool_results():
    """B-UX-4: feedKey must not collapse distinct tool_result entries sharing ts/kind/text."""
    app_js = read_client_file("app.js")
    kora_py = read_repo_file("synapse/bridge/kora.py")
    feed_key_body = extract_block_after_pattern(app_js, r"function feedKey")
    client_ok = (
        "e.id" in feed_key_body
        or "entry.id" in feed_key_body
        or "block_id" in feed_key_body
        or "tool_use_id" in feed_key_body
        or "index" in feed_key_body
        or "seq" in feed_key_body
    )
    # Only the log-entry mapper counts — tool_use_id elsewhere (e.g. pretool hook) is unrelated.
    mapper = extract_block_after_pattern(kora_py, r"def _message_to_log_entries")
    tool_result_line = ""
    for line in mapper.splitlines():
        if 'add("tool_result"' in line or "add('tool_result'" in line:
            tool_result_line = line
            break
    server_ok = bool(tool_result_line) and not re.search(
        r'add\(["\']tool_result["\'],\s*["\'](?:ок|ошибка)["\']\s*\)',
        tool_result_line,
    )
    assert client_ok or server_ok, (
        "B-UX-4: feedKey is ts|kind|text only — parallel tool results with same coarse text collapse"
    )


def test_b_ux_5_load_lists_has_inflight_guard():
    """B-UX-5: loadLists needs in-flight / sequence guard like pollFeed and browse."""
    body = extract_block_after_pattern(read_client_file("app.js"), r"async function loadLists")
    assert body, "Could not extract loadLists"
    assert (
        "listsInFlight" in body
        or "loadInFlight" in body
        or "latestLists" in body
        or "listsSeq" in body
        or "listsToken" in body
    ), "B-UX-5: loadLists has no in-flight guard — stale poll can stomp fresher data"


def test_b_ux_6_route_handles_malformed_hash():
    """B-UX-6: route() must not throw URIError on invalid percent-escapes in hash."""
    body = extract_block_after_pattern(read_client_file("app.js"), r"function route\b")
    assert body, "Could not extract route()"
    assert "try" in body and "decodeURIComponent" in body, (
        "B-UX-6: decodeURIComponent in route() is uncaught — malformed hash crashes render loop"
    )


def test_b_ux_7_picker_rows_keyboard_accessible():
    """B-UX-7: picker folder rows must be focusable and activatable from keyboard."""
    browse_body = extract_block_after_pattern(read_client_file("app.js"), r"async function browse")
    assert browse_body, "Could not extract browse()"
    assert (
        "tabIndex" in browse_body
        or "tabindex" in browse_body
        or 'role="button"' in browse_body
        or "role = \"button\"" in browse_body
        or "keydown" in browse_body
    ), "B-UX-7: picker <li> rows are click-only — unreachable by keyboard/AT"


def test_b_ux_8_picker_dialog_focus_management():
    """B-UX-8: aria-modal picker must trap/move/restore focus on open/close."""
    js = read_client_file("app.js")
    open_body = extract_block_after_pattern(js, r"function openPicker")
    close_body = extract_block_after_pattern(js, r"function closePicker")
    assert open_body and close_body, "Could not extract openPicker/closePicker"
    assert (
        ".focus(" in open_body
        or "trapFocus" in open_body
        or "focusTrap" in open_body
        or "inert" in open_body
        or "aria-hidden" in open_body
    ), "B-UX-8: openPicker does not move/trap focus despite aria-modal=true"
    assert (
        "previousFocus" in close_body
        or ".focus(" in close_body
        or "releaseFocus" in close_body
    ), "B-UX-8: closePicker does not restore focus after dialog closes"
    assert "trapFocus" in js, "B-UX-8: picker moves focus but does not trap Tab inside the dialog"
    assert '$("shell").inert = true' in open_body, (
        "B-UX-8: aria-modal picker does not isolate background content"
    )


def test_b_ux_9_status_widget_keyboard_accessible():
    """B-UX-9: status-widget dot must be focusable and keyboard-activatable."""
    body = read_static_file("status-widget.js")
    dot_section = body[body.find("createElement"):body.find("document.body.appendChild")]
    assert (
        "tabIndex" in dot_section
        or "tabindex" in dot_section
        or 'role="button"' in dot_section
        or 'role="link"' in dot_section
        or "keydown" in dot_section
    ), "B-UX-9: status-widget dot is mouse-only <div> — no keyboard affordance"


def test_b_ux_10_drawer_focus_trap_and_inert():
    """B-UX-10: closed drawer must leave tab order; open drawer must trap focus."""
    js = read_client_file("app.js")
    open_body = extract_block_after_pattern(js, r"function openDrawer")
    close_body = extract_block_after_pattern(js, r"function closeDrawer")
    sync_body = extract_block_after_pattern(js, r"function syncDrawerA11y") or ""
    css = read_client_file("style.css")
    assert open_body and close_body, "Could not extract drawer open/close"
    js_ok = (
        "inert" in open_body
        or "inert" in close_body
        or "inert" in sync_body
        or "aria-hidden" in open_body
        or "aria-hidden" in close_body
        or "aria-hidden" in sync_body
    )
    css_ok = "inert" in css or "visibility: hidden" in css
    assert js_ok or css_ok, (
        "B-UX-10: drawer hidden only via transform — off-canvas controls stay in tab order"
    )
    assert '$("main").inert = open' in sync_body, (
        "B-UX-10: open drawer does not isolate the backdrop-covered main content"
    )
    assert "trapFocus" in js, "B-UX-10: open drawer does not trap Tab inside the sidebar"


# ============================================================================================
# B45, B52-B55 Tests (from docs/bugs.md Hunt 2026-07-14 hands-on browser hunt)
# ============================================================================================

def test_b45_rename_title_node_restored_first():
    """B45: commitRename must replace input with titleEl before any potential throw,
    and setViewTitle must guard against null node."""
    js = read_client_file("app.js")
    commit_body = extract_block_after_pattern(js, r"async function commitRename")
    set_title_body = extract_block_after_pattern(js, r"function setViewTitle")
    assert commit_body and set_title_body, "Could not extract commitRename or setViewTitle"

    # Verify commitRename does input.replaceWith(titleEl) early (before try or reading input value)
    assert _index_before(commit_body, "replaceWith(titleEl)", "try {"), (
        "B45: commitRename does not restore titleEl to DOM before try block"
    )
    # Verify setViewTitle guards against null $("view-title")
    assert "if (n)" in set_title_body or "if (!n)" in set_title_body or "n &&" in set_title_body, (
        "B45: setViewTitle does not guard against null view-title node"
    )


def test_b52_gate_card_re_enabled_on_409():
    """B52: gate-card buttons must be re-enabled on 409 busy response."""
    js = read_client_file("app.js")
    render_gate_body = extract_block_after_pattern(js, r"function renderGateCard")
    assert render_gate_body, "Could not extract renderGateCard"

    # We should have a check for 409 status that sets busy message but leaves consumed=false
    # so that buttons are re-enabled in finally.
    assert "409" in render_gate_body, "B52: renderGateCard has no status 409 check"
    assert "consumed" in render_gate_body and "consumed = false" in render_gate_body, (
        "B52: consumed flag is not properly managed"
    )


def test_b53_successful_gate_action_keeps_buttons_disabled():
    """B53: successful gate action sets consumed=true so buttons stay disabled and no duplicate submissions can happen."""
    js = read_client_file("app.js")
    render_gate_body = extract_block_after_pattern(js, r"function renderGateCard")
    assert render_gate_body, "Could not extract renderGateCard"

    assert "consumed = true" in render_gate_body, "B53: renderGateCard does not set consumed = true on success"
    assert "!consumed" in render_gate_body or "if (consumed)" in render_gate_body or "if (!consumed)" in render_gate_body, (
        "B53: finally block does not guard button re-enabling with consumed flag"
    )


def test_b54_archive_opened_thread_redirects_home():
    """B54: archiving the currently active thread must redirect to home (hash = '#/')."""
    js = read_client_file("app.js")
    archive_body = extract_block_after_pattern(js, r"async function archiveThread")
    assert archive_body, "Could not extract archiveThread"

    assert "location.hash = \"#/\"" in archive_body or "location.hash = '#/'" in archive_body, (
        "B54: archiveThread does not redirect to home when active thread is archived"
    )


def test_b55_garbage_thread_route_stops_polling_and_shows_error():
    """B55: a 404 thread route must stop polling feedNotFound and add not found entry."""
    js = read_client_file("app.js")
    poll_body = extract_block_after_pattern(js, r"async function pollFeed")
    assert poll_body, "Could not extract pollFeed"

    assert "feedNotFound" in poll_body, "B55: pollFeed does not check or set feedNotFound"
    assert "404" in poll_body, "B55: pollFeed does not handle 404 status"
    assert "thread not found" in poll_body or "not found or deleted" in poll_body, (
        "B55: pollFeed does not append not found event message"
    )
