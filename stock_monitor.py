"""
stock_monitor.py
-----------------
Monitors a stock for a price-near-resistance + high-volume breakout condition
and sends an alert via Telegram and/or Discord.

Run: python stock_monitor.py --ticker AAPL --target-price 230.00 --volume-multiplier 2.0
See README section at the bottom of this file (or the chat message) for full setup.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

import requests
import yfinance as yf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("stock_monitor")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass
class MonitorConfig:
    ticker: str
    target_price: float
    volume_multiplier: float = 2.0          # trigger when volume > multiplier * avg volume
    price_tolerance_pct: float = 0.15        # % band around target price counted as "touched"
    poll_interval_sec: int = 60              # how often to poll
    avg_volume_lookback_days: int = 20       # window for the "average volume" baseline
    cooldown_minutes: int = 30               # don't re-alert for the same condition within this window
    state_file: str = "alert_state.json"     # persists last-alert time across separate process runs (e.g. CI)


# --------------------------------------------------------------------------
# Notifiers
# --------------------------------------------------------------------------

class Notifier:
    def send(self, message: str) -> bool:
        raise NotImplementedError


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, message: str) -> bool:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            resp = requests.post(
                url,
                data={"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"},
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            log.error(f"Telegram notification failed: {e}")
            return False


class DiscordNotifier(Notifier):
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send(self, message: str) -> bool:
        try:
            resp = requests.post(self.webhook_url, json={"content": message}, timeout=10)
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            log.error(f"Discord notification failed: {e}")
            return False


class MultiNotifier(Notifier):
    """Fans a message out to every configured notifier."""

    def __init__(self, notifiers: List[Notifier]):
        self.notifiers = notifiers

    def send(self, message: str) -> bool:
        if not self.notifiers:
            log.warning("No notifiers configured — alert would not be delivered anywhere.")
            return False
        results = [n.send(message) for n in self.notifiers]
        return any(results)


def build_notifier_from_env() -> MultiNotifier:
    notifiers: List[Notifier] = []

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        notifiers.append(TelegramNotifier(tg_token, tg_chat))
        log.info("Telegram notifier enabled.")

    discord_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if discord_url:
        notifiers.append(DiscordNotifier(discord_url))
        log.info("Discord notifier enabled.")

    if not notifiers:
        log.warning(
            "No notification channels configured. Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID "
            "and/or DISCORD_WEBHOOK_URL environment variables."
        )
    return MultiNotifier(notifiers)


# --------------------------------------------------------------------------
# Data fetching
# --------------------------------------------------------------------------

class StockDataFetcher:
    """Wraps yfinance calls needed for the monitor."""

    def __init__(self, ticker: str):
        self.ticker_symbol = ticker.upper()
        self.ticker = yf.Ticker(self.ticker_symbol)

    def get_current_price_and_day_volume(self) -> Tuple[float, float]:
        """Latest intraday price and cumulative volume traded so far today."""
        data = self.ticker.history(period="1d", interval="1m")
        if data.empty:
            raise ValueError(f"No intraday data returned for {self.ticker_symbol}")
        current_price = float(data["Close"].iloc[-1])
        day_volume_so_far = float(data["Volume"].sum())
        return current_price, day_volume_so_far

    def get_average_daily_volume(self, lookback_days: int) -> float:
        hist = self.ticker.history(period=f"{lookback_days}d", interval="1d")
        if hist.empty:
            raise ValueError(f"No historical daily data for {self.ticker_symbol}")
        # Drop today's partial bar if present so the baseline isn't skewed low.
        hist = hist.iloc[:-1] if len(hist) > 1 else hist
        return float(hist["Volume"].mean())


# --------------------------------------------------------------------------
# Trigger logic
# --------------------------------------------------------------------------

class TriggerEvaluator:
    def __init__(self, config: MonitorConfig, avg_volume: float):
        self.config = config
        self.avg_volume = avg_volume
        self.volume_threshold = avg_volume * config.volume_multiplier

    def price_condition_met(self, current_price: float) -> bool:
        band = self.config.target_price * (self.config.price_tolerance_pct / 100)
        return abs(current_price - self.config.target_price) <= band

    def volume_condition_met(self, current_volume: float) -> bool:
        return current_volume >= self.volume_threshold

    def evaluate(self, current_price: float, current_volume: float) -> bool:
        return self.price_condition_met(current_price) and self.volume_condition_met(current_volume)


# --------------------------------------------------------------------------
# Monitor loop
# --------------------------------------------------------------------------

class StockMonitor:
    def __init__(self, config: MonitorConfig, notifier: Notifier):
        self.config = config
        self.notifier = notifier
        self.fetcher = StockDataFetcher(config.ticker)
        self.last_alert_time: Optional[datetime] = self._load_last_alert_time()
        self.evaluator: Optional[TriggerEvaluator] = None

    def _load_last_alert_time(self) -> Optional[datetime]:
        """Reads the last-alert timestamp from disk so the cooldown survives
        across separate process runs (important when run via cron / CI, where
        each run is a brand-new process with no memory of the previous one)."""
        path = self.config.state_file
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r") as f:
                data = json.load(f)
            ts = data.get("last_alert_time")
            return datetime.fromisoformat(ts) if ts else None
        except (json.JSONDecodeError, ValueError, OSError) as e:
            log.warning(f"Could not read state file {path}: {e}")
            return None

    def _save_last_alert_time(self) -> None:
        path = self.config.state_file
        try:
            with open(path, "w") as f:
                json.dump({"last_alert_time": self.last_alert_time.isoformat()}, f)
        except OSError as e:
            log.warning(f"Could not write state file {path}: {e}")

    def _refresh_baseline(self) -> None:
        avg_volume = self.fetcher.get_average_daily_volume(self.config.avg_volume_lookback_days)
        self.evaluator = TriggerEvaluator(self.config, avg_volume)
        log.info(
            f"[{self.config.ticker}] Baseline avg volume ({self.config.avg_volume_lookback_days}d): "
            f"{avg_volume:,.0f} | Trigger volume threshold: {self.evaluator.volume_threshold:,.0f}"
        )

    def _in_cooldown(self) -> bool:
        if self.last_alert_time is None:
            return False
        elapsed_min = (datetime.now() - self.last_alert_time).total_seconds() / 60
        return elapsed_min < self.config.cooldown_minutes

    def _format_alert(self, price: float, volume: float) -> str:
        return (
            f"🚨 *{self.config.ticker} Alert*\n"
            f"Price: ${price:,.2f} (target ${self.config.target_price:,.2f})\n"
            f"Volume so far today: {volume:,.0f} "
            f"(≥ {self.config.volume_multiplier}x the {self.config.avg_volume_lookback_days}d average)\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

    def check_once(self) -> bool:
        """Runs a single check. Returns True if an alert was sent."""
        if self.evaluator is None:
            self._refresh_baseline()

        try:
            price, volume = self.fetcher.get_current_price_and_day_volume()
        except ValueError as e:
            log.error(str(e))
            return False

        log.info(
            f"[{self.config.ticker}] Price: ${price:,.2f} | Volume: {volume:,.0f} "
            f"| Threshold: {self.evaluator.volume_threshold:,.0f}"
        )

        if self._in_cooldown():
            return False

        if self.evaluator.evaluate(price, volume):
            message = self._format_alert(price, volume)
            sent = self.notifier.send(message)
            if sent:
                log.info(f"Alert sent for {self.config.ticker}.")
                self.last_alert_time = datetime.now()
                self._save_last_alert_time()
            return sent
        return False

    def run(self) -> None:
        log.info(
            f"Starting monitor for {self.config.ticker} | target=${self.config.target_price} "
            f"| poll every {self.config.poll_interval_sec}s"
        )
        self._refresh_baseline()
        last_baseline_refresh = datetime.now()

        while True:
            try:
                self.check_once()
                # Refresh the average-volume baseline once a day so it doesn't go stale.
                if (datetime.now() - last_baseline_refresh).total_seconds() > 6 * 3600:
                    self._refresh_baseline()
                    last_baseline_refresh = datetime.now()
            except Exception as e:
                log.exception(f"Unexpected error during check: {e}")
            time.sleep(self.config.poll_interval_sec)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args() -> MonitorConfig:
    parser = argparse.ArgumentParser(description="Monitor a stock for a price + volume breakout alert.")
    parser.add_argument("--ticker", required=True, help="Stock ticker symbol, e.g. AAPL")
    parser.add_argument("--target-price", required=True, type=float, help="Target/resistance price")
    parser.add_argument("--volume-multiplier", type=float, default=2.0,
                         help="Volume must be >= this multiple of the average daily volume (default 2.0)")
    parser.add_argument("--price-tolerance-pct", type=float, default=0.15,
                         help="Percent band around target price counted as 'touched' (default 0.15)")
    parser.add_argument("--poll-interval", type=int, default=60, help="Seconds between checks (default 60)")
    parser.add_argument("--avg-volume-days", type=int, default=20,
                         help="Lookback window in days for average volume baseline (default 20)")
    parser.add_argument("--cooldown-minutes", type=int, default=30,
                         help="Minutes to wait before re-alerting (default 30)")
    parser.add_argument("--state-file", type=str, default=None,
                         help="File used to persist the cooldown timer across runs "
                              "(default: alert_state_<TICKER>.json)")
    parser.add_argument("--once", action="store_true", help="Run a single check and exit, instead of looping")

    args = parser.parse_args()
    state_file = args.state_file or f"alert_state_{args.ticker.upper()}.json"
    return MonitorConfig(
        ticker=args.ticker,
        target_price=args.target_price,
        volume_multiplier=args.volume_multiplier,
        price_tolerance_pct=args.price_tolerance_pct,
        poll_interval_sec=args.poll_interval,
        avg_volume_lookback_days=args.avg_volume_days,
        cooldown_minutes=args.cooldown_minutes,
        state_file=state_file,
    ), args.once


def main() -> None:
    config, run_once = parse_args()
    notifier = build_notifier_from_env()
    monitor = StockMonitor(config, notifier)

    if run_once:
        monitor.check_once()
    else:
        try:
            monitor.run()
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            sys.exit(0)


if __name__ == "__main__":
    main()
