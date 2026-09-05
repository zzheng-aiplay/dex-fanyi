"""Runner configuration: the Dex connection and how headless Claude is invoked.

Distinct from a *project* config (`pipeline/config.json`), which is the
translation project's own source of truth — see `project.py`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() not in ("", "0", "false", "no")


def _seconds(name: str, default: int) -> timedelta:
    return timedelta(seconds=int(os.environ.get(name, default)))


@dataclass(frozen=True)
class Config:
    server_address: str
    worker_bind_address: str
    worker_target: str | None
    blob_cache_dir: Path

    # Headless Claude invocation. Mirrors the agent options the generated
    # Workflow scripts use: model opus, effort low, no structured-output schema.
    # `bare` skips CLAUDE.md, hooks, and auto-memory, which cuts the per-call
    # system-prompt overhead by roughly 9x — these calls are pure prompt-to-JSON
    # and need none of it.
    claude_bin: str
    model: str
    effort: str
    max_turns: int
    bare: bool

    # How many chapters run concurrently. The generated Workflow script runs
    # them strictly serially; waves keep that rate-limit safety while still
    # overlapping work.
    wave_size: int
    # Pass-1 phases are judgment-heavy rewrites, so they run at medium effort
    # (matching the generated workflow) with a longer per-phase budget.
    pass1_effort: str
    pass1_timeout: timedelta
    # Cap on the fan-outs *inside* one chapter phase (dialogue repairs, audit
    # lenses) so a chapter cannot open a dozen Claude processes at once.
    inner_concurrency: int
    # No 180s watchdog here — this is the whole point of moving off the
    # Workflow runtime for this stage.
    chapter_timeout: timedelta
    parse_attempts: int

    # dry_run fakes everything (agent calls AND pipeline scripts).
    # fake_agent fakes only the expensive Claude call, so the deterministic
    # scripts and the harvest seam still run for real — that's the mode that
    # actually validates the artifact contract without spending on opus.
    dry_run: bool
    fake_agent: bool
    gate_reminder: timedelta

    @staticmethod
    def from_env() -> Config:
        home = Path.home()
        return Config(
            server_address=os.environ.get("DEX_FLOW_SERVICE_ADDRESS", "127.0.0.1:8801"),
            worker_bind_address=os.environ.get("DEX_WORKER_BIND_ADDRESS", "127.0.0.1:8812"),
            worker_target=os.environ.get("DEX_WORKER_TARGET") or None,
            blob_cache_dir=Path(
                os.environ.get("DEX_BLOB_CACHE_DIR", home / ".dex" / "fanyi-blob-cache")
            ),
            claude_bin=os.environ.get("FANYI_CLAUDE_BIN", "claude"),
            model=os.environ.get("FANYI_MODEL", "opus"),
            effort=os.environ.get("FANYI_EFFORT", "low"),
            max_turns=int(os.environ.get("FANYI_MAX_TURNS", 1)),
            bare=_flag("FANYI_BARE", True),
            wave_size=int(os.environ.get("FANYI_WAVE_SIZE", 4)),
            pass1_effort=os.environ.get("FANYI_PASS1_EFFORT", "medium"),
            pass1_timeout=_seconds("FANYI_PASS1_TIMEOUT_S", 45 * 60),
            inner_concurrency=int(os.environ.get("FANYI_INNER_CONCURRENCY", 3)),
            chapter_timeout=_seconds("FANYI_CHAPTER_TIMEOUT_S", 30 * 60),
            parse_attempts=int(os.environ.get("FANYI_PARSE_ATTEMPTS", 3)),
            dry_run=_flag("FANYI_DRY_RUN"),
            fake_agent=_flag("FANYI_FAKE_AGENT"),
            gate_reminder=_seconds("FANYI_GATE_REMINDER_S", 24 * 3600),
        )
