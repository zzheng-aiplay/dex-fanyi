"""Probe: does restarting a CLOSED flow id (a) succeed under DEFAULT reuse policy,
(b) carry prior Attribute values into the new run, and (c) let
StartFlowOptions.with_attribute overwrite them?
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from dex import (
    Attribute,
    Context,
    Flow,
    PersistenceSchema,
    StartFlowOptions,
    Step,
    StepDecision,
    StepList,
    graceful_complete,
)

from fanyi_dex.app import _await_worker  # noqa: PLC2701
from fanyi_dex.config import Config

val = Attribute("reuseprobe-val", str)


class ReadStep(Step[str]):
    async def execute(self, context: Context, input: str) -> StepDecision:  # type: ignore[override]
        got = val.get(context)
        val.set(context, f"{got if got is not None else '<unset>'}+run:{input}")
        return graceful_complete(f"start_step_saw={got!r}")


class ReuseFlow(Flow[str]):
    def __init__(self) -> None:
        self.read = ReadStep()

    def get_flow_type(self) -> str:
        return "FanyiReuseProbe"

    def get_steps(self) -> StepList[str]:
        return StepList.start_step(self.read)

    def get_persistence_schema(self) -> PersistenceSchema:
        return PersistenceSchema.of(val)


async def settle(client, fid):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        info = await client.describe_flow(fid)
        if info.status.name != "RUNNING":
            return info
        await asyncio.sleep(0.5)
    raise TimeoutError(fid)


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
    flow = ReuseFlow()
    registry = Registry((flow,), allow_async_handlers=True)
    config.blob_cache_dir.mkdir(parents=True, exist_ok=True)
    cache = open_blob_cache(BlobCacheConfig(str(config.blob_cache_dir), 1 << 28))
    worker = AsyncWorker(
        registry,
        cache,
        WorkerOptions(bind_address="127.0.0.1:8896", server_address=config.server_address),
    )
    client = AsyncClient(
        registry,
        cache,
        ClientOptions(
            server_address=config.server_address, worker_target=worker.worker_target
        ),
    )
    task = asyncio.create_task(worker.start())
    await _await_worker(worker.worker_target.address, task)

    fid = f"reuse-probe-{int(time.time())}"
    try:
        print(f"flow id: {fid}")
        run1 = await client.start_flow(
            flow, fid, "one", StartFlowOptions().with_attribute(val, "SEED-A")
        )
        info = await settle(client, fid)
        r = await client.wait_for_flow(fid, timeout=timedelta(seconds=2))
        print(f"run1={run1} status={info.status.name}")
        for c in r.completions:
            print(f"  run1 {c.step_type}: {c.decode(str)}")
        print(f"  attribute after run1 = {await client.get_attribute(fid, val)!r}")

        # (b)+(c): restart the SAME id, seeding a DIFFERENT value
        try:
            run2 = await client.start_flow(
                flow, fid, "two", StartFlowOptions().with_attribute(val, "SEED-B")
            )
            info = await settle(client, fid)
            r = await client.wait_for_flow(fid, timeout=timedelta(seconds=2))
            print(f"run2={run2} status={info.status.name}")
            for c in r.completions:
                print(f"  run2 {c.step_type}: {c.decode(str)}")
            print(f"  attribute after run2 = {await client.get_attribute(fid, val)!r}")
        except Exception as e:  # noqa: BLE001
            print(f"  run2 REJECTED: {type(e).__name__}: {e}")

        # (b) alone: restart with no seed at all
        try:
            run3 = await client.start_flow(flow, fid, "three")
            info = await settle(client, fid)
            r = await client.wait_for_flow(fid, timeout=timedelta(seconds=2))
            print(f"run3={run3} status={info.status.name}")
            for c in r.completions:
                print(f"  run3 {c.step_type}: {c.decode(str)}")
            print(f"  attribute after run3 = {await client.get_attribute(fid, val)!r}")
        except Exception as e:  # noqa: BLE001
            print(f"  run3 REJECTED: {type(e).__name__}: {e}")
    finally:
        await client.close()
        await worker.close()
        task.cancel()
        cache.close()


if __name__ == "__main__":
    asyncio.run(main())
