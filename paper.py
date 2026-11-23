#!/usr/bin/env python3
"""
Polymarket BTC hourly ladder -- PAPER trading bot v2 (no money, no keys, no logins).

Every ~5 seconds during each hour it:
  1. Gets the live BTC/USDT price (Binance = the settlement source) and recent volatility.
  2. Calculates fair odds that BTC closes the hour above each strike near the price.
  3. Reads the REAL order book for every strike in one request.
  4. TAKER paper trades: "buys" a real ask that is >= GAP below fair odds.
  5. MAKER paper trades: keeps imaginary limit orders at fair +/- HALF_SPREAD and counts
     them as filled only when a REAL trade on Polymarket happens at or through that price.
After the hour, it settles everything and rewrites SUMMARY.md.
"""
import csv, json, math, os, statistics, sys, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests

# ------------------------------ settings ------------------------------
POLL_SEC = 5            # how often to check prices and books
TRADES_EVERY_SEC = 15   # how often to pull real trades (for maker fills)
SNAPSHOT_EVERY_SEC = 30 # how often to save a full snapshot row per strike
GAP = 0.05              # taker: buy when fair odds beat the real ask by >= 5c
LOG_GAP = 0.02          # log every opportunity >= 2c (to study other thresholds later)
HALF_SPREAD = 0.03      # maker: imaginary limit orders at fair -3c (buy) / fair +3c (sell)
STAKE = 10.0            # taker paper dollars per trade
MAKER_SHARES = 10       # maker paper shares per fill
MAKER_CAP = 3           # max maker fills per strike per hour
MIN_DOLLARS = 5.0       # need this much $ at the best price to count a taker trade
STRIKE_RANGE = 1000     # only strikes within $1,000 of BTC
PRICE_BAND = (0.05, 0.95)
STOP_AT_MIN = 58
TARGET_ROI = 0.032
ALLOW_FALLBACK = os.environ.get("ALLOW_COINBASE") == "1"   # testing only
DATA = "data"
# ----------------------------------------------------------------------

ET = ZoneInfo("America/New_York")
S = requests.Session(); S.headers["User-Agent"] = "paper-bot/2.0"
GAMMA, CLOB, DAPI = "https://gamma-api.polymarket.com", "https://clob.polymarket.com", "https://data-api.polymarket.com/v2"


def req(method, url, tries=3, **kw):
    for a in range(tries):
        try:
            r = S.request(method, url, timeout=10, **kw)
            if r.status_code == 429:
                time.sleep(1 + 2 * a); continue
            r.raise_for_status(); return r.json()
        except Exception:
            if a == tries - 1: return None
            time.sleep(0.5 + a)


def get(url, params=None): return req("GET", url, params=params)


# ------------------------------ BTC ------------------------------
def btc_live():
    d = get("https://data-api.binance.vision/api/v3/ticker/price", {"symbol": "BTCUSDT"})
    if d and d.get("price"): return "binance", float(d["price"])
    if ALLOW_FALLBACK:
        d = get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
        if d and d.get("price"): return "coinbase", float(d["price"])
    return None, None


def btc_vol():
    d = get("https://data-api.binance.vision/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1m", "limit": 122})
    closes = [float(k[4]) for k in d] if d else []
    if not closes and ALLOW_FALLBACK:
        c = get("https://api.exchange.coinbase.com/products/BTC-USD/candles", {"granularity": 60})
        closes = [float(k[4]) for k in sorted(c or [])][-122:]
    if len(closes) < 60: return None
    return statistics.pstdev([math.log(b / a) for a, b in zip(closes, closes[1:])])


def fair_above(price, strike, vol_min, minutes_left):
    sd = vol_min * math.sqrt(max(minutes_left, 0.1))
    return 0.5 * (1 + math.erf(math.log(price / strike) / sd / math.sqrt(2)))


# ------------------------------ Polymarket ------------------------------
def ladder_slug(end_ts):
    d = datetime.fromtimestamp(end_ts, ET); h = d.hour % 12 or 12
    return f"bitcoin-above-on-{d.strftime('%B').lower()}-{d.day}-{d.year}-{h}{'am' if d.hour < 12 else 'pm'}-et"


def load_strikes(end_ts):
    ev = get(f"{GAMMA}/events", {"slug": ladder_slug(end_ts)})
    out = []
    for m in (ev[0].get("markets", []) if ev else []):
        try:
            toks, outs = json.loads(m["clobTokenIds"]), json.loads(m["outcomes"])
            out.append({"strike": float(str(m["groupItemTitle"]).replace(",", "").replace("$", "")),
                        "yes": toks[outs.index("Yes")], "cond": m["conditionId"], "slug": m.get("slug")})
        except Exception:
            continue
    return out


def books(tokens):
    d = req("POST", f"{CLOB}/books", json=[{"token_id": t} for t in tokens])
    out = {}
    for b in d or []:
        bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
        asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
        bb = max(bids, default=(None, 0)); ba = min(asks, default=(None, 0))
        out[b.get("asset_id")] = {"bid": bb[0], "bid_sz": bb[1], "ask": ba[0], "ask_sz": ba[1]}
    return out


def recent_trades(cond, since):
    d = get(f"{DAPI}/trades", {"condition": cond, "limit": 100})
    return [t for t in (d or {}).get("data", []) if t.get("timestamp", 0) > since]


# ------------------------------ files ------------------------------
def append(path, cols, rows):
    if not rows: return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new: w.writeheader()
        w.writerows(rows)


def read(path):
    if not os.path.exists(path): return []
    with open(path, newline="") as f: return list(csv.DictReader(f))


now_iso = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
SNAP = ["time_utc", "hour_end_utc", "strike", "minutes_left", "btc", "vol_min", "fair_yes",
        "yes_bid", "yes_bid_sz", "yes_ask", "yes_ask_sz"]
OPP = ["time_utc", "hour_end_utc", "strike", "side", "price", "fair", "gap", "dollars_available", "minutes_left"]
TAKER = ["time_utc", "hour_end_utc", "strike", "side", "price", "fair", "gap", "shares", "stake",
         "minutes_left", "btc", "market_slug"]
MAKER = ["time_utc", "hour_end_utc", "strike", "side", "price", "fair", "trade_price", "shares", "stake",
         "minutes_left", "market_slug"]
SETTLED = ["outcome_yes", "payout", "pnl", "settled_utc"]


# ------------------------------ main loop ------------------------------
def run_hour():
    end_ts = (int(time.time()) // 3600 + 1) * 3600
    strikes = load_strikes(end_ts)
    if not strikes:
        print("No ladder market for", ladder_slug(end_ts)); return
    print(f"Hour ending {datetime.fromtimestamp(end_ts, ET):%I %p ET}: {len(strikes)} strikes")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    taken = {(r["hour_end_utc"], r["strike"]) for r in read(f"{DATA}/taker_trades.csv")}
    quotes = {}            # strike -> list of (time, bid, ask, fair) history this hour
    maker_n = {}           # strike -> fills this hour
    last_trade_ts = {s["cond"]: int(time.time()) for s in strikes}
    vol, vol_t, snap_t, trades_t, polls = None, 0, 0, 0, 0
    while True:
        t0 = time.time()
        if datetime.fromtimestamp(t0, ET).minute >= STOP_AT_MIN or t0 >= end_ts - 90: break
        if t0 - vol_t > 60:
            vol = btc_vol() or vol; vol_t = t0
        src, price = btc_live()
        if price and vol:
            ml = (end_ts - t0) / 60
            near = [s for s in strikes if abs(s["strike"] - price) <= STRIKE_RANGE]
            bk = books([s["yes"] for s in near])
            stamp = now_iso(); snaps, opps, takes = [], [], []
            for s in near:
                b = bk.get(s["yes"])
                if not b: continue
                fv = fair_above(price, s["strike"], vol, ml)
                quotes.setdefault(s["strike"], []).append((t0, fv - HALF_SPREAD, fv + HALF_SPREAD, fv))
                if t0 - snap_t >= SNAPSHOT_EVERY_SEC:
                    snaps.append({"time_utc": stamp, "hour_end_utc": end_ts, "strike": s["strike"],
                                  "minutes_left": round(ml, 2), "btc": price, "vol_min": f"{vol:.6f}",
                                  "fair_yes": round(fv, 4), "yes_bid": b["bid"], "yes_bid_sz": b["bid_sz"],
                                  "yes_ask": b["ask"], "yes_ask_sz": b["ask_sz"]})
                cands = []
                if b["ask"] and PRICE_BAND[0] <= b["ask"] <= PRICE_BAND[1]:
                    cands.append(("YES", b["ask"], fv, b["ask"] * b["ask_sz"]))
                if b["bid"] and PRICE_BAND[0] <= 1 - b["bid"] <= PRICE_BAND[1]:
                    cands.append(("NO", round(1 - b["bid"], 4), 1 - fv, (1 - b["bid"]) * b["bid_sz"]))
                for side, px, f, dollars in cands:
                    g = f - px
                    if g >= LOG_GAP:
                        opps.append({"time_utc": stamp, "hour_end_utc": end_ts, "strike": s["strike"], "side": side,
                                     "price": px, "fair": round(f, 4), "gap": round(g, 4),
                                     "dollars_available": round(dollars, 2), "minutes_left": round(ml, 2)})
                    key = (str(end_ts), str(s["strike"]))
                    if g >= GAP and dollars >= MIN_DOLLARS and key not in taken and src == "binance" or \
                            (ALLOW_FALLBACK and g >= GAP and dollars >= MIN_DOLLARS and key not in taken):
                        takes.append({"time_utc": stamp, "hour_end_utc": end_ts, "strike": s["strike"], "side": side,
                                      "price": px, "fair": round(f, 4), "gap": round(g, 4),
                                      "shares": round(STAKE / px, 2), "stake": STAKE, "minutes_left": round(ml, 2),
                                      "btc": price, "market_slug": s["slug"]})
                        taken.add(key)
            if snaps: snap_t = t0
            append(f"{DATA}/snapshots/{day}.csv", SNAP, snaps)
            append(f"{DATA}/opportunities/{day}.csv", OPP, opps)
            append(f"{DATA}/taker_trades.csv", TAKER, takes)
            # ---- maker fills: real trades that crossed our imaginary limit orders ----
            if t0 - trades_t >= TRADES_EVERY_SEC and (src == "binance" or ALLOW_FALLBACK):
                trades_t = t0; fills = []
                for s in near:
                    hist = quotes.get(s["strike"])
                    if not hist: continue
                    for tr in sorted(recent_trades(s["cond"], last_trade_ts[s["cond"]]), key=lambda x: x["timestamp"]):
                        last_trade_ts[s["cond"]] = max(last_trade_ts[s["cond"]], tr["timestamp"])
                        if maker_n.get(s["strike"], 0) >= MAKER_CAP: break
                        live = [q for q in hist if q[0] <= tr["timestamp"]]
                        if not live: continue
                        _, qb, qa, qf = live[-1]
                        p, out = float(tr["price"]), tr.get("outcome")
                        sh = min(MAKER_SHARES, float(tr.get("size", 0)))
                        if sh <= 0: continue
                        if out == "Yes" and PRICE_BAND[0] <= qa <= PRICE_BAND[1] and p >= qa:
                            side, px = "SELL_YES", qa      # we sold Yes at our ask (= bought No at 1-ask)
                        elif out == "No" and PRICE_BAND[0] <= qb <= PRICE_BAND[1] and 1 - p <= qb:
                            side, px = "BUY_YES", qb
                        else:
                            continue
                        maker_n[s["strike"]] = maker_n.get(s["strike"], 0) + 1
                        fills.append({"time_utc": now_iso(), "hour_end_utc": end_ts, "strike": s["strike"], "side": side,
                                      "price": round(px, 4), "fair": round(qf, 4), "trade_price": p, "shares": sh,
                                      "stake": round(sh * (px if side == "BUY_YES" else 1 - px), 2),
                                      "minutes_left": round((end_ts - tr["timestamp"]) / 60, 2), "market_slug": s["slug"]})
                append(f"{DATA}/maker_fills.csv", MAKER, fills)
            polls += 1
            if polls % 12 == 0:
                print(f"{datetime.now(ET):%H:%M:%S} BTC {price:,.0f} ({src}) | {len(near)} strikes | "
                      f"taker {len(taken)} | maker fills {sum(maker_n.values())}")
        time.sleep(max(0.2, POLL_SEC - (time.time() - t0)))


# ------------------------------ settle + summary ------------------------------
_outcomes = {}
def yes_won(slug):
    if slug in _outcomes: return _outcomes[slug]
    m = get(f"{GAMMA}/markets", {"slug": slug})
    if not m or not m[0].get("closed"): return None
    try:
        px, outs = json.loads(m[0]["outcomePrices"]), json.loads(m[0]["outcomes"])
        _outcomes[slug] = float(px[outs.index("Yes")]) > 0.5
    except Exception:
        return None
    return _outcomes[slug]


def settle(src_file, out_file, cols, pnl_fn):
    done = {(r["time_utc"], r["strike"], r["side"]) for r in read(out_file)}
    out = []
    for t in read(src_file):
        if (t["time_utc"], t["strike"], t["side"]) in done or time.time() < int(t["hour_end_utc"]) + 300: continue
        y = yes_won(t["market_slug"])
        if y is None: continue
        payout, pnl = pnl_fn(t, y)
        out.append({**t, "outcome_yes": int(y), "payout": round(payout, 2), "pnl": round(pnl, 2), "settled_utc": now_iso()})
    append(out_file, cols + SETTLED, out)


def taker_pnl(t, y):
    won = y if t["side"] == "YES" else not y
    pay = float(t["shares"]) if won else 0.0
    return pay, pay - float(t["stake"]) - 0.01 * float(t["stake"])   # ~1% taker fee


def maker_pnl(t, y):
    sh, px = float(t["shares"]), float(t["price"])
    if t["side"] == "BUY_YES":
        pay = sh if y else 0.0; return pay, pay - sh * px
    pay = sh if not y else 0.0; return pay, pay - sh * (1 - px)      # no maker fees


def block(name, rows):
    if not rows: return [f"**{name}:** no settled trades yet.", ""]
    stake = sum(float(r["stake"]) for r in rows); pnl = sum(float(r["pnl"]) for r in rows)
    wins = sum(1 for r in rows if float(r["pnl"]) > 0); roi = pnl / stake if stake else 0
    return [f"**{name}**", "", "| Metric | Value |", "|---|---|",
            f"| Settled trades | {len(rows)} |", f"| Win rate | {wins/len(rows)*100:.1f}% |",
            f"| Paper staked | ${stake:,.0f} |", f"| Paper P&L (after est. fees) | ${pnl:+,.2f} |",
            f"| ROI | {roi*100:+.2f}% |", f"| vs 3.2% target | **{'ABOVE' if roi > TARGET_ROI else 'BELOW'}** |", ""]


def summary():
    T, M = read(f"{DATA}/taker_results.csv"), read(f"{DATA}/maker_results.csv")
    lines = ["# Paper bot -- running results", "",
             f"_Updated {datetime.now(ET):%b %d, %I:%M %p ET}. No real money._", ""]
    lines += block(f"Taker (buy real asks {GAP*100:.0f}c+ below fair odds)", T)
    lines += block(f"Maker (limit orders at fair ±{HALF_SPREAD*100:.0f}c, filled by real trades)", M)
    if len(T) + len(M) < 300: lines.append("_Small sample so far -- give it a couple of weeks before judging._")
    open("SUMMARY.md", "w").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    if (sys.argv[1] if len(sys.argv) > 1 else "hour") == "hour": run_hour()
    settle(f"{DATA}/taker_trades.csv", f"{DATA}/taker_results.csv", TAKER, taker_pnl)
    settle(f"{DATA}/maker_fills.csv", f"{DATA}/maker_results.csv", MAKER, maker_pnl)
    summary(); print("Settled + summary updated.")
