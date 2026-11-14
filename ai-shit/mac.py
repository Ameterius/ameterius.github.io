#!/usr/bin/env python3
"""mac_agent.py — видимая консоль для удалённой помощи (как SSH, только через релей).

Настройки — в relay.cfg (RELAY_CFG → ./relay.cfg → ~/.relay.cfg).
Зависимостей нет, только стандартная библиотека Python 3.

Что происходит:
  1. Открывается ОДИН настоящий интерактивный шелл в псевдотерминале.
  2. Видимое окно (если [ui] gui = yes): владелец видит свой шелл и всё,
     что делает помощник, и может работать сам.
  3. Вывод шелла идёт через Java-релей второй стороне.
  4. Обрыв связи НЕ убивает шелл: агент молча переподключается с backoff,
     сессия продолжается с того же места.
  5. Закрыл окно / нажал СТОП — сессия закончилась. Автозапуска нет.

Запуск:
    python3 mac_agent.py
"""

import fcntl
import json
import os
import pty
import random
import re
import select
import signal
import socket
import struct
import sys
import termios
import threading
import time

from relcfg import Config

# Убираем ANSI-последовательности, оставляем переводы строк и табы
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean(text):
    return CTRL_RE.sub("", ANSI_RE.sub("", text))


# =====================================================================
#  Link — соединение с релеем: keepalive, пинг, автопереподключение.
#  Одинаковый код в агенте и контроллере намеренно: каждый файл
#  самодостаточен, общих модулей кроме relcfg.py нет.
# =====================================================================
class Link:
    PING_EVERY = 15          # секунд между пингами (держит NAT/файрвол живым)
    DEAD_AFTER = 70          # если столько секунд нет данных — канал считаем мёртвым
    CONNECT_TIMEOUT = 10     # таймаут ТОЛЬКО на установку соединения

    def __init__(self, cfg, log, on_message, on_state=None, peer_label="вторая сторона"):
        self.cfg = cfg
        self.log = log
        self.on_message = on_message
        self.on_state = on_state or (lambda state: None)
        self.peer_label = peer_label

        self.room = cfg.get("client.room", "")
        self.token = cfg.get("relay.token", "")
        self.host = cfg.get("client.host", "127.0.0.1")
        self.port = cfg.get_int("client.port", 8765)

        self.sock = None
        self.connected = False
        self.peer = False
        self.running = True
        self.last_rx = 0.0
        self.send_lock = threading.Lock()

        # ---- авто-коннект (всё настраивается в relay.cfg) ----
        self.auto_connect = cfg.get_bool("net.auto_connect", True)
        self.retry_delay = cfg.get_float("net.retry_delay", 1.0)    # первая пауза, сек
        self.retry_max = cfg.get_float("net.retry_max", 5.0)        # потолок паузы, сек
        self.ping_every = cfg.get_float("net.ping_every", 15.0)
        self.dead_after = cfg.get_float("net.dead_after", 70.0)
        self.connect_timeout = cfg.get_float("net.connect_timeout", 10.0)
        self.backoff = self.retry_delay

        # ---- режим "по требованию" ----
        self.refresh_every = cfg.get_float("relay.refresh_every", 60.0)   # перепроверка релея
        self.cooldown = cfg.get_float("relay.cooldown", 300.0)            # КД, если не нужен
        self.offline_max = cfg.get_float("relay.offline_max", 60.0)       # молчание -> КД
        self.idle_cooldown = cfg.get_float("agent.idle_cooldown", 900.0)  # КД после сессии
        self.wanted = True            # релей сказал: коннект нужен
        self.kd_until = 0.0           # до какого времени сидим в КД
        self.peer_was = False
        self.session_started = 0.0
        self.silent_since = 0.0
        self.connect_began = 0.0

    # ---------- жизненный цикл ----------
    def start(self):
        threading.Thread(target=self._supervisor, daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def stop(self):
        self.running = False
        self._close()

    # ---------- отправка ----------
    def send(self, obj):
        sock = self.sock
        if sock is None or not self.connected:
            return False
        try:
            with self.send_lock:
                sock.sendall((json.dumps(obj) + "\n").encode())
            return True
        except Exception:
            self._close()
            return False

    # ---------- переподключение ----------
    def _cooldown_left(self):
        return max(0.0, self.kd_until - time.time())

    def _supervisor(self):
        delay = self.retry_delay
        while self.running:
            # --- КД: релей/контроллер сказал "сейчас не нужен" ---
            left = self._cooldown_left()
            if left > 0:
                self.on_state("cooldown")
                self.log(f"[сеть] КД: следующая перепроверка релея через {int(left)} с\n")
                while self.running and self._cooldown_left() > 0:
                    time.sleep(min(5.0, self._cooldown_left()))
                if not self.running:
                    break
                self.log("[сеть] КД закончился — перепроверяю релей\n")

            try:
                self._session()                   # внутри: рукопожатие + возможно КД
                delay = self.retry_delay
            except Exception as e:
                if self.running:
                    self.log(f"\n[сеть] {e}\n")
            if not self.running:
                break
            if not self.auto_connect:
                self.log("[сеть] авто-коннект выключен ([net] auto_connect = no)\n")
                break

            # если релей сказал "не нужен" — уходим в КД, а не в ретрай
            if not self.wanted and self._cooldown_left() <= 0:
                self.kd_until = time.time() + self.cooldown
                self.log(f"[сеть] релей: коннект не требуется — КД {int(self.cooldown)} с\n")
                continue

            delay = min(max(delay, self.backoff), self.retry_max)
            self.log(f"[сеть] переподключение через {delay:g} с\n")
            time.sleep(delay)
            delay = min(delay * 2, self.retry_max)

    def _session(self):
        self.connect_began = time.time()
        sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        # ВАЖНО: снимаем таймаут чтения. Иначе recv падает через N секунд тишины
        # и сессия рвётся сама по себе — именно это и рвало коннект.
        sock.settimeout(None)

        # Держим канал живым на уровне TCP (помогает против NAT-таймаутов)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt, val in (("TCP_KEEPIDLE", 15), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
                if hasattr(socket, opt):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
        except Exception:
            pass

        count = 1 if self.auto_connect else 0
        first = (f"ROOM:{self.room}:{self.token}:{count}\n" if self.token
                 else f"ROOM:{self.room}::{count}\n")
        sock.sendall(first.encode())

        self.sock = sock
        self.connected = True
        self.last_rx = time.time()
        self.backoff = self.retry_delay
        self.session_started = time.time()
        self.silent_since = 0.0
        self.wanted = True
        self.log(f"[сеть] подключено к {self.host}:{self.port}, комната {self.room}\n")
        self.on_state("conn")

        try:
            buf = b""
            while self.running:
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError("релей закрыл соединение")
                self.last_rx = time.time()
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if line:
                        self._dispatch(line)
        finally:
            had_peer = self.peer
            was_connected = self.connected
            self._close()
            if was_connected:
                self.on_state("lost")
            if had_peer:
                self.log(f"\n[!] {self.peer_label} отключился\n")

    def _close(self):
        self.connected = False
        self.peer = False
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

    # ---------- приём ----------
    def _dispatch(self, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        t = msg.get("type")
        if t == "peer_connected":
            self.peer = True
            self.peer_was = True
            self.on_state("peer")
            self.log(f"\n[!] {self.peer_label} подключился\n")
            self.on_message(msg)
        elif t == "peer_disconnected":
            self.peer = False
            self.peer_was = True
            self.on_state("idle")
            self.log(f"\n[!] {self.peer_label} отключился\n")
        elif t == "relay_demand":
            # релей спрашивает: нужен ли коннект? Отвечаем своим флагом спроса.
            self.send({"type": "demand", "still": self.auto_connect})
            self.log(f"\n[сеть] перепроверка релея → отвечаю: "
                     f"{'нужен' if self.auto_connect else 'не нужен'}\n")
        elif t == "relay_cmd":
            self._relay_cmd(msg)
        elif t == "ping":
            self.send({"type": "pong"})
        elif t == "pong":
            pass
        elif t == "error":
            text = str(msg.get("msg", ""))
            self.log(f"\n[сеть] релей: {text}\n")
            # релей не пустил (нет места / плохой токен) — не долбим его сразу
            if "занята" in text or "токен" in text:
                self._close()
                self.backoff = self.retry_max   # не долбим релей, но и не ждём вечность
        else:
            self.on_message(msg)

    def _relay_cmd(self, msg):
        """Команда от релея, которую контроллер прописал в relay.cfg."""
        cmd = str(msg.get("cmd", ""))
        if cmd in ("cooldown", "standby"):
            seconds = float(msg.get("seconds") or self.cooldown)
            self.kd_until = time.time() + seconds
            self.peer = False
            self.log(f"\n[сеть] релей: КД {int(seconds)} с "
                     f"(контроллер указал, что коннект сейчас не нужен)\n")
            self._close()
        elif cmd == "wake":
            self.kd_until = 0.0
            self.log("\n[сеть] релей: разбудили из КД\n")
        else:
            self.log(f"\n[сеть] неизвестная команда релея: {cmd}\n")

    # ---------- пинг и сторож ----------
    def _heartbeat(self):
        while self.running:
            time.sleep(self.ping_every)
            if not self.connected:
                continue
            if not self.connected and self.offline_max > 0:
                gone = time.time() - (self.silent_since or time.time())
                if gone > self.offline_max:
                    self.kd_until = time.time() + self.cooldown
                    self.silent_since = time.time()
                    self.log(f"\n[сеть] релей недоступен {int(gone)} с — "
                             f"ухожу в КД {int(self.cooldown)} с\n")
                    continue
            if time.time() - self.last_rx > self.dead_after:
                self.log("\n[сеть] тишина слишком долго — переподключаюсь\n")
                self._close()
                continue
            self.send({"type": "ping"})


# =====================================================================
#  Шелл в псевдотерминале
# =====================================================================
class ShellSession:
    def __init__(self, on_output, shell_path="", cols=100, rows=30):
        self.on_output = on_output
        self.shell_path = shell_path
        self.cols, self.rows = cols, rows
        self.pid = None
        self.master = None
        self.alive = False

    def _pick_shell(self):
        if self.shell_path and os.path.exists(self.shell_path):
            return self.shell_path
        env_shell = os.environ.get("SHELL", "")
        if env_shell and os.path.exists(env_shell):
            return env_shell
        for candidate in ("/bin/zsh", "/bin/bash", "/bin/sh"):
            if os.path.exists(candidate):
                return candidate
        return "/bin/sh"

    def start(self):
        shell = self._pick_shell()
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env["PS1"] = "\\h:\\W \\$ "
        env["PROMPT"] = "%m:%1~ %# "
        env["CLICOLOR"] = "1"

        pid, master = pty.fork()
        if pid == 0:
            try:
                os.execvpe(shell, [shell, "-i"], env)
            except Exception:
                os._exit(1)

        self.pid, self.master = pid, master
        self.alive = True
        self.resize(self.cols, self.rows)
        self.on_output(f"[сессия] запущен {shell} (pid {pid})\n")
        threading.Thread(target=self._reader, daemon=True).start()

    def resize(self, cols, rows):
        if not self.master:
            return
        self.cols, self.rows = max(20, cols), max(5, rows)
        try:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ,
                        struct.pack("HHHH", self.rows, self.cols, 0, 0))
        except Exception:
            pass

    def _reader(self):
        while self.alive and self.master is not None:
            try:
                r, _, _ = select.select([self.master], [], [], 0.5)
                if not r:
                    continue
                data = os.read(self.master, 65536)
                if not data:
                    break
                self.on_output(clean(data.decode("utf-8", "replace")))
            except Exception:
                break
        self.alive = False
        self.on_output("\n[сессия] шелл завершился\n")

    def write(self, data):
        if self.master is None or not self.alive:
            return
        try:
            os.write(self.master, data.encode())
        except OSError:
            pass

    def send_ctrl(self, ch):
        """Ctrl-C / Ctrl-D / Ctrl-Z — настоящий сигнал шеллу."""
        mapping = {"c": "\x03", "d": "\x04", "z": "\x1a", "l": "\x0c", "\\": "\x1c"}
        self.write(mapping.get(ch, ch))

    def redraw(self):
        self.write("\n")

    def restart(self):
        self.kill()
        time.sleep(0.2)
        self.start()

    def kill(self):
        if self.pid:
            try:
                os.killpg(os.getpgid(self.pid), signal.SIGKILL)
            except Exception:
                try:
                    os.kill(self.pid, signal.SIGKILL)
                except Exception:
                    pass
        if self.master is not None:
            try:
                os.close(self.master)
            except Exception:
                pass
        self.master = None
        self.pid = None
        self.alive = False


# =====================================================================
#  Агент
# =====================================================================
class Agent:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.room = cfg.get("client.room", "")
        self.gui_status = None      # выставит GUI, если он есть
        self.shell = ShellSession(
            on_output=self.on_shell_output,
            shell_path=cfg.get("agent.shell", ""),
            cols=cfg.get_int("ui.cols", 100),
            rows=cfg.get_int("ui.rows", 30),
        )
        self.link = Link(cfg, log, self.on_message,
                         on_state=self.on_state, peer_label="Помощник")

    # ---------- вывод ----------
    def maybe_idle_kd(self):
        """Сессия закончилась — не висим на релее, уходим в КД на idle_cooldown."""
        link = self.link
        if (link.idle_cooldown > 0 and link.peer_was and not link.peer
                and link.connected and link.session_started
                and time.time() - link.session_started > 30):
            link.kd_until = time.time() + link.idle_cooldown
            link.peer_was = False
            self.log(f"\n[сеть] сессия завершена — КД {int(link.idle_cooldown)} с\n")
            link._close()

    def on_shell_output(self, text):
        self.log(text)
        if self.link.peer:
            self.link.send({"type": "out", "data": text})

    def on_state(self, state):
        if self.gui_status:
            self.gui_status(state)
        if state == "idle":
            self.maybe_idle_kd()

    # ---------- команды от помощника ----------
    def on_message(self, msg):
        t = msg.get("type")
        if t == "peer_connected":
            self.link.send({"type": "hello", "host": socket.gethostname(),
                            "user": os.environ.get("USER", "?"), "cwd": os.getcwd(),
                            "cols": self.shell.cols, "rows": self.shell.rows})
            self.shell.redraw()
        elif t == "input":
            self.shell.write(msg.get("data", ""))
        elif t == "ctrl":
            self.shell.send_ctrl(msg.get("key", "c"))
        elif t == "resize":
            self.shell.resize(int(msg.get("cols", 100)), int(msg.get("rows", 30)))
        elif t == "redraw":
            self.shell.redraw()
        elif t == "restart":
            self.log("\n[!] Помощник перезапустил шелл\n")
            self.shell.restart()

    # ---------- запуск ----------
    def run(self):
        self.shell.start()
        self.link.start()
        self.log(f"[i] Код комнаты: {self.room} — сообщи его помощнику\n"
                 f"[i] Всё, что происходит, видно в этом окне. Закрыл окно — доступа нет.\n"
                 f"[i] Обрыв связи не страшен: переподключение автоматическое.\n\n")

    def stop(self):
        self.link.stop()
        self.shell.kill()


# ============================ GUI (Tk) ============================
class AgentGUI:
    def __init__(self, cfg):
        import tkinter as tk
        from tkinter import scrolledtext
        self.tk = tk
        self.root = tk.Tk()
        self.root.title("Mac Remote Console — сессия видна владельцу")
        self.root.geometry("820x560")

        top = tk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=6)
        tk.Label(top, text=f"Релей: {cfg.get('client.host')}:{cfg.get('client.port')}   "
                           f"Комната: {cfg.get('client.room')}",
                 font=("Menlo", 11, "bold")).pack(side="left")
        self.status = tk.Label(top, text="● нет соединения", fg="gray")
        self.status.pack(side="right")

        self.text = scrolledtext.ScrolledText(self.root, bg="#111111", fg="#dddddd",
                                             insertbackground="#dddddd",
                                             font=("Menlo", 11), wrap="char", state="disabled")
        self.text.pack(fill="both", expand=True, padx=8)

        bottom = tk.Frame(self.root)
        bottom.pack(fill="x", padx=8, pady=6)
        tk.Label(bottom, text=">").pack(side="left")
        self.entry = tk.Entry(bottom, bg="#111111", fg="#ffffff", insertbackground="#ffffff",
                              font=("Menlo", 11))
        self.entry.pack(side="left", fill="x", expand=True, padx=4)
        self.entry.bind("<Return>", self.local_enter)
        self.entry.focus_set()

        tk.Button(bottom, text="Ctrl-C",
                  command=lambda: self.agent.shell.send_ctrl("c")).pack(side="left", padx=2)
        tk.Button(bottom, text="Перезапустить шелл",
                  command=lambda: self.agent.shell.restart()).pack(side="left", padx=2)
        tk.Button(bottom, text="СТОП", bg="#f8d7da", command=self.stop).pack(side="left", padx=2)

        self.root.protocol("WM_DELETE_WINDOW", self.stop)
        self.agent = Agent(cfg, self.append)
        self.agent.gui_status = self.set_state
        self.root.after(100, self.agent.run)

    def append(self, text):
        def _do():
            self.text.configure(state="normal")
            self.text.insert("end", text)
            self.text.see("end")
            self.text.configure(state="disabled")
        self.root.after(0, _do)

    def set_state(self, state):
        def _do():
            if state == "peer":
                self.status.configure(text="● помощник активен", fg="darkgreen")
            elif state == "conn":
                self.status.configure(text="● релей на связи, ждём помощника", fg="green")
            elif state == "cooldown":
                self.status.configure(text="● КД: коннект сейчас не нужен, сплю", fg="#8a6d00")
            else:
                self.status.configure(text="● соединение потеряно, переподключаюсь", fg="orange")
        self.root.after(0, _do)

    def local_enter(self, _event=None):
        line = self.entry.get()
        self.entry.delete(0, "end")
        self.agent.shell.write(line + "\n")

    def stop(self):
        self.agent.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ============================ без GUI ============================
def run_headless(cfg):
    lock = threading.Lock()

    def emit(text):
        with lock:
            sys.stdout.write(text)
            sys.stdout.flush()

    agent = Agent(cfg, emit)
    agent.run()

    def stdin_loop():
        while True:
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if not line:
                return
            agent.shell.write(line)

    threading.Thread(target=stdin_loop, daemon=True).start()
    try:
        while agent.shell.alive:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        agent.stop()
        print("\n[сессия] завершена")


def main():
    cfg = Config()
    if not cfg.found:
        print("[!] relay.cfg не найден (искал ./relay.cfg и ~/.relay.cfg).\n"
              "    Скопируй relay.cfg рядом со скриптом или задай RELAY_CFG.")
        sys.exit(1)
    print(f"[i] Конфиг: {cfg.path}")

    if not cfg.get("client.room", "").strip():
        room = "".join(random.choices("0123456789", k=6))
        if cfg.save("client.room", room):
            print(f"[i] Код комнаты сгенерирован и записан в {cfg.path}: {room}")
        else:
            print(f"[i] Код комнаты сгенерирован: {room} (в файл записать не удалось)")

    if cfg.get_bool("ui.gui", True):
        try:
            AgentGUI(cfg).run()
            return
        except ImportError:
            print("[i] tkinter недоступен — работаю в текущем терминале\n")
    run_headless(cfg)


if __name__ == "__main__":
    main()
