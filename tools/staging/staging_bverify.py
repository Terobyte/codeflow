"""Staging harness for the B23/B24/B25 live browser verification (:7861).

Same as staging_7861.py but with SHORT liveness thresholds so B23 (a COMPLETED task
must report OK past the unreachable window) is observable in seconds, not 5 minutes.
Isolated journal_dir, real Kora. Does NOT touch production :7860.

Usage:
    STAGING_JOURNAL_DIR=/tmp/synapse-staging-bverify .venv/bin/python staging_bverify.py
"""
from __future__ import annotations

import dataclasses
import os

from dotenv import load_dotenv

load_dotenv()

from synapse.config import SynapseConfig
from synapse.pipeline.app import _require_api_token, build_host
from synapse.pipeline.webrtc_server import build_web_app

cfg = SynapseConfig.from_env()
_require_api_token(cfg)
journal = os.environ.get("STAGING_JOURNAL_DIR", "/tmp/synapse-staging-bverify")
# short liveness windows: stale at 3s, UNREACHABLE at 6s — a COMPLETED task ages past
# 6s within the test, so B23's "COMPLETED → OK regardless of age" is directly observable.
cfg = dataclasses.replace(cfg, journal_dir=journal, stale_after_s=3.0, unreachable_after_s=6.0)

host = build_host(cfg)
app = build_web_app(host)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="localhost", port=7861, log_level="warning")
