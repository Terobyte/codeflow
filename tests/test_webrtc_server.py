import subprocess
import sys

import pytest


def test_app_import_stays_cv2_free():
    # S4 guarantee: `import synapse.pipeline.app` must NOT pull cv2/aiortc/fastapi (voice-extra,
    # lazily imported inside run()). Fresh interpreter so a sibling importorskip("cv2") can't mask it.
    code = "import synapse.pipeline.app, sys; sys.exit(1 if 'cv2' in sys.modules else 0)"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"app import pulled cv2: {r.stderr}"


def test_web_app_exposes_offer_routes_and_client_mount():
    pytest.importorskip("aiortc")
    pytest.importorskip("cv2")
    pytest.importorskip("fastapi")
    try:
        from synapse.pipeline.webrtc_server import build_web_app
    except (ImportError, RuntimeError) as e:  # prebuilt frontend raises RuntimeError on missing dist
        pytest.skip(f"webrtc deps/prebuilt UI unavailable: {e}")

    from starlette.routing import Mount

    app = build_web_app(cfg=object())  # cfg only used per-connection inside run_session
    method_paths = {
        (route.path, method)
        for route in app.routes
        for method in (getattr(route, "methods", None) or set())
    }
    assert ("/api/offer", "POST") in method_paths
    assert ("/api/offer", "PATCH") in method_paths
    assert ("/", "GET") in method_paths
    assert any(isinstance(r, Mount) and r.path == "/client" for r in app.routes)

    # Regression (browser hung at "authenticating -> Unable to connect"): the prebuilt RTVI client
    # POSTs /start FIRST, then the SDP offer to /sessions/<id>/api/offer. A server missing either
    # 404s the handshake before WebRTC ever begins. These two routes are the fix.
    assert ("/start", "POST") in method_paths
    assert ("/sessions/{session_id}/api/offer", "POST") in method_paths
    assert ("/sessions/{session_id}/api/offer", "PATCH") in method_paths
