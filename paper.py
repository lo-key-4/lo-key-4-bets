#!/usr/bin/env python3
"""
Polymarket BTC hourly ladder -- PAPER trading bot (no money, no keys, no logins).

Each run covers the rest of the current hour. Every minute it:
  1. Gets BTC/USDT price + recent volatility (Binance public data -- the settlement source).
  2. Calculates fair odds that BTC closes above each strike near the current price.
  3. Reads the REAL order book (best bid / ask / size) for each strike.
  4. Logs everything, and "paper buys" when a real ask is cheap vs fair odds.
Then it settles last hours' paper trades and rewrites SUMMARY.md.
"""
import csv, json, math, os, statistics, sys, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests

# ------------------------------ settings ------------------------------
GAP = 0.05            # paper-buy when fair odds beat the real price by >= 5c
STAKE = 10.0          # paper dollars per trade
MIN_DOLLARS = 5.0     # need at least this much $ available at the best price
STRIKE_RANGE = 1000   # only strikes within $1,000 of BTC price
PRICE_BAND = (0.05, 0.95)
STOP_AT_MIN = 58      # stop logging at hh:58
TARGET_ROI = 0.032
DATA = "data"
# ----------------------------------------------------------------------

ET = ZoneInfo("America/New_York")
S = requests.Session(); S.headers["User-Agent"] = "paper-bot/1.0"
GAMMA, CLOB = "https://gamma-api.polymarket.com", "https://clob.polymarket.com"


def get(url, params=None, tries=4):
    for a in range(tries):
        try:
            r = S.get(url, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(2 + 2 * a); continue
            r.raise_for_status(); return r.json()
        except Exception:
            if a == tries - 1: return None
            time.sleep(1 + a)


# ------------------------------ BTC data ------------------------------
def btc_minutes():
    """Last ~150 closed 1-min candles as (open_time_s, close). Binance first (settlement source)."""
    d = get("https://data-api.binance.vision/api/v3/klines",
            {"symbol": "BTCUSDT", "interval": "1m", "limit": 152})
    if d:
        now_ms = time.time() * 1000
        return "binance", [(int(k[0] / 1000), float(k[4])) for k in d if k[6] < now_ms]
    d = get("https://api.exchange.coinbase.com/products/BTC-USD/candles", {"granularity": 60})
    if d:
        now = time.time()
        c = sorted((int(k[0]), float(k[4])) for k in d if k[0] + 60 <= now)
        return "coinbase", c[-150:]
    return None, []


def btc_live():
    """Live BTC price right now (Binance first, Coinbase fallback)."""
    d = get("https://data-api.binance.vision/api/v3/ticker/price", {"symbol": "BTCUSDT"})
    if d and d.get("price"): return "binance", float(d["price"])
    d = get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
    if d and d.get("price"): return "coinbase", float(d["price"])
    return None, None


def fair_above(price, strike, vol_min, minutes_left):
    sd = vol_min * math.sqrt(max(minutes_left, 0.25))
    return 0.5 * (1 + math.erf(math.log(price / strike) / sd / math.sqrt(2)))


# ------------------------------ Polymarket ------------------------------
def ladder_slug(end_ts):
    d = datetime.fromtimestamp(end_ts, ET)
    h = d.hour % 12 or 12
    return f"bitcoin-above-on-{d.strftime('%B').lower()}-{d.day}-{d.year}-{h}{'am' if d.hour < 12 else 'pm'}-et"


def load_strikes(end_ts):
    ev = get(f"{GAMMA}/events", {"slug": ladder_slug(end_ts)})
    if not ev: return []
    out = []
    for m in ev[0].get("markets", []):
        try:
            k = float(str(m["groupItemTitle"]).replace(",", "").replace("$", ""))
            toks, outs = json.loads(m["clobTokenIds"]), json.loads(m["outcomes"])
            out.append({"strike": k, "yes": toks[outs.index("Yes")], "slug": m.get("slug"),
                        "fee": json.dumps(m.get("feeSchedule") or {})})
        except Exception:
            continue
    return out


def book(token):
    b = get(f"{CLOB}/book", {"token_id": token})
    if not b: return None
    bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
    asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
    bb = max(bids, default=(None, 0)); ba = min(asks, default=(None, 0))
    return {"bid": bb[0], "bid_sz": bb[1], "ask": ba[0], "ask_sz": ba[1]}


# ------------------------------ files ------------------------------
def append(path, cols, rows):
    if not rows: return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new: w.writeheader()
        w.writerows(rows)


def read(path):
    if not os.path.exists(path): return []
    with open(path, newline="") as f: return list(csv.DictReader(f))


SNAP_COLS = ["time_utc", "hour_end_utc", "strike", "minutes_left", "btc", "src", "vol_min",
             "fair_yes", "yes_bid", "yes_bid_sz", "yes_ask", "yes_ask_sz", "fee_schedule"]
TRADE_COLS = ["time_utc", "hour_end_utc", "strike", "side", "price", "fair", "gap",
              "shares", "stake", "minutes_left", "btc", "yes_token", "market_slug"]
RES_COLS = TRADE_COLS + ["outcome_yes", "payout", "pnl", "settled_utc"]


# ------------------------------ main loop ------------------------------
def run_hour():
    now = time.time()
    end_ts = (int(now) // 3600 + 1) * 3600
    strikes = load_strikes(end_ts)
    if not strikes:
        print("No ladder market found for", ladder_slug(end_ts)); return
    print(f"Hour ending {datetime.fromtimestamp(end_ts, ET):%I %p ET}: {len(strikes)} strikes")
    traded = {(r["hour_end_utc"], r["strike"]) for r in read(f"{DATA}/trades.csv")}
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    while True:
        t0 = time.time()
        if datetime.fromtimestamp(t0, ET).minute >= STOP_AT_MIN or t0 >= end_ts - 90: break
        _, mins = btc_minutes()
        src, price = btc_live()
        if len(mins) >= 60 and price:
            rets = [math.log(b[1] / a[1]) for a, b in zip(mins[-121:], mins[-120:])]
            vol = statistics.pstdev(rets)
            minutes_left = (end_ts - time.time()) / 60
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            snaps, trades = [], []
            for s in strikes:
                if abs(s["strike"] - price) > STRIKE_RANGE: continue
                b = book(s["yes"])
                if not b: continue
                fv = fair_above(price, s["strike"], vol, minutes_left)
                snaps.append({"time_utc": stamp, "hour_end_utc": end_ts, "strike": s["strike"],
                              "minutes_left": round(minutes_left, 2), "btc": price, "src": src,
                              "vol_min": f"{vol:.6f}", "fair_yes": round(fv, 4),
                              "yes_bid": b["bid"], "yes_bid_sz": b["bid_sz"],
                              "yes_ask": b["ask"], "yes_ask_sz": b["ask_sz"], "fee_schedule": s["fee"]})
                key = (str(end_ts), str(s["strike"]))
                if key in traded or src != "binance": continue   # only trade on the settlement source
                pick = None
                if b["ask"] and PRICE_BAND[0] <= b["ask"] <= PRICE_BAND[1] and fv - b["ask"] >= GAP \
                        and b["ask"] * b["ask_sz"] >= MIN_DOLLARS:
                    pick = ("YES", b["ask"], fv)
                elif b["bid"] and PRICE_BAND[0] <= 1 - b["bid"] <= PRICE_BAND[1] \
                        and (1 - fv) - (1 - b["bid"]) >= GAP and (1 - b["bid"]) * b["bid_sz"] >= MIN_DOLLARS:
                    pick = ("NO", round(1 - b["bid"], 4), 1 - fv)
                if pick:
                    side, px, f = pick
                    trades.append({"time_utc": stamp, "hour_end_utc": end_ts, "strike": s["strike"],
                                   "side": side, "price": px, "fair": round(f, 4), "gap": round(f - px, 4),
                                   "shares": round(STAKE / px, 2), "stake": STAKE,
                                   "minutes_left": round(minutes_left, 2), "btc": price,
                                   "yes_token": s["yes"], "market_slug": s["slug"]})
                    traded.add(key)
            append(f"{DATA}/snapshots/{day}.csv", SNAP_COLS, snaps)
            append(f"{DATA}/trades.csv", TRADE_COLS, trades)
            print(f"{datetime.now(ET):%H:%M} BTC {price:,.0f} ({src}) | {len(snaps)} books | "
                  f"{len(trades)} paper trades")
        time.sleep(max(1, 60 - (time.time() - t0)))


def settle():
    """Record outcomes for paper trades whose hour has ended."""
    done = {(r["hour_end_utc"], r["strike"]) for r in read(f"{DATA}/results.csv")}
    out = []
    for t in read(f"{DATA}/trades.csv"):
        key = (t["hour_end_utc"], t["strike"])
        if key in done or time.time() < int(t["hour_end_utc"]) + 300: continue
        m = get(f"{GAMMA}/markets", {"slug": t["market_slug"]})
        if not m or not m[0].get("closed"): continue
        try:
            px = json.loads(m[0]["outcomePrices"]); outs = json.loads(m[0]["outcomes"])
            yes_won = float(px[outs.index("Yes")]) > 0.5
        except Exception:
            continue
        won = yes_won if t["side"] == "YES" else not yes_won
        payout = float(t["shares"]) if won else 0.0
        out.append({**t, "outcome_yes": int(yes_won), "payout": round(payout, 2),
                    "pnl": round(payout - float(t["stake"]), 2),
                    "settled_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    append(f"{DATA}/results.csv", RES_COLS, out)
    return len(out)


def summary():
    R = read(f"{DATA}/results.csv"); open_n = len(read(f"{DATA}/trades.csv")) - len(R)
    lines = ["# Paper bot -- running results", "",
             f"_Updated {datetime.now(ET):%b %d, %I:%M %p ET}. No real money. Rule: buy when fair odds beat the real ask by {GAP*100:.0f}c+._", ""]
    if not R:
        lines += ["No settled trades yet.", f"Open paper trades: {open_n}"]
    else:
        stake = sum(float(r["stake"]) for r in R); pnl = sum(float(r["pnl"]) for r in R)
        # fee estimate: taker fee ~1% of notional (Polymarket crypto fee schedule rate 0.01)
        fees = sum(0.01 * float(r["stake"]) for r in R)
        wins = sum(1 for r in R if float(r["pnl"]) > 0)
        roi, roi_f = pnl / stake, (pnl - fees) / stake
        verdict = "ABOVE" if roi_f > TARGET_ROI else "BELOW"
        lines += ["| Metric | Value |", "|---|---|",
                  f"| Settled trades | {len(R)} |", f"| Win rate | {wins/len(R)*100:.1f}% |",
                  f"| Paper staked | ${stake:,.0f} |", f"| Paper P&L | ${pnl:+,.2f} |",
                  f"| ROI before fees | {roi*100:+.2f}% |", f"| ROI after est. fees | {roi_f*100:+.2f}% |",
                  f"| vs 3.2% target | **{verdict}** |", f"| Open trades | {open_n} |", ""]
        if len(R) < 200: lines.append(f"_Only {len(R)} trades so far; results are noisy until a few hundred._")
    open("SUMMARY.md", "w").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "hour"
    if mode == "hour": run_hour()
    n = settle(); summary(); print(f"Settled {n} trades. Summary updated.")
