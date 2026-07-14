"""UI v4 redesign (tero run 2026-07-14, CodeFlow/Organic canon) — additive lexical checks.

Everything pinned in test_ui_client.py / test_bugs_audit.py / test_kora_status_ui.py /
test_slice5_pwa.py must keep passing UNMODIFIED; this file only adds coverage for the new
Organic-theme surface: design tokens in style.css, new structural ids in index.html, the
disp/kora role split + XSS discipline in app.js, and the recolored manifest.
"""
from __future__ import annotations

import json
from pathlib import Path

CLIENT_DIR = Path(__file__).parent.parent / "synapse" / "pipeline" / "client"
STATIC_DIR = Path(__file__).parent.parent / "synapse" / "pipeline" / "static"


def read(name: str) -> str:
    return (CLIENT_DIR / name).read_text(encoding="utf-8")


def test_style_css_carries_organic_design_tokens():
    css = read("style.css")
    for token in (
        "Caprasimo", "Figtree", "#c67139", "#f5ead8",
        "prefers-reduced-motion", ":focus-visible", 'data-state="on"',
        'data-state="connecting"', 'data-state="error"', 'data-state="idle"',
        "color-scheme: light",
    ):
        assert token in css, f"style.css missing {token!r}"


def test_style_css_reduced_motion_covers_new_animations():
    css = read("style.css")
    block_start = css.find("@media (prefers-reduced-motion: reduce)")
    assert block_start != -1
    block = css[block_start:css.find("}", css.find("{", block_start)) + 1]
    # extend to the closing brace of the whole media block, not just the first rule
    depth = 0
    end = block_start
    started = False
    for i, ch in enumerate(css[block_start:], start=block_start):
        if ch == "{":
            depth += 1
            started = True
        elif ch == "}":
            depth -= 1
            if started and depth == 0:
                end = i + 1
                break
    block = css[block_start:end]
    for token in (".bg-blob", "#live-orb", "#live-wave", ".msg"):
        assert token in block, f"prefers-reduced-motion block missing new-animation target {token!r}"


def test_style_css_forbids_100vh():
    css = read("style.css")
    assert "100vh" not in css


def test_index_has_redesign_structural_ids():
    body = read("index.html")
    for token in (
        "disp-toggle", "tab-chat", "tab-diff", "view-diff",
        "live-overlay", "live-status", "live-mute", "live-end",
        "logo-wave", "bg-blob",
    ):
        assert token in body, f"index.html missing {token!r}"
    assert 'content="#f5ead8"' in body  # R3: theme-color recolored, no dark→light flash
    assert 'content="default"' in body  # R3: status-bar-style no longer black-translucent
    assert "black-translucent" not in body


def test_index_composer_has_mic_left_of_input_send_right():
    body = read("index.html")
    mic_i = body.index('id="mic-btn"')
    input_i = body.index('id="msg-input"')
    send_i = body.index('id="msg-send"')
    assert mic_i < input_i < send_i, "PF3: composer canon is mic-left / input / send-right"


def test_index_live_overlay_has_aria_live_status():
    body = read("index.html")
    live_status_tag = body[body.index('id="live-status"') - 60 : body.index('id="live-status"') + 40]
    assert 'aria-live="polite"' in live_status_tag


def test_app_js_has_disp_and_kora_roles_and_no_innerhtml():
    js = read("app.js")
    for token in (
        "synapse-disp-on", "msg-disp", "msg-kora", "dispOn",
        "liveRequested", "liveMuteFn", "disconnectVoice",
        "onBotStartedSpeaking", "onBotStoppedSpeaking", "svgEl",
    ):
        assert token in js, f"app.js missing {token!r}"
    assert "innerHTML" not in js
    assert "insertAdjacentHTML" not in js
    assert "document.write" not in js


def test_app_js_mic_never_gets_disabled_attribute_for_disp_toggle():
    js = read("app.js")
    # R2: dispOn=false must never set mic-btn.disabled — only a visual dim class.
    assert '$("mic-btn").disabled' not in js
    assert "disp-off" in js


def test_manifest_theme_matches_organic_bg():
    data = json.loads((STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert data["theme_color"] == "#f5ead8"
    assert data["background_color"] == "#f5ead8"
    # pinned fields untouched by the redesign
    assert data["name"] == "Синапс"
    assert data["start_url"] == "/client/"
