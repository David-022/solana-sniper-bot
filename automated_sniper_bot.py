import time
import logging
import random
import threading
from datetime import datetime, time as dtime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dexscreener import DexscreenerClient

from flask import Flask

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("meme_sniper")

# ---------------------------
# Flask dummy server (RENDER)
# ---------------------------
app = Flask(__name__)

@app.route("/")
def root():
    return "OK"


def start_flask():
    """
    Starts Flask in a background thread so Render stays alive.
    """
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# ---------------------------
# HTTP session with retries
# ---------------------------
def make_session():
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.request_timeout = 7
    return s


session = make_session()


# ---------------------------
# Telegram env (CLEANED)
# ---------------------------
import os
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS = [p.strip() for p in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if p.strip()]


def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        logger.info("Telegram not configured.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "text": text,
        "parse_mode": "Markdown"
    }

    for cid in TELEGRAM_CHAT_IDS:
        payload["chat_id"] = cid
        try:
            r = session.post(url, json=payload, timeout=7)
            if r.status_code != 200:
                logger.warning(f"Telegram send failed for {cid}: {r.text}")
        except Exception as e:
            logger.warning(f"Telegram error: {e}")


# ---------------------------
# Dexscreener client
# ---------------------------
client = DexscreenerClient()


def safe_float(x):
    try:
        if x is None or x == "":
            return 0.0
        return float(x)
    except:
        return 0.0


def safe_div(a, b, default=0.0):
    try:
        return a / b if b else default
    except:
        return default


def token_age_minutes(ts):
    if not ts:
        return 9999
    try:
        ts = int(ts)
    except:
        return 9999

    now = int(time.time())
    # ms → s
    if ts > 10_000_000_000:
        ts //= 1000
    return max(0, (now - ts) // 60)


def fetch_token_detail(addr):
    url = f"https://api.dexscreener.com/tokens/v1/solana/{addr}"
    try:
        r = session.get(url, timeout=7)
        if r.status_code != 200:
            return None
        return r.json()
    except:
        return None


def analyze_token(data):
    """
    Your original token analysis logic.
    """
    try:
        if isinstance(data, list):
            data = data[0]

        base = data.get("baseToken", {}) or {}
        name = base.get("name", "unknown")
        symbol = base.get("symbol", "")

        price = safe_float(data.get("priceUsd"))
        market_cap = safe_float(data.get("marketCap"))
        volume = safe_float((data.get("volume") or {}).get("h24"))
        liquidity = safe_float((data.get("liquidity") or {}).get("usd"))
        price_change = safe_float((data.get("priceChange") or {}).get("h24"))
        age = token_age_minutes(data.get("pairCreatedAt"))
        url = data.get("url", "")

        # Your original filtering
        if market_cap < 10000 or market_cap > 2000000:
            return None
        if liquidity < 5000:
            return None
        if volume < 1000:
            return None
        if age < 30 or age > 120:
            return None

        momentum = (
            price_change * 0.4 +
            (safe_div(volume, market_cap) * 100) * 0.3 +
            (min(liquidity / (market_cap * 0.5 + 1), 1.0) * 25) * 0.2 +
            (1 if age < 120 else 0.8 if age < 360 else 0.5) * 10 * 0.1
        )

        return {
            "name": name,
            "symbol": symbol,
            "price": price,
            "market_cap": market_cap,
            "volume": volume,
            "liquidity": liquidity,
            "price_change": price_change,
            "age": age,
            "momentum": momentum,
            "url": url
        }
    except Exception as e:
        logger.warning(f"analysis error: {e}")
        return None


def run_sniper_cycle():
    logger.info("Running sniper scan...")

    try:
        profiles = client.get_latest_token_profiles()
    except Exception as e:
        logger.warning(f"Error loading profiles: {e}")
        return

    valid_tokens = []

    for p in profiles:
        try:
            chain = getattr(p, "chain_id", None)
            if chain != "solana":
                continue

            addr = getattr(p, "token_address", None)
            if not addr:
                continue

            detail = fetch_token_detail(addr)
            if not detail:
                continue

            analyzed = analyze_token(detail)
            if analyzed:
                valid_tokens.append(analyzed)

        except Exception as e:
            logger.warning(f"Profile error: {e}")

        time.sleep(0.6)

    if not valid_tokens:
        logger.info("No good tokens this round.")
        return

    valid_tokens.sort(key=lambda x: x["momentum"], reverse=True)
    best = valid_tokens[0]

    msg = (
        f"🔥 *New Token Detected!*\n"
        f"{best['name']} ({best['symbol']})\n"
        f"💵 ${best['price']:.6f}\n"
        f"💧 Liquidity: ${best['liquidity']:,}\n"
        f"💰 Market Cap: ${best['market_cap']:,}\n"
        f"📈 24h Change: {best['price_change']:.2f}%\n"
        f"⚡ Momentum: {best['momentum']:.2f}\n"
        f"🔗 {best['url']}"
    )

    send_telegram(msg)


# ---------------------------
# SCHEDULER
# ---------------------------
WINDOW_START = dtime(20, 30)   # 20:30 UTC → 21:30 Nigerian
WINDOW_END = dtime(23, 30)     # 23:30 UTC → 00:30 Nigerian


def in_window():
    now = datetime.now().time()
    return WINDOW_START <= now <= WINDOW_END


def scheduler_loop():
    logger.info("Sniper bot scheduler started.")

    while True:
        if in_window():
            run_sniper_cycle()
            delay = 600  # 10 minutes
            logger.info(f"Sleeping {delay//60} minutes...")
            time.sleep(delay)
        else:
            logger.info("Outside window. Sleeping 5 min...")
            time.sleep(300)


def main():
    # Start flask (Render keep-alive)
    threading.Thread(target=start_flask, daemon=True).start()

    # Start scanning scheduler
    scheduler_loop()


if __name__ == "__main__":
    main()
