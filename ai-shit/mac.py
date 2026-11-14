#!/usr/bin/env python3
"""controller.py — консоль помощника (клиент к Java-релею). Зависимостей НЕТ.

Запуск:
    python3 controller.py --relay 1.2.3.4:8765 --room 123456
    python3 controller.py --relay 1.2.3.4:8765 --room 123456 --token секрет
    python3 controller.py --relay ... --no-gui     # обычная консоль в терминале

В окне: поле внизу — это терминал на Mac. Enter отправляет строку,
Ctrl-C/кнопка Ctrl-C посылает SIGINT, всё как в ssh. Вывод виден и владельцу.
"""

import argparse
import json
import socket
import sys
import threading
import time

class Client:
    def __init__(self, args, on_output, on_state):
        self.args = args
        self.on_output = on_output
        self.on_state = on_state          # on_state("conn"|"peer"|"lost")
        self.sock = None
        self.connected = False
        self.peer = False
        self.send_lock = threading.Lock()

    def connect(self):
        host, port = self.args.relay_host, self.args.relay_port
        token = self.args.token
        first = f"ROOM:{self.args.room}:{token}\n" if token else f"ROOM:{self.args.room}\n"
        try:
            self.sock = socket.create_connection((host, port), timeout=10)
            self.sock.sendall(first.encode())
            self.connected = True
            self.on_state("conn")
            threading.Thread(target=self.recv_loop, daemon=True).start()
            return True
        except Exception as e:
            self.on_output(f"[сеть] не удалось подключиться: {e}\n")
            return False

    def disconnect(self):
        self.connected = False
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def send(self, obj):
        if not self.connected or not self.sock:
            return
        try:
            with self.send_lock:
                self.sock.sendall((json.dumps(obj) + "\n").encode())
        except Exception:
            self.connected = False

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
                t = msg.get("type")
                if t == "peer_connected":
                    self.peer = True
                    self.on_state("peer")
                    self.send({"type": "resize", "cols": self.args.cols, "rows": self.args.rows})
                elif t == "peer_disconnected":
                    self.peer = False
                    self.on_output("\n[!] Mac отключился\n")
                    self.on_state("conn")
                elif t == "out":
                    self.on_output(msg.get("data", ""))
                elif t == "hello":
                    self.on_output(
                        f"\n[+] Подключено: {msg.get('user')}@{msg.get('host')} "
                        f"({msg.get('cwd')})\n\n")
                elif t == "error":
                    self.on_output(f"\n[!] релей: {msg.get('msg')}\n")
                elif t == "pong":
                    self.on_output("\n[<] pong\n")
        except Exception:
            pass
        finally:
            self.connected = False
            self.peer = False
            self.on_output("\n[сеть] соединение закрыто\n")
            self.on_state("lost")

    # ---- действия ----
    def send_line(self, line):
        self.send({"type": "input", "data": line + "\n"})

    def send_ctrl(self, key="c"):
        self.send({"type": "ctrl", "key": key})

    def restart_shell(self):
        self.send({"type": "restart"})


# ============================ GUI (Tk) ============================
class ControllerGUI:
    def __init__(self, args):
        import tkinter as tk
        from tkinter import scrolledtext
        self.tk = tk
        self.args = args

        self.root = tk.Tk()
        self.root.title("Remote Console — " + args.room)
        self.root.geometry("900x600")

        top = tk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=6)
        tk.Label(top, text=f"Релей: {args.relay_host}:{args.relay_port}   Комната: {args.room}",
                 font=("Menlo", 11, "bold")).pack(side="left")
        self.status = tk.Label(top, text="● отключено", fg="gray")
        self.status.pack(side="right")

        self.text = scrolledtext.ScrolledText(self.root, bg="#0b0b0b", fg="#d6d6d6",
                                             insertbackground="#ffffff",
                                             font=("Menlo", 11), wrap="char", state="disabled")
        self.text.pack(fill="both", expand=True, padx=8)

        bottom = tk.Frame(self.root)
        bottom.pack(fill="x", padx=8, pady=6)
        tk.Label(bottom, text=">").pack(side="left")
        self.entry = tk.Entry(bottom, bg="#0b0b0b", fg="#ffffff",
                              insertbackground="#ffffff", font=("Menlo", 11))
        self.entry.pack(side="left", fill="x", expand=True, padx=4)
        self.entry.bind("<Return>", self.on_enter)
        self.entry.bind("<Up>", self.hist_up)
        self.entry.bind("<Down>", self.hist_down)
        self.entry.bind("<Control-c>", lambda e: (self.client.send_ctrl("c"), "break")[1])

        tk.Button(bottom, text="Ctrl-C", command=lambda: self.client.send_ctrl("c")).pack(side="left", padx=2)
        tk.Button(bottom, text="Ctrl-D", command=lambda: self.client.send_ctrl("d")).pack(side="left", padx=2)
        tk.Button(bottom, text="Рестарт шелла", command=lambda: self.client.restart_shell()).pack(side="left", padx=2)
        tk.Button(bottom, text="Отключиться", bg="#f8d7da", command=self.stop).pack(side="left", padx=2)

        self.history, self.hist_pos = [], 0
        self.client = Client(args, self.append, self.set_state)
        self.root.protocol("WM_DELETE_WINDOW", self.stop)
        self.root.after(100, self.client.connect)
        self.entry.focus_set()

    def append(self, text):
        def _do():
            self.text.configure(state="normal")
            self.text.insert("end", text)
            self.text.see("end")
            self.text.configure(state="disabled")
        self.root.after(0, _do)

    def set_state(self, state):
        def _do():
            label = {"conn": ("● подключено, ждём Mac", "green"),
                     "peer": ("● Mac на связи", "darkgreen"),
                     "lost": ("● соединение закрыто", "red")}[state]
            self.status.configure(text=label[0], fg=label[1])
            if state == "peer":
                self.entry.configure(state="normal")
        self.root.after(0, _do)

    def on_enter(self, _e=None):
        line = self.entry.get()
        self.entry.delete(0, "end")
        if line:
            self.history.append(line)
            self.hist_pos = len(self.history)
            self.client.send_line(line)
        else:
            self.client.send_line("")
        return "break"

    def hist_up(self, _e=None):
        if self.history:
            self.hist_pos = max(0, self.hist_pos - 1)
            self.entry.delete(0, "end")
            self.entry.insert(0, self.history[self.hist_pos])
        return "break"

    def hist_down(self, _e=None):
        if self.history:
            self.hist_pos = min(len(self.history), self.hist_pos + 1)
            self.entry.delete(0, "end")
            if self.hist_pos < len(self.history):
                self.entry.insert(0, self.history[self.hist_pos])
        return "break"

    def stop(self):
        self.client.disconnect()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ============================ без GUI ============================
def run_headless(args):
    lock = threading.Lock()

    def out(text):
        with lock:
            sys.stdout.write(text)
            sys.stdout.flush()

    def state(s):
        if s == "peer":
            out("\n[+] Mac на связи. Вводи команды (exit — выйти из этой консоли)\n")
        elif s == "conn":
            out("[сеть] подключено, ждём Mac...\n")
        elif s == "lost":
            pass

    c = Client(args, out, state)
    if not c.connect():
        return
    try:
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            c.send_line(line.rstrip("\n"))
    except KeyboardInterrupt:
        c.send_ctrl("c")
    finally:
        c.disconnect()


def main():
    p = argparse.ArgumentParser(description="Консоль помощника (клиент релея)")
    p.add_argument("--relay", default="127.0.0.1:8765", help="адрес релея, host:port")
    p.add_argument("--room", required=True, help="код комнаты")
    p.add_argument("--token", default="", help="общий токен релея")
    p.add_argument("--no-gui", action="store_true")
    p.add_argument("--cols", type=int, default=100)
    p.add_argument("--rows", type=int, default=30)
    args = p.parse_args()

    host, _, port = args.relay.partition(":")
    args.relay_host, args.relay_port = host, int(port or 8765)

    if not args.no_gui:
        try:
            ControllerGUI(args).run()
            return
        except ImportError:
            print("[i] tkinter недоступен — работаю в текущем терминале\n")
    run_headless(args)


if __name__ == "__main__":
    main()
