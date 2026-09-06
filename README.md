# Polymarket BTC 5-minute entry bot

An entry-only Python 3.11+ bot for Polymarket BTC Up/Down 5-minute markets.
It buys the leading outcome only when its best buy price is at least $0.80 and
there are 150 seconds or fewer left in the market. It allows one entry per
market window and never changes between paper and live modes by itself.
Market discovery directly queries Polymarket Gamma for the current
`btc-updown-5m-<UTC-window-timestamp>` market, which rolls every five minutes.

> **Important:** This is trading software, not investment advice. It has no
> early-exit logic: a filled position is held until Polymarket resolves it.
> Ask before adding exit logic or changing the strategy.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

The requested `py-clob-client` package is pinned to `0.34.6`. Its upstream
repository is currently archived and recommends Polymarket's newer unified
SDK, so test paper mode and revalidate the live integration before funding it.

## Configure securely

All configuration is in the `BotConfig` object at the top of
`btc_five_minute_bot.py`; environment variables override its documented
defaults. Do not put private keys or API credentials in source code.

```bash
# Default and safest mode. This is the only mode selection; it is never changed by the bot.
export POLYMARKET_BOT_MODE=PAPER

# Optional strategy/operations settings (shown with their defaults).
export PRICE_THRESHOLD=0.80
export TIME_THRESHOLD_SECONDS=150
export TRADE_SIZE_USDC=1.00
export MAX_DAILY_LOSS_USDC=10.00
export POLL_INTERVAL_SECONDS=5
export MAX_CONSECUTIVE_ERRORS=8
export INITIAL_BACKOFF_SECONDS=2
export MAX_BACKOFF_SECONDS=60
```

For a private local `.env`, add it to `.gitignore` and load it with your shell
or secret manager. Never commit it.

## Run paper trading

```bash
POLYMARKET_BOT_MODE=PAPER python3 btc_five_minute_bot.py
```

Paper mode uses live Gamma/CLOB prices but makes no authenticated request and
does not submit an order. A simulated fill is recorded at the observed price.

## View trades and results

Run this in a second terminal in the same folder as the bot:

```bash
python3 trade_dashboard.py
```

Open `http://127.0.0.1:8080` in a browser. This local, read-only UI refreshes
every five seconds and shows accepted fills, open positions, resolved P/L, and
unavailable entries. It only reads the bot's JSONL/state files; it cannot
place or modify trades. To use a different log folder or port:

```bash
python3 trade_dashboard.py --directory /path/to/bot/logs --port 8081
```

## Switch to live trading deliberately

Only change the explicit mode variable when you intend to submit real orders:

```bash
export POLYMARKET_BOT_MODE=LIVE
export POLYMARKET_PRIVATE_KEY='...'
# Required when funds live in a proxy/smart wallet; optional for direct EOA use.
export POLYMARKET_FUNDER_ADDRESS='0x...'
# Use the signature type appropriate for your wallet: 0=EOA, 1=Magic/email, 2=proxy.
export POLYMARKET_SIGNATURE_TYPE=0
python3 btc_five_minute_bot.py
```

The bot derives CLOB API credentials through `py-clob-client`; do not paste
API secrets into the code. Ensure your wallet has funds and Polymarket-required
allowances before live operation. Live orders are fill-or-kill limit buys,
set to the observed qualifying price. They will not execute at a worse price
if the market moves before submission; in that case the FOK order is rejected.

## Files and safety behavior

- `paper_trades.jsonl` / `live_trades.jsonl`: one JSON record per simulated or
  live accepted fill, including token ID, price, size, timestamp, and end time.
- `bot_events.jsonl`: structured attempts, fills, skips, retries, circuit
  breaker activity, settlement updates, and critical stops.
- `bot_state.json`: persistent one-entry-per-market tracking and locally
  observed settled P/L. Keep this file when restarting.

The daily-loss circuit breaker sums locally observed resolved losses for the
current UTC day and prevents new entries after `MAX_DAILY_LOSS_USDC`. It
applies in both modes. Transient failures use exponential backoff; after the
configured consecutive-error threshold the process exits with a critical log
instead of silently retrying indefinitely.

During every final-150-second market check, a leading price below 80¢ is
recorded as an `entry_unavailable` event. This makes missed entries visible in
the dashboard and event log without changing the entry rule.
