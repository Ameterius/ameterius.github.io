"""relcfg.py — общий модуль чтения relay.cfg для Mac-агента и контроллера.

Один файл, стандартная библиотека, никаких зависимостей.

Ключевое свойство: модуль версионно-совместим. Если рядом окажется СТАРАЯ
копия relcfg.py (без get_float и т.п.), скрипты всё равно запустятся —
они проверяют, что модуль умеет, и при нехватке методов берут встроенную
реализацию из самого скрипта. Поэтому рассинхрон файлов больше не ломает старт.

Использование:
    from relcfg import Config, build_config, parse_args

    args = parse_args("Контроллер")
    cfg = build_config(args)
    cfg.get("client.room")
    cfg.get_int("client.port", 8765)
    cfg.get_float("relay.cooldown", 300.0)
    cfg.get_bool("net.auto_connect", True)
    cfg.save("client.room", "123456")

Порядок поиска файла: --cfg → RELAY_CFG → ./relay.cfg → ~/.relay.cfg
"""

import configparser
import os

__version__ = "2.0"

FILENAME = "relay.cfg"

# Значения по умолчанию: используются, если ключа нет в файле.
DEFAULTS = {
    "relay.bind_port": "8765",
    "relay.token": "",
    "relay.refresh_every": "60",
    "relay.cooldown": "300",
    "relay.demand_ttl": "600",
    "relay.offline_grace": "60",
    "relay.command_file": "",
    "client.host": "127.0.0.1",
    "client.port": "8765",
    "client.room": "",
    "client.want_room": "yes",
    "net.auto_connect": "yes",
    "net.retry_delay": "1",
    "net.retry_max": "5",
    "net.ping_every": "15",
    "net.dead_after": "70",
    "net.connect_timeout": "10",
    "ui.gui": "yes",
    "ui.cols": "100",
    "ui.rows": "30",
    "agent.shell": "",
    "agent.idle_cooldown": "900",
}

_TRUE = ("1", "true", "yes", "y", "on", "да")


def find_config(path=None):
    """--cfg → RELAY_CFG → ./relay.cfg → ~/.relay.cfg. Возвращает путь или None."""
    if path and os.path.isfile(path):
        return path
    env = os.environ.get("RELAY_CFG", "").strip()
    if env:
        if os.path.isfile(env):
            return env
        print(f"[!] RELAY_CFG указывает на {env}, но файла нет — ищу дальше")
    if os.path.isfile(FILENAME):
        return FILENAME
    home_cfg = os.path.join(os.path.expanduser("~"), "." + FILENAME)
    if os.path.isfile(home_cfg):
        return home_cfg
    return None


class Config:
    """Тонкая обёртка над configparser с безопасными геттерами."""

    def __init__(self, path=None):
        self.path = find_config(path)
        self.parser = configparser.ConfigParser()
        if self.path:
            try:
                self.parser.read(self.path, encoding="utf-8")
            except Exception as e:
                print(f"[!] Не смог прочитать {self.path}: {e}")

    # ---------- свойства ----------
    @property
    def found(self):
        return self.path is not None

    # ---------- чтение ----------
    def get(self, key, default=""):
        section, _, name = key.partition(".")
        try:
            value = self.parser.get(section, name).strip()
        except Exception:
            return str(default)
        return value if value else str(default)

    def get_int(self, key, default):
        try:
            return int(float(self.get(key, str(default)).strip()))
        except Exception:
            return int(default)

    def get_float(self, key, default):
        try:
            return float(self.get(key, str(default)).strip())
        except Exception:
            return float(default)

    def get_bool(self, key, default=True):
        value = self.get(key, "yes" if default else "no").strip().lower()
        return value in _TRUE

    def has(self, key):
        section, _, name = key.partition(".")
        return self.parser.has_option(section, name)

    def set(self, key, value):
        section, _, name = key.partition(".")
        if not self.parser.has_section(section):
            self.parser.add_section(section)
        self.parser.set(section, name, str(value))

    def apply_defaults(self):
        """Дописать в память отсутствующие ключи (файл при этом не меняется)."""
        for key, value in DEFAULTS.items():
            if not self.has(key):
                self.set(key, value)
        return self

    # ---------- запись ----------
    def save(self, key, value):
        """Записать значение в файл, сохранив комментарии и порядок строк."""
        self.set(key, value)
        if not self.path:
            return False
        section, _, name = key.partition(".")
        try:
            with open(self.path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return False

        current, done = "", False
        for i, raw in enumerate(lines):
            stripped = raw.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                current = stripped[1:-1].strip().lower()
                continue
            if current != section.lower():
                continue
            if "=" in stripped and stripped.split("=", 1)[0].strip().lower() == name.lower():
                indent = raw[:len(raw) - len(raw.lstrip())]
                lines[i] = f"{indent}{name} = {value}\n"
                done = True
                break

        if not done:
            # секции нет вовсе — добавляем её в конец
            if not any(l.strip().lower() == f"[{section.lower()}]" for l in lines):
                lines.append(f"\n[{section}]\n")
            lines.append(f"{name} = {value}\n")

        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.writelines(lines)
        except OSError:
            return False
        return True

    def __repr__(self):
        return f"<Config path={self.path!r} found={self.found}>"


def build_config(args=None):
    """Конфиг + дефолты + необязательные флаги CLI (они перекрывают файл)."""
    cfg = Config(getattr(args, "cfg", None) if args else None).apply_defaults()

    if cfg.found:
        print(f"[i] Конфиг: {cfg.path}")
    else:
        print("[!] relay.cfg не найден (искал ./relay.cfg и ~/.relay.cfg) — "
              "работаю на значениях по умолчанию и флагах.")

    if args:
        if getattr(args, "relay", None):
            host, _, port = args.relay.partition(":")
            cfg.set("client.host", host or "127.0.0.1")
            cfg.set("client.port", port or "8765")
        if getattr(args, "room", None):
            cfg.set("client.room", str(args.room))
        if getattr(args, "token", None) is not None:
            cfg.set("relay.token", args.token or "")
        if getattr(args, "no_gui", False):
            cfg.set("ui.gui", "no")
        if getattr(args, "want", None) is not None:
            cfg.set("client.want_room", "yes" if args.want else "no")
    return cfg


def parse_args(description, want_side=False):
    """Общие флаги: у контроллера дополнительно --want/--no-want."""
    import argparse
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--relay", help="host:port релея (перекрывает [client] в cfg)")
    p.add_argument("--room", help="код комнаты (перекрывает [client] room)")
    p.add_argument("--token", default=None, help="токен релея (перекрывает [relay] token)")
    p.add_argument("--cfg", help="путь к relay.cfg")
    p.add_argument("--no-gui", action="store_true", help="без окна, в текущем терминале")
    if want_side:
        p.add_argument("--want", dest="want", action="store_true", default=None,
                       help="хочу коннект (want_room = yes)")
        p.add_argument("--no-want", dest="want", action="store_false",
                       help="не нужен коннект — Mac уйдёт в КД")
    return p.parse_args()


# ---------------------------------------------------------------------
#  Проверка совместимости: старые копии этого файла не умели get_float.
#  Скрипты вызывают это, и при False берут свою встроенную реализацию.
# ---------------------------------------------------------------------
def is_compatible():
    try:
        c = Config()
        missing = [m for m in ("get", "get_int", "get_float", "get_bool", "save", "apply_defaults")
                   if not hasattr(c, m)]
        if missing:
            print(f"[!] relcfg.py неполный (нет: {', '.join(missing)}) — "
                  f"использую встроенную реализацию из скрипта")
            return False
        return True
    except Exception as e:
        print(f"[!] relcfg.py не загрузился ({e}) — использую встроенную реализацию")
        return False


if __name__ == "__main__":
    c = Config().apply_defaults()
    print(f"relcfg.py {__version__}\nфайл: {c.path}")
    print("ключ = значение")
    for key in DEFAULTS:
        print(f"  {key} = {c.get(key) or '(пусто)'}")
    print(f"\nсовместимость: {is_compatible()}")
