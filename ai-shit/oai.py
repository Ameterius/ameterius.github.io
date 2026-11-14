#!/usr/bin/env python3
"""
Серверная часть OpenAI-совместимого туннеля (Reverse TCP).

Запускается рядом с моделью (Ollama, vLLM, llama.cpp, LM Studio ...), САМ подключается
к релею (Relay.java) и выполняет запросы, которые релей получает от клиентов:

    клиент --HTTP--> [Relay.java] <==reverse TCP== [server.py] --HTTP--> локальный OpenAI API

Только стандартная библиотека, Python 3.8+.
Запуск:  python3 server.py            (читает server.ini рядом со скриптом)
         python3 server.py -c my.ini
"""
import argparse
import configparser
import hashlib
import hmac
import http.client
import logging
import os
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

# ---- типы кадров (должны совпадать с Relay.java) ----
# кадр: [uint32 длина = 5 + N][uint8 тип][uint32 id потока][N байт данных], big-endian
T_HELLO, T_AUTH, T_AUTH_OK = 1, 2, 3
T_REQ_HEAD, T_REQ_BODY, T_REQ_END = 10, 11, 12
T_RES_HEAD, T_RES_BODY, T_RES_END = 20, 21, 22
T_RST, T_PING, T_PONG = 30, 40, 41

MAX_FRAME = 1 << 20
CHUNK = 32 * 1024
PING_INTERVAL = 15
READ_TIMEOUT = 60
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length", "host",
}

log = logging.getLogger("tunnel")


class AuthError(Exception):
    pass


class Config:
    def __init__(self, path):
        cp = configparser.ConfigParser(interpolation=None)
        if not cp.read(path, encoding="utf-8"):
            raise SystemExit(f"Конфиг не найден: {path}")
        t, u = cp["tunnel"], cp["upstream"]
        self.host = t.get("host", "127.0.0.1").strip()
        self.port = t.getint("port", 7000)
        self.key = t.get("api_key", "").strip().encode("utf-8")
        self.tls = t.getboolean("tls", False)
        self.tls_verify = t.getboolean("tls_verify", True)
        self.tls_ca_file = t.get("tls_ca_file", "").strip()
        self.connections = max(1, t.getint("connections", 1))

        self.base_url = u.get("base_url", "http://127.0.0.1:11434").strip().rstrip("/")
        self.upstream_key = u.get("api_key", "").strip()
        self.upstream_timeout = u.getint("timeout_sec", 600)
        self.max_concurrency = max(1, u.getint("max_concurrency", 64))
        self.max_body = u.getint("max_body_mb", 64) * 1024 * 1024

        if not self.key or self.key.startswith(b"CHANGE_ME"):
            raise SystemExit("[tunnel] api_key не задан (в конфиге осталась заглушка)")
        url = urllib.parse.urlsplit(self.base_url)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise SystemExit("[upstream] base_url должен быть вида http://host:port")
        self.up_scheme, self.up_host = url.scheme, url.hostname
        self.up_port = url.port or (443 if url.scheme == "https" else 80)
        self.up_prefix = url.path.rstrip("/")


class Stream:
    """Один HTTP-запрос клиента, идущий через туннель."""

    def __init__(self, sid, head):
        self.sid = sid
        self.head = head
        self.body = bytearray()
        self.cancelled = False
        self.conn = None

    def cancel(self):
        self.cancelled = True
        conn = self.conn
        if conn is not None:
            try:
                sock = conn.sock
                if sock is not None:
                    sock.shutdown(socket.SHUT_RDWR)  # разблокирует чтение ответа модели
            except OSError:
                pass


class Session:
    """Одно reverse-TCP подключение к релею."""

    def __init__(self, cfg, pool, name):
        self.cfg, self.pool, self.name = cfg, pool, name
        self.sock = None
        self.wlock = threading.Lock()
        self.streams = {}
        self.closed = threading.Event()

    # ---------- кадры ----------
    def send(self, ftype, sid, payload=b""):
        data = struct.pack(">IBI", 5 + len(payload), ftype, sid) + payload
        with self.wlock:
            self.sock.sendall(data)

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            part = self.sock.recv(n - len(buf))
            if not part:
                raise ConnectionError("релей закрыл соединение")
            buf += part
        return bytes(buf)

    def recv(self):
        length, ftype, sid = struct.unpack(">IBI", self._recv_exact(9))
        if length < 5 or length - 5 > MAX_FRAME:
            raise ConnectionError(f"некорректная длина кадра {length}")
        return ftype, sid, self._recv_exact(length - 5)

    # ---------- жизненный цикл ----------
    def run(self):
        cfg = self.cfg
        raw = socket.create_connection((cfg.host, cfg.port), timeout=10)
        try:
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if cfg.tls:
                ctx = ssl.create_default_context(cafile=cfg.tls_ca_file or None)
                if not cfg.tls_verify:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                raw = ctx.wrap_socket(raw, server_hostname=cfg.host)
            self.sock = raw
            self.sock.settimeout(10)
            self._handshake()
            self.sock.settimeout(READ_TIMEOUT)
            log.info("[%s] туннель установлен: %s:%d", self.name, cfg.host, cfg.port)
            threading.Thread(target=self._pinger, daemon=True).start()
            self._read_loop()
        finally:
            self.closed.set()
            try:
                self.sock.close() if self.sock else raw.close()
            except OSError:
                pass
            for st in list(self.streams.values()):
                st.cancel()
            self.streams.clear()

    def _handshake(self):
        """Взаимная аутентификация: ключ не передаётся, обе стороны доказывают, что знают его."""
        key = self.cfg.key
        try:
            ftype, _, nonce_r = self.recv()
            if ftype != T_HELLO or len(nonce_r) != 32:
                raise ConnectionError("неожиданное приветствие релея")
            nonce_s = os.urandom(32)
            self.send(T_AUTH, 0, nonce_s + hmac.new(key, b"server" + nonce_r + nonce_s, hashlib.sha256).digest())
            ftype, _, mac = self.recv()
        except (ConnectionError, socket.timeout) as e:
            raise AuthError(f"рукопожатие не удалось ({e}) - скорее всего, неверный [tunnel] api_key") from e
        expected = hmac.new(key, b"relay" + nonce_s + nonce_r, hashlib.sha256).digest()
        if ftype != T_AUTH_OK or not hmac.compare_digest(mac, expected):
            raise AuthError("релей не подтвердил знание ключа (подмена релея?)")

    def _pinger(self):
        while not self.closed.wait(PING_INTERVAL):
            try:
                self.send(T_PING, 0)
            except OSError:
                return

    def _read_loop(self):
        while True:
            ftype, sid, data = self.recv()
            if ftype == T_REQ_HEAD:
                self.streams[sid] = Stream(sid, data.decode("utf-8", "replace"))
            elif ftype == T_REQ_BODY:
                st = self.streams.get(sid)
                if st is not None:
                    st.body += data
                    if len(st.body) > self.cfg.max_body:
                        self.streams.pop(sid, None)
                        self.send(T_RST, sid, b"request body too large")
            elif ftype == T_REQ_END:
                st = self.streams.get(sid)
                if st is not None:
                    self.pool.submit(self._handle, st)
            elif ftype == T_RST:
                st = self.streams.pop(sid, None)
                if st is not None:
                    log.info("[%s] #%d отменён релеем: %s", self.name, sid, data.decode("utf-8", "replace"))
                    st.cancel()
            # T_PONG и неизвестные кадры игнорируем

    # ---------- выполнение запроса к модели ----------
    def _handle(self, st):
        cfg = self.cfg
        conn = None
        started = time.time()
        status = "-"
        try:
            lines = st.head.split("\r\n")
            method, target = lines[0].split(" ", 1)
            if cfg.up_scheme == "https":
                conn = http.client.HTTPSConnection(cfg.up_host, cfg.up_port, timeout=cfg.upstream_timeout)
            else:
                conn = http.client.HTTPConnection(cfg.up_host, cfg.up_port, timeout=cfg.upstream_timeout)
            st.conn = conn
            if st.cancelled:
                return

            conn.putrequest(method, cfg.up_prefix + target, skip_accept_encoding=True)
            for line in lines[1:]:
                name, sep, value = line.partition(": ")
                if not sep or name.lower() in HOP_HEADERS or name.lower() == "authorization":
                    continue
                conn.putheader(name, value)
            if cfg.upstream_key:
                conn.putheader("Authorization", "Bearer " + cfg.upstream_key)
            body = bytes(st.body)
            if body or method in ("POST", "PUT", "PATCH"):
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body or None)

            resp = conn.getresponse()
            status = resp.status
            head = [f"{resp.status} {resp.reason}"]
            head += [f"{k}: {v}" for k, v in resp.getheaders() if k.lower() not in HOP_HEADERS]
            self.send(T_RES_HEAD, st.sid, "\r\n".join(head).encode("utf-8"))

            while True:
                chunk = resp.read1(CHUNK)  # отдаёт данные сразу, как только пришли (SSE-стриминг)
                if not chunk:
                    break
                self.send(T_RES_BODY, st.sid, chunk)
            self.send(T_RES_END, st.sid)
        except Exception as e:  # noqa: BLE001
            if st.cancelled:
                status = "cancelled"
            else:
                status = f"error: {e}"
                try:
                    self.send(T_RST, st.sid, f"{type(e).__name__}: {e}".encode("utf-8")[:1000])
                except OSError:
                    pass
        finally:
            if st.cancelled and status != "cancelled" and not str(status).startswith("error"):
                status = "cancelled"
            self.streams.pop(st.sid, None)
            if conn is not None:
                conn.close()
            log.info("[%s] #%d %s -> %s (%.2fs)", self.name, st.sid, st.head.split("\r\n", 1)[0], status,
                     time.time() - started)


def worker(cfg, pool, name):
    """Держит одно подключение к релею, переподключается при обрыве."""
    delay = 1
    while True:
        started = time.time()
        try:
            Session(cfg, pool, name).run()
        except AuthError as e:
            log.error("[%s] аутентификация: %s", name, e)
        except (OSError, ConnectionError, ssl.SSLError) as e:
            log.warning("[%s] соединение потеряно: %s", name, e)
        except Exception:  # noqa: BLE001
            log.exception("[%s] непредвиденная ошибка", name)
        if time.time() - started > 30:
            delay = 1
        log.info("[%s] переподключение через %d с", name, delay)
        time.sleep(delay)
        delay = min(delay * 2, 30)


def main():
    default_cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.ini")
    ap = argparse.ArgumentParser(description="OpenAI tunnel server (reverse TCP)")
    ap.add_argument("-c", "--config", default=default_cfg, help="путь к server.ini")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config(args.config)
    log.info("релей: %s:%d%s, модель: %s", cfg.host, cfg.port, " (TLS)" if cfg.tls else "", cfg.base_url)

    pool = ThreadPoolExecutor(max_workers=cfg.max_concurrency, thread_name_prefix="req")
    threads = [threading.Thread(target=worker, args=(cfg, pool, f"conn{i + 1}"), daemon=True)
               for i in range(cfg.connections)]
    for t in threads:
        t.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("остановка")


if __name__ == "__main__":
    main()
