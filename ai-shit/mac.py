#!/usr/bin/env python3
"""mac_agent.py — видимая консоль для удалённой помощи (как SSH, только через релей).

Настройки берутся из relay.cfg (переменная RELAY_CFG, иначе ./relay.cfg,
иначе ~/.relay.cfg). Аргументов командной строки нет. Зависимостей нет —
только стандартная библиотека Python 3.

Что происходит:
  1. Скрипт открывает ОДИН настоящий интерактивный шелл в псевдотерминале.
  2. Показывает ВИДИМОЕ окно консоли (если [ui] gui = yes): владелец видит
     свой шелл и всё, что делает помощник, и может работать сам.
  3. Вывод шелла транслируется через Java-релей второй стороне.
  4. Закрыл окно / нажал СТОП — сессия закончилась. Автозапуска нет.

Запуск:
    python3 mac_agent.py
    RELAY_CFG=/etc/relay.cfg python3 mac_agent.py
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


class ShellSession:
    """Один интерактивный шелл в псевдотерминале (как SSH-сессия)."""

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
            except (OSError, Exception):
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
        """Попросить шелл перерисовать приглашение (после подключения)."""
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


class Agent:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.sock = None
        self.connected = False
        self.peer = False
        self.send_lock = threading.Lock()
        self.room = cfg.get("client.room", "")
        self.token = cfg.get("relay.token", "")
        self.shell = ShellSession(
            on_output=self.on_shell_output,
            shell_path=cfg.get("agent.shell", ""),
            cols=cfg.get_int("ui.cols", 100),
            rows=cfg.get_int("ui.rows", 30),
        )

    # ---------- вывод ----------
    def on_shell_output(self, text):
        self.log(text)
        if self.peer:
            self.send({"type": "out", "data": text})

    # ---------- сеть ----------
    def send(self, obj):
        if not self.sock or not self.connected:
            return
        try:
            with self.send_lock:
                self.sock.sendall((json.dumps(obj) + "\n").encode())
        except Exception:
            self.connected = False

    def connect(self):
        host = self.cfg.get("client.host", "127.0.0.1")
        port = self.cfg.get_int("client.port", 8765)
        first = f"ROOM:{self.room}:{self.token}\n" if self.token else f"ROOM:{self.room}\n"
        delay = 2
        while True:
            try:
                s = socket.create_connection((host, port), timeout=10)
                s.sendall(first.encode())
                self.sock, self.connected = s, True
                self.log(f"[сеть] подключено к релею {host}:{port}, комната {self.room}\n")
                threading.Thread(target=self.recv_loop, daemon=True).start()
                return
            except Exception as e:
                self.log(f"[сеть] релей недоступен ({e}), повтор через {delay} с\n")
                time.sleep(delay)
                delay = min(delay * 2, 30)

    def disconnect(self):
        self.connected = False
        self.peer = False
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def recv_loop(self):
        f = self.sock.makefile("r", encoding="utf-8")
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                self.handle(msg)
        except Exception:
            pass
        finally:
            self.connected = False
            self.peer = False
            self.log("\n[сеть] соединение с релеем потеряно\n")

    def handle(self, msg):
        t = msg.get("type")
        if t == "peer_connected":
            self.peer = True
            self.log("\n[!] Помощник подключён — его ввод теперь идёт в этот шелл\n")
            self.send({"type": "hello", "host": socket.gethostname(),
                       "user": os.environ.get("USER", "?"), "cwd": os.getcwd(),
                       "cols": self.shell.cols, "rows": self.shell.rows})
            self.shell.redraw()
        elif t == "peer_disconnected":
            self.peer = False
            self.log("\n[!] Помощник отключился\n")
        elif t == "error":
            self.log(f"\n[сеть] релей: {msg.get('msg')}\n")
        elif not self.peer:
            self.log("\n[!] данные от неизвестного клиента проигнорированы\n")
        elif t == "input":
            self.shell.write(msg.get("data", ""))
        elif t == "ctrl":
            self.shell.send_ctrl(msg.get("key", "c"))
        elif t == "resize":
            self.shell.resize(int(msg.get("cols", 100)), int(msg.get("rows", 30)))
        elif t == "restart":
            self.log("\n[!] Помощник перезапустил шелл\n")
            self.shell.restart()
        elif t == "ping":
            self.send({"type": "pong", "t": time.time()})

    # ---------- запуск ----------
    def run(self):
        self.shell.start()
        self.connect()
        self.log(f"[i] Код комнаты: {self.room} — сообщи его помощнику\n"
                 f"[i] Всё, что происходит, видно в этом окне. Закрыл окно — доступа нет.\n\n")


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
        self.root.after(100, self.agent.run)

    def append(self, text):
        def _do():
            self.text.configure(state="normal")
            self.text.insert("end", text)
            self.text.see("end")
            self.text.configure(state="disabled")
            if self.agent.connected:
                self.status.configure(
                    text="● подключено к релею" + ("  |  помощник активен" if self.agent.peer else ""),
                    fg="darkgreen" if self.agent.peer else "green")
        self.root.after(0, _do)

    def local_enter(self, _event=None):
        line = self.entry.get()
        self.entry.delete(0, "end")
        self.agent.shell.write(line + "\n")

    def stop(self):
        self.agent.disconnect()
        self.agent.shell.kill()
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
        while agent.shell.alive or agent.connected:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        agent.shell.kill()
        agent.disconnect()
        print("\n[сессия] завершена")


def main():
    cfg = Config()
    if not cfg.found:
        print("[!] relay.cfg не найден (искал ./relay.cfg и ~/.relay.cfg).\n"
              "    Скопируй relay.cfg рядом со скриптом или задай RELAY_CFG.")
        sys.exit(1)
    print(f"[i] Конфиг: {cfg.path}")

    # код комнаты: если пустой — генерируем и записываем в конфиг
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
