#!/usr/bin/env python3
"""
All-in-One Mac mini M4 AI Daemon:
  1. Reads `agent.ini` config file (creates template on 1st run if missing)
  2. Keeps macOS awake via `caffeinate` (optional, enabled by default on Darwin)
  3. Automatically spawns, monitors, and auto-restarts `llama-server` (Apple Metal)
  4. Waits for `/health` readiness, then maintains a pool of multiplexed Reverse TCP
     connections to the Java Relay (with real-time SSE streaming & cancel propagation)
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import hashlib
import hmac
import json
import logging
import os
import platform
import shlex
import shutil
import signal
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

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
log = logging.getLogger("mac-ai-daemon")


@dataclass
class AgentConfig:
    # Relay settings
    relay_host: str
    relay_port: int
    tunnel_secret: str
    agent_name: str = field(default_factory=lambda: platform.node() or "mac-mini-m4")
    connections: int = 2

    # llama-server process supervisor settings
    llama_auto_start: bool = True
    llama_binary: str = "/opt/homebrew/bin/llama-server"
    llama_model_path: str = ""
    llama_host: str = "127.0.0.1"
    llama_port: int = 8080
    llama_ctx_size: int = 32768
    llama_parallel_slots: int = 2
    llama_gpu_layers: int = 99
    llama_flash_attn: str = "on"
    llama_kv_cache_type: str = "q8_0"
    llama_extra_args: str = "--jinja -cb"
    llama_api_key: str = ""
    llama_log_file: str = ""
    prevent_sleep: bool = True

    @property
    def llama_base_url(self) -> str:
        return f"http://{self.llama_host}:{self.llama_port}"


@dataclass
class ActiveStream:
    stream_id: int
    method: str
    path: str
    headers: Dict[str, str]
    body_queue: asyncio.Queue[Optional[bytes]] = field(default_factory=asyncio.Queue)
    task: Optional[asyncio.Task] = None


class LlamaSupervisor:
    """
    Spawns and supervises `llama-server` (plus `caffeinate` on macOS so the Mac never sleeps).
    Automatically restarts `llama-server` if it crashes or becomes unresponsive.
    """

    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.caffeinate_proc: Optional[asyncio.subprocess.Process] = None
        self.ready_event = asyncio.Event()

    def _resolve_binary(self) -> str:
        candidates = [
            self.cfg.llama_binary,
            shutil.which("llama-server") or "",
            "/opt/homebrew/bin/llama-server",
            "/usr/local/bin/llama-server",
        ]
        for c in candidates:
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        raise FileNotFoundError(
            f"llama-server binary not found (checked {self.cfg.llama_binary} and PATH). "
            "Install via: brew install llama.cpp"
        )

    def _build_cmd(self) -> List[str]:
        binary = self._resolve_binary()
        if not self.cfg.llama_model_path or not os.path.isfile(self.cfg.llama_model_path):
            raise FileNotFoundError(
                f"GGUF model file not found: '{self.cfg.llama_model_path}'. "
                "Check 'model_path' in agent.ini!"
            )

        cmd = [
            binary,
            "-m", self.cfg.llama_model_path,
            "-ngl", str(self.cfg.llama_gpu_layers),
            "-c", str(self.cfg.llama_ctx_size),
            "-np", str(self.cfg.llama_parallel_slots),
            "--host", self.cfg.llama_host,
            "--port", str(self.cfg.llama_port),
        ]
        if self.cfg.llama_flash_attn:
            cmd.extend(["-fa", self.cfg.llama_flash_attn])
        if self.cfg.llama_kv_cache_type:
            cmd.extend(["-ctk", self.cfg.llama_kv_cache_type, "-ctv", self.cfg.llama_kv_cache_type])
        if self.cfg.llama_api_key:
            cmd.extend(["--api-key", self.cfg.llama_api_key])
        if self.cfg.llama_extra_args:
            cmd.extend(shlex.split(self.cfg.llama_extra_args))
        return cmd

    async def _start_caffeinate(self) -> None:
        if not self.cfg.prevent_sleep or platform.system() != "Darwin":
            return
        caffeinate_bin = shutil.which("caffeinate") or "/usr/bin/caffeinate"
        if os.path.isfile(caffeinate_bin):
            try:
                self.caffeinate_proc = await asyncio.create_subprocess_exec(
                    caffeinate_bin, "-dimsu", "-w", str(os.getpid())
                )
                log.info("[Supervisor] macOS caffeinate active (preventing sleep 24/7)")
            except Exception as exc:
                log.warning("[Supervisor] Could not start caffeinate: %s", exc)

    async def _is_port_already_healthy(self, client: httpx.AsyncClient) -> bool:
        try:
            r = await client.get(f"{self.cfg.llama_base_url}/health", timeout=2.0)
            return r.status_code == 200
        except Exception:
            return False

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        if not self.cfg.llama_auto_start:
            log.info("[Supervisor] llama.auto_start=false -> using external LLM at %s", self.cfg.llama_base_url)
            self.ready_event.set()
            await stop_event.wait()
            return

        await self._start_caffeinate()

        log_fp = None
        if self.cfg.llama_log_file:
            Path(self.cfg.llama_log_file).parent.mkdir(parents=True, exist_ok=True)
            log_fp = open(self.cfg.llama_log_file, "ab", buffering=0)

        try:
            async with httpx.AsyncClient() as probe_client:
                while not stop_event.is_set():
                    # If an external llama-server is already healthy on that port, adopt & monitor it
                    if self.proc is None and await self._is_port_already_healthy(probe_client):
                        if not self.ready_event.is_set():
                            log.info("[Supervisor] Detected healthy llama-server already running at %s", self.cfg.llama_base_url)
                            self.ready_event.set()
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                        break

                    try:
                        cmd = self._build_cmd()
                    except Exception as cfg_err:
                        log.error("[Supervisor] %s", cfg_err)
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=10.0)
                        except asyncio.TimeoutError:
                            continue
                        break

                    log.info("[Supervisor] Launching llama-server: %s", " ".join(shlex.quote(x) for x in cmd))
                    stdout_target = log_fp if log_fp is not None else sys.stdout.buffer
                    stderr_target = log_fp if log_fp is not None else sys.stderr.buffer

                    self.proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=stdout_target,
                        stderr=stderr_target,
                    )

                    # Poll /health while loading weights into Apple Metal GPU
                    became_healthy = False
                    for _ in range(90):
                        if stop_event.is_set() or self.proc.returncode is not None:
                            break
                        if await self._is_port_already_healthy(probe_client):
                            became_healthy = True
                            self.ready_event.set()
                            log.info("[Supervisor] llama-server (PID %d) is UP and healthy at %s!", self.proc.pid, self.cfg.llama_base_url)
                            break
                        await asyncio.sleep(1.5)

                    if not became_healthy and not stop_event.is_set() and self.proc.returncode is None:
                        log.warning("[Supervisor] llama-server still loading model (continuing to monitor PID %d)...", self.proc.pid)
                        self.ready_event.set()

                    # Wait until either llama-server exits unexpectedly OR daemon is stopping
                    proc_wait = asyncio.create_task(self.proc.wait())
                    stop_wait = asyncio.create_task(stop_event.wait())
                    done, pending = await asyncio.wait(
                        {proc_wait, stop_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for p in pending:
                        p.cancel()

                    if stop_event.is_set():
                        break

                    rc = self.proc.returncode
                    self.proc = None
                    log.error("[Supervisor] llama-server exited unexpectedly (code=%s)! Restarting in 3s...", rc)
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        pass
        finally:
            await self.stop()
            if log_fp is not None:
                log_fp.close()

    async def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            log.info("[Supervisor] Stopping llama-server (PID %d)...", self.proc.pid)
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=8.0)
            except asyncio.TimeoutError:
                self.proc.kill()
            self.proc = None

        if self.caffeinate_proc is not None and self.caffeinate_proc.returncode is None:
            self.caffeinate_proc.terminate()
            self.caffeinate_proc = None


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

    async def run_forever(self, stop_event: asyncio.Event, llama_ready: asyncio.Event) -> None:
        await llama_ready.wait()
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
                backoff = 1.0

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

                async for chunk in resp.aiter_raw():
                    if not chunk:
                        continue
                    for offset in range(0, len(chunk), 262_144):
                        part = chunk[offset : offset + 262_144]
                        await self.send_frame(TYPE_HTTP_RES_BODY, stream.stream_id, part)

                await self.send_frame(TYPE_HTTP_RES_END, stream.stream_id, b"")
            finally:
                await resp.aclose()

        except asyncio.CancelledError:
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

    supervisor = LlamaSupervisor(cfg)
    supervisor_task = asyncio.create_task(supervisor.run_forever(stop_event))

    limits = httpx.Limits(max_keepalive_connections=32, max_connections=64)
    timeout = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=30.0)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as http_client:
        workers = [
            ReverseTunnelWorker(i + 1, cfg, http_client)
            for i in range(max(1, cfg.connections))
        ]
        worker_tasks = [
            asyncio.create_task(w.run_forever(stop_event, supervisor.ready_event))
            for w in workers
        ]
        await stop_event.wait()
        for t in worker_tasks:
            t.cancel()
        await asyncio.gather(*worker_tasks, supervisor_task, return_exceptions=True)


def write_default_ini(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# ==============================================================================\n"
        "# Mac mini M4 — Reverse TCP Agent + Auto-Managed llama-server Config\n"
        "# ==============================================================================\n\n"
        "[relay]\n"
        "host = YOUR_VPS_IP_OR_DOMAIN\n"
        "port = 9009\n"
        "secret = CHANGE_ME_TUNNEL_SECRET\n"
        "connections = 2\n"
        f"agent_name = {platform.node() or 'mac-mini-m4'}\n\n"
        "[llama]\n"
        "auto_start = true\n"
        "binary = /opt/homebrew/bin/llama-server\n"
        f"model_path = {Path.home()}/ai/models/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic.i1-Q6_K.gguf\n"
        "host = 127.0.0.1\n"
        "port = 8080\n"
        "ctx_size = 32768\n"
        "parallel_slots = 2\n"
        "gpu_layers = 99\n"
        "flash_attn = on\n"
        "kv_cache_type = q8_0\n"
        "extra_args = --jinja -cb\n"
        f"log_file = {Path.home()}/ai/logs/llama.log\n"
        "prevent_sleep = true\n",
        encoding="utf-8",
    )


def load_config() -> AgentConfig:
    p = argparse.ArgumentParser(description="Mac mini M4 All-in-One LLM + Reverse TCP Daemon")
    p.add_argument("-c", "--config", default=os.environ.get("AGENT_CONFIG", "agent.ini"), help="Path to agent.ini")
    p.add_argument("--relay-host", default="")
    p.add_argument("--relay-port", type=int, default=0)
    p.add_argument("--tunnel-secret", default="")
    p.add_argument("--llama-url", default="", help="Override external llama URL (disables auto_start unless --auto-start-llama)")
    p.add_argument("--connections", type=int, default=0)
    args = p.parse_args()

    cfg_path = Path(args.config)
    cp = configparser.ConfigParser()

    has_cli_or_env = bool(
        (args.relay_host or os.environ.get("RELAY_HOST"))
        and (args.tunnel_secret or os.environ.get("TUNNEL_SECRET"))
    )

    if cfg_path.is_file():
        cp.read(cfg_path, encoding="utf-8")
    elif not has_cli_or_env:
        write_default_ini(cfg_path)
        sys.stderr.write(
            f"\n[CONFIG] Created template config at: {cfg_path.resolve()}\n"
            "Edit [relay] host/secret and [llama] model_path, then run again!\n\n"
        )
        sys.exit(1)

    relay_sec = cp["relay"] if "relay" in cp else {}
    llama_sec = cp["llama"] if "llama" in cp else {}

    relay_host = args.relay_host or os.environ.get("RELAY_HOST") or relay_sec.get("host", "")
    relay_port = args.relay_port or int(os.environ.get("RELAY_PORT") or relay_sec.get("port", "9009"))
    tunnel_secret = args.tunnel_secret or os.environ.get("TUNNEL_SECRET") or relay_sec.get("secret", "")
    connections = args.connections or int(os.environ.get("TUNNEL_CONNECTIONS") or relay_sec.get("connections", "2"))
    agent_name = os.environ.get("AGENT_NAME") or relay_sec.get("agent_name", platform.node() or "mac-mini-m4")

    if not relay_host or relay_host == "YOUR_VPS_IP_OR_DOMAIN":
        sys.exit("Error: Please set [relay] host in agent.ini (or --relay-host)")
    if not tunnel_secret or tunnel_secret == "CHANGE_ME_TUNNEL_SECRET":
        sys.exit("Error: Please set [relay] secret in agent.ini (or --tunnel-secret)")

    # Parse llama settings
    auto_start_str = os.environ.get("LLAMA_AUTO_START", llama_sec.get("auto_start", "true")).lower()
    llama_auto_start = auto_start_str in ("1", "true", "yes", "on")

    llama_host = llama_sec.get("host", "127.0.0.1")
    llama_port = int(llama_sec.get("port", "8080"))

    llama_url_override = args.llama_url or os.environ.get("LLAMA_URL", "")
    if llama_url_override:
        # e.g. http://127.0.0.1:18080 -> used when connecting to an already-running server
        from urllib.parse import urlparse
        parsed = urlparse(llama_url_override)
        if parsed.hostname:
            llama_host = parsed.hostname
        if parsed.port:
            llama_port = parsed.port
        if "auto_start" not in llama_sec and not os.environ.get("LLAMA_AUTO_START"):
            llama_auto_start = False

    prevent_sleep_str = llama_sec.get("prevent_sleep", "true").lower()

    return AgentConfig(
        relay_host=relay_host.strip(),
        relay_port=relay_port,
        tunnel_secret=tunnel_secret.strip(),
        agent_name=agent_name.strip(),
        connections=connections,
        llama_auto_start=llama_auto_start,
        llama_binary=llama_sec.get("binary", "/opt/homebrew/bin/llama-server").strip(),
        llama_model_path=os.path.expanduser(llama_sec.get("model_path", "").strip()),
        llama_host=llama_host,
        llama_port=llama_port,
        llama_ctx_size=int(llama_sec.get("ctx_size", "32768")),
        llama_parallel_slots=int(llama_sec.get("parallel_slots", "2")),
        llama_gpu_layers=int(llama_sec.get("gpu_layers", "99")),
        llama_flash_attn=llama_sec.get("flash_attn", "on").strip(),
        llama_kv_cache_type=llama_sec.get("kv_cache_type", "q8_0").strip(),
        llama_extra_args=llama_sec.get("extra_args", "--jinja -cb").strip(),
        llama_api_key=llama_sec.get("api_key", "").strip(),
        llama_log_file=os.path.expanduser(llama_sec.get("log_file", "").strip()),
        prevent_sleep=prevent_sleep_str in ("1", "true", "yes", "on"),
    )


if __name__ == "__main__":
    config = load_config()
    try:
        asyncio.run(async_main(config))
    except KeyboardInterrupt:
        sys.exit(0)
