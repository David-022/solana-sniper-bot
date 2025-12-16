import sys
import time
import logging
import threading
import requests
import csv
from dexscreener import DexscreenerClient
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime, time as dtime
from flask import Flask

# --------------------------------------------
# --- Logging
# --------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
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
    

# --------------------------------------------
# --- Configuration
# --------------------------------------------
REFRESH_INTERVAL = 600  # 10 minutes (in seconds)
SLEEP_BETWEEN_TOKENS = 0.8
MAX_TOP_RESULTS = 5

MIN_MARKET_CAP = 10000
MAX_MARKET_CAP = 2000000
MIN_LIQUIDITY = 5000
MIN_VOLUME = 1000
MIN_AGE_MINUTES = 30
MAX_AGE_MINUTES = 120

# ---------------------------
# Telegram env (CLEANED)
# ---------------------------
import os
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS = [p.strip() for p in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if p.strip()]

DEXS_TOKEN_ENDPOINT = "https://api.dexscreener.com/tokens/v1/solana/{}"

# --- Trending settings ---
previous_scores = {}
TREND_THRESHOLD = 0.25  # 25% increase considered trending


# --------------------------------------------
# --- HTTP Session with Retry
# --------------------------------------------
def make_session(retries=3, backoff=0.4, timeout=6):
    s = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.request_timeout = timeout
    return s

session = make_session()

# --------------------------------------------
# --- Safe helpers
# --------------------------------------------
def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default

def safe_div(a, b, default=0.0):
    try:
        return a / b if b else default
    except Exception:
        return default

def simulate_token_age(timestamp):
    if not timestamp:
        return 1440
    try:
        ts = int(timestamp)
        now = int(time.time())
        if ts > 1e12:
            ts //= 1000
        elif ts > 1e10:
            ts //= 1000
        return max(0, (now - ts) // 60)
    except Exception:
        return 1440
        

# --------------------------------------------
# --- Telegram helpers
# --------------------------------------------
def send_telegram_alert(message):
    for chat_id in TELEGRAM_CHAT_IDS:
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        try:
            r = session.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                             json=payload, timeout=session.request_timeout)
            if r.status_code != 200:
                logging.warning(f"Telegram send failed ({chat_id}): {r.status_code}")
        except Exception as e:
            logging.warning(f"Telegram send error ({chat_id}): {e}")
            

# --------------------------------------------
# --- Analytics
# --------------------------------------------
def analyze_token_data(data):
    try:
        if isinstance(data, list):
            data = data[0]

        name = data.get("baseToken", {}).get("name", "")
        symbol = data.get("baseToken", {}).get("symbol", "")
        price = safe_float(data.get("priceUsd"))
        market_cap = safe_float(data.get("marketCap"))
        volume = safe_float(data.get("volume", {}).get("h24"))
        price_change = safe_float(data.get("priceChange", {}).get("h24"))
        liquidity = safe_float(data.get("liquidity", {}).get("usd", 0))
        age = simulate_token_age(data.get("pairCreatedAt"))
        url = data.get("url", "")
        info = data.get("info", {})

        socials = info.get("socials", [])
        twitter = next((s.get("url") for s in socials if s.get("type") == "twitter"), "")
        telegram = next((s.get("url") for s in socials if s.get("type") == "telegram"), "")

        if not (MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP):
            return None
        if volume < MIN_VOLUME or liquidity < MIN_LIQUIDITY:
            return None
        if not (MIN_AGE_MINUTES <= age <= MAX_AGE_MINUTES):
            return None
        if not twitter:
            return None

        # --- Advanced analytics ---
        liquidity_score = min(liquidity / (market_cap * 0.5 + 1), 1.0)
        activity_ratio = safe_div(volume, market_cap, 0.0)
        age_factor = 1.0 if age < 120 else 0.8 if age < 360 else 0.5 if age < 720 else 0.2
        hype_score = 1.0 + (0.1 if twitter else 0) + (0.1 if telegram else 0)

        momentum = (
            (price_change * 0.4) +
            (activity_ratio * 100 * 0.3) +
            (liquidity_score * 25 * 0.2) +
            (age_factor * 10 * 0.1)
        ) * hype_score

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
            "url": url,
            "twitter": twitter,
            "telegram": telegram,
        }

    except Exception as e:
        logging.warning(f"Failed to analyze token data: {e}")
        return None
        
        
# --------------------------------------------
# --- Fetch + Analyze
# --------------------------------------------
def fetch_and_analyze():
    client = DexscreenerClient()
    profiles = client.get_latest_token_profiles()

    results = []
    for profile in profiles:
        if profile.chain_id != "solana":
            continue
        token_address = profile.token_address
        try:
            r = session.get(DEXS_TOKEN_ENDPOINT.format(token_address), timeout=session.request_timeout)
            if r.status_code != 200:
                continue
            data = r.json()
            result = analyze_token_data(data)
            if result:
                results.append(result)
        except Exception as e:
            logging.warning(f"Error fetching token info: {e}")
        time.sleep(SLEEP_BETWEEN_TOKENS)

    if not results:
        return []
    return sorted(results, key=lambda x: x["momentum"], reverse=True)[:MAX_TOP_RESULTS]
    

# --------------------------------------------
# --- Run Sniper Cycle
# --------------------------------------------
def run_sniper_cycle():
    global previous_scores
    test_mode = "--test" in sys.argv
    
    if test_mode:
        logging.info("🧪 Running MemeBotTelegramAlart in TEST MODE (one cycle only).")
    else:
        logging.info("🚀 MemeBotTelegramAlart Lite started — single cycle, no loop.")

    results = fetch_and_analyze()

    if not results:
        logging.info("No valid tokens found this round.")
        return

    logging.info(f"✅ Found {len(results)} high-momentum tokens. Sending alerts...")

    for idx, token in enumerate(results, 1):
        name, symbol = token["name"], token["symbol"]
        address = token["url"].split("/")[-1] if token["url"] else symbol

        old_score = previous_scores.get(address, 0)
        new_score = token["momentum"]

        trending = (
            (old_score == 0 and new_score > 0) or
            (old_score > 0 and (new_score - old_score) / max(old_score, 1) >= TREND_THRESHOLD)
        )

        previous_scores[address] = new_score

        emoji = "🔥" if trending else "🪙"
        msg = (
            f"{emoji} *#{idx} — {name}* ({symbol})\n"
            f"💵 ${token['price']:.6f} | MC: ${token['market_cap']:,} | Vol: ${token['volume']:,}\n"
            f"💧 LQ: ${token['liquidity']:,} | ⚡ Momentum: {new_score:.2f}\n"
            f"📈 Change: {token['price_change']:.2f}% | ⏱ Age: {token['age']} mins\n"
            f"🔗 [Dexscreener]({token['url']})"
        )

        if trending:
            msg += "\n🚀 *Trending Up!* Momentum rising fast!"
        if token["twitter"]:
            msg += f"\n🐦 [Twitter]({token['twitter']})"
        if token["telegram"]:
            msg += f"\n💬 [Telegram]({token['telegram']})"

        send_telegram_alert(msg)
    		        
    		         
    		         

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