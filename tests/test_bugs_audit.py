"""Audit/regression tests for bugs from bugs.md.
These tests read the client source files and assert correct (fixed) patterns, failing on the current buggy code.
"""
from __future__ import annotations

import re
import json
from pathlib import Path

CLIENT_DIR = Path(__file__).parent.parent / "synapse" / "pipeline" / "client"
STATIC_DIR = Path(__file__).parent.parent / "synapse" / "pipeline" / "static"

# Helper to read files
def read_client_file(name: str) -> str:
    return (CLIENT_DIR / name).read_text(encoding="utf-8")

def read_static_file(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")

# Helper to extract brace-matched block after a pattern
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

# ============================================================================================
# B-CORE Tests (app.js + index.html)
# ============================================================================================

def test_b_core_1_input_cleared_after_success():
    body = read_client_file("app.js")
    fn_body = extract_block_after_pattern(body, r"async function sendMessage")
    assert fn_body, "Could not extract sendMessage function"
    
    # Assert that if input.value = "" is early, there is code in the catch/finally to restore it,
    # or input.value = "" is moved after postJSON/message.
    assert "input.value = text" in fn_body or "input.value = msg" in fn_body or fn_body.find("input.value = \"\"") > fn_body.find("postJSON"), (
        "B-CORE-1: input.value is cleared before send and not restored on failure."
    )

def test_b_core_2_fetch_helpers_have_timeout():
    body = read_client_file("app.js")
    for helper in ("getJSON", "postJSON"):
        helper_body = extract_block_after_pattern(body, r"function " + helper)
        assert helper_body, f"Could not extract {helper} definition"
        assert "AbortController" in helper_body or "withTimeout" in helper_body or "timeout" in helper_body, (
            f"B-CORE-2: helper {helper} does not implement fetch timeout / AbortController"
        )

def test_b_core_3_poll_feed_has_safe_pagination():
    body = read_client_file("app.js")
    fn_body = extract_block_after_pattern(body, r"async function pollFeed")
    assert fn_body, "Could not extract pollFeed function"
    
    # Assert that we don't have slice(feedCount) without id-based deduplication or cursor parameter
    assert "since" in fn_body or ("slice(feedCount)" not in fn_body) or ("Math.max(feedCount" in fn_body and "id" in fn_body), (
        "B-CORE-3: pollFeed uses slice(feedCount) which freezes when entries > 500."
    )

def test_b_core_4_add_project_error_handling():
    body = read_client_file("app.js")
    fn_body = extract_block_after_pattern(body, r'\$\("picker-choose"\)\.addEventListener\("click"')
    assert fn_body, "Could not extract picker-choose click listener"
    
    # Network failure res === null or res == null or !res (as isolated check, not part of !res.ok)
    assert re.search(r'res\s*===\s*null', fn_body) or re.search(r'res\s*==\s*null', fn_body) or re.search(r'\bif\s*\(\s*!res\s*\)', fn_body), (
        "B-CORE-4: picker-choose handles network error (res === null) as success, closing the picker."
    )

def test_b_core_5_voice_on_error_resets_client_and_ui():
    body = read_client_file("app.js")
    fn_body = extract_block_after_pattern(body, r"async function connectVoice")
    assert fn_body, "Could not extract connectVoice function"
    
    on_error_body = extract_block_after_pattern(fn_body, r"onError\s*:")
    assert on_error_body, "Could not extract onError callback"
    
    assert "client = null" in on_error_body and "setMicState" in on_error_body, (
        "B-CORE-5: onError does not reset client or call setMicState('error')."
    )

def test_b_core_6_modals_have_focus_trap_and_escape():
    html = read_client_file("index.html")
    js = read_client_file("app.js")
    assert 'role="dialog"' in html or 'aria-modal="true"' in html, (
        "B-CORE-6: Modal elements in index.html missing role='dialog' or aria-modal='true'"
    )
    assert "Escape" in js or "keydown" in js and "27" in js, (
        "B-CORE-6: Keydown handler for Escape key missing in app.js"
    )
    assert "overflow = \"hidden\"" in js or "overflow = 'hidden'" in js or "scroll" in js, (
        "B-CORE-6: Body overflow hidden (scroll lock) missing when opening modals"
    )

def test_b_core_7_poll_feed_deduplication():
    body = read_client_file("app.js")
    fn_body = extract_block_after_pattern(body, r"async function pollFeed")
    assert fn_body, "Could not extract pollFeed function"
    assert "inFlight" in fn_body or "Set" in fn_body or "rendered" in fn_body or "skip" in fn_body or "mutex" in fn_body or "activePoll" in fn_body, (
        "B-CORE-7: pollFeed has no protection against concurrent call race conditions (duplicates)."
    )

def test_b_core_8_aria_live_on_dynamic_statuses():
    html = read_client_file("index.html")
    for el_id in ("conn-status", "typing", "thread-badge", "kora-card-sub"):
        pattern = r'<[a-z0-9]+\s+[^>]*id="' + el_id + r'"[^>]*>'
        match = re.search(pattern, html)
        assert match, f"Could not find element with id {el_id} in index.html"
        tag = match.group(0)
        assert "aria-live" in tag or "role=\"status\"" in tag or "role='status'" in tag, (
            f"B-CORE-8: Element #{el_id} lacks aria-live or role='status'"
        )

def test_b_core_9_load_lists_initial_error_handling():
    body = read_client_file("app.js")
    fn_body = extract_block_after_pattern(body, r"async function loadLists")
    assert fn_body, "Could not extract loadLists function"
    assert "threads.length === 0" in fn_body or "threads.length == 0" in fn_body or "setConn" in fn_body, (
        "B-CORE-9: loadLists swallows errors silently without updating conn-status / showing error feedback."
    )

def test_b_core_10_maybe_reload_threshold_and_saving_input():
    body = read_client_file("app.js")
    probe_body = extract_block_after_pattern(body, r"async function probeSession")
    reload_body = extract_block_after_pattern(body, r"function maybeReload")
    
    assert "aliveMisses < 3" in probe_body or "aliveMisses < 4" in probe_body, (
        "B-CORE-10: probeSession aliveMisses threshold is low (2 misses)"
    )
    assert "msg-input" in reload_body and "value" in reload_body, (
        "B-CORE-10: maybeReload does not save msg-input value to sessionStorage before reload."
    )

def test_b_core_11_picker_path_error_not_overwrite_path():
    body = read_client_file("app.js")
    fn_body = extract_block_after_pattern(body, r'\$\("picker-choose"\)\.addEventListener\("click"')
    assert fn_body, "Could not extract picker-choose click listener"
    assert "picker-error" in fn_body or "pickerError" in fn_body, (
        "B-CORE-11: picker-choose click handler overwrites pickerPath with error message instead of using a separate error element."
    )

def test_b_core_12_browse_race_condition():
    body = read_client_file("app.js")
    fn_body = extract_block_after_pattern(body, r"async function browse")
    assert fn_body, "Could not extract browse function"
    assert "token" in fn_body or "counter" in fn_body or "latest" in fn_body or "activeRequest" in fn_body or "requestId" in fn_body, (
        "B-CORE-12: browse does not guard against race conditions from rapid directory taps."
    )

def test_b_core_13_render_active_thread_post_debounced():
    body = read_client_file("app.js")
    fn_body = extract_block_after_pattern(body, r"function render\b")
    assert fn_body, "Could not extract render function"
    assert "debounce" in fn_body or "activeThread" in fn_body or "/api/active-thread" not in fn_body, (
        "B-CORE-13: render() posts to active-thread unconditionally on every render without debouncing."
    )

def test_b_core_14_outcome_label_consistency():
    body = read_client_file("app.js")
    assert "outcomeLabel" in body or "getOutcome" in body or "formatOutcome" in body, (
        "B-CORE-14: Separate outcome mappings in renderBadge and threadCard (missing shared formatter)."
    )

def test_b_core_15_input_attributes_and_ime_guard():
    html = read_client_file("index.html")
    js = read_client_file("app.js")
    assert 'enterkeyhint="send"' in html or "enterkeyhint='send'" in html, (
        "B-CORE-15: msg-input lacks enterkeyhint='send'"
    )
    
    fn_body = extract_block_after_pattern(js, r'\$\("msg-input"\)\.addEventListener\("keydown"')
    assert fn_body, "Could not extract msg-input keydown listener"
    assert "isComposing" in fn_body or "keyCode === 229" in fn_body, (
        "B-CORE-15: msg-input keydown listener lacks IME composition guard (e.isComposing)"
    )

def test_b_core_16_focus_request_animation_frame():
    body = read_client_file("app.js")
    fn_body = extract_block_after_pattern(body, r'\$\("new-thread"\)\.addEventListener\("click"')
    assert fn_body, "Could not extract new-thread listener"
    assert "requestAnimationFrame" in fn_body or "setTimeout" in fn_body, (
        "B-CORE-16: msg-input focus is synchronous, which fails to lift the keyboard on iOS."
    )

# ============================================================================================
# B-UI Tests (style.css + status-widget.js + logs.html + manifest)
# ============================================================================================

def test_b_ui_1_contrast_dimmer():
    body = read_client_file("style.css")
    match = re.search(r"--dimmer:\s*([^;]+);", body)
    assert match, "Could not find --dimmer variable in style.css"
    val = match.group(1).strip().lower()
    assert val != "#5c7089", (
        "B-UI-1: --dimmer is set to low-contrast color #5c7089 (3.78:1 against background)"
    )

def test_b_ui_2_logs_html_contrast():
    body = read_static_file("logs.html")
    assert "font-size: 11px" not in body, "B-UI-2: logs.html .meta uses tiny 11px font size"
    assert "#6b7683" not in body, "B-UI-2: logs.html .meta uses low-contrast color #6b7683"

def test_b_ui_3_focus_visible_and_active_states():
    body = read_client_file("style.css")
    assert ":focus-visible" in body, "B-UI-3: style.css lacks :focus-visible rules"
    assert ":active" in body, "B-UI-3: style.css lacks :active rules"

def test_b_ui_4_prefers_reduced_motion():
    body = read_client_file("style.css")
    assert "prefers-reduced-motion" in body, (
        "B-UI-4: style.css lacks prefers-reduced-motion media query"
    )

def test_b_ui_5_status_widget_iife():
    body = read_static_file("status-widget.js").strip()
    assert body.startswith("(") or body.startswith(";(") or body.startswith("!function") or body.startswith("(()"), (
        "B-UI-5: status-widget.js does not wrap code in an IIFE to protect the global scope"
    )

def test_b_ui_6_status_widget_json_try_catch():
    body = read_static_file("status-widget.js")
    match = re.search(r"try\s*\{([\s\S]*?)\}\s*catch", body)
    assert match, "Could not find try/catch block in status-widget.js"
    try_body = match.group(1)
    assert "json" in try_body, (
        "B-UI-6: status-widget.js calls res.json() outside the try-catch block"
    )

def test_b_ui_7_status_widget_absolute_path():
    body = read_static_file("status-widget.js")
    assert '"./logs"' not in body and "'./logs'" not in body, (
        "B-UI-7: status-widget.js uses relative path './logs' which fails if widget is loaded outside client root"
    )

def test_b_ui_8_intervals_paused_in_background():
    logs_body = read_static_file("logs.html")
    widget_body = read_static_file("status-widget.js")
    assert "clearInterval" in logs_body, "B-UI-8: logs.html does not pause interval polling in background"
    assert "clearInterval" in widget_body, "B-UI-8: status-widget.js does not pause interval polling in background"

def test_b_ui_9_manifest_purpose_maskable():
    body = read_static_file("manifest.webmanifest")
    data = json.loads(body)
    icons = data.get("icons", [])
    icon_512 = next((i for i in icons if i.get("sizes") == "512x512"), None)
    assert icon_512, "manifest missing 512x512 icon"
    purpose = icon_512.get("purpose", "")
    assert "maskable" in purpose, (
        "B-UI-9: 512x512 icon in manifest.webmanifest missing purpose: maskable"
    )

def test_b_ui_10_manifest_description_and_display_override():
    body = read_static_file("manifest.webmanifest")
    data = json.loads(body)
    assert "description" in data, "B-UI-10: manifest.webmanifest missing description"
    assert "display_override" in data, "B-UI-10: manifest.webmanifest missing display_override"

def test_b_ui_11_logs_html_font_stack():
    body = read_static_file("logs.html")
    match = re.search(r"body\s*\{([\s\S]*?)\}", body)
    assert match, "Could not find body style in logs.html"
    style = match.group(1)
    font_match = re.search(r"font:\s*([^;]+);", style)
    assert font_match, "Could not find font property in body style"
    font_stack = font_match.group(1)
    assert "SF Mono" not in font_stack and "monospace" not in font_stack, (
        "B-UI-11: logs.html body font style mixes proportional (-apple-system) and monospace fallbacks"
    )

def test_b_ui_12_font_sizes_below_guidelines():
    style_body = read_client_file("style.css")
    logs_body = read_static_file("logs.html")
    assert "font-size: 11px" not in logs_body, "B-UI-12: logs.html uses 11px font size which is below guidelines"
    assert "font-size: 12px" not in style_body, "B-UI-12: style.css uses 12px font size which is below guidelines"

def test_b_ui_13_hero_landscape_iphone():
    body = read_client_file("style.css")
    hero_styles = extract_block_after_pattern(body, r"#hero\b")
    assert "10vh" not in hero_styles or "clamp" in hero_styles or "@media" in hero_styles, (
        "B-UI-13: #hero margin uses static 10vh without clamp or media query guard for landscape orientation"
    )

def test_b_ui_14_kora_card_affordance_and_states():
    html = read_client_file("index.html")
    css = read_client_file("style.css")
    assert "›" in html or "chevron" in html or "arrow" in html, (
        "B-UI-14: kora-card in index.html lacks visual navigation affordance (e.g. ›)"
    )
    assert "#kora-card:active" in css or "#kora-card:focus" in css, (
        "B-UI-14: style.css lacks active/focus styling for #kora-card link"
    )
