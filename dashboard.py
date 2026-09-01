"""
dashboard.py
------------
A web dashboard for managing your stock watchlist, backed by Supabase —
nothing is stored locally, so it's reachable from your phone or laptop
from anywhere once deployed.

ARCHITECTURE — read this first:
  - This dashboard only manages the watchlist (add / view / delete) and
    shows live prices. It does NOT run the alert engine itself.
  - The actual alerting (checking price + volume, sending Discord/
    Telegram/WhatsApp) stays on your GitHub Actions cron job, which reads
    from the SAME Supabase table. So anything you add here is picked up
    by the next scheduled check automatically — no manual sync step.
  - Why alerting isn't done from here: free web-app hosting tends to put
    idle apps to sleep, which would silently kill a background alert
    loop. GitHub Actions' scheduler is the reliable place for that; this
    dashboard is just the window into the same shared data.

Run locally:
    pip install -r requirements.txt
    streamlit run dashboard.py

Deploy online (Streamlit Community Cloud, free — see chat for full steps):
    Push this file to your GitHub repo, connect the repo at
    https://share.streamlit.io, and set the two secrets below in its
    "Secrets" panel (never commit real keys to the repo).

Required secrets (env vars locally, or Streamlit Cloud's Secrets panel):
    SUPABASE_URL          e.g. https://ynkjrhqozilkodbqbovc.supabase.co
    SUPABASE_SERVICE_KEY  the service_role key from Supabase Settings > API
                           (NOT the anon key — this app runs server-side
                           only, so the more privileged key never reaches
                           a user's browser)
"""

from __future__ import annotations

import os
from datetime import datetime

import requests
import streamlit as st

from stock_monitor import StockDataFetcher, log

st.set_page_config(page_title="Stock Alert Dashboard", page_icon="📈", layout="wide")

SUPABASE_URL = os.environ.get("SUPABASE_URL") or st.secrets.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or st.secrets.get("SUPABASE_SERVICE_KEY", "")
TABLE = "stock_watchlist"
PRICE_CACHE_TTL_SEC = 30


# ==========================================================================
# Supabase REST helpers (PostgREST — no extra SDK needed, just requests)
# ==========================================================================

def _headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def fetch_watchlist() -> list[dict]:
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/{TABLE}?select=*&order=ticker.asc",
        headers=_headers(), timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def add_stock(ticker: str, target_price: float, volume_multiplier: float) -> None:
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(),
        json={"ticker": ticker, "target_price": target_price, "volume_multiplier": volume_multiplier},
        timeout=10,
    )
    resp.raise_for_status()


def delete_stock(ticker: str) -> None:
    resp = requests.delete(
        f"{SUPABASE_URL}/rest/v1/{TABLE}?ticker=eq.{ticker}",
        headers=_headers(), timeout=10,
    )
    resp.raise_for_status()


@st.cache_data(ttl=PRICE_CACHE_TTL_SEC)
def get_live_price(ticker: str) -> float | None:
    try:
        price, _ = StockDataFetcher(ticker).get_current_price_and_day_volume()
        return price
    except Exception as e:
        log.warning(f"Could not fetch live price for {ticker}: {e}")
        return None


# ==========================================================================
# UI
# ==========================================================================

st.title("📈 Stock Alert Dashboard")

if not SUPABASE_URL or not SUPABASE_KEY:
    st.error(
        "SUPABASE_URL / SUPABASE_SERVICE_KEY are not set. Add them as environment "
        "variables (local run) or in the Streamlit Cloud Secrets panel (hosted run)."
    )
    st.stop()

st.caption("Synced live via Supabase — the same watchlist your GitHub Actions alert engine reads.")

st.subheader("Add a stock to watch")
with st.form("add_stock_form", clear_on_submit=True):
    col1, col2, col3, col4 = st.columns([2, 2, 2, 1])
    ticker_input = col1.text_input("Stock Ticker", placeholder="TSLA")
    target_price_input = col2.number_input("Resistance Price Target", min_value=0.0, step=0.5, format="%.2f")
    volume_multiplier_input = col3.number_input(
        "Volume Multiplier", min_value=0.1, step=0.1, value=2.0, format="%.1f"
    )
    submitted = col4.form_submit_button("Add ➕")

    if submitted:
        ticker = ticker_input.strip().upper()
        if not ticker:
            st.error("Enter a ticker symbol.")
        elif target_price_input <= 0:
            st.error("Target price must be greater than 0.")
        else:
            try:
                existing = [w["ticker"] for w in fetch_watchlist()]
                if ticker in existing:
                    st.warning(f"{ticker} is already on the watchlist.")
                else:
                    add_stock(ticker, target_price_input, volume_multiplier_input)
                    st.success(f"Added {ticker}.")
                    st.rerun()
            except requests.RequestException as e:
                st.error(f"Could not save {ticker}: {e}")

st.subheader("Active Watchlist")

try:
    items = fetch_watchlist()
except requests.RequestException as e:
    st.error(f"Could not load watchlist from Supabase: {e}")
    items = []

if not items:
    st.info("No stocks yet — add one above to get started.")
else:
    header = st.columns([1.5, 1.5, 1.5, 1.5, 1])
    for col, label in zip(header, ["Ticker", "Live Price", "Target", "Vol. Multiplier", ""]):
        col.markdown(f"**{label}**")

    for item in items:
        row = st.columns([1.5, 1.5, 1.5, 1.5, 1])
        row[0].write(item["ticker"])

        live_price = get_live_price(item["ticker"])
        row[1].write(f"${live_price:,.2f}" if live_price is not None else "—")

        row[2].write(f"${float(item['target_price']):,.2f}")
        row[3].write(f"{item['volume_multiplier']}x")

        if row[4].button("🗑️ Delete", key=f"delete_{item['ticker']}"):
            try:
                delete_stock(item["ticker"])
                st.rerun()
            except requests.RequestException as e:
                st.error(f"Could not delete {item['ticker']}: {e}")

st.divider()
st.caption(f"Page loaded: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} — refresh to update live prices.")
