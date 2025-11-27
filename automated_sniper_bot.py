import os
import sys
import time
import logging
import random
import csv
import threading
from datetime import datetime, time as dt_time
from typing import List, Dict, Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Dexscreener client (user must have this available)
from dexscreener import DexscreenerClient

# Flask for dummy HTTP server (so Render Web Service free works)
from flask import Flask

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sniper_bot")

# -------------------------
# Configuration (env-friendly)
# -------------------------
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", 600))  # seconds (10 min)
SLEEP_BETWEEN_TOKENS = float(os.environ.get("SLEEP_BETWEEN_TOKENS", 0.8))
MAX_TOP_RESULTS = int(os.environ.get("MAX_TOP_RESULTS", 5))
OUTPUT_CSV = os.environ.get("OUTPUT_CSV", "top_tokens.csv")

MIN_MARKET_CAP = float(os.environ.get("MIN_MARKET_CAP", 10000))
MAX_MARKET_CAP = float(os.environ.get("MAX_MARKET_CAP", 2000000))
MIN_LIQUIDITY = float(os.environ.get("MIN_LIQUIDITY", 5000))
MIN_VOLUME = float(os.environ.get("MIN_VOLUME", 1000))
MIN_AGE_MINUTES = int(os.environ.get("MIN_AGE_MINUTES", 30))
MAX_AGE_MINUTES = int(os.environ.get("MAX_AGE_MINUTES", 7200))

# Quiet hours: only very strong alerts
QUIET_HOURS_START = dt_time(23, 0)   # 23:00
QUIET_HOURS_END = dt_time(23, 30)    # 23:30
QUIET_THRESHOLD = float(os.environ.get("QUIET_THRESHOLD", 70.0))

# API failure protection
MAX_API_FAILURES = int(os.environ.get("MAX_API_FAILURES", 5))

# Volume/liquidity heuristics
VOLUME_SPIKE_MULTIPLIER = float(os.environ.get("VOLUME_SPIKE_MULTIPLIER", 2.0))
LIQUIDITY_DRAIN_PERCENT = float(os.environ.get("LIQUIDITY_DRAIN_PERCENT", 0.25))

# Trend detection
TREND_THRESHOLD = float(os.environ.get("TREND_THRESHOLD", 0.25))

# Telegram env
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS = [p.strip() for p in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if p.strip()]

# Dexscreener token endpoint
DEXS_TOKEN_ENDPOINT = os.environ.get("DEXS_TOKEN_ENDPOINT", "https://api.dexscreener.com/tokens/v1/solana/{}")

# Trading window
WINDOW_START = dt_time(20, 30)  # 20:30
WINDOW_END = dt_time(23, 30)     # 00:30 next day

# -------------------------
# HTTP session with retries
# -------------------------
def make_session(retries: int = 3, backoff: float = 0.4, timeout: int = 6) -> requests.Session:
    s = requests.Session()
    retry = Retry(total=retries, backoff_factor=backoff,
                  status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET", "POST"])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.request_timeout = timeout
    return s

session = make_session()

# -------------------------
# Helpers
# -------------------------
def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default

def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default

def safe_div(a: float, b: float, default: float = 0.0) -> float:
    try:
        return a / b if b else default
    except Exception:
        return default

def token_age_minutes(timestamp: Optional[int]) -> int:
    if not timestamp:
        return 1440
    try:
        ts = int(timestamp)
    except Exception:
        return 1440
    now_s = int(time.time())
    # detect ms vs s
    if ts > 1000000000000:
        ts //= 1000
    elif ts > 10000000000:
        ts //= 1000
    return max(0, (now_s - ts) // 60)

# -------------------------
# Flask dummy HTTP server for Render free web service
# -------------------------
def start_dummy_http_server():
    app = Flask(__name__)

    @app.route("/")
    def home():
        return "Sniper bot is running."

    # Render provides a PORT environment variable for Web Services.
    port = int(os.environ.get("PORT", 10000))
    # Run Flask (will block if not in thread)
    app.run(host="0.0.0.0", port=port)

# -------------------------
# Telegram client
# -------------------------
class TelegramClient:
    def __init__(self, bot_token: str, chat_ids: List[str], session: requests.Session):
        self.bot_token = bot_token
        self.chat_ids = chat_ids
        self.session = session

    def send_message(self, text: str) -> None:
        if not self.bot_token or not self.chat_ids:
            logger.debug("Telegram not configured — skipping message")
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        for cid in self.chat_ids:
            payload = {"chat_id": cid, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": False}
            try:
                r = self.session.post(url, json=payload, timeout=self.session.request_timeout)
                if r.status_code != 200:
                    logger.warning(f"Telegram send failed ({cid}): {r.status_code} {r.text}")
            except Exception as e:
                logger.warning(f"Telegram send exception ({cid}): {e}")

    def get_updates(self) -> List[Dict[str, Any]]:
        if not self.bot_token:
            return []
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates?offset=-1&limit=5"
        try:
            r = self.session.get(url, timeout=5)
            if r.status_code != 200:
                return []
            data = r.json()
            return data.get("result", [])
        except Exception:
            return []

# -------------------------
# Token analyzer
# -------------------------
class TokenAnalyzer:
    def __init__(self):
        pass

    def analyze_profile_data(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            if isinstance(data, list):
                data = data[0]

            base = data.get("baseToken", {})
            name = base.get("name", "unknown")
            symbol = base.get("symbol", "")
            price = safe_float(data.get("priceUsd"))
            market_cap = safe_float(data.get("marketCap"))
            volume = safe_float((data.get("volume") or {}).get("h24"))
            price_change = safe_float((data.get("priceChange") or {}).get("h24"))
            liquidity = safe_float((data.get("liquidity") or {}).get("usd", 0))
            age = token_age_minutes(data.get("pairCreatedAt"))
            url = data.get("url", "")
            info = data.get("info") or {}
            socials = info.get("socials", [])
            twitter = next((s.get("url") for s in socials if s.get("type") == "twitter"), "")
            telegram = next((s.get("url") for s in socials if s.get("type") == "telegram"), "")

            # basic filters
            if not (MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP):
                return None
            if volume < MIN_VOLUME or liquidity < MIN_LIQUIDITY:
                return None
            if not (MIN_AGE_MINUTES <= age <= MAX_AGE_MINUTES):
                return None
            if not twitter:
                return None

            # analytics
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
            logger.warning(f"analyze_profile_data error: {e}")
            return None

# -------------------------
# CSV Logger
# -------------------------
class CSVLogger:
    def __init__(self, path: str):
        self.path = path

    def save(self, results: List[Dict[str, Any]]):
        try:
            with open(self.path, mode="w", newline="", encoding="utf-8") as f:
                fieldnames = [
                    "Rank", "Name", "Symbol", "Price", "Market Cap", "Volume",
                    "Liquidity", "24h Change", "Age (mins)", "Momentum Score",
                    "Pair URL", "Twitter", "Telegram"
                ]
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for i, t in enumerate(results, 1):
                    writer.writerow({
                        "Rank": i,
                        "Name": t.get("name"),
                        "Symbol": t.get("symbol"),
                        "Price": f"{t.get('price',0):.6f}",
                        "Market Cap": f"{t.get('market_cap',0):,}",
                        "Volume": f"{t.get('volume',0):,}",
                        "Liquidity": f"{t.get('liquidity',0):,}",
                        "24h Change": f"{t.get('price_change',0):.2f}%",
                        "Age (mins)": t.get("age"),
                        "Momentum Score": f"{t.get('momentum',0):.2f}",
                        "Pair URL": t.get("url"),
                        "Twitter": t.get("twitter"),
                        "Telegram": t.get("telegram"),
                    })
        except Exception as e:
            logger.warning(f"CSV save error: {e}")

# -------------------------
# Core Sniper Bot
# -------------------------
class SniperBot:
    def __init__(self, telegram: TelegramClient, analyzer: TokenAnalyzer, csv_logger: CSVLogger):
        self.telegram = telegram
        self.analyzer = analyzer
        self.csv_logger = csv_logger
        self.client = DexscreenerClient()

        # runtime state
        self.previous_scores: Dict[str, float] = {}
        self.previous_volume: Dict[str, float] = {}
        self.previous_liquidity: Dict[str, float] = {}
        self.night_stats = {
            "cycles": 0,
            "tokens_scanned": 0,
            "momentum_values": [],
            "top_tokens": [],
            "trending_tokens": []
        }
        self.api_failures = 0
        self.session_active = False
        self.round_counter = 0

    # ---------- fetch & analyze ----------
    def fetch_profiles(self) -> List[Any]:
        try:
            profiles = self.client.get_latest_token_profiles()
            return profiles or []
        except Exception as e:
            logger.warning(f"Dexscreener client error: {e}")
            return []

    def fetch_token_detail(self, token_address: str) -> Optional[Dict[str, Any]]:
        try:
            r = session.get(DEXS_TOKEN_ENDPOINT.format(token_address), timeout=session.request_timeout)
            if r.status_code != 200:
                self.api_failures += 1
                return None
            self.api_failures = 0
            return r.json()
        except Exception as e:
            self.api_failures += 1
            logger.warning(f"fetch_token_detail error: {e}")
            return None

    def scan_once(self) -> List[Dict[str, Any]]:
        profiles = self.fetch_profiles()
        results: List[Dict[str, Any]] = []

        for p in profiles:
            try:
                if getattr(p, "chain_id", None) != "solana" and p.get("chain_id", None) != "solana":
                    continue
                addr = getattr(p, "token_address", None) or p.get("token_address", None)
                if not addr:
                    continue
                detail = self.fetch_token_detail(addr)
                if not detail:
                    continue
                analyzed = self.analyzer.analyze_profile_data(detail)
                if analyzed:
                    results.append(analyzed)
            except Exception as e:
                logger.warning(f"scan profile error: {e}")
            time.sleep(SLEEP_BETWEEN_TOKENS)

        results = sorted(results, key=lambda x: x.get("momentum", 0.0), reverse=True)[:MAX_TOP_RESULTS]
        return results

    # ---------- detectors ----------
    def detect_volume_spike(self, token: Dict[str, Any], addr: str) -> bool:
        cur = token.get("volume", 0.0)
        old = self.previous_volume.get(addr, cur)
        if old <= 0:
            self.previous_volume[addr] = cur
            return False
        ratio = cur / old if old else 1.0
        self.previous_volume[addr] = cur
        return ratio >= VOLUME_SPIKE_MULTIPLIER

    def detect_liquidity_drain(self, token: Dict[str, Any], addr: str) -> bool:
        cur = token.get("liquidity", 0.0)
        old = self.previous_liquidity.get(addr, cur)
        if old <= 0:
            self.previous_liquidity[addr] = cur
            return False
        drop = (old - cur) / old if old else 0.0
        self.previous_liquidity[addr] = cur
        return drop >= LIQUIDITY_DRAIN_PERCENT

    # ---------- helpers ----------
    def in_trading_window(self) -> bool:
        now = datetime.now().time()
        return now >= WINDOW_START or now <= WINDOW_END

    def in_quiet_hours(self) -> bool:
        now = datetime.now().time()
        return QUIET_HOURS_START <= now <= QUIET_HOURS_END

    def send_night_summary(self) -> None:
        s = self.night_stats
        if s["cycles"] == 0:
            self.telegram.send_message("📴 Night ended — no scans completed tonight.")
            return
        avg = (sum(s["momentum_values"]) / len(s["momentum_values"])) if s["momentum_values"] else 0
        best = max(s["top_tokens"], key=lambda x: x["momentum"]) if s["top_tokens"] else None
        trending = max(s["trending_tokens"], key=lambda x: x["momentum"]) if s["trending_tokens"] else None
        summary = (
            f"🌙 *Night Session Summary (9:30 PM → 12:30 AM)*\n"
            f"🔁 Cycles Completed: *{s['cycles']}*\n"
            f"📊 Tokens Scanned: *{s['tokens_scanned']}*\n"
            f"📈 Avg Momentum: *{avg:.2f}*\n\n"
        )
        if best:
            summary += f"🏆 *Best Performer:* {best['name']} — {best['momentum']:.2f}\n"
        if trending:
            summary += f"🔥 *Most Trending:* {trending['name']} — {trending['momentum']:.2f}\n"
        self.telegram.send_message(summary)

    def handle_telegram_commands(self) -> None:
        updates = self.telegram.get_updates()
        if not updates:
            return
        for upd in updates:
            msg = upd.get("message") or {}
            text = (msg.get("text") or "").strip().lower()
            chat_id = msg.get("chat", {}).get("id")
            if not text or not chat_id:
                continue
            if text == "/stats":
                s = self.night_stats
                avg = (sum(s["momentum_values"]) / len(s["momentum_values"])) if s["momentum_values"] else 0
                best_name = max(s["top_tokens"], key=lambda x: x["momentum"])['name'] if s["top_tokens"] else 'N/A'
                out = (
                    f"📊 *Night Stats So Far:*\n"
                    f"🔁 Cycles: {s['cycles']}\n"
                    f"📈 Avg Momentum: {avg:.2f}\n"
                    f"🔥 Trending Tokens: {len(s['trending_tokens'])}\n"
                    f"🏆 Best Token So Far: {best_name}"
                )
                self.telegram.send_message(out)
            elif text == "/top":
                if not self.night_stats.get("top_tokens"):
                    self.telegram.send_message("No tokens scanned yet.")
                    continue
                top_now = sorted(self.night_stats["top_tokens"], key=lambda x: x["momentum"], reverse=True)[:3]
                resp = "🏆 *Top Tokens Right Now*\n"
                for t in top_now:
                    resp += f"• {t['name']} — {t['momentum']:.2f}\n"
                self.telegram.send_message(resp)

    def run_cycle(self, test_mode: bool = False) -> None:
        self.round_counter += 1
        results = self.scan_once()
        self.night_stats["cycles"] += 1

        if not results:
            logger.info("No valid tokens found this round.")
            if test_mode:
                logger.info("Test mode: ending after one cycle.")
            return

        self.csv_logger.save(results)
        self.night_stats["tokens_scanned"] += len(results)
        self.night_stats["top_tokens"].extend(results)
        for t in results:
            self.night_stats["momentum_values"].append(t.get("momentum", 0.0))

        # Evaluate and alert
        for idx, token in enumerate(results, 1):
            addr = token.get("url", "").split("/")[-1] if token.get("url") else token.get("symbol")
            old_score = self.previous_scores.get(addr, 0.0)
            new_score = token.get("momentum", 0.0)
            trending = (old_score == 0 and new_score > 0) or (
                old_score > 0 and (new_score - old_score) / max(old_score, 1) >= TREND_THRESHOLD
            )
            self.previous_scores[addr] = new_score

            volume_spike = self.detect_volume_spike(token, addr)
            liquidity_rug = self.detect_liquidity_drain(token, addr)
            if liquidity_rug:
                self.night_stats["trending_tokens"].append(token)

            # Quiet hours suppression
            if self.in_quiet_hours() and new_score < QUIET_THRESHOLD and not (trending or volume_spike or liquidity_rug):
                continue

            # Build message
            if liquidity_rug:
                emoji = "🚨"
                extra = "\n⚠️ *Liquidity Drain Detected — Possible Rug!*"
            elif volume_spike:
                emoji = "💥"
                extra = "\n📈 *Volume Spike Detected!*"
            elif trending:
                emoji = "🔥"
                extra = "\n🚀 *Trending Up!*"
            else:
                emoji = "🪙"
                extra = ""

            msg = (
                f"{emoji} *#{idx} — {token.get('name')}* ({token.get('symbol')})\n"
                f"💵 ${token.get('price',0):.6f} | MC: ${token.get('market_cap',0):,}\n"
                f"💧 LQ: ${token.get('liquidity',0):,} | ⚡ Momentum: {new_score:.2f}\n"
                f"📈 Change: {token.get('price_change',0):.2f}% | ⏱ Age: {token.get('age')} mins\n"
                f"🔗 [Dexscreener]({token.get('url')}){extra}"
            )
            self.telegram.send_message(msg)

    def run(self, test_mode: bool = False):
        logger.info("SniperBot starting. Scheduled window: 21:30 -> 00:30.")
        try:
            while True:
                # Poll Telegram commands every cycle
                self.handle_telegram_commands()

                if self.in_trading_window():
                    if not self.session_active:
                        # start of session
                        self.session_active = True
                        self.api_failures = 0
                        self.night_stats = {"cycles":0, "tokens_scanned":0, "momentum_values":[], "top_tokens":[], "trending_tokens":[]}
                        start_msg = (
                            "📣 *Sniper Session Started*\n"
                            "🕤 Window: *9:30 PM → 12:30 AM*\n"
                            "🔍 Beginning scan cycles..."
                        )
                        self.telegram.send_message(start_msg)
                        logger.info("Session started")

                    if self.api_failures >= MAX_API_FAILURES:
                        warn = "⚠️ *Session auto-stopped due to repeated API failures.* System will retry tomorrow night."
                        self.telegram.send_message(warn)
                        self.session_active = False
                        time.sleep(600)
                        continue

                    logger.info("Running scan cycle...")
                    self.run_cycle(test_mode=test_mode)

                    if test_mode:
                        logger.info("Test mode - finished one cycle. Exiting.")
                        return

                    # random 5-10 minute pause
                    delay = random.randint(300, 600)
                    logger.info(f"Sleeping for {delay//60} minutes...")
                    time.sleep(delay)

                else:
                    # session end
                    if self.session_active:
                        self.send_night_summary()
                        end_msg = ("📴 *Sniper Session Ended*\n" "🕛 Time: 12:30 AM\n" "📉 Scanning paused until tomorrow.")
                        self.telegram.send_message(end_msg)
                        self.session_active = False
                        logger.info("Session ended")

                    # poll commands and wait until next window
                    time.sleep(300)
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt - stopping bot")
        except Exception as e:
            logger.exception(f"Unhandled exception in run: {e}")

# -------------------------
# Entrypoint
# -------------------------
def main():
    test_mode = "--test" in sys.argv

    # Start dummy Flask HTTP server in background thread (for Render web service)
    threading.Thread(target=start_dummy_http_server, daemon=True).start()
    logger.info("Started dummy Flask HTTP server thread (for Render port binding).")

    # build clients
    telegram = TelegramClient(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS, session)
    analyzer = TokenAnalyzer()
    csv_logger = CSVLogger(OUTPUT_CSV)
    bot = SniperBot(telegram, analyzer, csv_logger)

    # Run the bot (blocks)
    bot.run(test_mode=test_mode)

if __name__ == "__main__":
    main()
