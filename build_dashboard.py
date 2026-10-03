#!/usr/bin/env python3
"""Refresh the CONFIG block in dashboard.html from market_monitor.py.

Run this after changing thresholds, assets or the NEAR entry price in market_monitor.py
so the page always shows the same numbers the hourly monitor uses.

    python build_dashboard.py                # balanced profile (what the scheduled task runs)
    python build_dashboard.py --profile quiet
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import market_monitor as m  # noqa: E402


def entries_for(name, cfg):
    """Alert-driving entry first (if any), then display-only reference entries."""
    out = [{"price": cfg["entry"], "alerts": True}] if cfg["entry"] else []
    out += [{"price": p, "alerts": False} for p in m.DISPLAY_ENTRIES.get(name, [])]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="balanced", choices=m.PROFILES)
    args = ap.parse_args()

    marks = m.QUIET_ENTRY_MARKS if args.profile == "quiet" else m.ENTRY_MARKS
    cfg = {
        "profile": args.profile,
        "thresholds": m.PROFILES[args.profile],
        "assets": [{"name": n, "symbol": c["symbol"], "entries": entries_for(n, c)} for n, c in m.ASSETS.items()],
        "entryMarks": [[p, label, d] for p, label, d in marks],
        "cooldownH": m.COOLDOWN_S / 3600,
        "markCooldownH": m.MARK_COOLDOWN_S / 3600,
        "runBars": m.RUN_BARS,
    }
    path = HERE / "dashboard.html"
    html = path.read_text(encoding="utf-8")
    block = "const CONFIG = " + json.dumps(cfg, indent=2) + ";"
    new, n = re.subn(r"(// <CONFIG>\n).*?(\n// </CONFIG>)", lambda mo: mo.group(1) + block + mo.group(2), html, flags=re.S)
    if n != 1:
        sys.exit("CONFIG markers not found in dashboard.html")
    path.write_text(new, encoding="utf-8")
    print(f"dashboard.html updated ({args.profile} profile)")


if __name__ == "__main__":
    main()
