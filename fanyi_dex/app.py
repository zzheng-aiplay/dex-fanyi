"""Registry, BlobCache, Worker, and Client wiring."""

from __future__ import annotations

import asyncio
import time

import grpc

from dex import (
    AsyncClient,
    AsyncWorker,
    BlobCacheConfig,
    ClientOptions,
    Registry,
    WorkerOptions,
    WorkerTarget,
    open_blob_cache,
)

from fanyi_dex.book.book_flow import BookFlow
from fanyi_dex.book.curate_chapter import CurateChapterFlow
from fanyi_dex.book.produce_chapter import ProduceChapterFlow
from fanyi_dex.config import Config
from fanyi_dex.flow import BeatPlanFlow
from fanyi_dex.pass1_flow import Pass1Flow


def _flows(config: Config) -> tuple:
    """Every Flow the Worker and Client must agree on.

    `BeatPlanFlow` and `Pass1Flow` are v1 and stay registered: executions may still be
    open on them, parked at their gates, and a Step type is part of the durable contract
    of an open execution. New work goes through `BookFlow`.
    """
    curate = CurateChapterFlow(config)
    produce = ProduceChapterFlow(config)
    return (
        BookFlow(config, curate, produce),
        curate,
        produce,
        BeatPlanFlow(config),
        Pass1Flow(config),
    )


class App:
    """One Registry + BlobCache shared by the Worker and the Client."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.flows = _flows(config)
        self.book, self.curate, self.produce, self.beatplan, self.pass1 = self.flows
        self.registry = Registry(self.flows, allow_async_handlers=True)

        config.blob_cache_dir.mkdir(parents=True, exist_ok=True)
        self.blob_cache = open_blob_cache(
            BlobCacheConfig(str(config.blob_cache_dir), 1 << 30)
        )
        self.worker = AsyncWorker(
            self.registry,
            self.blob_cache,
            WorkerOptions(
                bind_address=config.worker_bind_address,
                server_address=config.server_address,
                worker_target=(
                    WorkerTarget(config.worker_target) if config.worker_target else None
                ),
            ),
        )
        self.client = AsyncClient(
            self.registry,
            self.blob_cache,
            ClientOptions(
                server_address=config.server_address,
                worker_target=self.worker.worker_target,
            ),
        )
        self._worker_task: asyncio.Task[None] | None = None

    async def start_worker(self) -> None:
        if self._worker_task is not None:
            return
        self._worker_task = asyncio.create_task(self.worker.start())
        await _await_worker(self.worker.worker_target.address, self._worker_task)

    async def close(self) -> None:
        await self.client.close()
        await self.worker.close()
        if self._worker_task is not None:
            try:
                await asyncio.wait_for(self._worker_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._worker_task.cancel()
            self._worker_task = None
        self.blob_cache.close()


class ClientOnly:
    """Client without a hosted Worker, for the CLI."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.flows = _flows(config)
        self.book, self.curate, self.produce, self.beatplan, self.pass1 = self.flows
        self.registry = Registry(self.flows, allow_async_handlers=True)
        config.blob_cache_dir.mkdir(parents=True, exist_ok=True)
        self.blob_cache = open_blob_cache(
            BlobCacheConfig(str(config.blob_cache_dir), 1 << 30)
        )
        self.client = AsyncClient(
            self.registry,
            self.blob_cache,
            ClientOptions(
                server_address=config.server_address,
                worker_target=WorkerTarget(
                    config.worker_target or config.worker_bind_address
                ),
            ),
        )

    async def close(self) -> None:
        await self.client.close()
        self.blob_cache.close()


async def _await_worker(address: str, worker_task: asyncio.Task[None]) -> None:
    """Wait until Dex Server can actually dispatch to this Worker.

    A raw TCP probe is not enough: `AsyncWorker.start` calls `add_insecure_port`
    (which makes the port connectable) *before* `_server.start()` (which makes gRPC
    serve). Probing the socket therefore returns while dispatches still fail with
    `connection refused`, which showed up as every first Step attempt failing and
    the Flow only advancing on attempt 2. An HTTP/2 handshake proves gRPC is up.
    """
    host, _, port_text = address.rpartition(":")
    target = f"{host or '127.0.0.1'}:{int(port_text)}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if worker_task.done():
            error = worker_task.exception()
            if error is not None:
                raise RuntimeError("AsyncWorker failed") from error
            raise RuntimeError("AsyncWorker stopped before becoming ready")
        channel = grpc.aio.insecure_channel(target)
        try:
            await asyncio.wait_for(channel.channel_ready(), timeout=0.5)
            return
        except (asyncio.TimeoutError, grpc.aio.AioRpcError):
            await asyncio.sleep(0.05)
        finally:
            await channel.close()
    raise RuntimeError("AsyncWorker did not become ready")
