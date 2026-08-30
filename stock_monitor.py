"""
stock_monitor.py
-----------------
Monitors a watchlist of stock tickers for a "resistance breakout + high volume"
condition and sends alerts to Discord (rich embed) and/or Telegram (and,
optionally, WhatsApp via CallMeBot). Designed to run either as a single
one-shot check (e.g. from a cron job / GitHub Actions) or as a continuous
loop that respects US market hours.

Quick start:
    pip install -r requirements.txt
    python stock_monitor.py --once            # one check of the whole watchlist
    python stock_monitor.py                   # continuous loop (Ctrl+C to stop)

See the bottom of this file / README for full setup instructions.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import List, Optional
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("stock_monitor")

EASTERN = ZoneInfo("America/New_York")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)


# ==========================================================================
# 1. CONFIGURATION — defining what to watch
# ==========================================================================
# Each entry in the watchlist is one ticker + its own resistance target and
# volume-spike multiplier. The watchlist can come from (in priority order):
#   1. --ticker/--target-price/--volume-multiplier CLI flags (single stock)
#   2. the WATCHLIST environment variable (a JSON string — handy for CI secrets/vars)
#   3. a watchlist.json file next to this script
#
# Example watchlist.json:
# [
#   {"ticker": "AAPL", "target_price": 230.00, "volume_multiplier": 2.0},
#   {"ticker": "TSLA", "target_price": 300.00, "volume_multiplier": 2.5}
# ]

@dataclass
class WatchItem:
    ticker: str
    target_price: float
    volume_multiplier: float = 2.0


@dataclass
class MonitorSettings:
    """Settings shared across every ticker in the watchlist."""
    watchlist: List[WatchItem]
    price_tolerance_pct: float = 0.15       # % band around target price counted as "touched"
    poll_interval_sec: int = 120            # how often to run a check cycle in loop mode
    avg_volume_lookback_days: int = 20      # window for the "average volume" baseline
    cooldown_minutes: int = 30              # don't re-alert the same ticker within this window
    market_hours_only: bool = True          # skip checks outside US market hours (9:30-16:00 ET, Mon-Fri)


def load_watchlist(args: argparse.Namespace) -> List[WatchItem]:
    # 1. Single-stock override via CLI (useful for quick tests, or for callers
    #    that already loop externally, e.g. a GitHub Actions matrix).
    if args.ticker and args.target_price is not None:
        return [WatchItem(
            ticker=args.ticker.upper(),
            target_price=args.target_price,
            volume_multiplier=args.volume_multiplier,
        )]

    # 2. WATCHLIST environment variable (JSON string).
    raw = os.environ.get("WATCHLIST")
    if raw:
        items = json.loads(raw)
        return [WatchItem(i["ticker"].upper(), float(i["target_price"]),
                           float(i.get("volume_multiplier", 2.0))) for i in items]

    # 3. watchlist.json file.
    if os.path.exists(args.watchlist_file):
        with open(args.watchlist_file) as f:
            items = json.load(f)
        return [WatchItem(i["ticker"].upper(), float(i["target_price"]),
                           float(i.get("volume_multiplier", 2.0))) for i in items]

    raise ValueError(
        "No watchlist found. Provide --ticker + --target-price, set the WATCHLIST "
        f"env var, or create {args.watchlist_file}."
    )


# ==========================================================================
# 2. MARKET DATA — fetching live price / volume via yfinance
# ==========================================================================

class StockDataFetcher:
    def __init__(self, ticker: str):
        self.ticker_symbol = ticker
        self.ticker = yf.Ticker(ticker)

    def get_current_price_and_day_volume(self) -> tuple[float, float]:
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
        # Drop today's partial bar (if present) so the baseline isn't skewed low.
        hist = hist.iloc[:-1] if len(hist) > 1 else hist
        return float(hist["Volume"].mean())


# ==========================================================================
# 3. ALERT LOGIC — price-near-resistance + volume-spike, with per-ticker cooldown
# ==========================================================================

@dataclass
class AlertEvent:
    """Everything a notifier needs to format a message, in one place."""
    ticker: str
    price: float
    target_price: float
    volume: float
    volume_threshold: float
    volume_multiplier: float
    avg_volume_days: int
    timestamp: datetime = field(default_factory=datetime.now)


class TriggerEvaluator:
    def __init__(self, item: WatchItem, avg_volume: float, price_tolerance_pct: float):
        self.item = item
        self.avg_volume = avg_volume
        self.price_tolerance_pct = price_tolerance_pct
        self.volume_threshold = avg_volume * item.volume_multiplier

    def price_condition_met(self, current_price: float) -> bool:
        band = self.item.target_price * (self.price_tolerance_pct / 100)
        return abs(current_price - self.item.target_price) <= band

    def volume_condition_met(self, current_volume: float) -> bool:
        return current_volume >= self.volume_threshold

    def evaluate(self, current_price: float, current_volume: float) -> bool:
        return self.price_condition_met(current_price) and self.volume_condition_met(current_volume)


def is_market_hours(now: Optional[datetime] = None) -> bool:
    """True during US regular trading hours (9:30-16:00 America/New_York, Mon-Fri).
    Does not account for market holidays."""
    now = (now or datetime.now(EASTERN)).astimezone(EASTERN)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


# ==========================================================================
# 4. NOTIFICATIONS — Discord (embed), Telegram, and optional WhatsApp
# ==========================================================================

class Notifier:
    def send(self, event: AlertEvent) -> bool:
        raise NotImplementedError


class DiscordNotifier(Notifier):
    """Posts a formatted embed (not just plain text) to a Discord webhook."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send(self, event: AlertEvent) -> bool:
        payload = {
            "embeds": [{
                "title": f"🚨 {event.ticker} Resistance Breakout",
                "color": 15158332,  # red
                "fields": [
                    {"name": "Price", "value": f"${event.price:,.2f}  (target ${event.target_price:,.2f})",
                     "inline": False},
                    {"name": "Volume", "value": f"{event.volume:,.0f}  "
                                                 f"(≥ {event.volume_multiplier}x the {event.avg_volume_days}d avg, "
                                                 f"threshold {event.volume_threshold:,.0f})",
                     "inline": False},
                ],
                "timestamp": event.timestamp.isoformat(),
            }]
        }
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=10)
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            log.error(f"Discord notification failed for {event.ticker}: {e}")
            return False


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, event: AlertEvent) -> bool:
        text = (
            f"🚨 *{event.ticker} Alert*\n"
            f"Price: ${event.price:,.2f} (target ${event.target_price:,.2f})\n"
            f"Volume: {event.volume:,.0f} (≥ {event.volume_multiplier}x the "
            f"{event.avg_volume_days}d average)\n"
            f"Time: {event.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            resp = requests.post(
                url, data={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            log.error(f"Telegram notification failed for {event.ticker}: {e}")
            return False


class WhatsAppNotifier(Notifier):
    """Free, personal-use-only WhatsApp alerts via the CallMeBot API.
    Each recipient must activate their own apikey first — see README."""

    def __init__(self, phone: str, api_key: str):
        self.phone = phone
        self.api_key = api_key

    def send(self, event: AlertEvent) -> bool:
        text = (
            f"{event.ticker} Alert: ${event.price:,.2f} (target ${event.target_price:,.2f}), "
            f"volume {event.volume:,.0f} (>= {event.volume_multiplier}x avg)"
        )
        try:
            resp = requests.get(
                "https://api.callmebot.com/whatsapp.php",
                params={"phone": self.phone, "text": text, "apikey": self.api_key},
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            log.error(f"WhatsApp notification to {self.phone} failed: {e}")
            return False


class MultiNotifier(Notifier):
    """Fans an alert out to every configured channel."""

    def __init__(self, notifiers: List[Notifier]):
        self.notifiers = notifiers

    def send(self, event: AlertEvent) -> bool:
        if not self.notifiers:
            log.warning("No notifiers configured — alert would not be delivered anywhere.")
            return False
        results = [n.send(event) for n in self.notifiers]
        return any(results)


def build_notifier_from_env() -> MultiNotifier:
    notifiers: List[Notifier] = []

    discord_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if discord_url:
        notifiers.append(DiscordNotifier(discord_url))
        log.info("Discord notifier enabled.")

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        notifiers.append(TelegramNotifier(tg_token, tg_chat))
        log.info("Telegram notifier enabled.")

    wa_phone = os.environ.get("WHATSAPP_PHONE")
    wa_key = os.environ.get("WHATSAPP_APIKEY")
    if wa_phone and wa_key:
        notifiers.append(WhatsAppNotifier(wa_phone, wa_key))
        log.info("WhatsApp notifier enabled (single recipient).")

    wa_recipients_raw = os.environ.get("WHATSAPP_RECIPIENTS")
    if wa_recipients_raw:
        try:
            for r in json.loads(wa_recipients_raw):
                notifiers.append(WhatsAppNotifier(r["phone"], r["apikey"]))
            log.info("WhatsApp notifier enabled (multi-recipient).")
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log.error(f"Could not parse WHATSAPP_RECIPIENTS: {e}")

    if not notifiers:
        log.warning(
            "No notification channels configured. Set DISCORD_WEBHOOK_URL, "
            "TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, and/or WHATSAPP_* environment variables."
        )
    return MultiNotifier(notifiers)


# ==========================================================================
# 5. PER-TICKER MONITOR — baseline, cooldown state, single check
# ==========================================================================

class TickerMonitor:
    """Owns the fetch/evaluate/cooldown lifecycle for exactly one ticker."""

    def __init__(self, item: WatchItem, settings: MonitorSettings, notifier: Notifier):
        self.item = item
        self.settings = settings
        self.notifier = notifier
        self.fetcher = StockDataFetcher(item.ticker)
        self.state_file = f"alert_state_{item.ticker}.json"
        self.last_alert_time: Optional[datetime] = self._load_last_alert_time()
        self.evaluator: Optional[TriggerEvaluator] = None

    def _load_last_alert_time(self) -> Optional[datetime]:
        if not os.path.exists(self.state_file):
            return None
        try:
            with open(self.state_file) as f:
                ts = json.load(f).get("last_alert_time")
            return datetime.fromisoformat(ts) if ts else None
        except (json.JSONDecodeError, ValueError, OSError) as e:
            log.warning(f"Could not read {self.state_file}: {e}")
            return None

    def _save_last_alert_time(self) -> None:
        try:
            with open(self.state_file, "w") as f:
                json.dump({"last_alert_time": self.last_alert_time.isoformat()}, f)
        except OSError as e:
            log.warning(f"Could not write {self.state_file}: {e}")

    def refresh_baseline(self) -> None:
        avg_volume = self.fetcher.get_average_daily_volume(self.settings.avg_volume_lookback_days)
        self.evaluator = TriggerEvaluator(self.item, avg_volume, self.settings.price_tolerance_pct)
        log.info(
            f"[{self.item.ticker}] Baseline avg volume "
            f"({self.settings.avg_volume_lookback_days}d): {avg_volume:,.0f} | "
            f"Volume trigger threshold: {self.evaluator.volume_threshold:,.0f}"
        )

    def _in_cooldown(self) -> bool:
        if self.last_alert_time is None:
            return False
        elapsed_min = (datetime.now() - self.last_alert_time).total_seconds() / 60
        return elapsed_min < self.settings.cooldown_minutes

    def check_once(self) -> bool:
        """Runs a single check for this ticker. Returns True if an alert was sent."""
        if self.evaluator is None:
            self.refresh_baseline()

        try:
            price, volume = self.fetcher.get_current_price_and_day_volume()
        except ValueError as e:
            log.error(str(e))
            return False

        log.info(
            f"[{self.item.ticker}] Price: ${price:,.2f} | Volume: {volume:,.0f} "
            f"| Threshold: {self.evaluator.volume_threshold:,.0f}"
        )

        if self._in_cooldown():
            return False

        if self.evaluator.evaluate(price, volume):
            event = AlertEvent(
                ticker=self.item.ticker,
                price=price,
                target_price=self.item.target_price,
                volume=volume,
                volume_threshold=self.evaluator.volume_threshold,
                volume_multiplier=self.item.volume_multiplier,
                avg_volume_days=self.settings.avg_volume_lookback_days,
            )
            sent = self.notifier.send(event)
            if sent:
                log.info(f"Alert sent for {self.item.ticker}.")
                self.last_alert_time = datetime.now()
                self._save_last_alert_time()
            return sent
        return False


# ==========================================================================
# 6. EXECUTION LOOP — runs the whole watchlist once, or continuously
# ==========================================================================

class WatchlistMonitor:
    def __init__(self, settings: MonitorSettings, notifier: Notifier):
        self.settings = settings
        self.tickers = [TickerMonitor(item, settings, notifier) for item in settings.watchlist]
        self._last_baseline_refresh = datetime.min

    def check_all_once(self) -> None:
        for tm in self.tickers:
            try:
                tm.check_once()
            except Exception as e:
                log.exception(f"Unexpected error checking {tm.item.ticker}: {e}")
        self._last_baseline_refresh = datetime.now()

    def run_loop(self) -> None:
        log.info(
            f"Starting monitor for {len(self.tickers)} ticker(s), "
            f"checking every {self.settings.poll_interval_sec}s "
            f"(market hours only: {self.settings.market_hours_only})"
        )
        while True:
            if self.settings.market_hours_only and not is_market_hours():
                log.info("Outside market hours — sleeping.")
            else:
                self.check_all_once()
                # Refresh each ticker's volume baseline roughly every 6 hours.
                if (datetime.now() - self._last_baseline_refresh).total_seconds() > 6 * 3600:
                    for tm in self.tickers:
                        tm.evaluator = None
            time.sleep(self.settings.poll_interval_sec)


# ==========================================================================
# CLI
# ==========================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor a stock watchlist for resistance-breakout + high-volume alerts."
    )
    parser.add_argument("--ticker", help="Single ticker to watch (overrides watchlist file/env)")
    parser.add_argument("--target-price", type=float, help="Target/resistance price (with --ticker)")
    parser.add_argument("--volume-multiplier", type=float, default=2.0,
                         help="Volume must be >= this multiple of average volume (default 2.0)")
    parser.add_argument("--watchlist-file", type=str, default="watchlist.json",
                         help="Path to a JSON watchlist file (default watchlist.json)")
    parser.add_argument("--price-tolerance-pct", type=float, default=0.15,
                         help="Percent band around target price counted as 'touched' (default 0.15)")
    parser.add_argument("--poll-interval", type=int, default=120,
                         help="Seconds between check cycles in loop mode (default 120)")
    parser.add_argument("--avg-volume-days", type=int, default=20,
                         help="Lookback window in days for average volume baseline (default 20)")
    parser.add_argument("--cooldown-minutes", type=int, default=30,
                         help="Minutes to wait before re-alerting the same ticker (default 30)")
    parser.add_argument("--ignore-market-hours", action="store_true",
                         help="Check even outside 9:30-16:00 ET (useful for testing, or non-US tickers)")
    parser.add_argument("--once", action="store_true",
                         help="Check the whole watchlist once and exit, instead of looping")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    watchlist = load_watchlist(args)
    settings = MonitorSettings(
        watchlist=watchlist,
        price_tolerance_pct=args.price_tolerance_pct,
        poll_interval_sec=args.poll_interval,
        avg_volume_lookback_days=args.avg_volume_days,
        cooldown_minutes=args.cooldown_minutes,
        market_hours_only=not args.ignore_market_hours,
    )
    notifier = build_notifier_from_env()
    monitor = WatchlistMonitor(settings, notifier)

    if args.once:
        # In one-shot mode (e.g. cron/CI), skip the market-hours gate entirely —
        # the scheduler is what decides when to run; we just check when asked.
        monitor.check_all_once()
    else:
        try:
            monitor.run_loop()
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            sys.exit(0)


if __name__ == "__main__":
    main()
