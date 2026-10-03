#!/usr/bin/env python3
"""Hourly market-condition monitor for NEAR, ZEC and BTC (Binance spot, public data).

Prints one JSON object. "alerts" lists conditions that crossed an objective threshold.
It does not predict price and is not financial advice - an alert only means the data
crossed a line.

Triggers (per asset, on 1h candles):
  - 1h move and 4h move at or above the profile thresholds
  - volume spike on the last closed candle together with an unusually wide candle
  - last closed candle closed at least 1 ATR beyond the 1h EMA200 after being on the other side
  - last closed candle closed beyond the prior 10-day high / low
  - last closed candle closed half an ATR beyond the 61.8% retracement of a 10-day run of 15%+
  - NEAR only: price touched a mark relative to the configured entry price

Usage:
  python market_monitor.py                 # normal run, updates state (cooldown, last price)
  python market_monitor.py --dry-run       # evaluate without touching state
  python market_monitor.py --profile sensitive|balanced|quiet
  python market_monitor.py --test          # emit a fake alert to test the notification path
"""
import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "monitor_state.json"
HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]

ASSETS = {
    "NEAR": {"symbol": "NEARUSDT", "entry": 5.00},
    "ZEC": {"symbol": "ZECUSDT", "entry": None},
    "BTC": {"symbol": "BTCUSDT", "entry": None},
}

# Entry prices drawn on the dashboard only. The monitor sends NO alerts for these;
# the single "entry" above (NEAR) is the one that drives the entry-mark alerts.
DISPLAY_ENTRIES = {"ZEC": [1100.0], "BTC": [57000.0, 86000.0]}

PROFILES = {
    "sensitive": {"move1h": 2.0, "move4h": 4.0, "vol_mult": 2.5, "range_atr": 1.5},
    "balanced": {"move1h": 4.0, "move4h": 8.0, "vol_mult": 3.0, "range_atr": 2.0},
    "quiet": {"move1h": 7.0, "move4h": 12.0, "vol_mult": 4.0, "range_atr": 3.0},
}

# (percent from entry, label, direction of the touch that counts)
ENTRY_MARKS = [(-15, "-15% from entry", "down"), (-10, "-10% from entry", "down"),
               (0, "back at entry", "up"), (10, "+10% from entry", "up")]
QUIET_ENTRY_MARKS = [(-15, "-15% from entry", "down")]

COOLDOWN_S = 2 * 3600
MARK_COOLDOWN_S = 12 * 3600
RUN_BARS = 240  # 10 days of 1h candles
MAX_MESSAGE = 190


def fetch_klines(symbol, limit=500):
    last_err = None
    for host in HOSTS:
        try:
            req = urllib.request.Request(
                f"{host}/api/v3/klines?symbol={symbol}&interval=1h&limit={limit}",
                headers={"User-Agent": "market-monitor/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                rows = json.load(resp)
            return [{"t": int(r[0]), "ct": int(r[6]), "o": float(r[1]), "h": float(r[2]),
                     "l": float(r[3]), "c": float(r[4]), "v": float(r[5])} for r in rows]
        except Exception as exc:  # try the next host
            last_err = exc
    raise RuntimeError(f"{symbol}: {last_err}")


def ema(values, n):
    k = 2 / (n + 1)
    out, e = [], None
    for i, v in enumerate(values):
        if i < n - 1:
            out.append(None)
            continue
        e = sum(values[:n]) / n if i == n - 1 else v * k + e * (1 - k)
        out.append(e)
    return out


def atr(bars, n=14):
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b["h"] - b["l"])
        else:
            pc = bars[i - 1]["c"]
            trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def fmt(p):
    return f"{p:,.2f}" if p >= 100 else f"{p:.3f}"


def evaluate(name, cfg, bars, prof, state_a, marks, now_ms):
    closed, cur = bars[:-1], bars[-1]
    price = cur["c"]
    closes = [b["c"] for b in bars]
    r1 = (price / closes[-2] - 1) * 100
    r4 = (price / closes[-5] - 1) * 100
    lb = closed[-1]
    lastc, prevc = lb["c"], closed[-2]["c"]
    trig = []

    if abs(r1) >= prof["move1h"]:
        trig.append(f"1h move {r1:+.1f}%")
    if abs(r4) >= prof["move4h"]:
        trig.append(f"4h move {r4:+.1f}%")

    avgv = sum(b["v"] for b in closed[-21:-1]) / 20
    if avgv > 0 and lb["v"] >= prof["vol_mult"] * avgv and (lb["h"] - lb["l"]) >= prof["range_atr"] * atr(closed):
        side = "up" if lb["c"] >= lb["o"] else "down"
        trig.append(f"volume {lb['v'] / avgv:.1f}x avg on wide {side} candle")

    a14 = atr(closed)
    e200 = ema([b["c"] for b in closed], 200)
    if e200[-1] is not None and e200[-2] is not None:
        if prevc >= e200[-2] and lastc < e200[-1] - a14:
            trig.append(f"closed decisively below 1h EMA200 ({fmt(e200[-1])})")
        elif prevc <= e200[-2] and lastc > e200[-1] + a14:
            trig.append(f"closed decisively above 1h EMA200 ({fmt(e200[-1])})")

    # Only the FIRST close beyond the prior 10-day extreme counts, not every bar of a trend
    prior = closed[-RUN_BARS - 1:-1]
    prior_prev = closed[-RUN_BARS - 2:-2]
    hi10, lo10 = max(b["h"] for b in prior), min(b["l"] for b in prior)
    hi10p, lo10p = max(b["h"] for b in prior_prev), min(b["l"] for b in prior_prev)
    if lastc > hi10 and prevc <= hi10p:
        trig.append(f"closed above 10-day high ({fmt(hi10)})")
    elif lastc < lo10 and prevc >= lo10p:
        trig.append(f"closed below 10-day low ({fmt(lo10)})")

    win = bars[-RUN_BARS:]
    hi, lo = max(b["h"] for b in win), min(b["l"] for b in win)
    i_hi = max(range(len(win)), key=lambda i: win[i]["h"])
    i_lo = min(range(len(win)), key=lambda i: win[i]["l"])
    if lo > 0 and i_lo < i_hi and (hi - lo) / lo >= 0.15:
        fib = hi - 0.618 * (hi - lo)
        if prevc >= fib and lastc < fib - 0.5 * a14:
            trig.append(f"closed below 61.8% retrace of 10-day run ({fmt(fib)})")
        elif prevc <= fib and lastc > fib + 0.5 * a14:
            trig.append(f"closed back above 61.8% retrace ({fmt(fib)})")

    entry = cfg.get("entry")
    prev_price, last_ms = state_a.get("last_price"), state_a.get("last_checked_ms")
    if entry and prev_price and last_ms:
        since = [b for b in bars if b["ct"] > last_ms] or [cur]
        lo_s, hi_s = min(b["l"] for b in since), max(b["h"] for b in since)
        fired = state_a.setdefault("marks_ms", {})
        for pct, label, direction in marks:
            lvl = entry * (1 + pct / 100)
            hit = (direction == "down" and prev_price > lvl >= lo_s) or (direction == "up" and prev_price < lvl <= hi_s)
            if hit and now_ms - fired.get(label, 0) >= MARK_COOLDOWN_S * 1000:
                trig.append(f"touched {label} ({fmt(lvl)})")
                fired[label] = now_ms

    pl = f", {(price / entry - 1) * 100:+.1f}% vs {fmt(entry)} entry" if entry else ""
    status = f"{name} {fmt(price)} (1h {r1:+.1f}%, 4h {r4:+.1f}%{pl})"
    return price, trig, status, pl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="balanced", choices=PROFILES)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    now_ms = int(time.time() * 1000)
    out = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "profile": args.profile, "alerts": [], "status": [], "errors": [], "suppressed": []}

    if args.test:
        out["alerts"].append({"asset": "TEST", "message": "TEST: market monitor notification check. Data alerts only, not advice."})
        print(json.dumps(out))
        return

    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        state = {}
    prof = PROFILES[args.profile]
    marks = QUIET_ENTRY_MARKS if args.profile == "quiet" else ENTRY_MARKS
    ok = 0

    for name, cfg in ASSETS.items():
        sa = state.setdefault(name, {})
        try:
            bars = fetch_klines(cfg["symbol"])
            price, trig, status, _ = evaluate(name, cfg, bars, prof, sa, marks, now_ms)
        except Exception as exc:
            out["errors"].append(f"{name}: {exc}")
            continue
        ok += 1
        out["status"].append(status)
        if trig:
            if now_ms - sa.get("last_alert_ms", 0) < COOLDOWN_S * 1000:
                out["suppressed"].append(f"{name}: {'; '.join(trig)}")
            else:
                msg = f"{status}: {'; '.join(trig)}. Data alert, not advice."
                if len(msg) > MAX_MESSAGE:
                    msg = msg[:MAX_MESSAGE - 1] + "…"
                out["alerts"].append({"asset": name, "message": msg})
                sa["last_alert_ms"] = now_ms
        sa["last_price"] = price
        sa["last_checked_ms"] = now_ms

    if ok == 0:
        state["fail_count"] = state.get("fail_count", 0) + 1
        if state["fail_count"] == 3:
            out["alerts"].append({"asset": "MONITOR", "message": "Market monitor could not reach Binance for 3 runs in a row. Alerts are paused until it recovers."})
    else:
        state["fail_count"] = 0

    if not args.dry_run:
        STATE_FILE.write_text(json.dumps(state, indent=1))
    print(json.dumps(out))


if __name__ == "__main__":
    sys.exit(main())
