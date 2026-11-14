#!/usr/bin/env python3
"""
Multiplexed Reverse TCP OpenAI Tunnel Agent (Mac mini M4 -> Java Relay)
=======================================================================
Connects outbound to the Java Relay over persistent TCP (bypassing NAT / dynamic IP),
authenticates via HMAC-SHA256 challenge-response, and multiplexes concurrent HTTP + SSE
streaming requests directly to the local `llama-server` (127.0.0.1:8080).

Features:
- Full concurrent multiplexing over a configurable pool of Reverse TCP sockets (default: 2)
- Real-time SSE token-by-token streaming (zero buffering)
- Instant propagation of client abort/stop (`STREAM_CANCEL` -> closes local `llama-server` stream
  immediately so the M4 GPU stops generating abandoned requests!)
- Automatic exponential-backoff reconnect & bidirectional PING/PONG heartbeats
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import platform
import signal
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import httpx

MAGIC = b"ORT1"
FRAME_HDR = struct.Struct(">BII")  # [1B type][4B stream_id][4B length]
MAX_FRAME_PAYLOAD = 16 * 1024 * 1024

TYPE_AUTH_CHALLENGE = 0x01
TYPE_AUTH_RESPONSE  = 0x02
TYPE_AUTH_OK        = 0x03
TYPE_AUTH_FAIL      = 0x04
TYPE_PING           = 0x05
TYPE_PONG           = 0x06

TYPE_HTTP_REQ_META  = 0x10
TYPE_HTTP_REQ_BODY  = 0x11
TYPE_HTTP_REQ_END   = 0x12

TYPE_HTTP_RES_META  = 0x20
TYPE_HTTP_RES_BODY  = 0x21
TYPE_HTTP_RES_END   = 0x22
TYPE_STREAM_CANCEL  = 0x23

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mac-tunnel-agent")


@dataclass
class AgentConfig:
    relay_host: str
    relay_port: int
    tunnel_secret: str
    llama_base_url: str = "http://127.0.0.1:8080"
    agent_name: str = field(default_factory=lambda: platform.node() or "mac-mini-m4")
    connections: int = 2
    llama_api_key: str = ""


@dataclass
class ActiveStream:
    stream_id: int
    method: str
    path: str
    headers: Dict[str, str]
    body_queue: asyncio.Queue[Optional[bytes]] = field(default_factory=asyncio.Queue)
    task: Optional[asyncio.Task] = None


class ReverseTunnelWorker:
    """Manages one persistent multiplexed Reverse TCP connection to the Java Relay."""

    def __init__(self, worker_id: int, cfg: AgentConfig, http_client: httpx.AsyncClient):
        self.worker_id = worker_id
        self.cfg = cfg
        self.http_client = http_client
        self.writer: Optional[asyncio.StreamWriter] = None
        self.write_lock = asyncio.Lock()
        self.streams: Dict[int, ActiveStream] = {}
        self._closed = False

    async def send_frame(self, frame_type: int, stream_id: int, payload: bytes = b"") -> None:
        if self.writer is None or self._closed:
            return
        header = FRAME_HDR.pack(frame_type, stream_id, len(payload))
        async with self.write_lock:
            if self.writer is None or self._closed:
                return
            self.writer.write(header)
            if payload:
                self.writer.write(payload)
            await self.writer.drain()

    async def read_frame(self, reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
        hdr_bytes = await reader.readexactly(FRAME_HDR.size)
        frame_type, stream_id, length = FRAME_HDR.unpack(hdr_bytes)
        if length > MAX_FRAME_PAYLOAD:
            raise ValueError(f"Frame payload too large: {length}")
        payload = await reader.readexactly(length) if length > 0 else b""
        return frame_type, stream_id, payload

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        backoff = 1.0
        tag = f"[Worker-{self.worker_id}]"

        while not stop_event.is_set():
            self._closed = False
            try:
                log.info(
                    "%s Connecting to Java Relay %s:%d ...",
                    tag,
                    self.cfg.relay_host,
                    self.cfg.relay_port,
                )
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.cfg.relay_host, self.cfg.relay_port),
                    timeout=15.0,
                )
                sock = writer.get_extra_info("socket")
                if sock is not None:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

                self.writer = writer

                # 1. Send magic bytes + authenticate
                writer.write(MAGIC)
                await writer.drain()

                ftype, _, challenge = await asyncio.wait_for(self.read_frame(reader), timeout=15.0)
                if ftype != TYPE_AUTH_CHALLENGE:
                    raise RuntimeError(f"Expected AUTH_CHALLENGE (0x01), got 0x{ftype:02x}")

                digest = hmac.new(
                    self.cfg.tunnel_secret.encode("utf-8"),
                    challenge,
                    hashlib.sha256,
                ).hexdigest()
                auth_payload = json.dumps(
                    {"hmac": digest, "agent": f"{self.cfg.agent_name}-w{self.worker_id}"}
                ).encode("utf-8")
                await self.send_frame(TYPE_AUTH_RESPONSE, 0, auth_payload)

                ftype, _, auth_reply = await asyncio.wait_for(self.read_frame(reader), timeout=15.0)
                if ftype == TYPE_AUTH_FAIL:
                    reason = auth_reply.decode("utf-8", errors="replace")
                    log.error("%s Authentication REJECTED by relay: %s", tag, reason)
                    await asyncio.sleep(10.0)
                    continue
                if ftype != TYPE_AUTH_OK:
                    raise RuntimeError(f"Unexpected auth reply frame: 0x{ftype:02x}")

                log.info("%s Tunnel connected & authenticated! Forwarding -> %s", tag, self.cfg.llama_base_url)
                backoff = 1.0  # reset backoff on healthy session

                heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                try:
                    await self._dispatch_loop(reader)
                finally:
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not stop_event.is_set():
                    log.warning("%s Disconnected (%s). Reconnecting in %.1fs...", tag, exc, backoff)
            finally:
                await self._cleanup_connection()

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                backoff = min(backoff * 1.8, 15.0)

    async def _heartbeat_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(20.0)
            ts_bytes = struct.pack(">Q", int(time.time() * 1000))
            await self.send_frame(TYPE_PING, 0, ts_bytes)

    async def _dispatch_loop(self, reader: asyncio.StreamReader) -> None:
        while not self._closed:
            ftype, stream_id, payload = await asyncio.wait_for(self.read_frame(reader), timeout=65.0)

            if ftype == TYPE_PING:
                await self.send_frame(TYPE_PONG, 0, payload)
            elif ftype == TYPE_PONG:
                continue
            elif ftype == TYPE_HTTP_REQ_META:
                meta = json.loads(payload.decode("utf-8"))
                method = meta.get("method", "GET")
                path = meta.get("path", "/")
                headers = meta.get("headers") or {}

                stream = ActiveStream(stream_id=stream_id, method=method, path=path, headers=headers)
                self.streams[stream_id] = stream
                stream.task = asyncio.create_task(self._proxy_to_llama(stream))

            elif ftype == TYPE_HTTP_REQ_BODY:
                stream = self.streams.get(stream_id)
                if stream is not None:
                    await stream.body_queue.put(payload)

            elif ftype == TYPE_HTTP_REQ_END:
                stream = self.streams.get(stream_id)
                if stream is not None:
                    await stream.body_queue.put(None)

            elif ftype == TYPE_STREAM_CANCEL:
                stream = self.streams.pop(stream_id, None)
                if stream is not None and stream.task is not None:
                    log.info("[Stream %d] Cancelled by client/relay — aborting llama-server generation", stream_id)
                    stream.task.cancel()

    async def _req_body_iterator(self, queue: asyncio.Queue[Optional[bytes]]):
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk

    async def _proxy_to_llama(self, stream: ActiveStream) -> None:
        url = self.cfg.llama_base_url.rstrip("/") + (
            stream.path if stream.path.startswith("/") else "/" + stream.path
        )
        req_headers = {
            k: v
            for k, v in stream.headers.items()
            if k.lower() not in HOP_BY_HOP_HEADERS
        }
        if self.cfg.llama_api_key:
            req_headers["Authorization"] = f"Bearer {self.cfg.llama_api_key}"

        try:
            req = self.http_client.build_request(
                method=stream.method,
                url=url,
                headers=req_headers,
                content=self._req_body_iterator(stream.body_queue),
            )
            resp = await self.http_client.send(req, stream=True)
            try:
                resp_headers = {
                    k: v
                    for k, v in resp.headers.items()
                    if k.lower() not in HOP_BY_HOP_HEADERS
                }
                res_meta = json.dumps(
                    {"status": resp.status_code, "headers": resp_headers}
                ).encode("utf-8")
                await self.send_frame(TYPE_HTTP_RES_META, stream.stream_id, res_meta)

                # Stream raw bytes immediately (every SSE token chunk is flushed right away!)
                async for chunk in resp.aiter_raw():
                    if not chunk:
                        continue
                    # Slice if chunk > 256KB to keep multiplexing responsive across concurrent streams
                    for offset in range(0, len(chunk), 262_144):
                        part = chunk[offset : offset + 262_144]
                        await self.send_frame(TYPE_HTTP_RES_BODY, stream.stream_id, part)

                await self.send_frame(TYPE_HTTP_RES_END, stream.stream_id, b"")
            finally:
                await resp.aclose()

        except asyncio.CancelledError:
            # Propagates cancellation cleanly to httpx -> closes TCP socket to llama-server,
            # which immediately stops LLM token generation on the Mac mini GPU!
            raise
        except Exception as exc:
            log.error("[Stream %d] Error contacting local llama-server (%s): %s", stream.stream_id, url, exc)
            err_msg = f"Local llama-server error: {exc}".encode("utf-8")
            try:
                await self.send_frame(TYPE_STREAM_CANCEL, stream.stream_id, err_msg)
            except Exception:
                pass
        finally:
            self.streams.pop(stream.stream_id, None)

    async def _cleanup_connection(self) -> None:
        self._closed = True
        for st in list(self.streams.values()):
            if st.task and not st.task.done():
                st.task.cancel()
        self.streams.clear()

        if self.writer is not None:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            self.writer = None


async def async_main(cfg: AgentConfig) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    limits = httpx.Limits(max_keepalive_connections=32, max_connections=64)
    timeout = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=30.0)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as http_client:
        workers = [
            ReverseTunnelWorker(i + 1, cfg, http_client)
            for i in range(max(1, cfg.connections))
        ]
        tasks = [asyncio.create_task(w.run_forever(stop_event)) for w in workers]
        await stop_event.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def parse_args() -> AgentConfig:
    p = argparse.ArgumentParser(description="Mac mini M4 Reverse TCP OpenAI Tunnel Agent")
    p.add_argument(
        "--relay-host",
        default=os.environ.get("RELAY_HOST", ""),
        help="Public IP or domain of the Java Relay VPS (env: RELAY_HOST)",
    )
    p.add_argument(
        "--relay-port",
        type=int,
        default=int(os.environ.get("RELAY_PORT", "9009")),
        help="Reverse TCP port on the Java Relay VPS (env: RELAY_PORT, default: 9009)",
    )
    p.add_argument(
        "--tunnel-secret",
        default=os.environ.get("TUNNEL_SECRET", ""),
        help="Shared HMAC secret matching Java Relay TUNNEL_SECRET (env: TUNNEL_SECRET)",
    )
    p.add_argument(
        "--llama-url",
        default=os.environ.get("LLAMA_URL", "http://127.0.0.1:8080"),
        help="Local llama-server base URL (env: LLAMA_URL, default: http://127.0.0.1:8080)",
    )
    p.add_argument(
        "--agent-name",
        default=os.environ.get("AGENT_NAME", platform.node() or "mac-mini-m4"),
        help="Identifier logged on the Java Relay",
    )
    p.add_argument(
        "--connections",
        type=int,
        default=int(os.environ.get("TUNNEL_CONNECTIONS", "2")),
        help="Number of parallel multiplexed Reverse TCP connections in the pool (default: 2)",
    )
    p.add_argument(
        "--llama-api-key",
        default=os.environ.get("LLAMA_API_KEY", ""),
        help="Optional local llama-server API key if set",
    )
    args = p.parse_args()

    if not args.relay_host:
        p.error("--relay-host (or RELAY_HOST env var) is required")
    if not args.tunnel_secret:
        p.error("--tunnel-secret (or TUNNEL_SECRET env var) is required")

    return AgentConfig(
        relay_host=args.relay_host,
        relay_port=args.relay_port,
        tunnel_secret=args.tunnel_secret,
        llama_base_url=args.llama_url,
        agent_name=args.agent_name,
        connections=args.connections,
        llama_api_key=args.llama_api_key,
    )


if __name__ == "__main__":
    config = parse_args()
    try:
        asyncio.run(async_main(config))
    except KeyboardInterrupt:
        sys.exit(0)
