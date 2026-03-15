from __future__ import annotations

import argparse
import asyncio
import base64
import json
import statistics
import struct
import subprocess
import time
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets


QUERY_BUNDLES = [
    {
        "label": "handbag",
        "queries": ["red handbag", "small red purse"],
        "ranking_query": "small red handbag for daily use",
    },
    {
        "label": "speaker",
        "queries": ["bookshelf speaker", "compact speaker"],
        "ranking_query": "compact speaker for a small room",
    },
    {
        "label": "teapot",
        "queries": ["white teapot", "ceramic tea pot"],
        "ranking_query": "simple white teapot for daily tea",
    },
    {
        "label": "shirt",
        "queries": ["striped shirt", "casual button shirt"],
        "ranking_query": "casual striped shirt for everyday wear",
    },
]


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    values = sorted(values)
    index = (len(values) - 1) * pct
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    weight = index - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def build_png(seed: int, width: int = 48, height: int = 48) -> bytes:
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            r = (seed * 53 + x * 7 + y * 11) % 256
            g = (seed * 97 + x * 13 + y * 5) % 256
            b = (seed * 193 + x * 3 + y * 17) % 256
            if (x // 8 + y // 8 + seed) % 2 == 0:
                rows.extend((r, g, b))
            else:
                rows.extend((255 - r, 255 - g, 255 - b))
    compressed = zlib.compress(bytes(rows), level=9)

    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + chunk_type
            + data
            + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )


@dataclass
class UserStats:
    label: str
    latencies_ms: list[float] = field(default_factory=list)
    response_latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0
    session_mismatches: int = 0
    unexpected_updates: int = 0
    completed: int = 0
    timeouts: int = 0


@dataclass
class MonitorStats:
    cpu_samples: list[float] = field(default_factory=list)
    rss_mb_samples: list[float] = field(default_factory=list)


class TileClient:
    def __init__(self, ws: websockets.ClientConnection, expected_session_id: str):
        self.ws = ws
        self.expected_session_id = expected_session_id

    async def recv_until(self, kinds: set[str], timeout_s: float) -> tuple[dict[str, Any] | None, int]:
        mismatches = 0
        while True:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout_s)
            except asyncio.TimeoutError:
                return None, mismatches
            message = json.loads(raw)
            message_session_id = message.get("sessionId")
            if message_session_id is not None and message_session_id != self.expected_session_id:
                mismatches += 1
            if message.get("kind") in kinds:
                return message, mismatches


def to_ws_url(base_url: str, path: str) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}{path}"


async def monitor_process(pid: int, stop_event: asyncio.Event) -> MonitorStats:
    stats = MonitorStats()
    while not stop_event.is_set():
        result = await asyncio.to_thread(
            subprocess.run,
            ["ps", "-p", str(pid), "-o", "%cpu=", "-o", "rss="],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split()
            if len(parts) >= 2:
                stats.cpu_samples.append(float(parts[0]))
                stats.rss_mb_samples.append(float(parts[1]) / 1024.0)
        await asyncio.sleep(1.0)
    return stats


async def run_similar_user(
    base_url: str,
    duration_s: float,
    interval_s: float,
    user_index: int,
    active: bool,
) -> UserStats:
    label = f"similar-{user_index}"
    stats = UserStats(label=label)
    user_id = f"user-{uuid.uuid4()}"
    session_id = f"session-{uuid.uuid4()}"
    image_bytes = build_png(user_index + 1)
    image_b64 = base64.b64encode(image_bytes).decode("ascii")

    tile_ws_url = to_ws_url(base_url, f"/ws_image_tile/{session_id}")
    deadline = time.perf_counter() + duration_s

    async with websockets.connect(tile_ws_url, max_size=2**22) as tile_ws:
        tile_client = TileClient(tile_ws, session_id)
        snapshot, mismatches = await tile_client.recv_until({"snapshot"}, timeout_s=10.0)
        stats.session_mismatches += mismatches
        if snapshot is None:
            stats.errors += 1
            return stats

        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
            if not active:
                while time.perf_counter() < deadline:
                    message, mismatches = await tile_client.recv_until(
                        {"similar", "recommended"}, timeout_s=1.0
                    )
                    stats.session_mismatches += mismatches
                    if message is not None:
                        stats.unexpected_updates += 1
                return stats

            while time.perf_counter() < deadline:
                started_at = time.perf_counter()
                response = await client.post(
                    "/test/similar",
                    json={
                        "user_id": user_id,
                        "session_id": session_id,
                        "image_b64": image_b64,
                    },
                )
                if response.status_code != 200:
                    stats.errors += 1
                    await asyncio.sleep(interval_s)
                    continue
                message, mismatches = await tile_client.recv_until({"similar"}, timeout_s=20.0)
                stats.session_mismatches += mismatches
                if message is None:
                    stats.timeouts += 1
                    await asyncio.sleep(interval_s)
                    continue
                stats.latencies_ms.append((time.perf_counter() - started_at) * 1000)
                stats.completed += 1
                await asyncio.sleep(interval_s)

    return stats


async def run_find_items_user(
    base_url: str,
    duration_s: float,
    interval_s: float,
    user_index: int,
    active: bool,
) -> UserStats:
    bundle = QUERY_BUNDLES[user_index % len(QUERY_BUNDLES)]
    label = f"find-items-{bundle['label']}-{user_index}"
    stats = UserStats(label=label)
    user_id = f"user-{uuid.uuid4()}"
    session_id = f"session-{uuid.uuid4()}"
    tile_ws_url = to_ws_url(base_url, f"/ws_image_tile/{session_id}")
    deadline = time.perf_counter() + duration_s

    async with websockets.connect(tile_ws_url, max_size=2**22) as tile_ws:
        tile_client = TileClient(tile_ws, session_id)
        snapshot, mismatches = await tile_client.recv_until({"snapshot"}, timeout_s=10.0)
        stats.session_mismatches += mismatches
        if snapshot is None:
            stats.errors += 1
            return stats

        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
            if not active:
                while time.perf_counter() < deadline:
                    message, mismatches = await tile_client.recv_until(
                        {"similar", "recommended"}, timeout_s=1.0
                    )
                    stats.session_mismatches += mismatches
                    if message is not None:
                        stats.unexpected_updates += 1
                return stats

            while time.perf_counter() < deadline:
                started_at = time.perf_counter()
                response = await client.post(
                    "/test/find_items",
                    json={
                        "user_id": user_id,
                        "session_id": session_id,
                        "queries": bundle["queries"],
                        "ranking_query": bundle["ranking_query"],
                        "publish": True,
                    },
                )
                if response.status_code != 200:
                    stats.errors += 1
                    await asyncio.sleep(interval_s)
                    continue
                payload = response.json()
                stats.response_latencies_ms.append((time.perf_counter() - started_at) * 1000)
                message, mismatches = await tile_client.recv_until({"recommended"}, timeout_s=20.0)
                stats.session_mismatches += mismatches
                if message is None:
                    stats.timeouts += 1
                    await asyncio.sleep(interval_s)
                    continue
                if message.get("sessionId") != session_id:
                    stats.session_mismatches += 1
                stats.latencies_ms.append((time.perf_counter() - started_at) * 1000)
                stats.completed += 1
                if payload.get("session_id") != session_id:
                    stats.session_mismatches += 1
                await asyncio.sleep(interval_s)

    return stats


def summarize_user_stats(user_stats: list[UserStats], monitor: MonitorStats, workload: str) -> dict[str, Any]:
    latencies = [value for stats in user_stats for value in stats.latencies_ms]
    response_latencies = [value for stats in user_stats for value in stats.response_latencies_ms]
    summary = {
        "workload": workload,
        "users": len(user_stats),
        "completed": sum(stats.completed for stats in user_stats),
        "errors": sum(stats.errors for stats in user_stats),
        "timeouts": sum(stats.timeouts for stats in user_stats),
        "session_mismatches": sum(stats.session_mismatches for stats in user_stats),
        "unexpected_updates": sum(stats.unexpected_updates for stats in user_stats),
        "latency_ms": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
            "max": max(latencies) if latencies else None,
            "avg": statistics.fmean(latencies) if latencies else None,
        },
        "response_latency_ms": {
            "p50": percentile(response_latencies, 0.50),
            "p95": percentile(response_latencies, 0.95),
            "p99": percentile(response_latencies, 0.99),
            "max": max(response_latencies) if response_latencies else None,
            "avg": statistics.fmean(response_latencies) if response_latencies else None,
        },
        "cpu_pct": {
            "avg": statistics.fmean(monitor.cpu_samples) if monitor.cpu_samples else None,
            "max": max(monitor.cpu_samples) if monitor.cpu_samples else None,
        },
        "rss_mb": {
            "avg": statistics.fmean(monitor.rss_mb_samples) if monitor.rss_mb_samples else None,
            "max": max(monitor.rss_mb_samples) if monitor.rss_mb_samples else None,
        },
        "per_user_completed": {stats.label: stats.completed for stats in user_stats},
    }
    return summary


async def main() -> None:
    parser = argparse.ArgumentParser(description="Hosted app load test driver")
    parser.add_argument("--base-url", default="http://127.0.0.1:8081")
    parser.add_argument("--workload", choices=["similar", "find-items"], required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--interval", type=float, default=2.5)
    parser.add_argument("--idle-users", type=int, default=0)
    parser.add_argument("--server-pid", type=int)
    parser.add_argument("--output")
    args = parser.parse_args()

    async with httpx.AsyncClient(base_url=args.base_url, timeout=10.0) as client:
        health = await client.get("/health")
        health.raise_for_status()
        if args.workload == "find-items" and not health.json().get("test_endpoints_enabled"):
            raise RuntimeError("The server health endpoint reports test endpoints are disabled")

    stop_event = asyncio.Event()
    monitor_task = None
    if args.server_pid:
        monitor_task = asyncio.create_task(monitor_process(args.server_pid, stop_event))

    user_tasks = []
    if args.workload == "similar":
        user_fn = run_similar_user
    else:
        user_fn = run_find_items_user

    for index in range(args.concurrency):
        user_tasks.append(
            asyncio.create_task(
                user_fn(
                    base_url=args.base_url,
                    duration_s=args.duration,
                    interval_s=args.interval,
                    user_index=index,
                    active=True,
                )
            )
        )

    for offset in range(args.idle_users):
        user_tasks.append(
            asyncio.create_task(
                user_fn(
                    base_url=args.base_url,
                    duration_s=args.duration,
                    interval_s=args.interval,
                    user_index=args.concurrency + offset,
                    active=False,
                )
            )
        )

    user_stats = await asyncio.gather(*user_tasks)
    stop_event.set()
    monitor = await monitor_task if monitor_task is not None else MonitorStats()
    summary = summarize_user_stats(user_stats, monitor, args.workload)

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(json.dumps(summary, indent=2) + "\n")

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
