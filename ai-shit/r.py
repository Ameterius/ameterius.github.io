"""relcfg.py — чтение relay.cfg для агента и контроллера. Стандартная библиотека.

Порядок поиска файла:
  1. переменная окружения RELAY_CFG
  2. ./relay.cfg
  3. ~/.relay.cfg
"""

import configparser
import os

FILENAME = "relay.cfg"


def find_config():
    env = os.environ.get("RELAY_CFG", "").strip()
    if env:
        return env if os.path.isfile(env) else None
    if os.path.isfile(FILENAME):
        return FILENAME
    home_cfg = os.path.join(os.path.expanduser("~"), "." + FILENAME)
    if os.path.isfile(home_cfg):
        return home_cfg
    return None


class Config:
    def __init__(self):
        self.path = find_config()
        self.parser = configparser.ConfigParser()
        if self.path:
            self.parser.read(self.path, encoding="utf-8")

    @property
    def found(self):
        return self.path is not None

    def get(self, key, default=""):
        section, _, name = key.partition(".")
        try:
            value = self.parser.get(section, name).strip()
        except Exception:
            return default
        return value if value else default

    def get_int(self, key, default):
        try:
            return int(self.get(key, str(default)).strip())
        except Exception:
            return default

    def get_bool(self, key, default=True):
        value = self.get(key, "yes" if default else "no").strip().lower()
        return value in ("1", "true", "yes", "y", "on", "да")

    def save(self, key, value):
        """Записать значение в файл, сохранив комментарии и порядок строк."""
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
            lines.append(f"\n[{section}]\n{name} = {value}\n")

        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.writelines(lines)
        except OSError:
            return False

        if not self.parser.has_section(section):
            self.parser.add_section(section)
        self.parser.set(section, name, value)
        return True
