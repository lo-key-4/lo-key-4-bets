# Polymarket BTC hourly paper bot

Paper trading only -- no money, no API keys, no logins.

Runs every hour on GitHub Actions. Each minute it records the real order book for the
"Bitcoin above ___" hourly strikes, compares it to fair odds from Binance BTC/USDT, and logs
paper trades when a real ask is 5c+ below fair value. Results: see **SUMMARY.md**.
