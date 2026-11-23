#!/usr/bin/env python3
"""
Polymarket US -- BTC Up/Down PAPER bot (5-minute, 15-minute, hourly). No money, no keys.

Every ~5 seconds:
  * BTC price ~ BRTI (median of Coinbase / Kraken / Bitstamp USD prices) + recent volatility
  * Fair odds that BTC ends each window at/above its official "price to beat"
  * Real Polymarket US order book (best bid/ask + size) and last trade
Three paper strategies, all priced at REAL bids/asks with Polymarket US fees:
  TAKER  - buy Up/Down when fair odds beat the real ask by >= GAP after fees; hold to settle
  SCALP  - buy the side fair odds favor when its ask is 55-65c; sell at +10c or -10c on the
           real bid (or hold to settle)
  MAKER  - imaginary limit orders at fair +/- HALF_SPREAD; filled only when a real trade
           prints at/through them (no taker fee for makers)
"""
import csv, json, math, os, statistics, sys, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests

KINDS = {"5m": 300, "15m": 900, "1h": 3600}
POLL_SEC = 5
GAP = 0.03
SCALP_BAND, SCALP_TP, SCALP_SL = (0.55, 0.65), 0.10, 0.10
HALF_SPREAD = 0.03
STAKE = 10.0
MAKER_SHARES, MAKER_CAP = 10, 3
MIN_DOLLARS = 5.0
FEE_COEF = 0.095          # Polymarket US feeCoefficient (taker fee ~= coef * p * (1-p) per contract)
STOP_AT_MIN = 59
SNAP_EVERY = 15
TARGET_ROI = 0.032
DATA = "data_us"
G = "https://gateway.polymarket.us"
ET = ZoneInfo("America/New_York")
S = requests.Session(); S.headers["User-Agent"] = "paper-bot-us/1.0"


def get(url, params=None, tries=3):
    for a in range(tries):
        try:
            r = S.get(url, params=params, timeout=8)
            if r.status_code == 429:
                time.sleep(1 + 2 * a); continue
            r.raise_for_status(); return r.json()
        except Exception:
            if a == tries - 1: return None
            time.sleep(0.5 + a)


fee = lambda p: FEE_COEF * p * (1 - p)
now_iso = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------ BTC (BRTI proxy) ------------------------------
def btc_price():
    px = []
    d = get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
    if d and d.get("price"): px.append(float(d["price"]))
    d = get("https://api.kraken.com/0/public/Ticker", {"pair": "XBTUSD"})
    try: px.append(float(list(d["result"].values())[0]["c"][0]))
    except Exception: pass
    d = get("https://www.bitstamp.net/api/v2/ticker/btcusd/")
    try: px.append(float(d["last"]))
    except Exception: pass
    return (statistics.median(px), len(px)) if px else (None, 0)


def btc_vol():
    c = get("https://api.exchange.coinbase.com/products/BTC-USD/candles", {"granularity": 60})
    closes = [float(k[4]) for k in sorted(c or [])][-122:]
    if len(closes) < 60: return None
    return statistics.pstdev([math.log(b / a) for a, b in zip(closes, closes[1:])])


def fair_up(price, strike, vol, minutes_left):
    eff = max(minutes_left - 2 / 3, 0.05)          # settlement = average of the final minute
    return 0.5 * (1 + math.erf(math.log(price / strike) / (vol * math.sqrt(eff)) / math.sqrt(2)))


# ------------------------------ Polymarket US ------------------------------
def window(kind, t):
    step = KINDS[kind]; start = int(t) // step * step
    return start, start + step, time.strftime(f"btc-updown-{kind}-%Y-%m-%d-%H%Mz", time.gmtime(start))


_mk = {}
def market(slug):
    m = _mk.get(slug)
    if m and m.get("strike"): return m
    e = get(f"{G}/v1/events/slug/{slug}"); e = (e or {}).get("event", e or {})
    if not e.get("markets"): return None
    mm = e["markets"][0]; ptb = (mm.get("assetPriceTerms") or {}).get("priceToBeat")
    _mk[slug] = {"slug": mm["slug"], "strike": float(ptb["value"]) if ptb else None}
    return _mk[slug]


def book(mslug):
    b = (get(f"{G}/v1/markets/{mslug}/book") or {}).get("marketData", {})
    bids = [(float(x["px"]["value"]), float(x["qty"])) for x in b.get("bids", [])]
    offs = [(float(x["px"]["value"]), float(x["qty"])) for x in b.get("offers", [])]
    bb = max(bids, default=(None, 0)); ba = min(offs, default=(None, 0))
    return {"bid": bb[0], "bid_sz": bb[1], "ask": ba[0], "ask_sz": ba[1]}


def bbo(mslug):
    b = (get(f"{G}/v1/markets/{mslug}/bbo") or {}).get("marketData", {})
    lt = (b.get("lastTradePx") or {}).get("value") if isinstance(b.get("lastTradePx"), dict) else b.get("lastTradePx")
    return (float(lt) if lt else None), float(b.get("sharesTraded") or 0)


def up_won(mslug):
    m = get(f"{G}/v1/markets/{mslug}/bbo")
    md = (m or {}).get("marketData", {})
    sp = md.get("settlementPx")
    if isinstance(sp, dict): sp = sp.get("value")
    if sp is None: return None
    return float(sp) > 0.5


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


SNAP = ["time_utc", "kind", "market", "minutes_left", "btc", "sources", "strike", "vol_min", "fair_up",
        "up_bid", "up_bid_sz", "up_ask", "up_ask_sz", "last_trade", "shares_traded"]
TRADE = ["time_utc", "strategy", "kind", "market", "window_end", "side", "entry", "exit", "exit_reason",
         "shares", "stake", "fees", "fair_at_entry", "minutes_left"]


# ------------------------------ main loop ------------------------------
def run():
    now = time.time()
    end_of_run = (int(now) // 3600) * 3600 + STOP_AT_MIN * 60
    if end_of_run - now < 5 * 60:          # started late (GitHub delays): cover the next hour instead
        end_of_run += 3600
    end_of_run = min(end_of_run, now + 60 * 60)
    vol, vol_t, snap_t = None, 0, 0
    state = {}   # market -> {"taker":bool, "scalp":dict|None/'done', "maker_n":int, "quotes":(bid,ask,fv), "traded":float}
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d"); polls = 0
    while time.time() < end_of_run:
        t0 = time.time()
        if t0 - vol_t > 60: vol = btc_vol() or vol; vol_t = t0
        price, nsrc = btc_price()
        if price and vol:
            snaps, trades = [], []
            for kind in KINDS:
                start, end, slug = window(kind, t0)
                ml = (end - t0) / 60
                if ml < 0.2: continue
                m = market(slug)
                if not m or not m["strike"]: continue
                b = book(m["slug"]); lt, traded = bbo(m["slug"])
                fv = fair_up(price, m["strike"], vol, ml)
                st = state.setdefault(m["slug"], {"taker": False, "scalp": None, "maker_n": 0, "quotes": None,
                                                  "traded": traded, "end": end, "kind": kind})
                if t0 - snap_t >= SNAP_EVERY:
                    snaps.append({"time_utc": now_iso(), "kind": kind, "market": m["slug"], "minutes_left": round(ml, 2),
                                  "btc": round(price, 2), "sources": nsrc, "strike": m["strike"], "vol_min": f"{vol:.6f}",
                                  "fair_up": round(fv, 4), "up_bid": b["bid"], "up_bid_sz": b["bid_sz"], "up_ask": b["ask"],
                                  "up_ask_sz": b["ask_sz"], "last_trade": lt, "shares_traded": traded})
                base = {"kind": kind, "market": m["slug"], "window_end": end, "minutes_left": round(ml, 2)}
                # sides available to BUY at real prices: Up at ask, Down at (1 - Up bid)
                sides = []
                if b["ask"] and 0.02 <= b["ask"] <= 0.98: sides.append(("UP", b["ask"], fv, b["ask"] * b["ask_sz"]))
                if b["bid"] and 0.02 <= 1 - b["bid"] <= 0.98: sides.append(("DOWN", round(1 - b["bid"], 4), 1 - fv, (1 - b["bid"]) * b["bid_sz"]))
                # ---- TAKER ----
                if not st["taker"] and nsrc >= 2:
                    for side, px, f, dollars in sides:
                        if f - px - fee(px) >= GAP and dollars >= MIN_DOLLARS:
                            sh = round(STAKE / px, 2)
                            trades.append({**base, "time_utc": now_iso(), "strategy": "TAKER", "side": side, "entry": px,
                                           "exit": "", "exit_reason": "settle", "shares": sh, "stake": STAKE,
                                           "fees": round(sh * fee(px), 4), "fair_at_entry": round(f, 4)})
                            st["taker"] = True; break
                # ---- SCALP (5m & 15m) ----
                if kind != "1h" and nsrc >= 2:
                    sc = st["scalp"]
                    if sc is None and ml > 1:
                        for side, px, f, dollars in sides:
                            if SCALP_BAND[0] <= px <= SCALP_BAND[1] and f > 0.5 and dollars >= MIN_DOLLARS:
                                st["scalp"] = {"side": side, "entry": px, "shares": round(STAKE / px, 2), "fair": f, "ml": ml,
                                               "t": now_iso()}
                                break
                    elif isinstance(sc, dict):
                        exit_px = b["bid"] if sc["side"] == "UP" else (1 - b["ask"] if b["ask"] else None)
                        if exit_px is not None:
                            reason = "take_profit" if exit_px >= sc["entry"] + SCALP_TP else \
                                     "stop_loss" if exit_px <= sc["entry"] - SCALP_SL else None
                            if reason:
                                fees = sc["shares"] * (fee(sc["entry"]) + fee(exit_px))
                                trades.append({**base, "time_utc": sc["t"], "strategy": "SCALP", "side": sc["side"],
                                               "entry": sc["entry"], "exit": round(exit_px, 4), "exit_reason": reason,
                                               "shares": sc["shares"], "stake": STAKE, "fees": round(fees, 4),
                                               "fair_at_entry": round(sc["fair"], 4), "minutes_left": round(sc["ml"], 2)})
                                st["scalp"] = "done"
                # ---- MAKER ---- (fill when a new real trade prints at/through our quote)
                q = st["quotes"]
                if q and traded > st["traded"] and lt is not None and st["maker_n"] < MAKER_CAP and nsrc >= 2:
                    qb, qa, qf = q; new = min(traded - st["traded"], MAKER_SHARES)
                    side = "SELL_UP" if (0.02 <= qa <= 0.98 and lt >= qa) else "BUY_UP" if (0.02 <= qb <= 0.98 and lt <= qb) else None
                    if side:
                        px = qa if side == "SELL_UP" else qb
                        trades.append({**base, "time_utc": now_iso(), "strategy": "MAKER", "side": side, "entry": round(px, 4),
                                       "exit": "", "exit_reason": "settle", "shares": round(new, 2),
                                       "stake": round(new * (px if side == "BUY_UP" else 1 - px), 2), "fees": 0,
                                       "fair_at_entry": round(qf, 4)})
                        st["maker_n"] += 1
                st["traded"] = traded; st["quotes"] = (fv - HALF_SPREAD, fv + HALF_SPREAD, fv)
            if snaps: snap_t = t0
            append(f"{DATA}/snapshots/{day}.csv", SNAP, snaps)
            append(f"{DATA}/open_trades.csv", TRADE, trades)
            polls += 1
            if polls % 12 == 0:
                print(f"{datetime.now(ET):%H:%M:%S} BTC {price:,.0f} ({nsrc} sources) | trades logged {len(read(f'{DATA}/open_trades.csv'))}")
        # unclosed scalps at window end -> settle like holds
        for mk, st in state.items():
            sc = st.get("scalp")
            if isinstance(sc, dict) and time.time() >= st["end"]:
                append(f"{DATA}/open_trades.csv", TRADE, [{"time_utc": sc["t"], "strategy": "SCALP", "kind": st["kind"],
                       "market": mk, "window_end": st["end"], "side": sc["side"], "entry": sc["entry"], "exit": "",
                       "exit_reason": "settle", "shares": sc["shares"], "stake": STAKE,
                       "fees": round(sc["shares"] * fee(sc["entry"]), 4), "fair_at_entry": round(sc["fair"], 4),
                       "minutes_left": round(sc["ml"], 2)}])
                st["scalp"] = "done"
        time.sleep(max(0.2, POLL_SEC - (time.time() - t0)))


# ------------------------------ settle + summary ------------------------------
def settle():
    done = {(r["time_utc"], r["strategy"], r["market"], r["side"]) for r in read(f"{DATA}/results.csv")}
    out, cache = [], {}
    for t in read(f"{DATA}/open_trades.csv"):
        key = (t["time_utc"], t["strategy"], t["market"], t["side"])
        if key in done: continue
        sh, entry, fees = float(t["shares"]), float(t["entry"]), float(t["fees"])
        if t["exit"]:   # scalp closed before expiry
            pnl = sh * (float(t["exit"]) - entry) - fees
        else:
            if time.time() < int(t["window_end"]) + 90: continue
            if t["market"] not in cache: cache[t["market"]] = up_won(t["market"])
            uw = cache[t["market"]]
            if uw is None: continue
            if t["side"] in ("UP", "BUY_UP"): won = uw
            else: won = not uw
            cost = entry if t["side"] in ("UP", "DOWN", "BUY_UP") else 1 - entry
            pnl = sh * ((1.0 if won else 0.0) - cost) - fees
        out.append({**t, "pnl": round(pnl, 4), "settled_utc": now_iso()})
    append(f"{DATA}/results.csv", TRADE + ["pnl", "settled_utc"], out)


def summary():
    R = read(f"{DATA}/results.csv")
    lines = ["# Polymarket US paper bot -- running results", "",
             f"_Updated {datetime.now(ET):%b %d, %I:%M %p ET}. No real money. Real bids/asks, Polymarket US fees included._", "",
             "| Strategy | Market | Trades | Win rate | Staked | P&L | ROI | vs 3.2% |", "|---|---|---|---|---|---|---|---|"]
    for strat in ("TAKER", "SCALP", "MAKER"):
        for kind in ("5m", "15m", "1h"):
            rows = [r for r in R if r["strategy"] == strat and r["kind"] == kind]
            if not rows: continue
            stake = sum(float(r["stake"]) for r in rows); pnl = sum(float(r["pnl"]) for r in rows)
            wins = sum(1 for r in rows if float(r["pnl"]) > 0); roi = pnl / stake if stake else 0
            lines.append(f"| {strat} | {kind} | {len(rows)} | {wins/len(rows)*100:.0f}% | ${stake:,.0f} | ${pnl:+,.2f} | "
                         f"{roi*100:+.1f}% | {'ABOVE' if roi > TARGET_ROI else 'below'} |")
    if len(R) < 300: lines += ["", "_Small sample so far -- judge after ~2 weeks._"]
    open("SUMMARY_US.md", "w").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    if (sys.argv[1] if len(sys.argv) > 1 else "run") == "run": run()
    settle(); summary(); print("settled + summary updated")
