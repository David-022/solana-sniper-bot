# =========================================================
# ADVANCED AI CRYPTO SCANNER BOT
# MOBILE FRIENDLY SINGLE FILE VERSION
# =========================================================

import asyncio
import logging
import time
import os

import aiohttp
import aiosqlite
import threading

from aiogram import (
    Bot,
    Dispatcher,
    Router
)

from aiogram.enums import ParseMode

from aiogram.filters import Command

from aiogram.types import (
    Message,
    CallbackQuery
)

from aiogram.client.default import (
    DefaultBotProperties
)

from aiogram.utils.keyboard import (
    InlineKeyboardBuilder
)

from dexscreener import (
    DexscreenerClient
)
from dotenv import load_dotenv

from flask import Flask

# =========================================================
# FLASK KEEP ALIVE
# =========================================================

app = Flask(__name__)

@app.route("/")
def home():
    return "BOT RUNNING"


def start_flask():

    port = int(
        os.environ.get("PORT", 10000)
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded = True,
        use_reloader = False
    )

# =========================================================
# CONFIG
# =========================================================

# === Load Env Variables ===
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")


DB_FILE = "crypto_scanner.db"

# Scanner Settings

SCAN_INTERVAL = 60

MAX_TOP_RESULTS = 5

# Filters

MIN_MARKET_CAP = 10000
MAX_MARKET_CAP = 2000000

MIN_VOLUME = 3000
MIN_LIQUIDITY = 8000

MIN_AGE_MINUTES = 3
MAX_AGE_MINUTES = 100

TREND_THRESHOLD = 0.20

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    force=True
)

# =========================================================
# BOT
# =========================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    )
)

dp = Dispatcher()

router = Router()

dp.include_router(router)

logging.info("BOT STARTED")

# =========================================================
# SIMPLE MEMORY CACHE
# =========================================================

token_cache = {}

CACHE_TTL = 300


async def get_cached_token(
    token_address
):

    cached = token_cache.get(
        token_address
    )

    if not cached:
        return None

    data = cached["data"]

    timestamp = cached["timestamp"]

    now = time.time()

    if now - timestamp > CACHE_TTL:

        del token_cache[token_address]

        return None

    return data


async def cache_token(
    token_address,
    data
):

    token_cache[token_address] = {
        "data": data,
        "timestamp": time.time()
    }

# =========================================================
# DATABASE
# =========================================================

async def init_db():

    async with aiosqlite.connect(
        DB_FILE
    ) as db:

        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            min_market_cap INTEGER DEFAULT 10000,
            max_market_cap INTEGER DEFAULT 2000000,
            min_liquidity INTEGER DEFAULT 5000,
            alerts_enabled INTEGER DEFAULT 1
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS tracked_tokens (
            chat_id INTEGER,
            token_address TEXT,
            PRIMARY KEY (
                chat_id,
                token_address
            )
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS sent_alerts (
            token_address TEXT PRIMARY KEY,
            timestamp INTEGER
        )
        """)

        await db.commit()

# =========================================================
# DATABASE HELPERS
# =========================================================

async def add_user(chat_id):

    async with aiosqlite.connect(
        DB_FILE
    ) as db:

        await db.execute(
            """
            INSERT OR IGNORE
            INTO users (chat_id)
            VALUES (?)
            """,
            (chat_id,)
        )

        await db.commit()


async def is_registered(chat_id):

    async with aiosqlite.connect(
        DB_FILE
    ) as db:

        async with db.execute(
            """
            SELECT chat_id
            FROM users
            WHERE chat_id = ?
            """,
            (chat_id,)
        ) as cursor:

            row = await cursor.fetchone()

    return row is not None


async def get_users():

    async with aiosqlite.connect(
        DB_FILE
    ) as db:

        async with db.execute(
            """
            SELECT chat_id
            FROM users
            WHERE alerts_enabled = 1
            """
        ) as cursor:

            rows = await cursor.fetchall()

    return [r[0] for r in rows]


async def track_token(
    chat_id,
    token
):

    async with aiosqlite.connect(
        DB_FILE
    ) as db:

        await db.execute(
            """
            INSERT OR IGNORE
            INTO tracked_tokens
            (
                chat_id,
                token_address
            )
            VALUES (?, ?)
            """,
            (
                chat_id,
                token
            )
        )

        await db.commit()


async def get_watchlist(chat_id):

    async with aiosqlite.connect(
        DB_FILE
    ) as db:

        async with db.execute(
            """
            SELECT token_address
            FROM tracked_tokens
            WHERE chat_id = ?
            """,
            (chat_id,)
        ) as cursor:

            rows = await cursor.fetchall()

    return [r[0] for r in rows]

# =========================================================
# HELPERS
# =========================================================

def safe_float(
    value,
    default=0.0
):

    try:
        return float(value)

    except Exception:
        return default


def token_age_minutes(timestamp):

    if not timestamp:
        return 99999

    now = int(time.time())

    ts = int(timestamp)

    if ts > 1e10:
        ts //= 1000

    return max(
        0,
        (now - ts) // 60
    )

# =========================================================
# ANALYTICS ENGINE
# =========================================================


def analyze_token(data):

    try:

        if isinstance(data, list):

            if not data:
                return None

            data = data[0]

        name = (
            data.get(
                "baseToken",
                {}
            )
            .get("name", "")
        )

        symbol = (
            data.get(
                "baseToken",
                {}
            )
            .get("symbol", "")
        )

        market_cap = safe_float(
            data.get("marketCap")
        )

        liquidity = safe_float(
            data.get(
                "liquidity",
                {}
            ).get("usd")
        )

        volume = safe_float(
            data.get(
                "volume",
                {}
            ).get("h24")
        )

        price_change = safe_float(
            data.get(
                "priceChange",
                {}
            ).get("h24")
        )

        buys = safe_float(
            data.get(
                "txns",
                {}
            )
            .get("h24", {})
            .get("buys")
        )

        sells = safe_float(
            data.get(
                "txns",
                {}
            )
            .get("h24", {})
            .get("sells")
        )

        age = token_age_minutes(
            data.get("pairCreatedAt")
        )

        url = data.get(
            "url",
            ""
        )

        # =================================================
        # FILTERS
        # =================================================

        if not (
            MIN_MARKET_CAP
            <= market_cap
            <= MAX_MARKET_CAP
        ):
            return None

        if volume < MIN_VOLUME:
            return None

        if liquidity < MIN_LIQUIDITY:
            return None

        if not (
            MIN_AGE_MINUTES
            <= age
            <= MAX_AGE_MINUTES
        ):
            return None

        # =================================================
        # ANALYTICS
        # =================================================

        buy_pressure = (
            buys / (sells + 1)
        )

        liquidity_health = (
            liquidity /
            (market_cap + 1)
        )

        activity_ratio = (
            volume /
            (market_cap + 1)
        )

        velocity_score = (
            activity_ratio * 100
        )

        rug_risk = 0

        if liquidity_health < 0.05:
            rug_risk += 35

        if buy_pressure < 0.5:
            rug_risk += 25

        if liquidity < 5000:
            rug_risk += 25

        if age < 2:
            rug_risk += 15

        ai_score = (
            (
                price_change * 0.35
            )
            +
            (
                velocity_score * 0.30
            )
            +
            (
                buy_pressure * 15 * 0.20
            )
            +
            (
                liquidity_health
                * 100
                * 0.15
            )
        )

        return {
            "name": name,
            "symbol": symbol,
            "market_cap": market_cap,
            "liquidity": liquidity,
            "volume": volume,
            "price_change": price_change,
            "buy_pressure": buy_pressure,
            "velocity_score": velocity_score,
            "rug_risk": rug_risk,
            "ai_score": ai_score,
            "age": age,
            "url": url
        }

    except Exception as e:

        logging.warning(
            f"Analytics error: {e}"
        )

        return None

# =========================================================
# ALERT FORMATTER
# =========================================================

def format_alert(token):

    risk = "LOW"

    if token["rug_risk"] > 50:
        risk = "HIGH"

    elif token["rug_risk"] > 25:
        risk = "MEDIUM"

    emoji = "🔥"

    return (
        f"{emoji} "
        f"{token['name']} "
        f"({token['symbol']})\n\n"

        f"💰 MC: "
        f"${token['market_cap']:,.0f}\n"

        f"💧 Liquidity: "
        f"${token['liquidity']:,.0f}\n"

        f"📊 Volume: "
        f"${token['volume']:,.0f}\n"

        f"📈 Change: "
        f"{token['price_change']:.2f}%\n"

        f"⚡ AI Score: "
        f"{token['ai_score']:.2f}\n"

        f"🚀 Velocity: "
        f"{token['velocity_score']:.2f}\n"

        f"🧠 Buy Pressure: "
        f"{token['buy_pressure']:.2f}\n"

        f"⚠ Rug Risk: "
        f"{risk}\n"

        f"⏱ Age: "
        f"{token['age']} mins\n\n"

        f"🔗 {token['url']}"
    )

# =========================================================
# DUPLICATE ALERT PREVENTION
# =========================================================

sent_alerts = set()

def should_send_alert(address):

    if address in sent_alerts:
        return False

    sent_alerts.add(address)

    return True

# =========================================================
# KEYBOARDS
# =========================================================

def main_menu():

    kb = InlineKeyboardBuilder()

    kb.button(
        text="📊 Scanner",
        callback_data="scanner"
    )

    kb.button(
        text="⭐ Watchlist",
        callback_data="watchlist"
    )

    kb.button(
        text="🔥 Trending",
        callback_data="trending"
    )

    kb.button(
        text="⚙ Settings",
        callback_data="settings"
    )

    kb.button(
        text="🆔 My ID",
        callback_data="myid"
    )

    kb.button(
        text="📡 Status",
        callback_data="status"
    )

    kb.adjust(2)

    return kb.as_markup()


def token_keyboard(address):

    kb = InlineKeyboardBuilder()

    kb.button(
        text="⭐ Track",
        callback_data=f"track:{address}"
    )

    kb.button(
        text="❌ Ignore",
        callback_data="ignore"
    )

    kb.adjust(2)

    return kb.as_markup()

# =========================================================
# COMMANDS
# =========================================================

@router.message(Command("start"))
async def start_handler(
    message: Message
):

    await message.answer(
        (
            "🔥 Welcome to "
            "AI Crypto Scanner Bot\n\n"

            "Real-time Solana "
            "token intelligence."
        ),
        reply_markup=main_menu()
    )


@router.message(Command("help"))
async def help_handler(
    message: Message
):

    await message.answer(
        "Choose an option below:",
        reply_markup=main_menu()
    )

'''
@router.message(Command("myid"))
async def myid_handler(
    message: Message
):

    await message.answer(
        f"🆔 {message.chat.id}"
    )


@router.message(Command("status"))
async def status_handler(
    message: Message
):

    registered = await is_registered(
        message.chat.id
    )

    if registered:

        await message.answer(
            "✅ Approved"
        )

    else:

        await message.answer(
            (
                "❌ Not approved.\n\n"
                f"Send ID to "
                f"{ADMIN_USERNAME}"
            )
        )


@router.message(Command("watchlist"))
async def watchlist_handler(
    message: Message
):

    tokens = await get_watchlist(
        message.chat.id
    )

    if not tokens:

        await message.answer(
            "📭 Empty watchlist"
        )

        return

    msg = "⭐ WATCHLIST\n\n"

    msg += "\n".join(tokens)

    await message.answer(msg)
'''
# =========================================================
# ADMIN
# =========================================================

@router.message(Command("register"))
async def register_handler(
    message: Message
):

    if (
        message.chat.id
        != ADMIN_CHAT_ID
    ):
        return

    try:

        user_id = int(
            message.text.split()[1]
        )

        await add_user(user_id)

        await message.answer(
            f"✅ Registered {user_id}"
        )

        await bot.send_message(
            user_id,
            (
                "✅ You are now approved."
            )
        )

    except Exception:

        await message.answer(
            "Usage:\n/register USER_ID"
        )

@router.message()
async def ignore_text(
    message: Message
):

    await message.answer(
        (
            "⚠ Please use the menu buttons.\n"
            "Use '/help' to access buttons"
        )
    )
    
# =========================================================
# CALLBACKS
# =========================================================

@router.callback_query()
async def callback_handler(
    callback: CallbackQuery
):

    data = callback.data

    if data.startswith("track:"):

        token = data.split(
            ":",
            1
        )[1]

        await track_token(
            callback.from_user.id,
            token
        )

        await callback.message.answer(
            "⭐ Added to watchlist"
        )

    elif data == "scanner":

        await callback.message.answer(
            "📊 Scanner running live"
        )

    elif data == "watchlist":

        tokens = await get_watchlist(
            callback.from_user.id
        )

        if tokens:

            await callback.message.answer(
                "\n".join(tokens)
            )

        else:

            await callback.message.answer(
                "📭 Empty watchlist"
            )

    elif data == "settings":

        await callback.message.answer(
            (
                "⚙ Settings panel "
                "coming soon"
            )
        )
        
    elif data == "help":

    	await callback.message.answer(
        	(
            	"🔥 AI Crypto Scanner Bot\n\n"

            	"Use the buttons below "
            	"to navigate the bot."
        	),
        	reply_markup=main_menu()
    	)
        
    elif data == "myid":
        await callback.message.answer(
        	f"🆔 {callback.from_user.id}"
    	)
    
    elif data == "status":
    	registered = await is_registered(
    		callback.from_user.id
    	)
    	
    	if registered:
    		await callback.message.answer(
    			"✅ Approved"
    		)
    		
    	else:
    		await callback.message.answer(
    			(
    				"❌ Not approved.\n\n"
    				f"Send ID to "
    				f"{ADMIN_USERNAME}"
    			)
    		
    		)
    

    await callback.answer()

# =========================================================
# FETCH TOKEN
# =========================================================

DEX_URL = (
    "https://api.dexscreener.com/"
    "tokens/v1/solana/{}"
)

async def fetch_token(
    session,
    token_address
):

    cached = await get_cached_token(
        token_address
    )

    if cached:
        return cached

    try:

        async with session.get(
            DEX_URL.format(token_address)
        ) as response:

            if response.status != 200:
                return None

            data = await response.json()

            analyzed = analyze_token(
                data
            )

            if analyzed:

                await cache_token(
                    token_address,
                    analyzed
                )

            return analyzed

    except Exception as e:

        logging.warning(
            f"Fetch error: {e}"
        )

        return None

# =========================================================
# CONCURRENT TOKEN SCANNER
# =========================================================

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)

async def scan_tokens():

    client = DexscreenerClient()

    profiles = await asyncio.to_thread(
        client.get_latest_token_profiles
    )

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:

        tasks = []

        for profile in profiles:

            if not profile.token_address:
                continue

            if (
                profile.chain_id
                != "solana"
            ):
                continue

            task = asyncio.create_task(
                fetch_token(
                    session,
                    profile.token_address
                )
            )
            
            tasks.append(task)

        # MOBILE SAFE LIMIT
        tasks = tasks[:25]

        results = await asyncio.gather(
            *tasks,
            return_exceptions = True
        )
        
    cleaned = []
    
    for result in results:
    	if isinstance(
    		result,
    		Exception
    	):
    		continue
    	
    	if result is not None:
    		cleaned.append(result)

    return cleaned

# =========================================================
# ALERT LOOP
# =========================================================
async def alert_loop():

    while True:

        logging.info(
            "Running scan..."
        )

        try:

            results = await asyncio.wait_for(
            scan_tokens(),
            timeout=40
            )

            results = sorted(
                results,
                key=lambda x: (
                    x["ai_score"]
                ),
                reverse=True
            )[:MAX_TOP_RESULTS]

            users = await get_users()

            for token in results:

                address = (
                    token["url"]
                    .split("/")[-1]
                    if token["url"]
                    else token["symbol"]
                )

                if not should_send_alert(
                    address
                ):
                    continue

                msg = format_alert(
                    token
                )

                await asyncio.gather(
                	*[
                		bot.send_message(
                			user_id,
                			msg,
                			reply_markup=token_keyboard(address)
                		)
                		for user_id in users
                	],
                	return_exceptions = True
                )

                       
        except asyncio.TimeoutError:
        	logging.warning("Scanner timed out.")

        except Exception as e:

            logging.warning(
                f"Scanner error: {e}"
            )

        logging.info(
            f"Sleeping "
            f"{SCAN_INTERVAL}s"
        )

        await asyncio.sleep(
            SCAN_INTERVAL
        )
# =========================================================
# MAIN
# =========================================================

async def main():

    await init_db()

    asyncio.create_task(
        alert_loop()
    )

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    await dp.start_polling(bot)

# =========================================================
# ENTRY
# =========================================================

if __name__ == "__main__":

    threading.Thread(
        target=start_flask,
        daemon=True
    ).start()

    asyncio.run(main())