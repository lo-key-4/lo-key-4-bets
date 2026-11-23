# Polymarket BTC hourly paper bot

Paper trading only -- no money, no API keys, no logins.

Runs every hour on GitHub Actions. Every ~5 seconds it records the real order book for the
"Bitcoin above ___" hourly strikes, compares it to fair odds from Binance BTC/USDT, and logs
paper trades when a real ask is 5c+ below fair value. Results: see **SUMMARY.md**.

v2 also tests a maker strategy: imaginary limit orders at fair value +/-3c, counted as filled only when a real Polymarket trade crosses them.
