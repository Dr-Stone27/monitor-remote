#!/usr/bin/env python3
"""Phone remote for a DDC/CI monitor. Serves one page on the local network and
turns taps into monitor commands through ./ddc, using the codes in config.json.
It only works while the Mac is awake and cabled to the monitor, since the
commands travel down that cable.

The port is only open while the enrolled monitor is connected, so on any other
network there is nothing listening. Run by the LaunchAgent that install.sh sets up."""

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # python3 -I leaves the script's folder off the path
from common import MONITOR_ID, ConfigError, displays, load_config, run_ddc  # noqa: E402

try:
    CONFIG = load_config()
except ConfigError as e:
    print(f"Config problem: {e}", flush=True)
    time.sleep(30)  # launchd restarts us; don't spin
    sys.exit(1)

PORT = CONFIG["port"]
INPUTS = CONFIG["inputs"]
PRESETS = CONFIG["presets"]
SLIDERS = {s["id"]: s for s in CONFIG["sliders"]}

VCP = {s["id"]: s["vcp"] for s in CONFIG["sliders"]}
ALLOWED = {sid: set(range(s["min"], s["max"] + 1, s["step"])) for sid, s in SLIDERS.items()}
if INPUTS:
    VCP["input"] = INPUTS["vcp"]
    ALLOWED["input"] = {o["value"] for o in INPUTS["options"]}
if PRESETS:
    VCP["preset"] = PRESETS["vcp"]
    ALLOWED["preset"] = {o["value"] for o in PRESETS["options"]}

MAX_BODY = 1024
MAX_CONNECTIONS = 16
MAX_PER_DEVICE = 6
WRITES_PER_SECOND = 10
LOG_LIMIT = 1_000_000

# The monitor handles one DDC conversation at a time
lock = threading.Lock()


class Busy(Exception):
    pass


def ddc(*args, wait=True):
    if not lock.acquire(timeout=5 if wait else 0):
        raise Busy
    try:
        return run_ddc(*args)
    finally:
        lock.release()


def read(*codes):
    _, out = ddc("get", *codes)
    lines = out.splitlines()
    return [int(line.split()[0]) if line[:1].isdigit() else None for line in lines] + [None] * (len(codes) - len(lines))


def state():
    keys = (["preset"] if PRESETS else []) + list(SLIDERS)
    return dict(zip(keys, read(*(VCP[k] for k in keys))))


def confirm(code, value, tries=5):
    # Mid-switch the monitor can answer with a stale value, so only trust two matching reads in a row
    for _ in range(tries):
        if read(code, code) == [value, value]:
            return True
        time.sleep(0.1)
    return False


def encode(what, value):
    # Some settings read back as the menu value but are written as a step number
    s = SLIDERS.get(what)
    if s and s["write"] == "step":
        return (value - s["min"]) // s["step"]
    return value


class Bucket:
    """Caps monitor writes so a flood can't hammer the monitor or lock the owner out."""

    def __init__(self, rate):
        self.rate, self.tokens, self.last = rate, float(rate), time.monotonic()
        self.mu = threading.Lock()

    def take(self):
        with self.mu:
            now = time.monotonic()
            self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens < 1:
                return False
            self.tokens -= 1
            return True


writes = Bucket(WRITES_PER_SECOND)


def secret_file(name, make):
    f = HERE / name
    if not f.exists():
        f.write_text(make())
    f.chmod(0o600)
    return f.read_text().strip()


TOKEN = secret_file(".token", lambda: secrets.token_urlsafe(16))
LOCAL_NAME = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True).stdout.strip().lower() + ".local"


def monitor_present():
    try:
        wanted = MONITOR_ID.read_text().strip()
    except FileNotFoundError:
        return False
    return any(hmac.compare_digest(i, wanted) for i in displays())


# The page is fixed once the config is loaded, so its inline script and style can be pinned by hash in the CSP.
# "</" is escaped so a label can never close the script tag it is embedded in.
PAGE = (HERE / "page.html").read_text().replace("__CONFIG__", json.dumps({
    "inputs": INPUTS["options"] if INPUTS else [],
    "presets": PRESETS["options"] if PRESETS else [],
    "sliders": [{k: s[k] for k in ("id", "label", "min", "max", "step", "locked_in_presets")} for s in SLIDERS.values()],
}).replace("</", "<\\/"))


def _hashes(tag):
    return " ".join(
        f"'sha256-{base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()}'"
        for body in re.findall(rf"<{tag}>(.*?)</{tag}>", PAGE, re.S)
    )


SECURITY_HEADERS = {
    "Content-Security-Policy": f"default-src 'none'; script-src {_hashes('script')}; style-src {_hashes('style')}; "
                               "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}


def host_allowed(host):
    # A DNS-rebinding page arrives with its own domain in Host; IP literals and our own .local name can't be rebound
    host = (host or "").strip().lower()
    if host.startswith("["):
        name, _, port = host[1:].partition("]")
        port = port.lstrip(":")
    else:
        name, _, port = host.rpartition(":") if host.count(":") == 1 else (host, "", "")
    if port and port != str(PORT):
        return False
    if name in (LOCAL_NAME, "localhost"):
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def clean(text, limit=200):
    # Request lines come from the network; keep control characters out of the log
    return "".join(c if c.isprintable() else "?" for c in text)[:limit]


def log(line):
    f = HERE / "access.log"
    try:
        if f.exists() and f.stat().st_size > LOG_LIMIT:
            f.replace(HERE / "access.log.1")
        with open(f, "a") as fh:
            fh.write(line + "\n")
        f.chmod(0o600)
    except OSError:
        pass


def reject_constant(name):
    raise ValueError(name)


class Handler(BaseHTTPRequestHandler):
    timeout = 10  # drops connections that go quiet mid-request

    def version_string(self):
        return ""

    def _send(self, status, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _route(self):
        # Exact match on the whole path, compared in constant time
        parts = self.path.split("/")
        if len(parts) not in (2, 3) or parts[0] != "" or not hmac.compare_digest(
                parts[1].encode(errors="replace"), TOKEN.encode()):
            return None
        return parts[2] if len(parts) == 3 else ""

    def _checks(self):
        if not host_allowed(self.headers.get("Host")):
            self._send(421, "{}")
            return None
        route = self._route()
        if route is None:
            self._send(404, "{}")
            return None
        return route

    def do_GET(self):
        route = self._checks()
        if route is None:
            return
        try:
            if route == "":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if route == "state":
                return self._send(200, json.dumps(state()))
        except Busy:
            return self._send(503, json.dumps({"ok": False, "error": "busy"}))
        self._send(404, "{}")

    def do_POST(self):
        route = self._checks()
        if route is None:
            return
        if route != "set":
            return self._send(404, "{}")
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() != f"http://{self.headers.get('Host', '').lower()}":
            return self._send(403, json.dumps({"ok": False, "error": "origin"}))
        # application/json forces browsers to ask permission before a cross-site POST, which this server never grants
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self._send(415, json.dumps({"ok": False, "error": "content type"}))
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return self._send(413, json.dumps({"ok": False, "error": "size"}))
        try:
            body = json.loads(self.rfile.read(length), parse_constant=reject_constant)
            what, value = body["what"], body["value"]
            if not isinstance(value, int) or isinstance(value, bool) or value not in ALLOWED[what]:
                raise ValueError
            live = body.get("live") is True and what in SLIDERS
        except Exception:
            return self._send(400, json.dumps({"ok": False, "error": "bad request"}))
        if not writes.take():
            return self._send(429, json.dumps({"ok": False, "error": "busy"}))
        try:
            # Mid-drag slider values are dropped rather than queued when the monitor is busy
            command = "set-alt" if what == "input" and INPUTS["method"] == "lg" else "set"
            rc, _ = ddc(command, VCP[what], encode(what, value), wait=not live)
            if rc != 0:
                return self._send(502, json.dumps({"ok": False}))
            if live:
                return self._send(200, json.dumps({"ok": True}))
            # Monitors often misreport their current input, so an input switch isn't confirmed
            confirmed = None if what == "input" else confirm(VCP[what], value)
            self._send(200, json.dumps({"ok": True, "confirmed": confirmed, "state": state()}))
        except Busy:
            self._send(429, json.dumps({"ok": False, "error": "busy"}))
        except subprocess.TimeoutExpired:
            self._send(502, json.dumps({"ok": False}))

    def log_request(self, code="-", size="-"):
        # Only actions and failures; polls and wrong-token probes would otherwise let anyone grow the log
        if code in (200, 404) and (self.command == "GET" or code == 404):
            return
        line = clean(self.requestline.replace(TOKEN, "<token>"))
        log(f"{self.log_date_time_string()} {self.client_address[0]} \"{line}\" {code}")

    def log_message(self, fmt, *args):
        pass


class Server(ThreadingHTTPServer):
    request_queue_size = 32
    slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
    per_ip = {}
    per_ip_lock = threading.Lock()

    def process_request(self, request, client_address):
        # Beyond the caps, new connections are closed straight away instead of each getting a thread.
        # The per-device cap stops one machine from taking every slot and locking the phone out.
        ip = client_address[0]
        with self.per_ip_lock:
            if self.per_ip.get(ip, 0) >= MAX_PER_DEVICE or not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            self.per_ip[ip] = self.per_ip.get(ip, 0) + 1
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            ip = client_address[0]
            with self.per_ip_lock:
                self.per_ip[ip] -= 1
                if not self.per_ip[ip]:
                    del self.per_ip[ip]
            self.slots.release()

    def handle_error(self, request, client_address):
        pass


def main():
    link = f"http://{LOCAL_NAME}:{PORT}/{TOKEN}/"
    linkfile = HERE / "phone-link.txt"
    linkfile.write_text(link + "\n")
    linkfile.chmod(0o600)
    print(f"Watching for {CONFIG['name']}; phone link in phone-link.txt", flush=True)
    httpd = None
    while True:
        try:
            present = monitor_present()
        except Exception:
            present = False
        if present and httpd is None:
            try:
                httpd = Server(("0.0.0.0", PORT), Handler)
                threading.Thread(target=httpd.serve_forever, daemon=True).start()
                print(time.strftime("%H:%M:%S"), "monitor connected, listening", flush=True)
            except OSError as e:
                print(time.strftime("%H:%M:%S"), f"can't listen on port {PORT}: {e}", flush=True)
                httpd = None
        elif not present and httpd is not None:
            httpd.shutdown()
            httpd.server_close()
            httpd = None
            print(time.strftime("%H:%M:%S"), "monitor gone, port closed", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
