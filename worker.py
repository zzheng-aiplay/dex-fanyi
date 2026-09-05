"""Long-running Worker hosting every Flow. Keep running; restart freely.

Hosts the end-to-end volume Flow (`FanyiBook` + its two chapter SubFlows) and the
v1 Flows, which stay registered because executions are still open on them.
"""

from __future__ import annotations

import asyncio
import contextlib

from fanyi_dex.app import App
from fanyi_dex.config import Config


def _already_bound(address: str) -> bool:
    """Is something already serving on this Worker's address?

    macOS lets a second Worker bind the same port, and Dex then dispatches to whichever
    it reaches — so a Worker left over from before a code change silently serves the old
    code. That cost one confusing debugging session: a test run produced output from a
    prompt file that had been rewritten an hour earlier.
    """
    import socket

    host, _, port = address.rpartition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port)), timeout=0.3):
            return True
    except OSError:
        return False


async def main() -> None:
    config = Config.from_env()
    if _already_bound(config.worker_bind_address):
        print(
            f"refusing to start: something is already serving {config.worker_bind_address}.\n"
            f"  A leftover Worker there will serve STALE code — Dex dispatches to whichever it\n"
            f"  reaches. Stop it first (pkill -f worker.py), or set DEX_WORKER_BIND_ADDRESS to\n"
            f"  a free port.",
            flush=True,
        )
        raise SystemExit(1)
    app = App(config)
    await app.start_worker()
    mode = "DRY RUN" if config.dry_run else "live"
    print(
        f"dex-fanyi worker ready ({mode})\n"
        f"  flows      : {', '.join(f.get_flow_type() for f in app.flows)}\n"
        f"  dex server : {config.server_address}\n"
        f"  worker     : {app.worker.worker_target.address}\n"
        f"  claude     : {config.claude_bin} --model {config.model} "
        f"--effort {config.effort}{' --bare' if config.bare else ''}\n"
        f"  wave size  : {config.wave_size} chapter(s) at a time\n"
        f"  per-chapter: {int(config.chapter_timeout.total_seconds())}s budget, "
        f"{config.parse_attempts} parse attempts",
        flush=True,
    )
    try:
        await asyncio.Event().wait()
    finally:
        await app.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
