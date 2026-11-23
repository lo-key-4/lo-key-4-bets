# Polymarket US BTC paper bot

Paper trading only -- no money, no API keys, no logins. Runs every hour on GitHub Actions.

Watches Polymarket US **BTC Up or Down** markets (5-minute, 15-minute, hourly). Every ~5 seconds it
records the real order book, estimates fair odds from BTC's price (median of Coinbase / Kraken /
Bitstamp as a stand-in for the CF Benchmarks BRTI index these markets settle on), and paper-tests:

- **TAKER**: buy when fair odds beat the real ask by 3c+ after Polymarket US fees
- **SCALP**: buy the favored side at 55-65c, exit at +10c / -10c on the real bid
- **MAKER**: imaginary limit orders at fair +/-3c, filled only when real trades print through them

Results: **SUMMARY_US.md**. (`paper.py` / `data/` = earlier test on the international exchange, retired.)
