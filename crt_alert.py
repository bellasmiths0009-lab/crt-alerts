#!/usr/bin/env python3
"""CRT (Candle Range Theory) alerts -> Telegram.

Alerts when candle 2 closes after sweeping ONE side of candle 1 (the parent)
and closing back inside the parent range. Checked on 4H and Daily.

Usage:
  python crt_alert.py            one check (sends new alerts, remembers them)
  python crt_alert.py --window   GitHub mode: catch-up check, then poll every
                                 20s around each candle close (alerts in ~1-3 min)
  python crt_alert.py --loop     always-on mode for a server: check every 30s
  python crt_alert.py --test     send a test Telegram message
  python crt_alert.py --scan 30  print CRTs in the last 30 candles (no Telegram)
"""
import json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "sent.json")
TF_LABEL = {"H4": "4H", "D": "Daily"}


def get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch_oanda(symbol, tf, count=60):
    token = os.environ["OANDA_API_TOKEN"]
    q = urllib.parse.urlencode({
        "granularity": tf, "count": count, "price": "M",
        "dailyAlignment": 17, "alignmentTimezone": "America/New_York",
        "weeklyAlignment": "Friday",
    })
    url = f"https://api-fxpractice.oanda.com/v3/instruments/{symbol}/candles?{q}"
    data = get_json(url, {"Authorization": f"Bearer {token}"})
    out = []
    for c in data["candles"]:
        if not c.get("complete"):
            continue
        m = c["mid"]
        t = datetime.strptime(c["time"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        out.append({"t": t, "o": float(m["o"]), "h": float(m["h"]),
                    "l": float(m["l"]), "c": float(m["c"])})
    return out


def fetch_kraken(symbol, tf, count=60):
    interval = {"H4": 240, "D": 1440}[tf]
    url = f"https://api.kraken.com/0/public/OHLC?pair={symbol}&interval={interval}"
    data = get_json(url)
    if data.get("error"):
        raise RuntimeError(data["error"])
    rows = [v for k, v in data["result"].items() if k != "last"][0]
    rows = rows[:-1]  # last row is the candle still forming
    out = []
    for r in rows[-count:]:
        out.append({"t": datetime.fromtimestamp(int(r[0]), tz=timezone.utc),
                    "o": float(r[1]), "h": float(r[2]),
                    "l": float(r[3]), "c": float(r[4])})
    return out


PROVIDERS = {"oanda": fetch_oanda, "kraken": fetch_kraken}


def detect_crt(p, c):
    """p = candle 1 (parent), c = candle 2. Returns dict or None."""
    inside = p["l"] < c["c"] < p["h"]
    mid = (p["h"] + p["l"]) / 2
    if c["h"] > p["h"] and c["l"] >= p["l"] and inside:
        return {"side": "BEARISH", "swept": "high", "strong": c["c"] < mid, "mid": mid}
    if c["l"] < p["l"] and c["h"] <= p["h"] and inside:
        return {"side": "BULLISH", "swept": "low", "strong": c["c"] > mid, "mid": mid}
    return None


def fmt_time(t, tz):
    return t.astimezone(tz).strftime("%a %d %b %H:%M")


def build_message(name, tf, p, c, r, tz):
    arrow = "\U0001F7E2" if r["side"] == "BULLISH" else "\U0001F534"
    lines = [
        f"{arrow} CRT {r['side']}: {name} ({TF_LABEL[tf]})",
        f"Candle 1 (parent): {fmt_time(p['t'], tz)}",
        f"Candle 2 (CRT): {fmt_time(c['t'], tz)}",
        f"Swept parent {r['swept']}, closed back inside",
        f"Parent high {p['h']:.5g} | 50% {r['mid']:.5g} | low {p['l']:.5g}",
    ]
    if r["strong"]:
        lines.append("Strong rejection: close did not reach 50%")
    lines.append("Candle times are open times (" + str(tz) + "). Check your chart.")
    return "\n".join(lines)


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                           data=data, timeout=30).read()


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    keys = sorted(state, key=state.get)[-500:]
    with open(STATE_FILE, "w") as f:
        json.dump({k: state[k] for k in keys}, f, indent=1)


def check_once(cfg, tz, state, scan=0):
    errors = 0
    for pair in cfg["pairs"]:
        for tf in cfg.get("timeframes", ["H4", "D"]):
            try:
                candles = PROVIDERS[pair["provider"]](pair["symbol"], tf)
            except Exception as e:
                errors += 1
                print(f"ERROR {pair['name']} {tf}: {e}", flush=True)
                continue
            if len(candles) < 2:
                continue
            if scan:
                pairs_to_check = list(zip(candles[-scan - 1:-1], candles[-scan:]))
            else:
                pairs_to_check = [(candles[-2], candles[-1])]
            for p, c in pairs_to_check:
                r = detect_crt(p, c)
                if not r:
                    continue
                msg = build_message(pair["name"], tf, p, c, r, tz)
                if scan:
                    print(msg + "\n")
                    continue
                key = f"{pair['name']}|{tf}|{c['t'].isoformat()}"
                if key in state:
                    continue
                send_telegram(msg)
                state[key] = datetime.now(timezone.utc).isoformat()
                save_state(state)
                print("Sent:", key, flush=True)
    return errors


def boundaries(now):
    """4H candle close times (UTC) around now: OANDA (17:00 New York) and Kraken (UTC)."""
    out = []
    for tzname, hours in (("America/New_York", (1, 5, 9, 13, 17, 21)),
                          ("UTC", (0, 4, 8, 12, 16, 20))):
        z = ZoneInfo(tzname)
        for d in (-1, 0, 1):
            day = (now.astimezone(z) + timedelta(days=d)).date()
            for h in hours:
                out.append(datetime(day.year, day.month, day.day, h, tzinfo=z)
                           .astimezone(timezone.utc))
    return sorted(out)


def run_window(cfg, tz, state):
    """Catch-up check, then poll every 20s from each close until 4 min after it."""
    errors = check_once(cfg, tz, state)
    now = datetime.now(timezone.utc)
    target = next((b for b in boundaries(now)
                   if b + timedelta(minutes=4) > now and b - now <= timedelta(minutes=20)), None)
    if not target:
        print("No candle close soon, done.")
        return errors
    print("Watching close at", target.isoformat(), flush=True)
    wait = (target + timedelta(seconds=3) - datetime.now(timezone.utc)).total_seconds()
    if wait > 0:
        time.sleep(wait)
    while datetime.now(timezone.utc) < target + timedelta(minutes=4):
        errors = check_once(cfg, tz, state)
        time.sleep(20)
    return errors


def main():
    args = sys.argv[1:]
    if "--test" in args:
        send_telegram("CRT alerts: test message OK")
        print("Test sent")
        return
    scan = int(args[args.index("--scan") + 1]) if "--scan" in args else 0

    with open(os.path.join(HERE, "config.json")) as f:
        cfg = json.load(f)
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    state = load_state()

    if "--loop" in args:
        while True:
            try:
                check_once(cfg, tz, state)
            except Exception as e:
                print("loop error:", e, flush=True)
            time.sleep(30)
    if "--window" in args:
        errors = run_window(cfg, tz, state)
        save_state(state)
    else:
        errors = check_once(cfg, tz, state, scan)
        if not scan:
            save_state(state)
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
