#!/usr/bin/env python3
"""Polymarket BTC Up/Down 5-minute entry bot.

This is intentionally an entry-only bot. Positions are held until Polymarket
resolves the market; it does not contain early-exit logic.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import asyncio
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class BotConfig:
    """All operator-tunable values live here (or in the listed environment variables)."""

    # SAFETY: this is the only mode switch. The bot never changes it itself.
    mode: str = os.getenv("POLYMARKET_BOT_MODE", "PAPER").upper()  # PAPER or LIVE
    price_threshold: float = float(os.getenv("PRICE_THRESHOLD", "0.80"))
    time_threshold_seconds: int = int(os.getenv("TIME_THRESHOLD_SECONDS", "150"))
    trade_size_usdc: float = float(os.getenv("TRADE_SIZE_USDC", "1.00"))
    difference_5_usdc_threshold: float = float(os.getenv("DIFFERENCE_5_USDC_THRESHOLD", "30"))
    difference_10_usdc_threshold: float = float(os.getenv("DIFFERENCE_10_USDC_THRESHOLD", "50"))
    difference_25_usdc_threshold: float = float(os.getenv("DIFFERENCE_25_USDC_THRESHOLD", "65"))
    difference_50_usdc_threshold: float = float(os.getenv("DIFFERENCE_50_USDC_THRESHOLD", "80"))
    difference_100_usdc_threshold: float = float(os.getenv("DIFFERENCE_100_USDC_THRESHOLD", "100"))
    difference_250_usdc_threshold: float = float(os.getenv("DIFFERENCE_250_USDC_THRESHOLD", "120"))
    difference_500_usdc_threshold: float = float(os.getenv("DIFFERENCE_500_USDC_THRESHOLD", "150"))
    difference_5_trade_size_usdc: float = float(os.getenv("DIFFERENCE_5_TRADE_SIZE_USDC", "5"))
    difference_10_trade_size_usdc: float = float(os.getenv("DIFFERENCE_10_TRADE_SIZE_USDC", "10"))
    difference_25_trade_size_usdc: float = float(os.getenv("DIFFERENCE_25_TRADE_SIZE_USDC", "25"))
    difference_50_trade_size_usdc: float = float(os.getenv("DIFFERENCE_50_TRADE_SIZE_USDC", "50"))
    difference_100_trade_size_usdc: float = float(os.getenv("DIFFERENCE_100_TRADE_SIZE_USDC", "100"))
    difference_250_trade_size_usdc: float = float(os.getenv("DIFFERENCE_250_TRADE_SIZE_USDC", "250"))
    difference_500_trade_size_usdc: float = float(os.getenv("DIFFERENCE_500_TRADE_SIZE_USDC", "500"))
    reference_capture_delay_seconds: int = int(os.getenv("REFERENCE_CAPTURE_DELAY_SECONDS", "15"))
    twap_max_age_seconds: int = int(os.getenv("TWAP_MAX_AGE_SECONDS", "30"))
    max_daily_loss_usdc: float = float(os.getenv("MAX_DAILY_LOSS_USDC", "10.00"))
    poll_interval_seconds: float = float(os.getenv("POLL_INTERVAL_SECONDS", "5"))
    max_consecutive_errors: int = int(os.getenv("MAX_CONSECUTIVE_ERRORS", "8"))
    initial_backoff_seconds: float = float(os.getenv("INITIAL_BACKOFF_SECONDS", "2"))
    max_backoff_seconds: float = float(os.getenv("MAX_BACKOFF_SECONDS", "60"))
    gamma_url: str = "https://gamma-api.polymarket.com/markets"
    clob_host: str = "https://clob.polymarket.com"
    chain_id: int = 137
    event_log: Path = Path(os.getenv("EVENT_LOG_FILE", "bot_events.jsonl"))
    state_file: Path = Path(os.getenv("STATE_FILE", "bot_state.json"))

    @property
    def trade_log(self) -> Path:
        return Path("paper_trades.jsonl" if self.mode == "PAPER" else "live_trades.jsonl")

    def validate(self) -> None:
        if self.mode not in {"PAPER", "LIVE"}:
            raise ValueError("POLYMARKET_BOT_MODE must be exactly PAPER or LIVE")
        if not 0 < self.price_threshold <= 1:
            raise ValueError("price threshold must be in (0, 1]")
        if self.time_threshold_seconds < 0 or self.trade_size_usdc <= 0:
            raise ValueError("time threshold must be non-negative and trade size positive")
        thresholds = (self.difference_5_usdc_threshold, self.difference_10_usdc_threshold,
                      self.difference_25_usdc_threshold, self.difference_50_usdc_threshold,
                      self.difference_100_usdc_threshold, self.difference_250_usdc_threshold,
                      self.difference_500_usdc_threshold)
        if thresholds != tuple(sorted(thresholds)) or len(set(thresholds)) != len(thresholds):
            raise ValueError("difference thresholds must be strictly increasing")
        if self.max_daily_loss_usdc < 0 or self.max_consecutive_errors < 1:
            raise ValueError("max daily loss must be non-negative and error threshold positive")


@dataclass(frozen=True)
class Outcome:
    name: str
    token_id: str


@dataclass(frozen=True)
class Market:
    market_id: str
    gamma_id: str
    question: str
    start_time: datetime
    end_time: datetime
    outcomes: tuple[Outcome, ...]


class JsonlLogger:
    def __init__(self, config: BotConfig) -> None:
        self.config = config

    def write(self, kind: str, **data: Any) -> None:
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": kind, **data}
        with self.config.event_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    def trade(self, record: dict[str, Any]) -> None:
        with self.config.trade_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")


class ChainlinkTwapFeed:
    """Caches Polymarket RTDS's official relay of Chainlink BTC/USD 60-second TWAP."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._price: float | None = None
        self._timestamp_ms: int | None = None

    def start(self) -> None:
        threading.Thread(target=self._run, name="chainlink-twap", daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                asyncio.run(self._consume())
                logging.getLogger("polymarket_bot").warning("RTDS stream ended; reconnecting in 5 seconds.")
            except Exception as exc:
                logging.getLogger("polymarket_bot").warning("RTDS stream error: %s; reconnecting in 5 seconds.", exc)
            time.sleep(5)

    async def _consume(self) -> None:
        from polymarket import AsyncPublicClient
        from polymarket.streams import CryptoPricesChainlinkTwapSpec
        async with AsyncPublicClient() as client:
            async with await client.subscribe(
                CryptoPricesChainlinkTwapSpec(window_seconds=60, symbols=["btc/usd"])
            ) as stream:
                async for event in stream:
                    with self._lock:
                        self._price = float(event.payload.value)
                        self._timestamp_ms = int(event.payload.timestamp)

    def latest(self, max_age_seconds: int) -> float | None:
        with self._lock:
            if self._price is None or self._timestamp_ms is None:
                return None
            if time.time() - self._timestamp_ms / 1000 > max_age_seconds:
                return None
            return self._price


class PolymarketBot:
    def __init__(self, config: BotConfig) -> None:
        config.validate()
        self.config = config
        self.events = JsonlLogger(config)
        self.log = logging.getLogger("polymarket_bot")
        self.state = self._load_state()
        self.known_market_ids: set[str] = set()
        self._client: Any | None = None
        self.twap_feed = ChainlinkTwapFeed()

    def _load_state(self) -> dict[str, Any]:
        if not self.config.state_file.exists():
            return {"entered_market_ids": [], "open_trades": {}, "settlements": {}, "price_to_beat": {}}
        try:
            data = json.loads(self.config.state_file.read_text(encoding="utf-8"))
            data.setdefault("entered_market_ids", [])
            data.setdefault("open_trades", {})
            data.setdefault("settlements", {})
            data.setdefault("price_to_beat", {})
            return data
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot safely load state file {self.config.state_file}: {exc}") from exc

    def _save_state(self) -> None:
        temporary = self.config.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.config.state_file)

    @property
    def entered_market_ids(self) -> set[str]:
        return set(self.state["entered_market_ids"])

    def _mark_entered(self, market: Market, record: dict[str, Any]) -> None:
        market_id = market.market_id
        self.state["entered_market_ids"].append(market_id)
        self.state["open_trades"][market_id] = {
            "gamma_id": market.gamma_id,
            "outcome": record["outcome"],
            "filled_price": record.get("filled_price", record["price"]),
            "size_usdc": record["size_usdc"],
        }
        self._save_state()  # persist before the next poll to prevent restart duplicates

    def recover_one_trade_from_log(self) -> None:
        """Recover one prior fill per poll, avoiding a burst of Gamma requests after restart."""
        if not self.config.trade_log.exists():
            return
        for line in self.config.trade_log.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                market_id = record["market_id"]
            except (json.JSONDecodeError, KeyError):
                continue
            if market_id in self.state["settlements"] or market_id in self.state["open_trades"]:
                continue
            gamma_id = record.get("gamma_id")
            if not gamma_id:
                rows = self._http_json(f"{self.config.gamma_url}?{urlencode({'condition_ids': market_id})}")
                if not isinstance(rows, list) or not rows:
                    continue
                gamma_id = str(rows[0].get("id", ""))
            if not gamma_id:
                continue
            self.state["open_trades"][market_id] = {
                "gamma_id": gamma_id,
                "outcome": record["outcome"],
                "filled_price": record.get("filled_price", record["price"]),
                "size_usdc": record["size_usdc"],
            }
            if market_id not in self.state["entered_market_ids"]:
                self.state["entered_market_ids"].append(market_id)
            self._save_state()
            self.events.write("trade_state_recovered", market_id=market_id, gamma_id=gamma_id)
            return

    def _http_json(self, url: str) -> Any:
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "btc-5m-bot/1.0"})
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _json_list(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return json.loads(value)
        return []

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _is_btc_five_minute(raw: dict[str, Any]) -> bool:
        event = raw.get("events", [{}])
        event = event[0] if isinstance(event, list) and event else {}
        text = " ".join(str(x) for x in (
            raw.get("question", ""), raw.get("title", ""), raw.get("slug", ""),
            event.get("title", ""), event.get("slug", ""),
        )).lower()
        is_btc = "bitcoin" in text or re.search(r"(?<![a-z])btc(?![a-z])", text)
        is_five_minutes = bool(re.search(r"\b5\s*(?:m|min|minute)", text)) or "5m" in text
        is_directional = "up" in text and "down" in text
        return bool(is_btc and is_five_minutes and is_directional)

    def discover_markets(self) -> list[Market]:
        # Polymarket's BTC 5-minute event slug ends in the UTC window-start Unix timestamp.
        # Querying the current slug directly is reliable and avoids scanning unrelated markets.
        window_start = int(time.time() // 300 * 300)
        query = urlencode({"slug": f"btc-updown-5m-{window_start}"})
        response = self._http_json(f"{self.config.gamma_url}?{query}")
        rows = response.get("data", []) if isinstance(response, dict) else response
        markets: list[Market] = []
        for raw in rows:
            if not isinstance(raw, dict) or not self._is_btc_five_minute(raw):
                continue
            end_time = self._parse_time(raw.get("endDate") or raw.get("endDateIso") or raw.get("closeTime"))
            market_id = str(raw.get("conditionId") or raw.get("id") or "")
            try:
                names, token_ids = self._json_list(raw.get("outcomes")), self._json_list(raw.get("clobTokenIds"))
            except json.JSONDecodeError:
                continue
            if not market_id or not end_time or len(names) != len(token_ids) or len(token_ids) < 2:
                continue
            outcomes = tuple(Outcome(str(name), str(token)) for name, token in zip(names, token_ids))
            markets.append(Market(
                market_id, str(raw.get("id") or ""), str(raw.get("question") or raw.get("title") or market_id),
                datetime.fromtimestamp(window_start, timezone.utc), end_time, outcomes,
            ))
        return markets

    def capture_price_to_beat(self, market: Market) -> None:
        """Save the closest available official 60s Chainlink TWAP at window opening."""
        if market.market_id in self.state["price_to_beat"]:
            return
        if (datetime.now(timezone.utc) - market.start_time).total_seconds() > self.config.reference_capture_delay_seconds:
            return
        if price := self.twap_feed.latest(self.config.twap_max_age_seconds):
            self.state["price_to_beat"][market.market_id] = price
            self._save_state()

    def size_for_difference(self, market: Market) -> tuple[float, float, float, float] | None:
        reference = self.state["price_to_beat"].get(market.market_id)
        current = self.twap_feed.latest(self.config.twap_max_age_seconds)
        if reference is None or current is None:
            return None
        difference = abs(current - float(reference))
        if difference > self.config.difference_500_usdc_threshold:
            size = self.config.difference_500_trade_size_usdc
        elif difference >= self.config.difference_250_usdc_threshold:
            size = self.config.difference_250_trade_size_usdc
        elif difference >= self.config.difference_100_usdc_threshold:
            size = self.config.difference_100_trade_size_usdc
        elif difference >= self.config.difference_50_usdc_threshold:
            size = self.config.difference_50_trade_size_usdc
        elif difference >= self.config.difference_25_usdc_threshold:
            size = self.config.difference_25_trade_size_usdc
        elif difference >= self.config.difference_10_usdc_threshold:
            size = self.config.difference_10_trade_size_usdc
        elif difference >= self.config.difference_5_usdc_threshold:
            size = self.config.difference_5_trade_size_usdc
        else:
            size = self.config.trade_size_usdc
        return size, difference, float(reference), current

    def client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from py_clob_client.client import ClobClient
        except ImportError as exc:
            raise RuntimeError("py-clob-client is not installed; install requirements.txt") from exc
        if self.config.mode == "LIVE":
            private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
            if not private_key:
                raise RuntimeError("POLYMARKET_PRIVATE_KEY is required in LIVE mode")
            kwargs: dict[str, Any] = {"key": private_key, "chain_id": self.config.chain_id}
            if funder := os.getenv("POLYMARKET_FUNDER_ADDRESS"):
                kwargs["funder"] = funder
            if signature_type := os.getenv("POLYMARKET_SIGNATURE_TYPE"):
                kwargs["signature_type"] = int(signature_type)
            self._client = ClobClient(self.config.clob_host, **kwargs)
            self._client.set_api_creds(self._client.create_or_derive_api_creds())
        else:
            self._client = ClobClient(self.config.clob_host)
        return self._client

    def best_buy_price(self, token_id: str) -> float:
        return float(self.client().get_price(token_id, side="BUY")["price"])

    def daily_loss(self) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        return sum(
            -float(value["pnl_usdc"])
            for value in self.state["settlements"].values()
            if value["settled_at"].startswith(today) and float(value["pnl_usdc"]) < 0
        )

    def circuit_breaker_tripped(self) -> bool:
        # Paper testing must continue collecting simulated results; only live funds use this stop.
        return self.config.mode == "LIVE" and self.daily_loss() >= self.config.max_daily_loss_usdc

    def sync_settlements(self) -> None:
        """Record resolved local positions so the daily-loss breaker applies in both modes."""
        changed = False
        for market_id, trade in list(self.state["open_trades"].items()):
            gamma_id = trade.get("gamma_id")
            if not gamma_id:
                continue
            try:
                raw = self._http_json(f"{self.config.gamma_url}/{gamma_id}")
                if not isinstance(raw, dict) or not raw.get("closed"):
                    continue
                names = self._json_list(raw.get("outcomes"))
                prices = self._json_list(raw.get("outcomePrices"))
                final_prices = {str(name): float(price) for name, price in zip(names, prices)}
                final_price = final_prices.get(str(trade["outcome"]))
                final_outcome = next((name for name, value in final_prices.items() if value == 1.0), None)
                if final_price not in {0.0, 1.0}:
                    continue
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                self.events.write("settlement_check_error", market_id=market_id, error=str(exc))
                continue
            pnl = float(trade["size_usdc"]) * (final_price / float(trade["filled_price"]) - 1)
            settlement = {
                "settled_at": datetime.now(timezone.utc).isoformat(),
                "pnl_usdc": pnl,
                "final_price": final_price,
                "final_outcome": final_outcome,
            }
            self.state["settlements"][market_id] = settlement
            del self.state["open_trades"][market_id]
            self.events.write("market_settled", market_id=market_id, **settlement)
            changed = True
        if changed:
            self._save_state()

    def _record_skip(self, market: Market, outcome: Outcome, price: float, remaining: float, reason: str) -> None:
        self.events.write("skipped_opportunity", market_id=market.market_id, token_id=outcome.token_id,
                          price=price, seconds_remaining=remaining, reason=reason)

    def _record_unavailable(self, market: Market, outcome: Outcome, price: float, remaining: float) -> None:
        """Keep an auditable record of every eligible-window check that did not enter."""
        self.events.write("entry_unavailable", market_id=market.market_id, token_id=outcome.token_id,
                          outcome=outcome.name, price=price, seconds_remaining=remaining,
                          reason="price_below_threshold", price_threshold=self.config.price_threshold)

    def consider_market(self, market: Market) -> None:
        remaining = (market.end_time - datetime.now(timezone.utc)).total_seconds()
        if market.market_id in self.entered_market_ids:
            return
        prices = [(outcome, self.best_buy_price(outcome.token_id)) for outcome in market.outcomes]
        outcome, price = max(prices, key=lambda item: item[1])  # leading outcome means highest buy price
        if remaining < 0:
            self._record_skip(market, outcome, price, remaining, "market_expired")
            return
        if remaining > self.config.time_threshold_seconds:
            return
        # Exact entry gate: from 150 seconds through expiry, buy the leader at any price >= threshold.
        if price < self.config.price_threshold:
            self._record_unavailable(market, outcome, price, remaining)
            return
        if self.circuit_breaker_tripped():
            self._record_skip(market, outcome, price, remaining, "daily_loss_circuit_breaker")
            self.log.critical("Daily loss circuit breaker is active; no new trades today.")
            return
        sizing = self.size_for_difference(market)
        if sizing is None:
            self._record_skip(market, outcome, price, remaining, "price_to_beat_or_current_price_unavailable")
            return
        self._enter(market, outcome, price, remaining, *sizing)

    def _enter(self, market: Market, outcome: Outcome, price: float, remaining: float, size_usdc: float,
               price_difference_usdc: float, price_to_beat: float, current_btc_price: float) -> None:
        base = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": self.config.mode,
            "market_id": market.market_id,
            "gamma_id": market.gamma_id,
            "question": market.question,
            "token_id": outcome.token_id,
            "outcome": outcome.name,
            "price": price,
            "size_usdc": size_usdc,
            "price_difference_usdc": price_difference_usdc,
            "price_to_beat": price_to_beat,
            "current_btc_price": current_btc_price,
            "market_end_time": market.end_time.isoformat(),
            "seconds_remaining": remaining,
        }
        self.events.write("trade_attempt", **base)
        if self.config.mode == "PAPER":
            record = {**base, "status": "simulated_fill", "filled_price": price}
            self.events.trade(record)
            self.events.write("trade_fill", **record)
            self._mark_entered(market, record)
            self.log.info("PAPER fill: %s %s at $%.3f", outcome.name, market.market_id, price)
            return

        # FOK at the observed price preserves the selected budget and cannot fill at a worse later price.
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        shares = size_usdc / price
        order = OrderArgs(token_id=outcome.token_id, price=price, size=shares, side=BUY)
        response = self.client().post_order(self.client().create_order(order), OrderType.FOK)
        # FOK allows conservative accounting at the observed price even if it receives price improvement.
        record = {**base, "status": "fok_fill_accepted", "limit_price": price,
                  "filled_price": price, "response": response}
        self.events.trade(record)
        self.events.write("trade_fill", **record)
        # FOK either fills fully or does not execute. Only successful API acknowledgement gets locked.
        if isinstance(response, dict) and response and not response.get("errorMsg"):
            self._mark_entered(market, record)
            self.log.info("LIVE FOK submitted: %s %s", outcome.name, market.market_id)
        else:
            self.events.write("skipped_opportunity", **base, reason="live_order_not_accepted")

    def run_forever(self) -> None:
        self.log.info("Starting bot in %s mode; no automatic mode switching is possible.", self.config.mode)
        self.events.write("startup", mode=self.config.mode, config=asdict(self.config))
        self.twap_feed.start()
        failures = 0
        while True:
            try:
                markets = self.discover_markets()
                self.recover_one_trade_from_log()
                self.sync_settlements()
                current_ids = {market.market_id for market in markets}
                rolled_off = self.known_market_ids - current_ids
                if rolled_off:
                    self.log.info("%d market window(s) rolled off.", len(rolled_off))
                self.known_market_ids = current_ids
                for market in markets:
                    self.capture_price_to_beat(market)
                    self.consider_market(market)
                failures = 0
                time.sleep(self.config.poll_interval_seconds)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
                failures += 1
                delay = min(self.config.initial_backoff_seconds * (2 ** (failures - 1)), self.config.max_backoff_seconds)
                self.events.write("operational_error", consecutive_errors=failures, error=str(exc), retry_in_seconds=delay)
                if failures >= self.config.max_consecutive_errors:
                    self.log.critical("Stopping after %d consecutive errors: %s", failures, exc)
                    self.events.write("critical_stop", consecutive_errors=failures, error=str(exc))
                    raise
                self.log.warning("Transient error (%d/%d): %s; retrying in %.1fs", failures,
                                 self.config.max_consecutive_errors, exc, delay)
                time.sleep(delay)


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    try:
        PolymarketBot(BotConfig()).run_forever()
    except KeyboardInterrupt:
        logging.getLogger("polymarket_bot").info("Stopped by operator.")
        return 0
    except Exception:
        logging.getLogger("polymarket_bot").exception("Bot stopped.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
