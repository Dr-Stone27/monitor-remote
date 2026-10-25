"""Shared by server.py and tools.py: the config file, talking to ./ddc, and
recognising the enrolled monitor."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
DDC = str(HERE / "ddc")
CONFIG = HERE / "config.json"
MONITOR_ID = HERE / ".monitor-id"
LABEL = "local.monitor-remote"
RESERVED = {"input", "preset"}


class ConfigError(Exception):
    pass


def _num(v, where, top):
    # Codes can be written as "0x15" or 21; JSON has no hex literals
    if isinstance(v, bool):
        raise ConfigError(f"{where}: {v!r} is not a number")
    try:
        n = int(v, 0) if isinstance(v, str) else int(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: {v!r} is not a number")
    if not 0 <= n <= top:
        raise ConfigError(f"{where}: {n} is outside 0-{top}")
    return n


def load_config(path=CONFIG):
    try:
        raw = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise ConfigError("config.json is missing. Copy a profile first, e.g. cp profiles/generic.json config.json")
    except json.JSONDecodeError as e:
        raise ConfigError(f"config.json is not valid JSON: {e}")
    try:
        cfg = {"name": str(raw.get("name", "Monitor")), "port": _num(raw.get("port", 8787), "port", 65535),
               "inputs": None, "presets": None, "sliders": []}
        if raw.get("inputs"):
            inp = raw["inputs"]
            method = inp.get("method", "standard")
            if method not in ("standard", "lg"):
                raise ConfigError("inputs.method must be \"standard\" or \"lg\"")
            cfg["inputs"] = {
                "vcp": _num(inp["vcp"], "inputs.vcp", 255), "method": method,
                "options": [{"label": str(o["label"]), "port": str(o.get("port", "")),
                             "value": _num(o["value"], f"input {o['label']}", 65535)} for o in inp["options"]],
            }
        if raw.get("presets"):
            pre = raw["presets"]
            cfg["presets"] = {
                "vcp": _num(pre["vcp"], "presets.vcp", 255),
                "options": [{"label": str(o["label"]), "value": _num(o["value"], f"preset {o['label']}", 65535)}
                            for o in pre["options"]],
            }
        for s in raw.get("sliders", []):
            sid = str(s["id"])
            if sid in RESERVED or not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", sid):
                raise ConfigError(f"slider id {sid!r} must be lowercase letters, digits or _, and not input/preset")
            lo, hi = _num(s.get("min", 0), f"{sid}.min", 65535), _num(s.get("max", 100), f"{sid}.max", 65535)
            step = _num(s.get("step", 1), f"{sid}.step", 65535)
            if lo >= hi or step < 1:
                raise ConfigError(f"{sid}: needs min < max and a step of at least 1")
            write = s.get("write", "value")
            if write not in ("value", "step"):
                raise ConfigError(f"{sid}.write must be \"value\" or \"step\"")
            cfg["sliders"].append({
                "id": sid, "label": str(s["label"]), "vcp": _num(s["vcp"], f"{sid}.vcp", 255),
                "min": lo, "max": hi, "step": step, "write": write,
                "locked_in_presets": [_num(x, f"{sid}.locked_in_presets", 65535) for x in s.get("locked_in_presets", [])],
            })
        ids = [s["id"] for s in cfg["sliders"]]
        if len(ids) != len(set(ids)):
            raise ConfigError("two sliders share an id")
    except KeyError as e:
        raise ConfigError(f"config.json is missing the field {e}")
    except (TypeError, AttributeError):
        raise ConfigError("config.json has a field of the wrong type; compare it with a profile")
    return cfg


def run_ddc(*args, timeout=10):
    r = subprocess.run([DDC, *map(str, args)], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip()


def displays():
    """Displays macOS currently sees, from the identity each one reports over its cable."""
    out = subprocess.run(["ioreg", "-lw0", "-r", "-d", "1", "-k", "DisplayAttributes"],
                         capture_output=True, text=True, timeout=10).stdout
    found = {}
    for block in re.findall(r'"ProductAttributes"=\{([^}]*)\}', out):
        def field(name):
            m = re.search(rf'"{name}"=(?:"([^"]*)"|(\d+))', block)
            return (m.group(1) if m.group(1) is not None else m.group(2)) if m else ""
        name = field("ProductName")
        if not name:
            continue  # adapters and converters report themselves here too, without a product name
        d = {"name": name, "maker": field("ManufacturerID"), "product": field("ProductID"),
             "serial": field("AlphanumericSerialNumber") or field("SerialNumber")}
        found[identity(d)] = d
    return found


def identity(d):
    # Stored as a hash so the monitor's serial number never sits on disk
    return hashlib.sha256(f"{d['maker']}:{d['product']}:{d['serial']}".encode()).hexdigest()
