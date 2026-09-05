"""Probe 4: does a WORKER OUTAGE widen the tie window?

Timeline: gate parks on any_of(chan.for_one(), Timer(6s)). At t=3s the worker is
closed. The timer fires at t=6s with no worker to dispatch to. At t=10s the
operator publishes the approval. At t=13s a fresh worker comes up.

If execute then sees has_timer_fired=True AND a channel result, the audit's
scenario (timer due while the worker is unavailable, approval published before
the wait is next evaluated) is reachable over a multi-second window rather than
a millisecond race.

    uv run python tests/probe_tie4.py
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from dex import (
    Attribute,
    Channel,
    Context,
    Flow,
    PersistenceSchema,
    Step,
    StepDecision,
    StepList,
    Timer,
    Wait,
    graceful_complete,
)

from fanyi_dex.app import _await_worker  # noqa: PLC2701
from fanyi_dex.config import Config

chan = Channel("probe4-chan", str)
seen = Attribute("probe4-seen", str)


class GateStep(Step[str]):
    def wait_for(self, context: Context, input: str) -> Wait:
        print(f"  [gate] wait_for at {time.time():.3f}", flush=True)
        return Wait.any_of(chan.for_one(), Timer.by_duration(timedelta(seconds=6)))

    async def execute(self, context: Context, input: str) -> StepDecision:  # type: ignore[override]
        verdict = (
            f"has_timer_fired={context.has_timer_fired()} "
            f"results={list(chan.results(context))} "
            f"queue_size={chan.size(context)}"
        )
        print(f"  [gate] execute at {time.time():.3f} {verdict}", flush=True)
        seen.set(context, verdict)
        return graceful_complete(verdict)


class ProbeFlow(Flow[str]):
    def __init__(self) -> None:
        self.gate = GateStep()

    def get_flow_type(self) -> str:
        return "FanyiTieProbe4"

    def get_steps(self) -> StepList[str]:
        return StepList.start_step(self.gate)

    def get_persistence_schema(self) -> PersistenceSchema:
        return PersistenceSchema.of(chan, seen)


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

    def make_worker(port: int) -> AsyncWorker:
        return AsyncWorker(
            registry,
            cache,
            WorkerOptions(
                bind_address=f"127.0.0.1:{port}", server_address=config.server_address
            ),
        )

    worker_a = make_worker(8894)
    task_a = asyncio.create_task(worker_a.start())
    await _await_worker(worker_a.worker_target.address, task_a)
    client = AsyncClient(
        registry,
        cache,
        ClientOptions(
            server_address=config.server_address, worker_target=worker_a.worker_target
        ),
    )

    flow_id = f"probe-tie4-{int(time.time())}"
    print(f"probe4: flow={flow_id}", flush=True)
    started = time.time()
    await client.start_flow(flow, flow_id, "probe")
    await asyncio.sleep(3)
    print(f"  [test] closing worker at {time.time() - started:.1f}s", flush=True)
    await worker_a.close()
    task_a.cancel()

    await asyncio.sleep(7)  # timer due at 6s, no worker; publish at ~10s
    print(f"  [test] publish at {time.time() - started:.1f}s", flush=True)
    try:
        await client.publish(flow_id, chan, "APPROVAL-PAYLOAD")
    except Exception as exc:  # noqa: BLE001
        print(f"  [test] publish error: {exc}", flush=True)

    await asyncio.sleep(3)
    print(f"  [test] new worker at {time.time() - started:.1f}s", flush=True)
    worker_b = make_worker(8893)
    task_b = asyncio.create_task(worker_b.start())
    await _await_worker(worker_b.worker_target.address, task_b)

    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            info = await client.describe_flow(flow_id)
            if info.status.name != "RUNNING":
                break
            await asyncio.sleep(1)
        result = await client.wait_for_flow(flow_id, timeout=timedelta(seconds=2))
        print(f"RESULT status={result.status}")
        for completion in result.completions:
            print(f"  completion {completion.step_type}: {completion.decode(str)}")
    finally:
        await client.close()
        await worker_b.close()
        task_b.cancel()
        cache.close()


if __name__ == "__main__":
    asyncio.run(main())
