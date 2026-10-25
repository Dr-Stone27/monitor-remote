#!/usr/bin/env python3
"""Setup and mapping helpers for the monitor remote.

  python3 tools.py check            validate config.json
  python3 tools.py enroll           choose which connected monitor the remote serves
  python3 tools.py link             show the phone link and copy it to the clipboard
  python3 tools.py new-link         issue a new phone link; the old one stops working
  python3 tools.py pause | resume   stop or start the remote, e.g. while mapping
  python3 tools.py scan <name>      save every code the monitor reports to snapshots/<name>.txt
  python3 tools.py diff <a> <b>     show which codes differ between two snapshots
  python3 tools.py watch <vcp>...   print codes whenever they change (Ctrl-C to stop)
"""

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # python3 -I leaves the script's folder off the path
from common import LABEL, MONITOR_ID, ConfigError, displays, load_config, run_ddc  # noqa: E402

PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
SNAPSHOTS = HERE / "snapshots"
DOMAIN = f"gui/{os.getuid()}"

# Codes that trigger actions (factory reset, degauss, saving settings and the like) on many monitors.
# Reading them is harmless by the standard, but scans skip them to be safe.
SKIP = {0x01, 0x03, 0x04, 0x05, 0x06, 0x08, 0x0A, 0x0B, 0x0C, 0x0E}


def remote_running():
    return subprocess.run(["launchctl", "print", f"{DOMAIN}/{LABEL}"], capture_output=True).returncode == 0


def warn_if_running():
    if remote_running():
        print("Note: the remote is running and may talk to the monitor at the same moment, which can\n"
              "spoil readings. Pause it first with: python3 tools.py pause\n")


def cmd_check():
    cfg = load_config()
    print(f"config.json OK: {cfg['name']}")
    if cfg["inputs"]:
        print(f"  inputs ({cfg['inputs']['method']} method): " + ", ".join(o["label"] for o in cfg["inputs"]["options"]))
    if cfg["presets"]:
        print("  picture modes: " + ", ".join(o["label"] for o in cfg["presets"]["options"]))
    for s in cfg["sliders"]:
        print(f"  slider {s['label']}: {s['min']}-{s['max']} in steps of {s['step']}")


def cmd_enroll():
    found = list(displays().items())
    if not found:
        sys.exit("No external monitor found. Plug it in and try again.")
    for n, (_, d) in enumerate(found, 1):
        print(f"  {n}. {d['name']} (maker {d['maker'] or '?'}, model {d['product'] or '?'})")
    if len(found) == 1:
        choice = 1
    else:
        try:
            choice = int(input("Which one should the remote serve? "))
        except ValueError:
            choice = 0
    if not 1 <= choice <= len(found):
        sys.exit("No change made.")
    ident, d = found[choice - 1]
    MONITOR_ID.write_text(ident + "\n")
    MONITOR_ID.chmod(0o600)
    if not d["serial"]:
        print("This monitor reports no serial number, so any monitor of the same model will count as it.")
    print(f"Enrolled {d['name']}. The remote only listens while it is connected.")


def cmd_link():
    f = HERE / "phone-link.txt"
    if not f.exists():
        sys.exit("No link yet. Run ./install.sh first.")
    link = f.read_text().strip()
    subprocess.run(["pbcopy"], input=link.encode())
    print(link)
    print("Copied to the clipboard. Open it in Safari on your phone, then Share > Add to Home Screen.")


def cmd_new_link():
    (HERE / ".token").unlink(missing_ok=True)
    if remote_running():
        subprocess.run(["launchctl", "kickstart", "-k", f"{DOMAIN}/{LABEL}"], check=True)
        time.sleep(2)
        cmd_link()
    else:
        print("Old link removed. A new one is made when the remote next starts (python3 tools.py resume).")


def cmd_pause():
    subprocess.run(["launchctl", "bootout", f"{DOMAIN}/{LABEL}"], capture_output=True)
    print("Remote paused. Start it again with: python3 tools.py resume")


def cmd_resume():
    if not PLIST.exists():
        sys.exit("Not installed yet. Run ./install.sh")
    if not remote_running():
        subprocess.run(["launchctl", "bootstrap", DOMAIN, str(PLIST)], check=True)
    print("Remote running.")


def cmd_scan(name):
    warn_if_running()
    codes = [c for c in range(1, 256) if c not in SKIP]
    print(f"Reading {len(codes)} codes; this takes about half a minute...")
    _, out = run_ddc("get", *(hex(c) for c in codes), timeout=300)
    lines = out.splitlines()
    SNAPSHOTS.mkdir(exist_ok=True)
    readable = [f"{hex(c)} {line}" for c, line in zip(codes, lines) if line[:1].isdigit()]
    (SNAPSHOTS / f"{name}.txt").write_text("\n".join(readable) + "\n")
    print(f"{len(readable)} codes answered; saved to snapshots/{name}.txt")


def _load(name):
    f = SNAPSHOTS / f"{name}.txt"
    if not f.exists():
        sys.exit(f"No snapshot called {name}. Make one with: python3 tools.py scan {name}")
    return {line.split()[0]: line.split()[1] for line in f.read_text().splitlines() if line.strip()}


def cmd_diff(a, b):
    before, after = _load(a), _load(b)
    changed = [(c, before.get(c), after.get(c)) for c in sorted(set(before) | set(after), key=lambda x: int(x, 16))
               if before.get(c) != after.get(c)]
    if not changed:
        print("Nothing changed between the two snapshots.")
    for c, x, y in changed:
        print(f"  {c}: {x} -> {y}")


def cmd_watch(codes):
    warn_if_running()
    codes = [hex(int(c, 0)) for c in codes]
    print("Watching " + ", ".join(codes) + ". Change settings on the monitor; Ctrl-C to stop.")
    last = None
    try:
        while True:
            _, out = run_ddc("get", *codes)
            now = [line.split()[0] if line[:1].isdigit() else "?" for line in out.splitlines()]
            if now != last:
                print(time.strftime("%H:%M:%S"), "  ".join(f"{c}={v}" for c, v in zip(codes, now)), flush=True)
                last = now
            time.sleep(0.4)
    except KeyboardInterrupt:
        print()


def safe_name(name):
    # Snapshot names become file names; keep them to plain words
    return name.replace("-", "").replace("_", "").isalnum()


def main(argv):
    if not argv:
        sys.exit(__doc__)
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "check" and not args:
            cmd_check()
        elif cmd == "enroll" and not args:
            cmd_enroll()
        elif cmd == "link" and not args:
            cmd_link()
        elif cmd == "new-link" and not args:
            cmd_new_link()
        elif cmd == "pause" and not args:
            cmd_pause()
        elif cmd == "resume" and not args:
            cmd_resume()
        elif cmd == "scan" and len(args) == 1 and safe_name(args[0]):
            cmd_scan(args[0])
        elif cmd == "diff" and len(args) == 2 and all(map(safe_name, args)):
            cmd_diff(*args)
        elif cmd == "watch" and args:
            cmd_watch(args)
        else:
            sys.exit(__doc__)
    except ConfigError as e:
        sys.exit(f"Config problem: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
