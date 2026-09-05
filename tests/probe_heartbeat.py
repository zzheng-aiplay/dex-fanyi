"""Decisive probe: does a silent 90s async Execute survive the default heartbeat timeout?

SDK 0.2.5 exposes `StepOptions.heartbeat_timeout` but no way to *emit* a heartbeat
(no `context.heartbeat()`), and `Stream.write` is once-per-Step-execution. So if the
server enforces a 1-minute default heartbeat timeout, no `claude -p` call longer than
a minute can ever complete — which is the entire workload. This probe answers it.

    uv run python tests/probe_heartbeat.py            # heartbeat_timeout unset
    uv run python tests/probe_heartbeat.py --explicit # heartbeat_timeout = 10min
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import timedelta

from dex import (
    Attribute,
    Context,
    Flow,
    PersistenceSchema,
    Step,
    StepDecision,
    StepList,
    StepOptions,
    graceful_complete,
)

from fanyi_dex.app import _await_worker  # noqa: PLC2701
from fanyi_dex.config import Config

marker = Attribute("probe-marker", str)

SLEEP_S = 90
EXPLICIT = "--explicit" in sys.argv


class SilentStep(Step[str]):
    def get_step_options(self) -> StepOptions:
        options = StepOptions(execute_method_timeout=timedelta(minutes=10))
        if EXPLICIT:
            options = StepOptions(
                execute_method_timeout=timedelta(minutes=10),
                heartbeat_timeout=timedelta(minutes=10),
            )
        return options

    async def execute(self, context: Context, input: str) -> StepDecision:  # type: ignore[override]
        started = time.monotonic()
        print(f"  [step] attempt={context.attempt} sleeping {SLEEP_S}s silently", flush=True)
        await asyncio.sleep(SLEEP_S)
        elapsed = time.monotonic() - started
        marker.set(context, f"slept {elapsed:.0f}s on attempt {context.attempt}")
        return graceful_complete(f"ok after {elapsed:.0f}s, attempt {context.attempt}")


class ProbeFlow(Flow[str]):
    def __init__(self) -> None:
        self.silent = SilentStep()

    def get_flow_type(self) -> str:
        return "FanyiHeartbeatProbe"

    def get_steps(self) -> StepList[str]:
        return StepList.start_step(self.silent)

    def get_persistence_schema(self) -> PersistenceSchema:
        return PersistenceSchema.of(marker)


async def main() -> None:
    from dex import (
        AsyncClient,
        AsyncWorker,
        BlobCacheConfig,
        ClientOptions,
        Registry,
        WorkerOptions,
        open_blob_cache,
    )

    config = Config.from_env()
    flow = ProbeFlow()
    registry = Registry((flow,), allow_async_handlers=True)
    config.blob_cache_dir.mkdir(parents=True, exist_ok=True)
    cache = open_blob_cache(BlobCacheConfig(str(config.blob_cache_dir), 1 << 28))
    worker = AsyncWorker(
        registry,
        cache,
        WorkerOptions(
            bind_address="127.0.0.1:8899",
            server_address=config.server_address,
        ),
    )
    client = AsyncClient(
        registry,
        cache,
        ClientOptions(
            server_address=config.server_address,
            worker_target=worker.worker_target,
        ),
    )
    task = asyncio.create_task(worker.start())
    await _await_worker(worker.worker_target.address, task)

    flow_id = f"probe-hb-{'explicit' if EXPLICIT else 'default'}-{int(time.time())}"
    mode = "heartbeat_timeout=10min" if EXPLICIT else "heartbeat_timeout unset (server default)"
    print(f"probe: {mode}, silent sleep {SLEEP_S}s, flow {flow_id}", flush=True)
    started = time.monotonic()
    await client.start_flow(flow, flow_id, "probe")
    try:
        # Poll rather than long-poll: a long-poll deadline killing this process
        # also kills the Worker hosting the step under test.
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            info = await client.describe_flow(flow_id)
            if info.status.name != "RUNNING":
                break
            await asyncio.sleep(5)
        result = await client.wait_for_flow(flow_id, timeout=timedelta(seconds=1))
        print(f"RESULT status={result.status} after {time.monotonic() - started:.0f}s")
        if result.error_message:
            print(f"  error_type={result.error_type} message={result.error_message}")
        for completion in result.completions:
            print(f"  completion {completion.step_type}: {completion.decode(str)}")
    finally:
        await client.close()
        await worker.close()
        task.cancel()
        cache.close()


if __name__ == "__main__":
    asyncio.run(main())
