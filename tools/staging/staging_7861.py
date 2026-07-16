"""Staging smoke server on :7861 (Task 7).

Isolated journal_dir (STAGING_JOURNAL_DIR), real SynapseConfig from .env, real Kora enabled.
Does NOT touch production :7860 or its journals/. Untracked harness — not committed.

Usage:
    STAGING_JOURNAL_DIR=/tmp/synapse-staging-7861 .venv/bin/python staging_7861.py
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
journal = os.environ.get("STAGING_JOURNAL_DIR")
if journal:
    # frozen dataclass → replace; from_env() reads no JOURNAL_DIR env, so this is the only seam.
    cfg = dataclasses.replace(cfg, journal_dir=journal)

host = build_host(cfg)
app = build_web_app(host)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="localhost", port=7861, log_level="warning")
