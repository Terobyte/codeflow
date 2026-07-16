#!/usr/bin/env python3
"""Exec the real Claude CLI with a clean environment for Kora worker runs.

claude-agent-sdk merges ``ClaudeAgentOptions.env`` into the parent environment instead of
replacing it.  This tiny trusted launcher is therefore the actual Phase-0 credential boundary:
it receives the already-resolved real CLI path from trusted host code, then replaces the
environment completely with the allowlist before any Claude CLI code executes.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


_ALLOWLIST = (
    "HOME", "PATH", "SHELL", "TMPDIR", "LANG", "LC_ALL", "TERM", "USER",
    "ANTHROPIC_API_KEY",
)
_SDK_CONTROL_VARS = ("CLAUDE_CODE_ENTRYPOINT", "CLAUDE_AGENT_SDK_VERSION", "PWD")
_REAL_CLI_VAR = "SYNAPSE_KORA_REAL_CLI"


def main() -> None:
    real_cli = os.environ.get(_REAL_CLI_VAR)
    if not real_cli:
        raise RuntimeError(f"{_REAL_CLI_VAR} is required")
    if Path(real_cli).resolve() == Path(__file__).resolve():
        raise RuntimeError("KORA_CLI_PATH points at the sanitizer itself")
    clean_env = {
        key: os.environ[key]
        for key in (*_ALLOWLIST, *_SDK_CONTROL_VARS)
        if key in os.environ
    }
    os.execve(real_cli, [real_cli, *sys.argv[1:]], clean_env)


if __name__ == "__main__":
    main()
