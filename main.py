import os
import json
import time
import asyncio
import datetime
import random
import string
from typing import Dict, Any, Optional, List


def truncate_message(text: str, max_length: int = 1900) -> str:
    """Truncate a message to fit Discord's 2000 character limit with safety margin"""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."

import discord
from discord import app_commands
from discord.ext import commands

import aiohttp
from aiohttp import web

# Optional asyncpg for direct PostgreSQL connections
try:
    import asyncpg
except ImportError:
    asyncpg = None

# Database - Supabase REST Data API (recommended)
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

# Legacy PostgreSQL support (Railway/Supabase Direct)
DATABASE_URL = os.getenv("DATABASE_URL", "")
SUPABASE_PG_URL = os.getenv("SUPABASE_PG_URL", "")

# Use Supabase REST API if URL is set
USE_SUPABASE_API = bool(SUPABASE_URL and SUPABASE_KEY)

print(f"SUPABASE_URL present: {bool(SUPABASE_URL)}")
print(f"SUPABASE_KEY present: {bool(SUPABASE_KEY)}")
print(f"Using Supabase REST API: {USE_SUPABASE_API}")
if USE_SUPABASE_API:
    print(f"Supabase URL: {SUPABASE_URL}")
print(f"DATABASE_URL present: {bool(DATABASE_URL)}")

# Database pool for direct PostgreSQL (only used when not using Supabase REST API)
db_pool = None
supabase_headers: Dict[str, str] = {}

async def init_db():
    """Initialize database connection - either Supabase REST API or PostgreSQL"""
    global db_pool, supabase_headers

    # Try Supabase REST API first
    if USE_SUPABASE_API:
        supabase_headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }
        print(f"Using Supabase REST API: {SUPABASE_URL}/rest/v1/")
        print("Database initialized successfully!")
        return

    # Fallback to PostgreSQL
    DB_CONNECTION_STRING = DATABASE_URL or SUPABASE_PG_URL
    if not DB_CONNECTION_STRING:
        print("WARNING: No database configured, linked accounts will not be persisted!")
        return

    try:
        # Supabase uses postgresql://, convert to postgres:// if needed
        connection_str = DB_CONNECTION_STRING
        if connection_str.startswith("postgresql://"):
            connection_str = connection_str.replace("postgresql://", "postgres://", 1)

        print(f"Connecting to database: {connection_str[:50]}...")
        db_pool = await asyncpg.create_pool(connection_str, min_size=1, max_size=5)

        # Create tables if they don't exist
        async with db_pool.acquire() as conn:
            # Create linked_accounts table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS linked_accounts (
                    id SERIAL PRIMARY KEY,
                    discord_id BIGINT NOT NULL UNIQUE,
                    minecraft_name VARCHAR(255) NOT NULL,
                    linked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)

            # Create pending_codes table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_codes (
                    id SERIAL PRIMARY KEY,
                    discord_id BIGINT NOT NULL,
                    code VARCHAR(8) NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    used BOOLEAN DEFAULT FALSE
                )
            """)

            # Create indexes
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_linked_discord ON linked_accounts(discord_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_linked_minecraft ON linked_accounts(minecraft_name)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_code ON pending_codes(code)")

        print("Database initialized successfully!")
    except Exception as e:
        print(f"Failed to initialize database: {e}")
        db_pool = None

async def close_db():
    """Close database connection"""
    global db_pool
    if db_pool:
        await db_pool.close()

# =========================
# Supabase REST API Helpers
# =========================

async def supabase_select(table: str, filters: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    """Select rows from a table using Supabase REST API"""
    if not USE_SUPABASE_API:
        return []

    url = f"{SUPABASE_URL}/rest/v1/{table}"
    params = {}
    if filters:
        for key, value in filters.items():
            params[key] = f"eq.{value}"

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=supabase_headers, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    print(f"Supabase select error: {resp.status} - {await resp.text()}")
                    return []
    except asyncio.TimeoutError:
        print("Supabase select timeout")
        return []
    except Exception as e:
        print(f"Supabase select exception: {e}")
        return []

async def supabase_insert(table: str, data: Dict[str, Any]) -> bool:
    """Insert a row into a table using Supabase REST API"""
    if not USE_SUPABASE_API:
        return False

    url = f"{SUPABASE_URL}/rest/v1/{table}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=supabase_headers, json=data) as resp:
                if resp.status in (200, 201):
                    return True
                else:
                    text = await resp.text()
                    print(f"Supabase insert error: {resp.status} - {text}")
                    if "duplicate" in text.lower() or "unique" in text.lower():
                        return await supabase_upsert(table, data)
                    return False
    except Exception as e:
        print(f"Supabase insert exception: {e}")
        return False

async def supabase_upsert(table: str, data: Dict[str, Any]) -> bool:
    """Upsert a row into a table using Supabase REST API"""
    if not USE_SUPABASE_API:
        return False

    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = supabase_headers.copy()
    headers["Prefer"] = "resolution=merge-duplicates"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=data) as resp:
                if resp.status in (200, 201):
                    return True
                else:
                    print(f"Supabase upsert error: {resp.status} - {await resp.text()}")
                    return False
    except Exception as e:
        print(f"Supabase upsert exception: {e}")
        return False

async def supabase_update(table: str, data: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    """Update rows in a table using Supabase REST API"""
    if not USE_SUPABASE_API:
        return False

    url = f"{SUPABASE_URL}/rest/v1/{table}"
    params = {}
    for key, value in filters.items():
        params[key] = f"eq.{value}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.patch(url, headers=supabase_headers, json=data, params=params) as resp:
                if resp.status in (200, 204):
                    return True
                else:
                    print(f"Supabase update error: {resp.status} - {await resp.text()}")
                    return False
    except Exception as e:
        print(f"Supabase update exception: {e}")
        return False

async def supabase_delete(table: str, filters: Dict[str, Any]) -> bool:
    """Delete rows from a table using Supabase REST API"""
    if not USE_SUPABASE_API:
        return False

    url = f"{SUPABASE_URL}/rest/v1/{table}"
    params = {}
    for key, value in filters.items():
        params[key] = f"eq.{value}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.delete(url, headers=supabase_headers, params=params) as resp:
                if resp.status in (200, 204):
                    return True
                else:
                    print(f"Supabase delete error: {resp.status} - {await resp.text()}")
                    return False
    except Exception as e:
        print(f"Supabase delete exception: {e}")
        return False

def supabase_select_sync(table: str, filters: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    """Synchronous wrapper for select"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, supabase_select(table, filters))
                return future.result()
        else:
            return asyncio.run(supabase_select(table, filters))
    except Exception as e:
        print(f"supabase_select_sync error: {e}")
        return []

def supabase_insert_sync(table: str, data: Dict[str, Any]) -> bool:
    """Synchronous wrapper for insert"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, supabase_insert(table, data))
                return future.result()
        else:
            return asyncio.run(supabase_insert(table, data))
    except Exception as e:
        print(f"supabase_insert_sync error: {e}")
        return False

# =========================
# ENV / CONFIG
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0"))
# Additional role IDs that can use staff commands
EXTRA_STAFF_ROLE_IDS = [int(os.getenv("EXTRA_STAFF_ROLE_IDS", "0"))] if os.getenv("EXTRA_STAFF_ROLE_IDS") else []
# Specific user IDs that can use staff commands (comma-separated)
ALLOWED_USER_IDS = [int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()]
# DEBUG: Hardcoded user ID for testing
DEBUG_ALLOWED_USERS = []
# DEBUG: Hardcoded role IDs for testing
DEBUG_ALLOWED_ROLES = [1483822408182796418]

WEBSITE_URL = os.getenv("WEBSITE_URL", "").rstrip("/")  # e.g. https://neontiers.vercel.app
BOT_API_KEY = os.getenv("BOT_API_KEY", "")              # shared secret between bot and website

# Minecraft Verification API
MINECRAFT_API_URL = os.getenv("MINECRAFT_API_URL", "http://localhost:8080").rstrip("/")

WIPE_GLOBAL_COMMANDS = os.getenv("WIPE_GLOBAL_COMMANDS", "0") == "1"

COOLDOWN_SECONDS = 30 * 24 * 60 * 60
DATA_FILE = "data.json"

HTTP_TIMEOUT_SECONDS = 10  # hard timeout so it never "thinks forever"


# =========================
# CONSTANTS
# =========================
TICKET_TYPES = [
    ("Vanilla", "vanilla", 1469763891226480926),
    ("UHC", "uhc", 1469765994988704030),
    ("Pot", "pot", 1469763780593324032),
    ("NethPot", "nethpot", 1469763817218117697),
    ("SMP", "smp", 1469764274955223161),
    ("Sword", "sword", 1469763677141074125),
    ("Axe", "axe", 1469763738889486518),
    ("Mace", "mace", 1469763612452196375),
    ("Cart", "cart", 1469763920871952435),
    ("Creeper", "creeper", 1469764200812249180),
    ("DiaSMP", "diasmp", 1469763946968911893),
    ("OGVanilla", "ogvanilla", 1469764329460203571),
    ("ShieldlessUHC", "shieldlessuhc", 1469766017243807865),
    ("SpearMace", "spearmace", 1469968704203788425),
    ("SpearElytra", "spearelytra", 1469968762575912970),
    # --- Added gamemodes: replace 0s with real Discord tester role IDs ---
    ("DiaPot", "diapot", 0),
    ("NethSMP", "nethsmp", 0),
    ("Crystal", "crystal", 0),
]

# Required rounds for each gamemode (FT = First to, LT = Last to)
# Format: (default_ft, lt3_below_ft, loss_ft optional)
# If player is below LT3, they play fewer rounds
# If they lose a round against tester, they play even fewer rounds (for certain modes)
TICKET_ROUNDS = {
    "vanilla": ("FT4", "FT3", None),
    "diasmp": ("FT4", "FT3", "FT2"),  # FT2 if lose round
    "ogvanilla": ("FT4", "FT2", None),
    "nethpot": ("FT4", "FT2", None),
    "mace": ("FT4", "FT2", None),
    "smp": ("FT4", "FT3", "FT2"),  # FT2 if lose round
    "cart": ("FT4", "FT3", "FT2"),  # FT2 if lose round
    "sword": ("FT10", "FT6", None),
    "uhc": ("FT6", "FT3", None),
    "pot": ("FT10", "FT6", None),
    "creeper": ("FT6", "FT4", "FT3"),  # FT3 if lose round
    "shieldlessuhc": ("FT6", "FT4", None),
    "axe": ("FT20", "FT10", None),
    "spearmace": ("FT6", "FT3", None),
    "spearelytra": ("FT6", "FT3", None),
    "diapot": ("FT4", "FT2", None),
    "nethsmp": ("FT4", "FT3", "FT2"),  # FT2 if lose round
    "crystal": ("FT4", "FT3", None),
}


def get_ticket_rounds_display(mode_key: str) -> str:
    """Get the display string for required rounds based on gamemode"""
    rounds = TICKET_ROUNDS.get(mode_key.lower())
    if not rounds:
        return "FT4"

    default_ft, lt3_ft, loss_ft = rounds

    if loss_ft:
        return f"{default_ft}, LT3 alatt {lt3_ft}, ha nem nyersz a teszter ellen kört akkor {loss_ft}"
    else:
        return f"{default_ft}, LT3 alatt {lt3_ft}"

MODE_LIST = [t[0] for t in TICKET_TYPES]

RANKS = [
    "Unranked",
    "LT5", "HT5",
    "LT4", "HT4",
    "LT3", "HT3",
    "LT2", "HT2",
    "LT1", "HT1",
]

POINTS = {
    "Unranked": 0,
    "LT5": 1, "HT5": 2,
    "LT4": 3, "HT4": 4,
    "LT3": 6, "HT3": 8,
    "LT2": 10, "HT2": 12,
    "LT1": 14, "HT1": 18,
}

# Mapping from database gamemode names to bot code keys
# This handles differences between database naming and bot TICKET_TYPES
GAMEMODE_ALIASES = {
    # Database name variations -> TICKET_TYPES key (lowercase)
    "ogv": "ogvanilla",
    "ogvanilla": "ogvanilla",
    "nethpot": "nethpot",
    "uhc": "uhc",
    "shieldlessuhc": "shieldlessuhc",
    "spearmace": "spearmace",
    "spearelytra": "spearelytra",
}

# Reverse mapping: bot keys (lowercase) -> proper display names
GAMEMODE_DISPLAY_NAMES = {
    "vanilla": "Vanilla",
    "uhc": "UHC",
    "pot": "Pot",
    "nethpot": "NethPot",  # Note: capital P for NethPot
    "smp": "SMP",
    "sword": "Sword",
    "axe": "Axe",
    "mace": "Mace",
    "cart": "Cart",
    "creeper": "Creeper",
    "diasmp": "DiaSMP",
    "ogvanilla": "OGVanilla",
    "shieldlessuhc": "ShieldlessUHC",
    "spearmace": "SpearMace",
    "spearelytra": "SpearElytra",
    "diapot": "DiaPot",
    "nethsmp": "NethSMP",
    "crystal": "Crystal",
}

def normalize_gamemode(mode: str) -> str:
    """Normalize gamemode name to bot's TICKET_TYPES key format"""
    if not mode:
        return mode
    normalized = mode.lower().strip()
    return GAMEMODE_ALIASES.get(normalized, normalized)

def get_gamemode_display_name(mode_key: str) -> str:
    """Get proper display name for a gamemode key"""
    if not mode_key:
        return mode_key
    # First try exact match (case-sensitive) for proper casing like "NethPot"
    if mode_key in GAMEMODE_DISPLAY_NAMES:
        return GAMEMODE_DISPLAY_NAMES[mode_key]
    # Then try lowercase lookup
    return GAMEMODE_DISPLAY_NAMES.get(mode_key.lower().strip(), mode_key)


GAMEMODE_COLORS = {
    "mace": 0x808080,        # Grey
    "sword": 0x3498db,      # Blue
    "vanilla": 0x9b59b6,    # Purple
    "uhc": 0xe67e22,       # Orange
    "pot": 0xe74c3c,       # Red
    "nethpot": 0xc0392b,    # Dark Red
    "smp": 0x2ecc71,       # Green
    "axe": 0x8b4513,       # Brown
    "cart": 0xf1c40f,      # Yellow
    "creeper": 0x27ae60,   # Dark Green
    "diasmp": 0x1abc9c,    # Teal
    "ogvanilla": 0x8e44ad, # Dark Purple
    "shieldlessuhc": 0xd35400,  # Dark Orange
    "spearmace": 0x16a085,    # Dark Teal
    "spearelytra": 0x2980b9,   # Dark Blue
    "diapot": 0x00bcd4,       # Cyan (diamond)
    "nethsmp": 0x8b0000,      # Dark Red (nether)
    "crystal": 0x9b59b6,      # Purple (end crystal)
}

GAMEMODE_INDICATORS = {
    "mace": "⚫",
    "sword": "🔵",
    "vanilla": "🟣",
    "uhc": "🟠",
    "pot": "🔴",
    "nethpot": "🔴",
    "smp": "🟢",
    "axe": "🟤",
    "cart": "🟡",
    "creeper": "🟢",
    "diasmp": "🔵",
    "ogvanilla": "🟣",
    "shieldlessuhc": "🟠",
    "spearmace": "🟢",
    "spearelytra": "🔵",
    "diapot": "🩵",
    "nethsmp": "🟥",
    "crystal": "🟣",
}


def get_gamemode_indicator(mode_key: str, is_open: bool = True) -> str:
    """Get the color indicator emoji for a gamemode"""
    if is_open:
        return GAMEMODE_INDICATORS.get(mode_key.lower().strip(), "🟢")
    else:
        return "🔴"


def get_gamemode_color(mode_key: str) -> discord.Color:
    """Get the color for a gamemode"""
    if not mode_key:
        return discord.Color.default()
    color_val = GAMEMODE_COLORS.get(mode_key.lower().strip())
    if color_val is not None:
        return discord.Color(value=color_val)
    return discord.Color.default()


# =========================
# STORAGE
# =========================
def _load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {"ticket_state": {}, "cooldowns": {}, "queue_panel_message": None, "queue_message_ids": []}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"ticket_state": {}, "cooldowns": {}, "queue_panel_message": None, "queue_message_ids": []}


def _save_data(data: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _persist_queue_message_ids():
    """Save QUEUE_MESSAGE_IDS to data.json"""
    try:
        data = _load_data()
        # Convert to list of [msg_id, gamemode] for JSON
        ids_list = [[k, v] for k, v in QUEUE_MESSAGE_IDS.items()]
        data["queue_message_ids"] = ids_list
        _save_data(data)
    except Exception as e:
        print(f"Error persisting queue message IDs: {e}")


def get_open_ticket_channel_id(user_id: int, mode_key: str) -> Optional[int]:
    data = _load_data()
    return data.get("ticket_state", {}).get(str(user_id), {}).get(mode_key)


def set_open_ticket_channel_id(user_id: int, mode_key: str, channel_id: Optional[int]) -> None:
    data = _load_data()
    ticket_state = data.setdefault("ticket_state", {})
    user_state = ticket_state.setdefault(str(user_id), {})
    if channel_id is None:
        user_state.pop(mode_key, None)
    else:
        user_state[mode_key] = channel_id
    _save_data(data)


def get_last_closed(user_id: int, mode_key: str) -> float:
    data = _load_data()
    return float(data.get("cooldowns", {}).get(str(user_id), {}).get(mode_key, 0))


def set_last_closed(user_id: int, mode_key: str, ts: float) -> None:
    data = _load_data()
    cds = data.setdefault("cooldowns", {})
    u = cds.setdefault(str(user_id), {})
    u[mode_key] = ts
    _save_data(data)


def cooldown_left(user_id: int, mode_key: str) -> int:
    last = get_last_closed(user_id, mode_key)
    if last <= 0:
        return 0
    left = int((last + COOLDOWN_SECONDS) - time.time())
    return max(0, left)


# =========================
# LINK SYSTEM (Discord -> Minecraft Account Linking) - Database Version
# =========================

# JSON fallback functions for linked accounts
def _load_link_data() -> Dict[str, Any]:
    if not os.path.exists("links.json"):
        return {}
    try:
        with open("links.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_link_data(data: Dict[str, Any]) -> None:
    with open("links.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def get_linked_minecraft_name_async(discord_id: int) -> Optional[str]:
    """Get the Minecraft name linked to a Discord user (async)"""
    # Try Supabase REST API first
    if USE_SUPABASE_API:
        try:
            results = await supabase_select("linked_accounts", {"discord_id": str(discord_id)})
            if results:
                print(f"FOUND: Linked minecraft {results[0]['minecraft_name']} for discord {discord_id} (Supabase API)")
                return results[0]['minecraft_name']
            else:
                print(f"NOT FOUND in Supabase: No link for discord {discord_id}")
        except Exception as e:
            print(f"Error getting from Supabase: {e}")

    # Try PostgreSQL pool
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT minecraft_name FROM linked_accounts WHERE discord_id = $1",
                    discord_id
                )
                if row:
                    print(f"FOUND: Linked minecraft {row['minecraft_name']} for discord {discord_id} (DB)")
                else:
                    print(f"NOT FOUND in DB: No link for discord {discord_id}")
                return row['minecraft_name'] if row else None
        except Exception as e:
            print(f"Error getting from database: {e}")

    # Fallback to JSON
    print(f"FALLBACK: Checking JSON for discord {discord_id}")
    data = _load_link_data()
    result = data.get(str(discord_id))
    if result:
        print(f"FOUND: Linked minecraft {result} for discord {discord_id} (JSON)")
    else:
        print(f"NOT FOUND: No link for discord {discord_id} (JSON)")
    return result


async def link_minecraft_account_async(discord_id: int, minecraft_name: str) -> bool:
    """Link a Discord user to a Minecraft name (async)"""
    # Try Supabase REST API first
    if USE_SUPABASE_API:
        try:
            success = await supabase_upsert("linked_accounts", {
                "discord_id": str(discord_id),
                "minecraft_name": minecraft_name
            })
            if success:
                print(f"SUCCESS: Linked discord {discord_id} to minecraft {minecraft_name} (Supabase API)")
                return True
        except Exception as e:
            print(f"Error linking to Supabase: {e}")

    # Try PostgreSQL pool
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO linked_accounts (discord_id, minecraft_name, linked_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (discord_id) DO UPDATE SET
                        minecraft_name = EXCLUDED.minecraft_name,
                        linked_at = NOW()
                    """,
                    discord_id, minecraft_name
                )
            print(f"SUCCESS: Linked discord {discord_id} to minecraft {minecraft_name} (DB)")
            return True
        except Exception as e:
            print(f"Error linking to database: {e}")

    # Fallback to JSON
    print(f"FALLBACK: Saving to JSON for discord {discord_id}")
    data = _load_link_data()
    data[str(discord_id)] = minecraft_name
    _save_link_data(data)
    print(f"SUCCESS: Linked discord {discord_id} to minecraft {minecraft_name} (JSON)")
    return True


async def unlink_minecraft_account_async(discord_id: int) -> bool:
    """Unlink a Discord user from their Minecraft name. Returns True if unlinked."""
    # Try Supabase REST API first
    if USE_SUPABASE_API:
        try:
            success = await supabase_delete("linked_accounts", {"discord_id": str(discord_id)})
            if success:
                print(f"SUCCESS: Unlinked discord {discord_id} (Supabase API)")
                return True
        except Exception as e:
            print(f"Error unlinking from Supabase: {e}")

    if not db_pool:
        return False
    try:
        async with db_pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM linked_accounts WHERE discord_id = $1",
                discord_id
            )
        return result == "DELETE 1"
    except Exception as e:
        print(f"Error unlinking minecraft account: {e}")
        return False


async def get_discord_by_minecraft_async(minecraft_name: str) -> Optional[int]:
    """Get Discord ID by linked Minecraft name (async)"""
    # Try Supabase REST API first
    if USE_SUPABASE_API:
        try:
            results = await supabase_select("linked_accounts", {"minecraft_name": minecraft_name})
            if results:
                return int(results[0]['discord_id'])
        except Exception as e:
            print(f"Error getting discord by minecraft from Supabase: {e}")

    if not db_pool:
        return None
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT discord_id FROM linked_accounts WHERE LOWER(minecraft_name) = LOWER($1)",
                minecraft_name
            )
            return row['discord_id'] if row else None
    except Exception as e:
        print(f"Error getting discord by minecraft: {e}")
        return None


# Synchronous versions that fall back to JSON if DB not available
def get_linked_minecraft_name(discord_id: int) -> Optional[str]:
    """Get the Minecraft name linked to a Discord user (sync wrapper)"""
    # Try Supabase REST API
    if USE_SUPABASE_API:
        try:
            results = supabase_select_sync("linked_accounts", {"discord_id": str(discord_id)})
            if results:
                print(f"FOUND: Linked minecraft {results[0]['minecraft_name']} for discord {discord_id} (Supabase API)")
                return results[0]['minecraft_name']
        except Exception as e:
            print(f"Error getting from Supabase: {e}")

    if db_pool:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # If we're in an async context, we need to schedule this
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(asyncio.run, get_linked_minecraft_name_async(discord_id))
                    return future.result()
            else:
                return asyncio.run(get_linked_minecraft_name_async(discord_id))
        except:
            pass
    # Fallback to JSON
    data = _load_link_data()
    return data.get(str(discord_id))


def link_minecraft_account(discord_id: int, minecraft_name: str) -> None:
    """Link a Discord user to a Minecraft name (sync wrapper)"""
    # Try Supabase REST API
    if USE_SUPABASE_API:
        try:
            success = supabase_insert_sync("linked_accounts", {
                "discord_id": str(discord_id),
                "minecraft_name": minecraft_name
            })
            if success:
                print(f"SUCCESS: Linked discord {discord_id} to minecraft {minecraft_name} (Supabase API)")
                return
        except Exception as e:
            print(f"Error linking to Supabase: {e}")

    if db_pool:
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, link_minecraft_account_async(discord_id, minecraft_name))
                if future.result():
                    return
        except:
            pass
    # Fallback to JSON
    data = _load_link_data()
    data[str(discord_id)] = minecraft_name
    _save_link_data(data)


def unlink_minecraft_account(discord_id: int) -> bool:
    """Unlink a Discord user from their Minecraft name. Returns True if unlinked."""
    # Try Supabase REST API
    if USE_SUPABASE_API:
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, supabase_delete("linked_accounts", {"discord_id": str(discord_id)}))
                if future.result():
                    print(f"SUCCESS: Unlinked discord {discord_id} (Supabase API)")
                    return True
        except Exception as e:
            print(f"Error unlinking from Supabase: {e}")

    if db_pool:
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, unlink_minecraft_account_async(discord_id))
                if future.result():
                    return True
        except:
            pass
    # Fallback to JSON
    data = _load_link_data()
    if str(discord_id) in data:
        del data[str(discord_id)]
        _save_link_data(data)
        return True
    return False


def get_discord_by_minecraft(minecraft_name: str) -> Optional[int]:
    """Get Discord ID by linked Minecraft name (sync wrapper)"""
    # Try Supabase REST API
    if USE_SUPABASE_API:
        try:
            results = supabase_select_sync("linked_accounts", {"minecraft_name": minecraft_name})
            if results:
                return int(results[0]['discord_id'])
        except Exception as e:
            print(f"Error getting discord by minecraft from Supabase: {e}")

    if db_pool:
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, get_discord_by_minecraft_async(minecraft_name))
                return future.result()
        except:
            pass
    # Fallback to JSON
    data = _load_link_data()
    for discord_id, mc_name in data.items():
        if mc_name.lower() == minecraft_name.lower():
            return int(discord_id)
    return None


# =========================
# PENDING LINK CODES (Discord -> Minecraft linking with code)
# =========================

LINK_CODE_LENGTH = 8  # 6-8 characters
LINK_CODE_EXPIRY_MINUTES = 10


# =========================
# PENDING CODES - Database versions
# =========================

async def generate_link_code_async(discord_id: int) -> str:
    """Generate a new link code for a Discord user (async)"""
    # Generate random alphanumeric code
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=LINK_CODE_LENGTH))

    # Try Supabase REST API first
    if USE_SUPABASE_API:
        try:
            expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=LINK_CODE_EXPIRY_MINUTES)
            # Delete any existing pending codes for this user
            await supabase_delete("pending_codes", {"discord_id": str(discord_id)})
            # Insert new code
            success = await supabase_insert("pending_codes", {
                "discord_id": str(discord_id),
                "code": code.upper(),
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "expires_at": expires_at.isoformat(),
                "used": False
            })
            if success:
                print(f"Generated link code {code} for discord {discord_id} (Supabase API)")
                return code
        except Exception as e:
            print(f"Error generating link code in Supabase: {e}")

    if db_pool:
        try:
            expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=LINK_CODE_EXPIRY_MINUTES)
            async with db_pool.acquire() as conn:
                # Delete any existing pending codes for this user
                await conn.execute(
                    "DELETE FROM pending_codes WHERE discord_id = $1",
                    discord_id
                )
                # Insert new code
                await conn.execute(
                    "INSERT INTO pending_codes (discord_id, code, created_at, expires_at, used) VALUES ($1, $2, NOW(), $3, FALSE)",
                    discord_id, code, expires_at
                )
            return code
        except Exception as e:
            print(f"Error generating link code: {e}")

    # Fallback to JSON
    data = _load_pending_link_codes()
    # Remove any existing codes for this user
    data = {k: v for k, v in data.items() if v.get("discord_id") != discord_id}
    data[code] = {
        "discord_id": discord_id,
        "expires_at": time.time() + (LINK_CODE_EXPIRY_MINUTES * 60)
    }
    _save_pending_link_codes(data)
    return code


async def verify_link_code_async(code: str) -> Optional[int]:
    """Verify a link code and return Discord ID if valid, None if invalid/expired (async)"""
    # Try Supabase REST API first
    if USE_SUPABASE_API:
        try:
            # First, get the code and check if valid
            results = await supabase_select("pending_codes", {"code": code.upper(), "used": "false"})
            if results:
                # Check if not expired
                expires_at = datetime.datetime.fromisoformat(results[0]['expires_at'].replace('Z', '+00:00'))
                if expires_at > datetime.datetime.now(datetime.timezone.utc):
                    discord_id = int(results[0]['discord_id'])
                    # Mark code as used
                    await supabase_update("pending_codes", {"used": True}, {"code": code.upper()})
                    print(f"Verified link code {code} for discord {discord_id} (Supabase API)")
                    return discord_id
                else:
                    print(f"Link code {code} expired")
            return None
        except Exception as e:
            print(f"Error verifying link code in Supabase: {e}")

    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT discord_id FROM pending_codes WHERE UPPER(code) = UPPER($1) AND used = FALSE AND expires_at > NOW()",
                    code
                )
                if row:
                    # Mark code as used
                    await conn.execute(
                        "UPDATE pending_codes SET used = TRUE WHERE UPPER(code) = UPPER($1)",
                        code
                    )
                    return row['discord_id']
                return None
        except Exception as e:
            print(f"Error verifying link code: {e}")

    # Fallback to JSON
    return verify_link_code(code)


async def get_pending_link_code_async(discord_id: int) -> Optional[str]:
    """Get existing pending code for a Discord user if any (async)"""
    # Try Supabase REST API first
    if USE_SUPABASE_API:
        try:
            # Get codes for this discord_id that are not used and not expired
            results = await supabase_select("pending_codes", {"discord_id": str(discord_id)})
            if results:
                for row in results:
                    if not row.get('used', False):
                        expires_at = datetime.datetime.fromisoformat(row['expires_at'].replace('Z', '+00:00'))
                        if expires_at > datetime.datetime.now(datetime.timezone.utc):
                            return row['code']
            return None
        except Exception as e:
            print(f"Error getting pending link code from Supabase: {e}")


async def validate_link_code_for_user(discord_id: int, code: str) -> bool:
    """Check if a code belongs to the specified user (async)"""
    if USE_SUPABASE_API:
        try:
            results = await supabase_select("pending_codes", {"discord_id": str(discord_id), "code": code})
            if results:
                for row in results:
                    if not row.get('used', False):
                        expires_at = datetime.datetime.fromisoformat(row['expires_at'].replace('Z', '+00:00'))
                        if expires_at > datetime.datetime.now(datetime.timezone.utc):
                            return True
            return False
        except Exception as e:
            print(f"Error validating link code from Supabase: {e}")
            return False
    return False

    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT code FROM pending_codes WHERE discord_id = $1 AND used = FALSE AND expires_at > NOW()",
                    discord_id
                )
                return row['code'] if row else None
        except Exception as e:
            print(f"Error getting pending link code: {e}")

    # Fallback to JSON
    return get_pending_link_code(discord_id)


# Synchronous fallbacks
def _load_pending_link_codes() -> Dict[str, Any]:
    if not os.path.exists("pending_links.json"):
        return {}
    try:
        with open("pending_links.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_pending_link_codes(data: Dict[str, Any]) -> None:
    with open("pending_links.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def generate_link_code(discord_id: int) -> str:
    """Generate a new link code for a Discord user"""
    # Generate random alphanumeric code
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=LINK_CODE_LENGTH))

    # Store with expiry time
    data = _load_pending_link_codes()
    data[code] = {
        "discord_id": discord_id,
        "expires_at": time.time() + (LINK_CODE_EXPIRY_MINUTES * 60)
    }
    _save_pending_link_codes(data)

    return code


def verify_link_code(code: str) -> Optional[int]:
    """Verify a link code and return Discord ID if valid, None if invalid/expired"""
    data = _load_pending_link_codes()

    code_info = data.get(code.upper())
    if not code_info:
        return None

    # Check if expired
    if time.time() > code_info.get("expires_at", 0):
        # Remove expired code
        data.pop(code.upper(), None)
        _save_pending_link_codes(data)
        return None

    discord_id = code_info.get("discord_id")
    # Remove used code
    data.pop(code.upper(), None)
    _save_pending_link_codes(data)

    return discord_id


def get_pending_link_code(discord_id: int) -> Optional[str]:
    """Get existing pending code for a Discord user if any"""
    data = _load_pending_link_codes()
    for code, info in data.items():
        if info.get("discord_id") == discord_id:
            # Check if not expired
            if time.time() < info.get("expires_at", 0):
                return code
    return None


# =========================
# MINECRAFT VERIFICATION API
# =========================
async def check_minecraft_verification(discord_id: int) -> Dict[str, Any]:
    """Check if a Discord user is verified on the Minecraft server"""
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{MINECRAFT_API_URL}/api/verify/minecraft/{discord_id}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    return {"verified": False, "error": f"API returned {response.status}"}
    except Exception as e:
        return {"verified": False, "error": str(e)}


async def is_minecraft_verified(discord_id: int) -> bool:
    """Quick check if a Discord user is verified on Minecraft"""
    try:
        result = await check_minecraft_verification(discord_id)
        return result.get("verified", False)
    except Exception:
        return False


# =========================
# BAN SYSTEM
# =========================
def _load_ban_data() -> Dict[str, Any]:
    if not os.path.exists("bans.json"):
        return {}
    try:
        with open("bans.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_ban_data(data: Dict[str, Any]) -> None:
    with open("bans.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_player_banned(username: str) -> bool:
    """Check if a player is banned and if the ban has expired."""
    data = _load_ban_data()
    ban_info = data.get(username.lower())
    if not ban_info:
        return False

    # Check if ban has expired
    expires_at = ban_info.get("expires_at", 0)
    if expires_at > 0 and time.time() > expires_at:
        # Ban expired, remove it
        data.pop(username.lower(), None)
        _save_ban_data(data)
        return False

    return True


def get_ban_info(username: str) -> Optional[Dict[str, Any]]:
    """Get ban info for a player. Returns None if not banned or ban expired."""
    data = _load_ban_data()
    ban_info = data.get(username.lower())
    if not ban_info:
        return None

    expires_at = ban_info.get("expires_at", 0)
    if expires_at > 0 and time.time() > expires_at:
        data.pop(username.lower(), None)
        _save_ban_data(data)
        return None

    return ban_info


def ban_player(username: str, days: int, reason: str = "") -> None:
    """Ban a player for a specified number of days. Use days=0 for permanent ban."""
    data = _load_ban_data()
    expires_at = 0 if days == 0 else time.time() + (days * 24 * 60 * 60)
    data[username.lower()] = {
        "username": username,
        "reason": reason,
        "banned_at": time.time(),
        "expires_at": expires_at,
        "permanent": days == 0
    }
    _save_ban_data(data)


def unban_player(username: str) -> bool:
    """Unban a player. Returns True if they were banned and are now unbanned."""
    data = _load_ban_data()
    if username.lower() in data:
        data.pop(username.lower(), None)
        _save_ban_data(data)
        return True
    return False


# =========================
# PERMISSIONS
# =========================
def is_staff_member(member: discord.Member) -> bool:
    # Check debug allowed users first (hardcoded for testing)
    if DEBUG_ALLOWED_USERS and member.id in DEBUG_ALLOWED_USERS:
        return True
    # Check debug allowed roles (hardcoded for testing)
    if DEBUG_ALLOWED_ROLES:
        for role_id in DEBUG_ALLOWED_ROLES:
            if any(r.id == role_id for r in member.roles):
                return True
    # Check specific user IDs first
    if ALLOWED_USER_IDS and member.id in ALLOWED_USER_IDS:
        return True
    if member.guild_permissions.administrator:
        return True
    if STAFF_ROLE_ID and any(r.id == STAFF_ROLE_ID for r in member.roles):
        return True
    # Check extra staff role IDs
    for role_id in EXTRA_STAFF_ROLE_IDS:
        if role_id and any(r.id == role_id for r in member.roles):
            return True
    return False


def get_gamemode_tester_role_id(gamemode: str) -> Optional[int]:
    """Get the tester role ID for a specific gamemode from TICKET_TYPES"""
    for label, key, role_id in TICKET_TYPES:
        if key == gamemode.lower():
            return role_id
    return None


def has_gamemode_tester_role(member: discord.Member, gamemode: str) -> bool:
    """Check if member has the specific tester role for this gamemode"""
    role_id = get_gamemode_tester_role_id(gamemode)
    if not role_id:
        return False
    return any(r.id == role_id for r in member.roles)


def is_gamemode_tester_or_admin(member: discord.Member, gamemode: str) -> bool:
    """Check if member can act as a tester for this gamemode (admin or has specific role)"""
    # Admins always allowed
    if member.guild_permissions.administrator:
        return True
    # Debug overrides
    if DEBUG_ALLOWED_USERS and member.id in DEBUG_ALLOWED_USERS:
        return True
    if DEBUG_ALLOWED_ROLES:
        for role_id in DEBUG_ALLOWED_ROLES:
            if any(r.id == role_id for r in member.roles):
                return True
    # Specific gamemode tester role
    return has_gamemode_tester_role(member, gamemode)


async def get_player_rank_for_mode(username: str, mode_key: str) -> str:
    """
    Get a player's current rank for a specific gamemode from the website.
    Returns "Unranked" if not found or on error.
    """
    if not WEBSITE_URL:
        return "Unranked"
    try:
        res = await api_get_tests(username=username, mode=mode_key)
        if res.get("status") == 200:
            data = res.get("data", {})
            test = data.get("test")
            tests = data.get("tests", [])
            target = test if test else (tests[0] if tests else None)
            if target:
                rank = str(target.get("rank", "Unranked"))
                if rank and rank != "Unranked":
                    return rank
    except Exception:
        pass
    return "Unranked"


def get_rank_value_min(rank: str) -> int:
    """
    Get numeric points value for a rank (lower = weaker).
    This determines eligibility for certain actions.
    """
    return POINTS.get(rank, 0)


def can_open_ticket(rank: str) -> bool:
    """
    Can open ticket if rank is LT3 or above (points >= 6).
    Ranks: LT5(1) < HT5(2) < LT4(3) < HT4(4) < LT3(6) < HT3(8) < LT2(10) < HT2(12) < LT1(14) < HT1(18)
    """
    return get_rank_value_min(rank) >= 6  # LT3 = 6 points


def can_join_queue(rank: str) -> bool:
    """
    Can join queue if rank is between LT5 and HT4 (inclusive), or Unranked.
    That's points 0-4 inclusive (Unranked=0, LT5=1, HT5=2, LT4=3, HT4=4).
    """
    pts = get_rank_value_min(rank)
    return pts <= 4  # Unranked(0), LT5(1), HT5(2), LT4(3), HT4=4 all allowed


# =========================
# DISCORD BOT
# =========================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
http_session: Optional[aiohttp.ClientSession] = None


# =========================
# HEALTH SERVER (Railway)
# =========================
async def start_health_server():
    global http_session
    
    # Ensure http_session is available
    if http_session is None:
        http_session = aiohttp.ClientSession()
    
    app = web.Application()

    async def health(_request):
        return web.Response(text="ok")

    print("Health server endpoints registered")

    # API endpoint for Minecraft link code verification
    async def verify_link(request):
        print(f"verify_link called: code={request.query.get('code')}, minecraft={request.query.get('minecraft')}")
        try:
            # Get code from query params
            code = request.query.get("code", "")
            minecraft_name = request.query.get("minecraft", "")

            if not code or not minecraft_name:
                return web.json_response({"success": False, "error": "Missing code or minecraft parameter"}, status=400)

            print(f"Verifying code: {code}")
            # Verify the code
            discord_id = await verify_link_code_async(code.upper())
            print(f"Verified: discord_id={discord_id}")

            if discord_id is None:
                return web.json_response({"success": False, "error": "Invalid or expired code"}, status=400)

            # Ensure http_session is available for linking
            global http_session
            if http_session is None:
                print("Creating http_session")
                http_session = aiohttp.ClientSession()

            # Link the Minecraft account to the Discord account
            print(f"Linking account: {discord_id} -> {minecraft_name}")
            await link_minecraft_account_async(discord_id, minecraft_name)

            # Send confirmation DM to the user
            try:
                user = await bot.fetch_user(discord_id)
                if user:
                    embed = discord.Embed(
                        title="Összekapcsolás sikeres!",
                        description=f"A Discord fiókod össze lett kapcsolva a **Minecraft** fiókkal!\n\n**Minecraft név:** `{minecraft_name}`",
                        color=discord.Color.green()
                    )
                    await user.send(embed=embed)
            except Exception as e:
                print(f"Could not send DM: {e}")

            return web.json_response({"success": True, "discord_id": discord_id, "minecraft": minecraft_name})
        except Exception as e:
            import traceback
            print(f"verify_link error: {e}")
            traceback.print_exc()
            return web.json_response({"success": False, "error": str(e)}, status=500)

        # Send confirmation DM to the user
        try:
            user = await bot.fetch_user(discord_id)
            if user:
                embed = discord.Embed(
                    title="✅ Összekapcsolás sikeres!",
                    description=f"A Discord fiókod össze lett kapcsolva a **Minecraft** fiókkal!\n\n"
                               f"**Minecraft név:** `{minecraft_name}`\n"
                               f"**Összekapcsolva:** Örökre!",
                    color=discord.Color.green()
                )
                embed.set_footer(text="Most már használhatod a tierlistát!")
                await user.send(embed=embed)
        except Exception as e:
            print(f"Could not send DM to user: {e}")

        return web.json_response({
            "success": True, 
            "discord_id": discord_id,
            "minecraft": minecraft_name
        })

    app.router.add_get("/health", health)
    app.router.add_get("/api/link/verify", verify_link)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Health server running on 0.0.0.0:{port}")


# =========================
# WEBSITE API
# =========================
def _auth_headers() -> Dict[str, str]:
    if not BOT_API_KEY:
        return {}
    return {"Authorization": f"Bearer {BOT_API_KEY}"}


async def api_get_tests(username: str, mode: str) -> Dict[str, Any]:
    if not WEBSITE_URL:
        return {"status": 0, "data": {"tests": []}}

    url = f"{WEBSITE_URL}/api/tests?username={username}&gamemode={mode}"
    print(f"[API_GET_TESTS] Requesting: {url}")

    try:
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        async with http_session.get(url, headers=_auth_headers(), timeout=timeout) as resp:
            print(f"[API_GET_TESTS] Response status: {resp.status}")
            try:
                data = await resp.json()
            except Exception:
                data = {"error": await resp.text()}
            return {"status": resp.status, "data": data}
    except asyncio.TimeoutError:
        print(f"[API_GET_TESTS] Timeout fetching tests for {username}")
        return {"status": 0, "data": {"error": "timeout"}}
    except Exception as e:
        print(f"[API_GET_TESTS] Error: {e}")
        return {"status": 0, "data": {"error": str(e)}}


async def api_post_test(username: str, mode: str, rank: str, tester: discord.Member) -> Dict[str, Any]:
    mode_for_api = get_gamemode_display_name(mode)

    # Primary: Direct PostgreSQL upsert (atomic ON CONFLICT) – most reliable
    if db_pool is not None:
        print(f"[API_POST_TEST] DB upsert: {username}/{mode_for_api}")
        success = await db_upsert_test(
            username=username,
            mode=mode_for_api,
            rank=rank,
            tester_id=str(tester.id),
            tester_name=tester.display_name,
            ts=int(time.time())
        )
        if success:
            return {"status": 200, "data": {"success": True}}
        print("DB upsert failed, falling back")

    # Secondary: Supabase REST upsert
    if USE_SUPABASE_API:
        print(f"[API_POST_TEST] Supabase upsert: {username}/{mode_for_api}")
        payload_sb = {
            "username": username,
            "mode": mode_for_api,
            "rank": rank,
            "testerId": str(tester.id),
            "testerName": tester.display_name,
            "ts": int(time.time()),
        }
        if await supabase_upsert("tests", payload_sb):
            return {"status": 200, "data": {"success": True}}
        print("Supabase upsert failed, falling back")

    # Fallback: Website API – check existence first, then either PUT or POST
    if not WEBSITE_URL:
        return {"status": 0, "data": {"error": "WEBSITE_URL not set"}}

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)

    # Check if test already exists
    try:
        check_url = f"{WEBSITE_URL}/api/tests?username={username}&mode={mode_for_api}"
        async with http_session.get(check_url, headers=_auth_headers(), timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                test = data.get("test") or (data.get("tests") or [None])[0]
                if test and test.get("id"):
                    test_id = test["id"]
                    print(f"Test exists (id={test_id}), updating via PUT")
                    update_url = f"{WEBSITE_URL}/api/tests/{test_id}"
                    put_payload = {
                        "username": username,
                        "mode": mode_for_api,
                        "rank": rank,
                        "testerId": str(tester.id),
                        "testerName": tester.display_name,
                        "ts": int(time.time()),
                    }
                    async with http_session.put(update_url, json=put_payload, headers=_auth_headers(), timeout=timeout) as put_resp:
                        try:
                            put_data = await put_resp.json()
                        except Exception:
                            put_data = {}
                        print(f"PUT response: {put_resp.status} – {put_data}")
                        return {"status": put_resp.status, "data": put_data}
                else:
                    print("No existing test, creating via POST")
    except Exception as e:
        print(f"Error checking existing test: {e}")

    # POST new test (no upsert flag)
    url = f"{WEBSITE_URL}/api/tests"
    payload = {
        "username": username,
        "mode": mode_for_api,
        "rank": rank,
        "testerId": str(tester.id),
        "testerName": tester.display_name,
        "ts": int(time.time()),
    }
    print(f"[API_POST_TEST] POST new test: {username}/{mode_for_api}")
    try:
        async with http_session.post(url, json=payload, headers=_auth_headers(), timeout=timeout) as resp:
            try:
                data = await resp.json()
            except Exception:
                data = {"error": await resp.text()}
            print(f"[API_POST_TEST] POST response: {resp.status} – {data}")
            return {"status": resp.status, "data": data}
    except Exception as e:
        print(f"[API_POST_TEST] POST exception: {e}")
        return {"status": 0, "data": {"error": str(e)}}


async def api_rename_player(old_name: str, new_name: str) -> Dict[str, Any]:
    """Rename a player on the tierlist (admin only)"""
    if not WEBSITE_URL:
        return {"status": 0, "data": {"error": "WEBSITE_URL not set"}}

    # Use /api/tests/rename endpoint with POST method
    url = f"{WEBSITE_URL}/api/tests/rename"
    payload = {
        "oldName": old_name,
        "newName": new_name,
    }

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    async with http_session.post(url, json=payload, headers=_auth_headers(), timeout=timeout) as resp:
        try:
            data = await resp.json()
        except Exception:
            data = {"error": await resp.text()}
        return {"status": resp.status, "data": data}


async def api_set_ban(username: str, banned: bool, expires_at: Optional[int] = None, reason: str = "") -> Dict[str, Any]:
    """Set ban status on the website"""
    if not WEBSITE_URL:
        return {"status": 0, "data": {"error": "WEBSITE_URL not set"}}

    url = f"{WEBSITE_URL}/api/tests/ban"
    payload = {
        "username": username,
        "banned": banned,
    }

    if expires_at is not None:
        payload["expiresAt"] = expires_at
    if reason:
        payload["reason"] = reason

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    async with http_session.post(url, json=payload, headers=_auth_headers(), timeout=timeout) as resp:
        try:
            data = await resp.json()
        except Exception:
            data = {"error": await resp.text()}
        return {"status": resp.status, "data": data}


async def api_remove_player(username: str, gamemode: Optional[str] = None) -> Dict[str, Any]:
    """Remove a player from the tierlist (admin only)"""
    if not WEBSITE_URL:
        return {"status": 0, "data": {"error": "WEBSITE_URL not set"}}

    url = f"{WEBSITE_URL}/api/tests/remove"
    payload = {
        "username": username,
    }

    if gamemode:
        payload["gamemode"] = gamemode

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    async with http_session.post(url, json=payload, headers=_auth_headers(), timeout=timeout) as resp:
        try:
            data = await resp.json()
        except Exception:
            data = {"error": await resp.text()}
        return {"status": resp.status, "data": data}


# =========================
# UI VIEWS
# =========================
class CloseTicketView(discord.ui.View):
    def __init__(self, owner_id: int, mode_key: str):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.mode_key = mode_key

    @discord.ui.button(label="Ticket zárása", style=discord.ButtonStyle.danger, custom_id="neotiers_close_ticket")
    async def close(self, interaction: discord.Interaction, _button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Hiba: ez nem szövegcsatorna.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Hiba: member not found.", ephemeral=True)
            return

        topic = channel.topic or ""
        owner_id = 0
        if "owner=" in topic:
            try:
                owner_id = int(topic.split("owner=")[1].split("|")[0].strip())
            except (ValueError, IndexError):
                owner_id = 0

        if member.id != owner_id and not is_staff_member(member):
            await interaction.response.send_message("Nincs jogosultságod a ticket zárásához.", ephemeral=True)
            return

        await interaction.response.send_message("✅ Ticket zárása... 3 mp múlva törlöm a csatornát.", ephemeral=True)

        # Get owner_id and mode_key from channel topic
        topic = channel.topic or ""
        owner_id = 0
        mode_key = ""
        if "owner=" in topic:
            try:
                owner_id = int(topic.split("owner=")[1].split("|")[0].strip())
            except (ValueError, IndexError):
                owner_id = 0
        if "mode=" in topic:
            try:
                mode_key = topic.split("mode=")[1].split("|")[0].strip()
            except (ValueError, IndexError):
                mode_key = ""

        set_last_closed(owner_id, mode_key, time.time())
        set_open_ticket_channel_id(owner_id, mode_key, None)

        await asyncio.sleep(3)
        try:
            await channel.delete(reason="NeoTiers ticket closed")
        except discord.Forbidden:
            try:
                await channel.send("❌ Nem tudom törölni a csatornát (Missing Permissions). Add a botnak **Csatornák kezelése** jogot + a kategórián is.")
            except Exception:
                pass
        except Exception:
            pass

    @discord.ui.button(label="Tier adása", style=discord.ButtonStyle.success, custom_id="neotiers_give_tier")
    async def give_tier(self, interaction: discord.Interaction, _button: discord.ui.Button):
        """Give tier to the ticket owner - only for staff"""
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Hiba: member not found.", ephemeral=True)
            return

        if not is_staff_member(member):
            await interaction.response.send_message("Nincs jogosultságod tier adásához.", ephemeral=True)
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Hiba: ez nem szövegcsatorna.", ephemeral=True)
            return

        topic = channel.topic or ""
        owner_id = 0
        mode_key = ""
        if "owner=" in topic:
            try:
                owner_id = int(topic.split("owner=")[1].split("|")[0].strip())
            except (ValueError, IndexError):
                owner_id = 0
        if "mode=" in topic:
            try:
                mode_key = topic.split("mode=")[1].split("|")[0].strip()
            except (ValueError, IndexError):
                mode_key = ""

        if owner_id == 0:
            await interaction.response.send_message("Hiba: nem találom a ticket tulajdonosát.", ephemeral=True)
            return

        linked_minecraft = get_linked_minecraft_name(owner_id)
        if not linked_minecraft:
            await interaction.response.send_message("❌ A játékos nincs összekapcsolva! Nem tudom a Minecraft nevét.", ephemeral=True)
            return

        tier_select = TierSelectView(owner_id, linked_minecraft, mode_key, member)
        await interaction.response.send_message("Válaszd ki a játékmódot és a tier-t:", view=tier_select, ephemeral=True)


class TierSelectView(discord.ui.View):
    def __init__(self, owner_id: int, linked_minecraft: str, mode_key: str, tester: discord.Member):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.linked_minecraft = linked_minecraft
        self.mode_key = mode_key
        self.tester = tester
        # Find the mode label from TICKET_TYPES
        mode_label = mode_key
        for label, key, _rid in TICKET_TYPES:
            if key == mode_key:
                mode_label = label
                break
        self.mode_label = mode_label
        self.add_item(GameModeSelect(mode_label, mode_key))
        self.add_item(TierSelect())


class GameModeSelect(discord.ui.Select):
    def __init__(self, mode_label: str, mode_key: str):
        options = [discord.SelectOption(label=label, value=key) for label, key, _rid in TICKET_TYPES]
        super().__init__(placeholder="Játékmód...", options=options, custom_id="gamemode_select")
        self.mode_label = mode_label
        self._default_value = mode_key

    async def callback(self, interaction: discord.Interaction):
        # Update the tier select's placeholder
        await interaction.response.defer()


class TierSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=rank, value=rank)
            for rank in RANKS if rank != "Unranked"
        ]
        super().__init__(placeholder="Elért rang...", options=options, custom_id="tier_select")

    async def callback(self, interaction: discord.Interaction):
        selected_tier = self.values[0]
        view = self.view
        owner_id = view.owner_id
        linked_minecraft = view.linked_minecraft
        tester = view.tester
        mode_key = view.mode_key
        mode_label = view.mode_label

        # Get the owner member
        owner_member = interaction.guild.get_member(owner_id)
        if not owner_member:
            await interaction.response.send_message("Hiba: nem találom a Discord felhasználót.", ephemeral=True)
            return

        # Get previous rank from website
        prev_rank = "Unranked"
        prev_points = 0
        if WEBSITE_URL:
            try:
                # Normalize mode to match bot's TICKET_TYPES
                mode_param = normalize_gamemode(mode_key)
                print(f"Fetching previous rank for {linked_minecraft} in mode {mode_param}")
                res = await api_get_tests(username=linked_minecraft, mode=mode_param)
                print(f"API response: {res}")
                if res.get("status") == 200:
                    data = res.get("data", {})
                    test = data.get("test")
                    tests = data.get("tests", [])

                    # Find the best (highest points) test result for this mode
                    target = None
                    if test:
                        target = test
                    elif tests:
                        # If multiple tests, find the one with highest points for this mode
                        best_test = None
                        best_points = -1
                        for t in tests:
                            t_mode = str(t.get("gamemode", "")).lower()
                            t_rank = str(t.get("rank", "Unranked"))
                            t_points = POINTS.get(t_rank, 0)
                            if t_mode == mode_param and t_points > best_points:
                                best_points = t_points
                                best_test = t
                        target = best_test

                    if target:
                        prev_rank = str(target.get("rank", "Unranked")) or "Unranked"
                        prev_points = POINTS.get(prev_rank, 0)
                        print(f"Found previous rank: {prev_rank} = {prev_points} points")
            except Exception as e:
                print(f"Error fetching previous rank: {e}")

        # Calculate new points
        new_points = POINTS.get(selected_tier, 0)
        diff = new_points - prev_points
        points_str = f"+{diff}" if diff > 0 else str(diff)
        if diff == 0:
            points_str = "±0"

        # Create embed like /testresult
        skin_url = f"https://minotar.net/helm/{linked_minecraft}/128.png"

        # April Fools' effects
        display_mc = linked_minecraft
        display_mode = mode_label
        display_prev_rank = prev_rank
        display_selected_tier = selected_tier

        embed = discord.Embed(
            title=f"{display_mc} teszt eredménye 🏆",
            color=discord.Color.dark_grey()
        )
        embed.set_thumbnail(url=skin_url)
        embed.add_field(name="Tesztelő:", value=tester.mention, inline=False)
        embed.add_field(name="Játékmód:", value=display_mode, inline=False)
        embed.add_field(name="Minecraft név:", value=display_mc, inline=False)
        embed.add_field(name="Előző rang:", value=f"{display_prev_rank} ({prev_points} pont)", inline=False)
        embed.add_field(name="Elért rang:", value=f"{display_selected_tier} ({new_points} pont)", inline=False)
        embed.add_field(name="Pontok:", value=points_str, inline=False)

        # Send to the test results channel
        tier_channel_id_str = os.getenv("TIER_RESULTS_CHANNEL_ID", "0")
        print(f"DEBUG: TIER_RESULTS_CHANNEL_ID env var: {tier_channel_id_str}")

        tier_channel_id = 0
        try:
            tier_channel_id = int(tier_channel_id_str)
        except ValueError:
            print(f"DEBUG: Could not parse tier_channel_id: {tier_channel_id_str}")

        print(f"DEBUG: Parsed tier_channel_id: {tier_channel_id}")
        print(f"DEBUG: interaction.guild.id: {interaction.guild.id}")

        if not tier_channel_id:
            # Fallback: try to find channel by name
            tier_channel = discord.utils.get(interaction.guild.text_channels, name="teszteredmenyek")
            if not tier_channel:
                tier_channel = discord.utils.get(interaction.guild.text_channels, name="test-results")
                if not tier_channel:
                    tier_channel = discord.utils.get(interaction.guild.text_channels, name="eredmenyek")
        else:
            tier_channel = interaction.guild.get_channel(tier_channel_id)
            print(f"DEBUG: Got channel object: {tier_channel}")

        if tier_channel:
            print(f"DEBUG: Sending embed to channel: {tier_channel.name} ({tier_channel.id})")
            await tier_channel.send(embed=embed)
        else:
            # Log warning but continue with saving
            print(f"Warning: Could not find tier results channel. Searched for ID: {tier_channel_id}")

        # Save to website
        if WEBSITE_URL:
            try:
                # Normalize mode to proper display name before saving
                mode_to_save = get_gamemode_display_name(mode_key)
                save = await api_post_test(username=linked_minecraft, mode=mode_to_save, rank=selected_tier, tester=tester)
                save_ok = (save.get("status") == 200 or save.get("status") == 201)
                if save_ok:
                    await interaction.response.send_message(f"✅ Tier beállítva: **{selected_tier}** és mentve a weboldalra!", ephemeral=True)
                else:
                    await interaction.response.send_message(f"✅ Tier beállítva: **{selected_tier}** (weboldal mentés sikertelen)", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"✅ Tier beállítva: **{selected_tier}** (weboldal hiba: {e})", ephemeral=True)
        else:
            await interaction.response.send_message(f"✅ Tier beállítva: **{selected_tier}**", ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for label, mode_key, _rid in TICKET_TYPES:
            self.add_item(TicketButton(label=label, mode_key=mode_key))


class TicketButton(discord.ui.Button):
    def __init__(self, label: str, mode_key: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=f"neotiers_ticket_{mode_key}")
        self.mode_key = mode_key

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user

        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("Hiba: guild/member nem elérhető.", ephemeral=True)
            return

        # Check if user has a linked Minecraft account
        linked_minecraft = get_linked_minecraft_name(member.id)
        if not linked_minecraft:
            await interaction.response.send_message(
                "❌ **Nincs összekapcsolva a Minecraft fiókod!**\n\n"
                "Használd a `/link` parancsot a Discordban, majd `/link <kód>` a Minecraftban, "
                "hogy összekapcsold a fiókodat. Csak azok hozhatnak létre ticketet, akik összekapcsolták a fiókjukat!",
                ephemeral=True
            )
            return

        # Check if player is banned
        player_name = member.display_name
        if member.nick:
            player_name = member.nick

        if WEBSITE_URL:
            try:
                url = f"{WEBSITE_URL}/api/tests/ban?username={player_name}"
                timeout = aiohttp.ClientTimeout(total=5)
                async with http_session.get(url, headers=_auth_headers(), timeout=timeout) as resp:
                    if resp.status == 200:
                        ban_data = await resp.json()
                        if ban_data.get("banned"):
                            reason = ban_data.get("reason", "")
                            await interaction.response.send_message(
                                f"❌ Ki vagy tiltva a tesztelésből!\n" +
                                (f"**Ok:** {reason}" if reason else ""),
                                ephemeral=True
                            )
                            return
            except Exception:
                pass

        # Rank check
        player_rank = await get_player_rank_for_mode(linked_minecraft, self.mode_key)
        if not can_open_ticket(player_rank):
            await interaction.response.send_message(
                f"❌ A **{get_gamemode_display_name(self.mode_key)}** ticket megnyitásához legalább **LT3** rang szükséges. "
                f"Jelenlegi rangod: **{player_rank}**.",
                ephemeral=True
            )
            return

        # Cooldown check
        left = cooldown_left(member.id, self.mode_key)
        if left > 0:
            days = left // (24 * 3600)
            hours = (left % (24 * 3600)) // 3600
            await interaction.response.send_message(
                f"⏳ **Cooldown**: ebből a játékmódból ({self.mode_key}) csak **{days} nap {hours} óra** múlva nyithatsz új ticketet.",
                ephemeral=True
            )
            return

        # Acquire lock to prevent duplicate tickets
        lock_key = (member.id, self.mode_key)
        if lock_key not in TICKET_CREATION_LOCKS:
            TICKET_CREATION_LOCKS[lock_key] = asyncio.Lock()
        async with TICKET_CREATION_LOCKS[lock_key]:
            # Re-check for existing ticket inside lock
            existing_channel_id = get_open_ticket_channel_id(member.id, self.mode_key)
            if existing_channel_id:
                ch = guild.get_channel(existing_channel_id)
                if ch:
                    await interaction.response.send_message("Van már ticketed ebből a játékmódból. 🔒", ephemeral=True)
                    return
                else:
                    set_open_ticket_channel_id(member.id, self.mode_key, None)

            category = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
            if TICKET_CATEGORY_ID and not isinstance(category, discord.CategoryChannel):
                await interaction.response.send_message(
                    "❌ Ticket kategória rossz / nem kategória. Állítsd be jól a TICKET_CATEGORY_ID-t.",
                    ephemeral=True
                )
                return

            staff_role = guild.get_role(STAFF_ROLE_ID) if STAFF_ROLE_ID else None

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            }
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True, manage_channels=True
                )

            safe_name = member.name.lower().replace(" ", "-")
            channel_name = f"{self.mode_key}-{safe_name}"

            try:
                channel = await guild.create_text_channel(
                    name=channel_name,
                    category=category if isinstance(category, discord.CategoryChannel) else None,
                    overwrites=overwrites,
                    topic=f"NeoTiers ticket | owner={member.id} | mode={self.mode_key} | mc={linked_minecraft}",
                    reason="NeoTiers ticket created"
                )
            except discord.Forbidden:
                await interaction.response.send_message(
                    "❌ Nincs jogom csatornát létrehozni. Add a botnak **Csatornák kezelése** jogot (és a kategórián is).",
                    ephemeral=True
                )
                return

            set_open_ticket_channel_id(member.id, self.mode_key, channel.id)

            ping_text = ""

            rounds_display = get_ticket_rounds_display(self.mode_key)

            description = "Kattints egy alábbi gombra, hogy tudd tesztelni a gombon feltüntetett játékmódból."

            embed = discord.Embed(
                title="Teszt kérés",
                description=description,
                color=discord.Color.blurple()
            )

            display_mode = get_gamemode_display_name(self.mode_key)

            embed.add_field(name="Játékmód", value=display_mode, inline=True)
            embed.add_field(name="Minecraft név", value=f"`{linked_minecraft}`", inline=True)

            embed.add_field(name="Körök", value=rounds_display, inline=False)
            embed.add_field(name="Játékos", value=member.mention, inline=True)

            await channel.send(content=ping_text, embed=embed, view=CloseTicketView(owner_id=member.id, mode_key=self.mode_key))
            await interaction.response.send_message(f"✅ Ticket létrehozva: {channel.mention}", ephemeral=False)


# =========================
# QUEUE SYSTEM
# =========================

# Channel mappings for each gamemode queue
QUEUE_CHANNELS = {
    "sword": 1495038486120632410,
    "axe": 1495038602751774730,
    "mace": 1495038625719783586,
    "uhc": 1495038706103484487,
    "pot": 1495038741465792553,
    "nethpot": 1495038766769897482,
    "smp": 1495038799800176660,
    "vanilla": 1495038839591534834,
    "creeper": 1495038857597681818,
    "cart": 1495038915453779982,
    "diasmp": 1495038938640027760,
    "spearelytra": 1495038976988545206,
    "spearmace": 1495038999876600008,
    "shieldlessuhc": 1495039115119296572,
    "ogvanilla": 1495039145330872341,
    # --- Added gamemodes: replace 0s with real Discord queue channel IDs ---
    "diapot": 0,
    "nethsmp": 0,
    "crystal": 0,
}

# Ping role IDs for each gamemode
QUEUE_PING_ROLES = {
    "sword": 1495043729017278525,
    "axe": 1495043913583558758,
    "mace": 1495043981959237752,
    "uhc": 1495044042612805754,
    "pot": 1495044102730022942,
    "nethpot": 1495044163194847322,
    "smp": 1495044237551472893,
    "vanilla": 1495044315272052929,
    "creeper": 1495044383425171506,
    "cart": 1495044436403556443,
    "diasmp": 1495044514992095333,
    "shieldlessuhc": 1495044593211670711,
    "ogvanilla": 1495044664502386698,
    "spearelytra": 1495044732680667247,
    "spearmace": 1495044798472781944,
    # --- Added gamemodes: replace 0s with real Discord ping role IDs ---
    "diapot": 0,
    "nethsmp": 0,
    "crystal": 0,
}

# Category where ticket channels will be created
TICKET_CREATE_CATEGORY_ID = 1495038336744689674

# In-memory queue storage
ACTIVE_QUEUES: Dict[str, Dict[str, Any]] = {}
QUEUE_MESSAGE_IDS: Dict[int, str] = {}
QUEUE_PANEL_MESSAGE = None  # Tuple of (channel_id, message_id) for the queue panel message

# Lock to prevent duplicate ticket creation (key: (user_id, mode_key))
TICKET_CREATION_LOCKS: Dict[tuple, asyncio.Lock] = {}

class QueuePlayer:
    """Represents a player in a queue"""
    def __init__(self, discord_id: int, minecraft_name: str):
        self.discord_id = discord_id
        self.minecraft_name = minecraft_name
        self.joined_at = time.time()

class QueueActionView(discord.ui.View):
    """Join/Leave/Close/Next buttons for queue messages"""
    def __init__(self, gamemode: str = None):
        super().__init__(timeout=None)
        self.gamemode = gamemode

    def _get_gamemode(self, interaction: discord.Interaction) -> str:
        """Get gamemode from instance or from message/channel mapping"""
        if self.gamemode:
            return self.gamemode
        # Fallback 1: look up gamemode from message ID (for persistent view)
        msg_id = getattr(interaction.message, 'id', None) if interaction.message else None
        if msg_id and msg_id in QUEUE_MESSAGE_IDS:
            return QUEUE_MESSAGE_IDS[msg_id]
        # Fallback 2: derive from channel ID (each queue has its own channel)
        channel_id = getattr(interaction.channel, 'id', None) if interaction.channel else None
        if channel_id:
            for gm, cid in QUEUE_CHANNELS.items():
                if cid == channel_id:
                    return gm
        return None

    @discord.ui.button(label="Belépés a queue-ba", style=discord.ButtonStyle.success, custom_id="queue_join")
    async def join_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            await interaction.response.send_message("Hiba: nem tag.", ephemeral=True)
            return

        gamemode = self._get_gamemode(interaction)
        if not gamemode:
            await interaction.response.send_message("❌ Hiba: nem sikerült meghatározni a játékmódot.", ephemeral=True)
            return

        queue = ACTIVE_QUEUES.get(gamemode)
        if not queue:
            await interaction.response.send_message("❌ A queue nem létezik vagy nem nyitva.", ephemeral=True)
            return

        if any(p.discord_id == member.id for p in queue["players"]):
            await interaction.response.send_message("Már benne vagy a queue-ban!", ephemeral=True)
            return
        if any(t.discord_id == member.id for t in queue.get("testers", [])):
            await interaction.response.send_message("Már benna van a queue-ban teszterként!", ephemeral=True)
            return

        linked_mc = get_linked_minecraft_name(member.id)
        if not linked_mc:
            await interaction.response.send_message(
                "❌ Nincs összekapcsolva a Minecraft fiókod! Használd a `/link` parancsot.",
                ephemeral=True
            )
            return

        # Testers bypass
        if is_gamemode_tester_or_admin(member, gamemode):
            queue["testers"].append(QueuePlayer(member.id, linked_mc))
            await update_queue_message(gamemode)
            await interaction.response.send_message(
                f"✅ Beléptél teszterként a **{get_gamemode_display_name(gamemode)}** queue-ba!",
                ephemeral=True
            )
            return

        cd_left = cooldown_left(member.id, gamemode)
        if cd_left > 0:
            days = cd_left // (24 * 60 * 60)
            hours = (cd_left % (24 * 60 * 60)) // (60 * 60)
            await interaction.response.send_message(
                f"❌ Még **{days} nap {hours} óra** cooldown van hátra a **{get_gamemode_display_name(gamemode)}** módban. "
                f"Várj a cooldown lejártáig, mielőtt újra queue-hoz csatlakozol.",
                ephemeral=True
            )
            return

        player_rank = await get_player_rank_for_mode(linked_mc, gamemode)
        if not can_join_queue(player_rank):
            await interaction.response.send_message(
                f"❌ Csak **LT5-HT4** közöttiek csatlakozhatnak a queue-hoz. "
                f"Rangod: **{player_rank}** (minimum: LT5, maximum: HT4).",
                ephemeral=True
            )
            return

        queue["players"].append(QueuePlayer(member.id, linked_mc))
        await update_queue_message(gamemode)
        await interaction.response.send_message(
            f"✅ Beléptél a **{get_gamemode_display_name(gamemode)}** queue-ba!",
            ephemeral=True
        )

    @discord.ui.button(label="Kilépés a queue-ból", style=discord.ButtonStyle.danger, custom_id="queue_leave")
    async def leave_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            await interaction.response.send_message("Hiba: nem tag.", ephemeral=True)
            return

        gamemode = self._get_gamemode(interaction)
        if not gamemode:
            await interaction.response.send_message("❌ Hiba: nem sikerült meghatározni a játékmódot.", ephemeral=True)
            return

        queue = ACTIVE_QUEUES.get(gamemode)
        if not queue:
            await interaction.response.send_message("❌ A queue nem létezik.", ephemeral=True)
            return

        for i, p in enumerate(queue["players"]):
            if p.discord_id == member.id:
                queue["players"].pop(i)
                await update_queue_message(gamemode)
                await interaction.response.send_message(
                    f"✅ Kiléptél a **{get_gamemode_display_name(gamemode)}** queue-ból!",
                    ephemeral=True
                )
                return

        for i, t in enumerate(queue.get("testers", [])):
            if t.discord_id == member.id:
                queue["testers"].pop(i)
                await update_queue_message(gamemode)
                await interaction.response.send_message(
                    f"✅ Kiléptél a **{get_gamemode_display_name(gamemode)}** queue-ból!",
                    ephemeral=True
                )
                return

        await interaction.response.send_message("Nem vagy a queue-ban.", ephemeral=True)

    @discord.ui.button(label="❌ Queue bezárása", style=discord.ButtonStyle.secondary, custom_id="queue_close")
    async def close_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            await interaction.response.send_message("Hiba: nem tag.", ephemeral=True)
            return

        gamemode = self._get_gamemode(interaction)
        if not gamemode:
            await interaction.response.send_message("❌ Hiba: nem sikerült meghatározni a játékmódot.", ephemeral=True)
            return

        queue = ACTIVE_QUEUES.get(gamemode)
        if queue:
            # Normal case: queue exists in memory
            if not is_staff_member(member) and queue["opened_by"] != member.id:
                await interaction.response.send_message("❌ Csak a queue-t megnyitó tesztelő zárhatja be.", ephemeral=True)
                return
            view = ConfirmCloseQueueView(gamemode)
            await interaction.response.send_message(
                f"Biztosan be szeretnéd zárni a **{get_gamemode_display_name(gamemode)}** queue-t?",
                view=view,
                ephemeral=True
            )
            return

        # Orphaned queue: not in memory but message may still exist
        msg_id = None
        for mid, gm in QUEUE_MESSAGE_IDS.items():
            if gm == gamemode:
                msg_id = mid
                break
        if msg_id:
            channel_id = QUEUE_CHANNELS.get(gamemode)
            if channel_id:
                channel = bot.get_channel(channel_id)
                if channel and isinstance(channel, discord.TextChannel):
                    try:
                        msg = await channel.fetch_message(msg_id)
                        if msg.components:
                            if not is_staff_member(member):
                                await interaction.response.send_message("❌ Csak a queue-t megnyitó tesztelő vagy staff zárhatja be.", ephemeral=True)
                                return
                            view = ConfirmCloseQueueView(gamemode)
                            await interaction.response.send_message(
                                f"Biztosan be szeretnéd zárni a **{get_gamemode_display_name(gamemode)}** queue-t? (Queue állapot elveszett, de üzenet még nyitva)",
                                view=view,
                                ephemeral=True
                            )
                            return
                    except Exception:
                        pass
        await interaction.response.send_message("❌ A queue már lezárva vagy nem elérhető.", ephemeral=True)

    @discord.ui.button(label="Következő játékos", style=discord.ButtonStyle.primary, custom_id="queue_next")
    async def next_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            await interaction.response.send_message("Hiba: nem tag.", ephemeral=True)
            return

        gamemode = self._get_gamemode(interaction)
        if not gamemode:
            await interaction.response.send_message("❌ Hiba: nem sikerült meghatározni a játékmódot.", ephemeral=True)
            return

        queue = ACTIVE_QUEUES.get(gamemode)
        if not queue or not queue["players"]:
            await interaction.response.send_message("❌ Nincs több játékos a queue-ban.", ephemeral=True)
            return

        if not is_staff_member(member) and queue["opened_by"] != member.id:
            await interaction.response.send_message("❌ Csak a queue-t megnyitó tesztelő hívhatja a következő játékost.", ephemeral=True)
            return

        # Get next player (FIFO)
        next_player_obj = queue["players"].pop(0)
        queue["called_players"].append(next_player_obj.discord_id)
        await update_queue_message(gamemode)

        # Create ticket channel
        guild = interaction.guild
        category = guild.get_channel(TICKET_CREATE_CATEGORY_ID)
        if not category or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("❌ Hiba: ticket kategória nem található.", ephemeral=True)
            return

        channel_name = f"{gamemode}-{next_player_obj.minecraft_name}".lower().replace(" ", "-")[:50]
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.get_member(next_player_obj.discord_id): discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                ),
            }
            if STAFF_ROLE_ID:
                staff_role = guild.get_role(STAFF_ROLE_ID)
                if staff_role:
                    overwrites[staff_role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, read_message_history=True, manage_channels=True
                    )

            channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                topic=f"owner={next_player_obj.discord_id} | mode={gamemode} | mc={next_player_obj.minecraft_name}",
                reason=f"Queue ticket for {next_player_obj.minecraft_name}"
            )

            embed = discord.Embed(
                title="Teszt kérés",
                description=f"**Játékos:** {next_player_obj.minecraft_name}\n"
                           f"**Játékmód:** {get_gamemode_display_name(gamemode)}\n"
                           f"**Discord:** <@{next_player_obj.discord_id}>",
                color=discord.Color.blurple()
            )
            embed.set_thumbnail(url=f"https://minotar.net/helm/{next_player_obj.minecraft_name}/128.png")

            view = CloseTicketView(owner_id=next_player_obj.discord_id, mode_key=gamemode)
            await channel.send(embed=embed, view=view)

        except Exception as e:
            await interaction.response.send_message(f"❌ Hiba a channel létrehozása során: {e}", ephemeral=True)


class ConfirmCloseQueueView(discord.ui.View):
    def __init__(self, gamemode: str):
        super().__init__(timeout=30)
        self.gamemode = gamemode

    @discord.ui.button(label="Igen, zárja be", style=discord.ButtonStyle.danger, custom_id="queue_close_confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Hiba.", ephemeral=True)
            return

        queue = ACTIVE_QUEUES.get(self.gamemode)
        if queue:
            if queue["opened_by"] != member.id and not is_staff_member(member):
                await interaction.response.send_message("❌ Csak a queue-t megnyitó tesztelő zárhatja be.", ephemeral=True)
                return

            del ACTIVE_QUEUES[self.gamemode]
            await interaction.response.send_message(
                f"✅ **{get_gamemode_display_name(self.gamemode)}** queue bezárva.",
                ephemeral=True
            )
        else:
            await interaction.response.defer(ephemeral=True)

        if interaction.guild:
            await refresh_queue_panel(interaction.guild)

        try:
            msg_id = None
            for mid, gm in list(QUEUE_MESSAGE_IDS.items()):
                if gm == self.gamemode:
                    msg_id = mid
                    break
            if msg_id:
                channel_id = QUEUE_CHANNELS.get(self.gamemode)
                if channel_id:
                    channel = bot.get_channel(channel_id)
                    if channel and isinstance(channel, discord.TextChannel):
                        msg = await channel.fetch_message(msg_id)
                        if msg.components:
                            embed = discord.Embed(
                                title=f"{get_gamemode_indicator(self.gamemode, False)} {get_gamemode_display_name(self.gamemode)} Queue",
                                description="A queue zárva van.",
                                color=get_gamemode_color(self.gamemode)
                            )
                            await msg.edit(embed=embed, view=None)
                        if msg_id in QUEUE_MESSAGE_IDS:
                            del QUEUE_MESSAGE_IDS[msg_id]
                            _persist_queue_message_ids()
                        if not queue:
                            await interaction.followup.send(
                                f"✅ **{get_gamemode_display_name(self.gamemode)}** queue bezárva (állapot visszaállítva).",
                                ephemeral=True
                            )
        except Exception:
            pass

    @discord.ui.button(label="Mégsem", style=discord.ButtonStyle.secondary, custom_id="queue_close_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("❌ Mégse.", ephemeral=True)


class PingRoleSelect(discord.ui.Select):
    def __init__(self, selected_gamemodes: List[str] = None):
        self.selected_gamemodes = selected_gamemodes or []
        options = []
        for label, key, _rid in TICKET_TYPES:
            default = key in self.selected_gamemodes
            options.append(
                discord.SelectOption(
                    label=label,
                    value=key,
                    description=f"Ping értesítések ehhez a {label} queue-hoz",
                    default=default
                )
            )
        super().__init__(
            placeholder="Válaszd ki a queue-okat amikor pingelni szeretnél... (üres = mindet kikapcsolod)",
            min_values=0,
            max_values=len(TICKET_TYPES),
            options=options,
            custom_id="ping_queue_select"
        )

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Hiba: nem tag.", ephemeral=True)
            return

        guild = member.guild
        selected_gms = set(self.values)

        # All gamemodes with ping roles
        all_ping_gms = set(QUEUE_PING_ROLES.keys())

        # Compute differences: for each gamemode, decide to add or remove role
        added = []
        removed = []
        errors = []

        for gm in all_ping_gms:
            role_id = QUEUE_PING_ROLES[gm]
            role = guild.get_role(role_id)
            if not role:
                continue
            has_role = any(r.id == role_id for r in member.roles)
            should_have = gm in selected_gms
            if should_have and not has_role:
                try:
                    await member.add_roles(role, reason="Ping preference via /pingpanel")
                    added.append(role.name)
                except Exception as e:
                    errors.append(f"Nem sikerült hozzáadni {role.name}: {e}")
            elif not should_have and has_role:
                try:
                    await member.remove_roles(role, reason="Ping preference via /pingpanel")
                    removed.append(role.name)
                except Exception as e:
                    errors.append(f"Nem sikerült eltávolítani {role.name}: {e}")

        parts = []
        if added:
            parts.append(f"✅ Hozzáadva: {', '.join(added)}")
        if removed:
            parts.append(f"❌ Eltávolítva: {', '.join(removed)}")
        if not added and not removed:
            parts.append("Nincs változtatás.")
        if errors:
            parts.append("\nHibák:\n" + "\n".join(errors))

        await interaction.response.send_message("\n".join(parts), ephemeral=True)


class PingPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PingRoleSelect())
        self.add_item(ClearAllPingsButton())


class ClearAllPingsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="❌ Minden ping kikapcsolása",
            style=discord.ButtonStyle.danger,
            custom_id="clear_all_pings"
        )

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Hiba: nem tag.", ephemeral=True)
            return

        guild = member.guild
        removed = []
        errors = []

        for gm, role_id in QUEUE_PING_ROLES.items():
            role = guild.get_role(role_id)
            if not role:
                continue
            has_role = any(r.id == role_id for r in member.roles)
            if has_role:
                try:
                    await member.remove_roles(role, reason="Ping preference clear all")
                    removed.append(role.name)
                except Exception as e:
                    errors.append(f"Nem sikerült eltávolítani {role.name}: {e}")

        parts = []
        if removed:
            parts.append(f"❌ Eltávolítva: {', '.join(removed)}")
        else:
            parts.append("Nincs bekapcsolva ping.")
        if errors:
            parts.append("\nHibák:\n" + "\n".join(errors))

        await interaction.response.send_message("\n".join(parts), ephemeral=True)


async def update_queue_message(gamemode: str):
    """Update the queue embed in its channel"""
    channel_id = QUEUE_CHANNELS.get(gamemode)
    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        return

    msg_id = None
    for mid, gm in QUEUE_MESSAGE_IDS.items():
        if gm == gamemode:
            msg_id = mid
            break
    if not msg_id:
        # Still try to refresh panel in case state changed
        if channel.guild:
            await refresh_queue_panel(channel.guild)
        return

    try:
        message = await channel.fetch_message(msg_id)
    except discord.NotFound:
        # Message was deleted, clean up mapping
        try:
            del QUEUE_MESSAGE_IDS[msg_id]
            _persist_queue_message_ids()
        except KeyError:
            pass
        if channel.guild:
            await refresh_queue_panel(channel.guild)
        return
    except Exception:
        # Fetch failed for other reasons, still refresh panel
        if channel.guild:
            await refresh_queue_panel(channel.guild)
        return

    queue = ACTIVE_QUEUES.get(gamemode)
    if not queue:
        embed = discord.Embed(
            title=f"{get_gamemode_indicator(gamemode, False)} {get_gamemode_display_name(gamemode)} Queue",
            description="A queue zárva van.",
            color=get_gamemode_color(gamemode)
        )
        try:
            await message.edit(embed=embed, view=None)
            # Remove from mapping as it's no longer active
            try:
                del QUEUE_MESSAGE_IDS[msg_id]
                _persist_queue_message_ids()
            except KeyError:
                pass
        except Exception:
            pass
        if channel.guild:
            await refresh_queue_panel(channel.guild)
        return

    player_lines = []
    for player in queue["players"]:
        member = channel.guild.get_member(player.discord_id)
        name = member.display_name if member else player.minecraft_name
        player_lines.append(f"{name} ({player.minecraft_name})")

    player_text = "\n".join(player_lines) if player_lines else "Még senki nincs a queue-ban."

    # Build testers list
    tester_lines = []
    for tester in queue.get("testers", []):
        member = channel.guild.get_member(tester.discord_id)
        name = member.display_name if member else tester.minecraft_name
        tester_lines.append(f"{name} ({tester.minecraft_name})")

    tester_text = "\n".join(tester_lines) if tester_lines else "Még egy teszter sincs a queue-ban."

    embed = discord.Embed(
        title=f"{get_gamemode_indicator(gamemode)} {get_gamemode_display_name(gamemode)} Queue",
        description=f"Játékosok: **{len(queue['players'])}** | Teszterek: **{len(queue.get('testers', []))}**",
        color=get_gamemode_color(gamemode)
    )
    embed.add_field(name="Játékosok", value=player_text, inline=False)
    embed.add_field(name="Teszterek", value=tester_text, inline=False)

    view = QueueActionView(gamemode)
    try:
        await message.edit(embed=embed, view=view)
    except Exception as e:
        print(f"Queue update error [{gamemode}]: {e}")
    if channel.guild:
        await refresh_queue_panel(channel.guild)


async def queue_maintenance_task():
    """Periodically update all queue messages"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await asyncio.sleep(30)
            for gm in list(ACTIVE_QUEUES.keys()):
                try:
                    await update_queue_message(gm)
                except Exception as e:
                    print(f"[QueueMaintenance] {gm}: {e}")
        except Exception as e:
            print(f"[QueueMaintenance] Fatal: {e}")

def _choices_from_list(values):
    return [app_commands.Choice(name=v, value=v) for v in values]


@app_commands.command(name="ticketpanel", description="Ticket panel üzenet kirakása.")
async def ticketpanel(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Hiba.", ephemeral=True)
            return
        if interaction.channel is None:
            await interaction.followup.send("Hiba: nincs csatorna.", ephemeral=True)
            return

        description = "Kattints egy alábbi gombra, hogy tudd tesztelni a gombon feltüntetett játékmódból."

        embed = discord.Embed(
            title="Teszt kérés",
            description=description,
            color=discord.Color.blurple()
        )

        await interaction.channel.send(embed=embed, view=TicketPanelView())
        await interaction.followup.send("✅ Ticket panel kirakva.", ephemeral=True)

    except discord.Forbidden:
        await interaction.followup.send("❌ Nem tudok ide írni (Missing Permissions). Adj írás jogot a botnak ebben a csatornában.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Hiba: {type(e).__name__}: {e}", ephemeral=True)


async def autocomplete_testresult_username(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    if not WEBSITE_URL:
        return []

    try:
        url = f"{WEBSITE_URL}/api/tests"
        timeout = aiohttp.ClientTimeout(total=5)
        async with http_session.get(url, headers=_auth_headers(), timeout=timeout) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            tests = data.get("tests", [])

            # Extract unique usernames
            usernames = set()
            for t in tests:
                u = t.get("username")
                if u:
                    usernames.add(u)

            # Filter by current input
            matches = [u for u in usernames if current.lower() in u.lower()]
            return [app_commands.Choice(name=u, value=u) for u in matches[:25]]
    except Exception:
        return []


class QueuePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for label, key, _rid in TICKET_TYPES:
            self.add_item(QueueOpenButton(label=label, mode_key=key))


class QueueOpenButton(discord.ui.Button):
    def __init__(self, label: str, mode_key: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=f"queue_open_{mode_key}")
        self.mode_key = mode_key
        self.mode_label = label

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Hiba: csak szerveren használható.", ephemeral=True)
            return
        if not is_gamemode_tester_or_admin(interaction.user, self.mode_key):
            await interaction.followup.send(
                "❌ Csak az adott játékmód tesztelői nyithatnak queue-t. Szerezd be a megfelelő tesztelő role-t!",
                ephemeral=True
            )
            return
            
        mode_key = self.mode_key
        mode_display = self.mode_label
        
        if mode_key in ACTIVE_QUEUES:
            await interaction.followup.send(f"❌ A **{mode_display}** queue már nyitva van!", ephemeral=True)
            return

        ACTIVE_QUEUES[mode_key] = {
            "opened_by": interaction.user.id,
            "opened_at": time.time(),
            "players": [],
            "testers": [QueuePlayer(interaction.user.id, get_linked_minecraft_name(interaction.user.id) or "TESZTER")],
            "called_players": []
        }

        channel_id = QUEUE_CHANNELS.get(mode_key)
        if not channel_id:
            await interaction.followup.send(f"❌ Nincs channel beállítva ehhez a gamemode-hoz: {mode_display}", ephemeral=True)
            return

        channel = interaction.guild.get_channel(channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(f"❌ Channel nem található: {channel_id}", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"{get_gamemode_indicator(mode_key)} {mode_display} Queue",
            description="A queue nyitva van! Kattints a gombokhoz alább.",
            color=get_gamemode_color(mode_key)
        )
        embed.add_field(name="Játékosok", value="Még senki nincs a queue-ban.", inline=False)
        # Show opening tester
        linked_mc = get_linked_minecraft_name(interaction.user.id) or "TESZTER"
        tester_name = interaction.user.display_name
        embed.add_field(name="Teszterek", value=f"{tester_name} ({linked_mc})", inline=False)

        # Get ping role for this gamemode
        ping_role_id = QUEUE_PING_ROLES.get(mode_key)
        ping_text = f"<@&{ping_role_id}> " if ping_role_id else ""

        view = QueueActionView(mode_key)
        message = await channel.send(content=ping_text, embed=embed, view=view)
        QUEUE_MESSAGE_IDS[message.id] = mode_key
        _persist_queue_message_ids()

        await interaction.followup.send(f"✅ **{mode_display}** queue megnyitva!", ephemeral=True)

        # Refresh queue panel
        await refresh_queue_panel(interaction.guild)


async def rebuild_queue_message_ids(guild):
    """Scan queue channels to rebuild QUEUE_MESSAGE_IDS mapping after restart"""
    global QUEUE_MESSAGE_IDS
    QUEUE_MESSAGE_IDS.clear()
    for gamemode, channel_id in QUEUE_CHANNELS.items():
        channel = guild.get_channel(channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            continue
        try:
            async for msg in channel.history(limit=200):
                # Look for active queue messages: embed title contains "Queue" but not "Panel", and has components
                if msg.embeds and msg.components and msg.embeds[0].title:
                    title = msg.embeds[0].title
                    # Skip the Queue Panel messages (they have "Panel" in title)
                    if "Queue" in title and "Panel" not in title:
                        QUEUE_MESSAGE_IDS[msg.id] = gamemode
                        break  # Only one active queue per channel
        except Exception as e:
            print(f"Error scanning channel {channel_id} for queue message: {e}")
    # Persist the rebuilt mapping
    _persist_queue_message_ids()


async def refresh_queue_panel(guild):
    """Refresh the queue panel"""
    global QUEUE_PANEL_MESSAGE
    if QUEUE_PANEL_MESSAGE is None:
        return
    channel_id, msg_id = QUEUE_PANEL_MESSAGE
    channel = guild.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        return
    try:
        msg = await channel.fetch_message(msg_id)
        embed = discord.Embed(
            title="Queue nyitás",
            description="Kattints a gombra a queue megnyitásához.",
            color=discord.Color.blurple()
        )
        embed.add_field(name="Információk", value="Válaszd ki a gamemodet és nyomd meg a gombot.", inline=False)
        embed.add_field(name="Gombok", value="Queue kezeléshez", inline=False)
        await msg.edit(embed=embed, view=QueuePanelView())
    except discord.NotFound:
        QUEUE_PANEL_MESSAGE = None
        data = _load_data()
        data["queue_panel_message"] = None
        _save_data(data)
    except Exception as e:
        print(f"Error refreshing queue panel: {e}")


@app_commands.command(name="queuepanel", description="Queue panel kirakása (tesztereknek)")
async def queuepanel(interaction: discord.Interaction):
    """Open queue panel - sends to channel for all testers"""
    await interaction.response.defer(ephemeral=True)
    
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("Hiba.", ephemeral=True)
        return
    if not is_staff_member(interaction.user):
        await interaction.followup.send("Nincs jogod.", ephemeral=True)
        return

    # Send to the channel where command was used - visible for everyone!
    channel = interaction.channel
    
    embed = discord.Embed(
        title="Queue nyitás",
        description="Kattints a gombra a queue megnyitásához.",
        color=discord.Color.blurple()
    )
    embed.add_field(name="Információk", value="Válaszd ki a gamemodet és nyomd meg a gombot.", inline=False)
    embed.add_field(name="Gombok", value="Queue kezeléshez", inline=False)
    
    # Send to channel - NOT ephemeral, visible for everyone
    message = await channel.send(embed=embed, view=QueuePanelView())
    global QUEUE_PANEL_MESSAGE
    QUEUE_PANEL_MESSAGE = (channel.id, message.id)
    
    # Persist panel message ID
    data = _load_data()
    data["queue_panel_message"] = [channel.id, message.id]
    _save_data(data)
    
    await interaction.followup.send("✅ Panel elküldve!", ephemeral=True)


@app_commands.command(name="pingpanel", description="Ping értesítések beállítása queue-okhoz")
async def pingpanel(interaction: discord.Interaction):
    """Set up ping notifications for queues"""
    await interaction.response.defer()

    try:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Hiba: csak szerveren használható.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🔔 Ping Beállítások",
            description="Válaszd ki a queue-okat amikor értesíteni szeretnél:",
            color=discord.Color.blue()
        )
        view = PingPanelView()
        await interaction.followup.send(embed=embed, view=view)

    except Exception as e:
        await interaction.followup.send(f"❌ Hiba: {type(e).__name__}: {e}", ephemeral=True)


@app_commands.command(name="closequeue", description="Bezár egy queue-t (staff vagy queue nyitó)")
@app_commands.describe(
    gamemode="A queue gamemódja"
)
@app_commands.choices(
    gamemode=_choices_from_list(MODE_LIST)
)
async def closequeue(interaction: discord.Interaction, gamemode: app_commands.Choice[str]):
    """Close a queue by gamemode"""
    await interaction.response.defer(ephemeral=True)
    
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("Hiba.", ephemeral=True)
        return
    
    mode_key = gamemode.value.lower()
    queue = ACTIVE_QUEUES.get(mode_key)
    
    if not queue:
        msg_id = None
        for mid, gm in QUEUE_MESSAGE_IDS.items():
            if gm == mode_key:
                msg_id = mid
                break
        if msg_id:
            channel_id = QUEUE_CHANNELS.get(mode_key)
            if channel_id:
                channel = interaction.guild.get_channel(channel_id)
                if channel and isinstance(channel, discord.TextChannel):
                    try:
                        msg = await channel.fetch_message(msg_id)
                        embed = discord.Embed(
                            title=f"{get_gamemode_indicator(mode_key, False)} {get_gamemode_display_name(mode_key)} Queue",
                            description="A queue zárva van.",
                            color=get_gamemode_color(mode_key)
                        )
                        await msg.edit(embed=embed, view=None)
                        del QUEUE_MESSAGE_IDS[msg_id]
                        _persist_queue_message_ids()
                        await interaction.followup.send(f"✅ **{gamemode.name}** queue bezárva (törölve a státuszból).", ephemeral=True)
                        await refresh_queue_panel(interaction.guild)
                        return
                    except discord.NotFound:
                        pass
        await interaction.followup.send(f"❌ A **{gamemode.name}** queue nincs nyitva.", ephemeral=True)
        return
    
    if queue.get("opened_by") != interaction.user.id and not is_staff_member(interaction.user):
        await interaction.followup.send("❌ Csak a queue-t megnyitó tesztelő vagy staff zárhatja be.", ephemeral=True)
        return
    
    del ACTIVE_QUEUES[mode_key]
    await interaction.followup.send(f"✅ **{gamemode.name}** queue bezárva.", ephemeral=True)
    
    await refresh_queue_panel(interaction.guild)
    
    try:
        msg_id = None
        for mid, gm in list(QUEUE_MESSAGE_IDS.items()):
            if gm == mode_key:
                msg_id = mid
                break
        if msg_id:
            channel_id = QUEUE_CHANNELS.get(mode_key)
            if channel_id:
                channel = interaction.guild.get_channel(channel_id)
                if channel and isinstance(channel, discord.TextChannel):
                    msg = await channel.fetch_message(msg_id)
                    embed = discord.Embed(
                        title=f"{get_gamemode_indicator(mode_key, False)} {get_gamemode_display_name(mode_key)} Queue",
                        description="A queue zárva van.",
                        color=get_gamemode_color(mode_key)
                    )
                    await msg.edit(embed=embed, view=None)
                    if msg_id in QUEUE_MESSAGE_IDS:
                        del QUEUE_MESSAGE_IDS[msg_id]
                        _persist_queue_message_ids()
    except Exception as e:
        print(f"Error updating queue message on close: {e}")


@app_commands.command(name="testresult", description="Minecraft tier teszt eredmény embed + weboldal mentés.")
@app_commands.describe(
    username="Minecraft név (ebből lesz a skin a weboldalon)",
    tester="Tesztelő (Discord user)",
    gamemode="Játékmód",
    rank="Elért rank (pl. LT3 / HT3)"
)
@app_commands.autocomplete(username=autocomplete_testresult_username)
@app_commands.choices(
    gamemode=_choices_from_list(MODE_LIST),
    rank=_choices_from_list(RANKS)
)
async def testresult(
    interaction: discord.Interaction,
    username: str,
    tester: discord.Member,
    gamemode: app_commands.Choice[str],
    rank: app_commands.Choice[str],
):
    import uuid
    execution_id = str(uuid.uuid4())[:8]
    print(f"[TESTRESULT {execution_id}] Command started for {username} by {interaction.user.id}")
    await interaction.response.defer(ephemeral=True)

    try:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Hiba.", ephemeral=True)
            return
        if not is_staff_member(interaction.user):
            await interaction.followup.send("Nincs jogosultságod ehhez a parancshoz.", ephemeral=True)
            return
        if interaction.channel is None:
            await interaction.followup.send("Hiba: nincs csatorna.", ephemeral=True)
            return

        mode_val = gamemode.value
        rank_val = rank.value

        # Previous rank from website (best-effort)
        prev_rank = "Unranked"
        print(f"[TESTRESULT {execution_id}] Getting previous rank for {username} in {mode_val}...")
        if WEBSITE_URL:
            try:
                res = await api_get_tests(username=username, mode=mode_val)
                print(f"[TESTRESULT {execution_id}] Got previous rank response: {res.get('status')}")
                if res.get("status") == 200:
                    data = res.get("data", {})
                    # Handle single result (test) or list (tests)
                    test = data.get("test")
                    tests = data.get("tests", [])

                    target = test if test else (tests[0] if tests else None)

                    if target:
                        prev_rank = str(target.get("rank", "Unranked")) or "Unranked"
            except Exception:
                pass

        prev_points = POINTS.get(prev_rank, 0)
        new_points = POINTS.get(rank_val, 0)
        diff = new_points - prev_points

        # PUBLIC EMBED (everyone sees)
        skin_url = f"https://minotar.net/helm/{username}/128.png"

        # April Fools' effects
        display_username = username
        display_mode = mode_val
        display_prev_rank = prev_rank
        display_rank_val = rank_val

        embed = discord.Embed(
            title=f"{display_username} teszt eredménye 🏆",
            color=discord.Color.dark_grey()
        )
        embed.set_thumbnail(url=skin_url)
        embed.add_field(name="Tesztelő:", value=tester.mention, inline=False)
        embed.add_field(name="Játékmód:", value=display_mode, inline=False)
        embed.add_field(name="Minecraft név:", value=display_username, inline=False)
        embed.add_field(name="Előző rang:", value=display_prev_rank, inline=False)
        embed.add_field(name="Elért rang:", value=display_rank_val, inline=False)

        # Only send to the results channel (eredmenyek), not the command channel
        # ALWAYS save to website first (UPsert)
        if not WEBSITE_URL:
            await interaction.followup.send("⚠️ WEBSITE_URL nincs beállítva, nem mentem webre.", ephemeral=True)
            return

        # Normalize mode to proper display name before saving
        mode_to_save = get_gamemode_display_name(mode_val)
        save = await api_post_test(username=username, mode=mode_to_save, rank=rank_val, tester=tester)
        save_status = save.get("status")
        save_data = save.get("data")
        save_ok = (save_status == 200 or save_status == 201)

        print(f"[TESTRESULT {execution_id}] DEBUG: save to website status: {save_status}, ok: {save_ok}")

        # Set cooldown for the tested player (ALWAYS do this after saving)
        channel = interaction.channel
        owner_id = None
        if channel and channel.topic:
            try:
                # Parse "owner=123456789"
                for part in channel.topic.split(" | "):
                    if part.startswith("owner="):
                        owner_id = int(part.split("=")[1])
                        break
            except Exception:
                pass

        if owner_id:
            set_last_closed(owner_id, mode_val, time.time())

        # Send to results channel if configured
        tier_channel_id_str = os.getenv("TIER_RESULTS_CHANNEL_ID", "0")
        try:
            tier_channel_id = int(tier_channel_id_str)
        except ValueError:
            tier_channel_id = 0

        if tier_channel_id:
            tier_channel = interaction.guild.get_channel(tier_channel_id)
            if tier_channel:
                print(f"[TESTRESULT {execution_id}] DEBUG: sending to channel {tier_channel.name}...")
                await tier_channel.send(embed=embed)
                print(f"[TESTRESULT {execution_id}] DEBUG: sent to results channel: {tier_channel.name}")
                await interaction.followup.send(
                    f"✅ Eredmény mentve!\nElőző: **{prev_rank}** → Elért: **{rank_val}** | "
                    f"{'+' if diff>=0 else ''}{diff} pont",
                    ephemeral=True
                )
                print(f"[TESTRESULT {execution_id}] DEBUG: followup sent, returning...")
                return
            else:
                print(f"[TESTRESULT {execution_id}] DEBUG: could not find results channel with ID: {tier_channel_id}")

        # Try fallback by name
        tier_channel = discord.utils.get(interaction.guild.text_channels, name="teszteredmenyek")
        if tier_channel:
            await tier_channel.send(embed=embed)
            return
        tier_channel = discord.utils.get(interaction.guild.text_channels, name="test-results")
        if tier_channel:
            await tier_channel.send(embed=embed)
            return

        # Fallback: send response if no results channel was found
        if save_ok:
            await interaction.followup.send(
                f"✅ Mentve + weboldal frissítve.\nElőző: **{prev_rank}** → Elért: **{rank_val}** | "
                f"{'+' if diff>=0 else ''}{diff} pont",
                ephemeral=True
            )
        else:
            # Truncate save_data to avoid Discord's 2000 character limit
            save_data_str = truncate_message(str(save_data), 1500)
            await interaction.followup.send(
                f"⚠️ Mentés hiba a weboldal felé (status {save_status}) | {save_data_str}",
                ephemeral=True
            )

    except aiohttp.ClientError as e:
        await interaction.followup.send(f"⚠️ Web hiba: {type(e).__name__}: {e}", ephemeral=True)
    except asyncio.TimeoutError:
        await interaction.followup.send("⚠️ Web timeout (nem válaszolt 10 mp-en belül).", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ Nem tudok ide írni / embedet küldeni (Missing Permissions).", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Hiba: {type(e).__name__}: {e}", ephemeral=True)


@app_commands.command(name="tierlistnamechange", description="Játékos nevének megváltoztatása a tierlistán (admin csak)")
@app_commands.describe(
    oldname="A jelenlegi név a tierlistán",
    newname="Az új név ami megjelenik a tierlistán"
)
async def tierlistnamechange(interaction: discord.Interaction, oldname: str, newname: str):
    await interaction.response.defer(ephemeral=True)

    try:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Hiba.", ephemeral=True)
            return
        if not is_staff_member(interaction.user):
            await interaction.followup.send("Nincs jogosultságod ehhez a parancshoz.", ephemeral=True)
            return

        # Call the website API to rename the player
        if not WEBSITE_URL:
            await interaction.followup.send("⚠️ WEBSITE_URL nincs beállítva.", ephemeral=True)
            return

        # Pre-delete any conflicting tests for newname in modes that oldname has
        try:
            # Fetch old player's tests to get their modes
            old_tests_url = f"{WEBSITE_URL}/api/tests?username={oldname}"
            async with http_session.get(old_tests_url, headers=_auth_headers(), timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)) as old_resp:
                if old_resp.status == 200:
                    old_data = await old_resp.json()
                    old_tests = old_data.get("data", {}).get("tests", [])
                    old_modes = {t.get("gamemode", "").lower() for t in old_tests if t.get("gamemode")}
                else:
                    old_modes = set()
        except Exception as e:
            print(f"Error fetching old tests for conflict check: {e}")
            old_modes = set()

        if old_modes:
            try:
                # Fetch new player's tests to find conflicts
                new_tests_url = f"{WEBSITE_URL}/api/tests?username={newname}"
                async with http_session.get(new_tests_url, headers=_auth_headers(), timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)) as new_resp:
                    if new_resp.status == 200:
                        new_data = await new_resp.json()
                        new_tests = new_data.get("data", {}).get("tests", [])
                        for test in new_tests:
                            test_mode = test.get("gamemode", "").lower()
                            if test_mode in old_modes:
                                test_id = test.get("id")
                                if test_id:
                                    print(f"Deleting conflicting test for {newname}/{test_mode}: id={test_id}")
                                    if USE_SUPABASE_API:
                                        await supabase_delete("tests", {"id": test_id})
                                    elif db_pool is not None:
                                        await db_delete_test(str(test_id))
                                    else:
                                        try:
                                            del_url = f"{WEBSITE_URL}/api/tests/{test_id}"
                                            async with http_session.delete(del_url, headers=_auth_headers(), timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)) as d_resp:
                                                print(f"Delete conflict test status: {d_resp.status}")
                                        except Exception as e:
                                            print(f"Failed to delete conflicting test {test_id}: {e}")
            except Exception as e:
                print(f"Error checking/deleting conflicts for {newname}: {e}")

        result = await api_rename_player(old_name=oldname, new_name=newname)
        status = result.get("status")
        data = result.get("data", {})

        if status == 200:
            updated_count = data.get("updatedCount", 0)

            # Also update linked_accounts in Supabase
            if USE_SUPABASE_API:
                try:
                    success = await supabase_update(
                        "linked_accounts",
                        {"minecraft_name": newname},
                        {"minecraft_name": oldname}
                    )
                    if success:
                        print(f"Updated linked_accounts: {oldname} -> {newname}")
                    else:
                        print(f"Warning: linked_accounts update returned False for {oldname} -> {newname}")
                except Exception as e:
                    print(f"Error updating linked_accounts: {e}")

            msg = f"✅ Sikeresen átnevezve: **{oldname}** → **{newname}**\nFrissítve: {updated_count} db bejegyzés (összes gamemód)"

            await interaction.followup.send(msg, ephemeral=True)
        elif status == 404:
            await interaction.followup.send(
                f"❌ Játékos nem találva: **{oldname}**",
                ephemeral=True
            )
        elif status == 401 or status == 403:
            await interaction.followup.send(
                "❌ Nincs jogosultságod ehhez a parancshoz.",
                ephemeral=True
            )
        else:
            # Truncate data to avoid Discord's 2000 character limit
            data_str = truncate_message(str(data), 1500)
            await interaction.followup.send(
                f"⚠️ Hiba (status {status}): {data_str}",
                ephemeral=True
            )

    except aiohttp.ClientError as e:
        await interaction.followup.send(f"⚠️ Web hiba: {type(e).__name__}: {e}", ephemeral=True)
    except asyncio.TimeoutError:
        await interaction.followup.send("⚠️ Web timeout (nem válaszolt 10 mp-en belül).", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Hiba: {type(e).__name__}: {e}", ephemeral=True)


@app_commands.command(name="profile", description="Megnézed egy játékos tierjeit a tierlistáról.")
@app_commands.describe(
    name="A játékos neve a tierlistán"
)
async def profile(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=False)

    try:
        if not WEBSITE_URL:
            await interaction.followup.send("⚠️ WEBSITE_URL nincs beállítva.", ephemeral=True)
            return

        # Use the new API endpoint that supports filtering by username only
        url = f"{WEBSITE_URL}/api/tests?username={name}"
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        async with http_session.get(url, headers=_auth_headers(), timeout=timeout) as resp:
            try:
                data = await resp.json()
            except Exception:
                data = {}

            if resp.status != 200:
                await interaction.followup.send(f"⚠️ Hiba a weboldal lekérésekor: {resp.status}", ephemeral=True)
                return

            tests = data.get("tests", [])

            if not tests:
                await interaction.followup.send(f"❌ Nincs találat erre a névre: **{name}**", ephemeral=False)
                return

            # Get global rank by fetching all tests and sorting
            all_url = f"{WEBSITE_URL}/api/tests"
            async with http_session.get(all_url, headers=_auth_headers(), timeout=timeout) as all_resp:
                try:
                    all_data = await all_resp.json()
                except Exception:
                    all_data = {}

            all_tests = all_data.get("tests", [])
            global_rank = None
            if all_tests:
                # Group by username and sum points
                player_totals = {}
                for t in all_tests:
                    username = t.get("username", "")
                    points = t.get("points", 0)
                    if username in player_totals:
                        player_totals[username] += points
                    else:
                        player_totals[username] = points

                # Sort by total points descending
                sorted_players = sorted(player_totals.items(), key=lambda x: x[1], reverse=True)

                # Find the player's position
                player_username = tests[0].get("username", "")
                player_total_points = player_totals.get(player_username, 0)

                for idx, (name, pts) in enumerate(sorted_players, 1):
                    if name == player_username:
                        global_rank = idx
                        break

            # Build embed
            display_name = tests[0].get('username', name)

            embed = discord.Embed(
                title=f"{display_name} profilja",
                color=discord.Color.blurple()
            )

            # Sort by points (desc)
            tests.sort(key=lambda x: x.get("points", 0), reverse=True)

            # List modes
            mode_strs = []
            total_points = 0
            for t in tests:
                m = t.get("gamemode", "?")
                r = t.get("rank", "?")
                p = t.get("points", 0)
                total_points += p
                # April Fools' funny rank display
                display_rank = r
                mode_strs.append(f"**{m}**: {display_rank} ({p}pt)")

            embed.description = "\n".join(mode_strs)

            # Add rank info
            rank_info = f"**Összes pont:** {total_points}"
            if global_rank:
                rank_info += f"\n**Globális rank:** #{global_rank}"

            embed.add_field(name="Statisztika", value=rank_info, inline=False)

            # Skin
            skin_url = f"https://minotar.net/helm/{tests[0].get('username', name)}/128.png"
            embed.set_thumbnail(url=skin_url)

            await interaction.followup.send(embed=embed)

    except aiohttp.ClientError as e:
        await interaction.followup.send(f"⚠️ Web hiba: {type(e).__name__}: {e}", ephemeral=True)
    except asyncio.TimeoutError:
        await interaction.followup.send("⚠️ Web timeout (nem válaszolt 10 mp-en belül).", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Hiba: {type(e).__name__}: {e}", ephemeral=True)


@app_commands.command(name="spin", description="Kiválaszt egy véletlenszerű játékost a megadott gamemodból és tierből.")
@app_commands.describe(
    gamemode="A játékmód (pl. sword, pot, smp)",
    tier="A tier (pl. ht3, lt1)",
    sajat="Include self in roll (default: no)"
)
@app_commands.choices(
    gamemode=_choices_from_list(MODE_LIST),
    tier=_choices_from_list(RANKS)
)
async def spin(interaction: discord.Interaction, gamemode: app_commands.Choice[str], tier: app_commands.Choice[str], sajat: bool = False):
    await interaction.response.defer(ephemeral=False)

    try:
        if not is_staff_member(interaction.user):
            await interaction.followup.send("Nincs jogosultságod ehhez a parancshoz.", ephemeral=True)
            return

        if not WEBSITE_URL:
            await interaction.followup.send("⚠️ WEBSITE_URL nincs beállítva.", ephemeral=True)
            return

        # Try to exclude the ticket owner (unless sajat=True)
        exclude_user = None
        if not sajat:
            # Use Discord user's display name to exclude
            exclude_user = interaction.user.display_name.lower().replace(" ", "-")

        # Build URL with exclusion if we found someone
        url = f"{WEBSITE_URL}/api/tests?mode={gamemode.value}&tier={tier.value}"
        if exclude_user:
            url += f"&exclude={exclude_user}"

        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        async with http_session.get(url, headers=_auth_headers(), timeout=timeout) as resp:
            try:
                data = await resp.json()
            except Exception:
                data = {}

            if resp.status != 200:
                await interaction.followup.send(f"⚠️ Hiba a weboldal lekérésekor: {resp.status}", ephemeral=True)
                return

            player = data.get("player")

            if not player:
                await interaction.followup.send("❌ Nincs találat erre a gamemódra és tier-re.", ephemeral=False)
                return

            username = player.get("username")
            rank = player.get("rank")

            embed = discord.Embed(
                title="🎲 Sorsolt játékos",
                description=f"**{username}** ({rank})",
                color=discord.Color.gold()
            )

            skin_url = f"https://minotar.net/helm/{username}/128.png"
            embed.set_thumbnail(url=skin_url)

            await interaction.followup.send(embed=embed)

    except aiohttp.ClientError as e:
        await interaction.followup.send(f"⚠️ Web hiba: {type(e).__name__}: {e}", ephemeral=True)
    except asyncio.TimeoutError:
        await interaction.followup.send("⚠️ Web timeout (nem válaszolt 10 mp-en belül).", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Hiba: {type(e).__name__}: {e}", ephemeral=True)


# =========================
# GLOBAL APP COMMAND ERROR HANDLER
# =========================

@app_commands.command(name="retire", description="Játékos nyugdíjazása egy gamemódban (admin csak, csak Tier 2).")
@app_commands.describe(
    name="A játékos neve a tierlistán",
    gamemode="A játékmód"
)
@app_commands.choices(
    gamemode=_choices_from_list(MODE_LIST)
)
async def retire(interaction: discord.Interaction, name: str, gamemode: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)

    try:
        if not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("Nincs jogosultságod ehhez a parancshoz.", ephemeral=True)
            return

        if not WEBSITE_URL:
            await interaction.followup.send("⚠️ WEBSITE_URL nincs beállítva.", ephemeral=True)
            return

        # First, check the player's current rank to ensure they are Tier 2
        url = f"{WEBSITE_URL}/api/tests?username={name}&gamemode={gamemode.value}"
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        async with http_session.get(url, headers=_auth_headers(), timeout=timeout) as resp:
            try:
                data = await resp.json()
            except Exception:
                data = {}

            if resp.status != 200:
                await interaction.followup.send(f"⚠️ Hiba a weboldal lekérésekor: {resp.status}", ephemeral=True)
                return

            test = data.get("test")
            if not test:
                await interaction.followup.send(
                    f"❌ Játékos nem találva: **{name}** ezen a gamemódon ({gamemode.value}).",
                    ephemeral=True
                )
                return

            current_rank = test.get("rank", "")
            # Check if Tier 2
            if current_rank not in ["LT2", "HT2"]:
                await interaction.followup.send(
                    f"❌ Csak Tier 2 (LT2/HT2) játékosok nyugdíjazhatók. **{name}** jelenleg: **{current_rank}**.",
                    ephemeral=True
                )
                return

        # Call the website API to retire (upsert with R prefix)
        retire_url = f"{WEBSITE_URL}/api/tests"
        payload = {
            "username": name,
            "gamemode": gamemode.value,
            "rank": f"R{current_rank}",
            "points": POINTS.get(current_rank, 0), # Keep same points
            "retired": True
        }

        async with http_session.post(retire_url, json=payload, headers=_auth_headers(), timeout=timeout) as retire_resp:
            try:
                retire_data = await retire_resp.json()
            except Exception:
                retire_data = {}

            if retire_resp.status == 200:
                msg = f"✅ Sikeres nyugdíjazás! **{name}** ({gamemode.value}) most **R{current_rank}**."

                await interaction.followup.send(msg, ephemeral=True)
            else:
                # Truncate retire_data to avoid Discord's 2000 character limit
                retire_data_str = truncate_message(str(retire_data), 1500)
                await interaction.followup.send(
                    f"⚠️ Hiba: {retire_resp.status} - {retire_data_str}",
                    ephemeral=True
                )

    except aiohttp.ClientError as e:
        await interaction.followup.send(f"⚠️ Web hiba: {type(e).__name__}: {e}", ephemeral=True)
    except asyncio.TimeoutError:
        await interaction.followup.send("⚠️ Web timeout.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Hiba: {type(e).__name__}: {e}", ephemeral=True)


@app_commands.command(name="unretire", description="Játékos visszahozása nyugdíjból (admin csak).")
@app_commands.describe(
    name="A játékos neve a tierlistán",
    gamemode="A játékmód"
)
@app_commands.choices(
    gamemode=_choices_from_list(MODE_LIST)
)
async def unretire(interaction: discord.Interaction, name: str, gamemode: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)

    try:
        if not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("Nincs jogosultságod ehhez a parancshoz.", ephemeral=True)
            return

        if not WEBSITE_URL:
            await interaction.followup.send("⚠️ WEBSITE_URL nincs beállítva.", ephemeral=True)
            return

        # First, get current rank to remove R prefix
        url = f"{WEBSITE_URL}/api/tests?username={name}&gamemode={gamemode.value}"
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        async with http_session.get(url, headers=_auth_headers(), timeout=timeout) as resp:
            try:
                data = await resp.json()
            except Exception:
                data = {}

            if resp.status != 200:
                await interaction.followup.send(f"⚠️ Hiba a weboldal lekérésekor: {resp.status}", ephemeral=True)
                return

            test = data.get("test")
            if not test:
                await interaction.followup.send(
                    f"❌ Játékos nem találva: **{name}** ezen a gamemódon ({gamemode.value}).",
                    ephemeral=True
                )
                return

            current_rank = test.get("rank", "")
            if not current_rank.startswith("R"):
                await interaction.followup.send(
                    f"❌ A játékos nincs nyugdíjazva ebben a gamemódban.",
                    ephemeral=True
                )
                return

            original_rank = current_rank[1:] # Remove R prefix

        # Upsert back to original rank
        post_url = f"{WEBSITE_URL}/api/tests"
        payload = {
            "username": name,
            "gamemode": gamemode.value,
            "rank": original_rank,
            "points": POINTS.get(original_rank, 0)
        }

        async with http_session.post(post_url, json=payload, headers=_auth_headers(), timeout=timeout) as post_resp:
            try:
                post_data = await post_resp.json()
            except Exception:
                post_data = {}

            if post_resp.status == 200:
                msg = f"✅ Sikeres visszahozatal! **{name}** ({gamemode.value}) visszatért a tierlistára ({original_rank})."

                await interaction.followup.send(msg, ephemeral=True)
            else:
                # Truncate post_data to avoid Discord's 2000 character limit
                post_data_str = truncate_message(str(post_data), 1500)
                await interaction.followup.send(
                    f"⚠️ Hiba: {post_resp.status} - {post_data_str}",
                    ephemeral=True
                )

    except aiohttp.ClientError as e:
        await interaction.followup.send(f"⚠️ Web hiba: {type(e).__name__}: {e}", ephemeral=True)
    except asyncio.TimeoutError:
        await interaction.followup.send("⚠️ Web timeout.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Hiba: {type(e).__name__}: {e}", ephemeral=True)


@app_commands.command(name="tierlistban", description="Játékos kitiltása a tesztelésből (admin csak).")
@app_commands.describe(
    name="A játékos neve a tierlistán",
    days="Kitiltás időtartama napokban (0 = örök ban)",
    reason="Kitiltás oka (opcionális)"
)
async def tierlistban(interaction: discord.Interaction, name: str, days: int, reason: str = ""):
    await interaction.response.defer(ephemeral=True)

    try:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Hiba.", ephemeral=True)
            return
        if not is_staff_member(interaction.user):
            await interaction.followup.send("Nincs jogosultságod ehhez a parancshoz.", ephemeral=True)
            return

        # Check if already banned
        if is_player_banned(name):
            ban_info = get_ban_info(name)
            if ban_info:
                expires_at = ban_info.get("expires_at", 0)
                if expires_at == 0:
                    await interaction.followup.send(
                        f"❌ **{name}** már örökkitiltás alatt áll.",
                        ephemeral=True
                    )
                else:
                    from datetime import datetime
                    exp_date = datetime.fromtimestamp(expires_at)
                    await interaction.followup.send(
                        f"❌ **{name}** már kitiltva. Lejárat: {exp_date.strftime('%Y-%m-%d %H:%M')}",
                        ephemeral=True
                    )
            return

        # Ban the player in bot
        ban_player(name, days, reason)

        # Sync ban to website
        expires_at = 0 if days == 0 else int(time.time() + (days * 24 * 60 * 60))
        if WEBSITE_URL:
            await api_set_ban(username=name, banned=True, expires_at=expires_at, reason=reason)

        # Build response message
        if days == 0:
            msg = f"✅ **{name}** örökre ki lett tiltva a tesztelésből."
        else:
            msg = f"✅ **{name}** ki lett tiltva {days} napra a tesztelésből."

        if reason:
            msg += f"\n**Ok:** {reason}"

        await interaction.followup.send(msg, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Hiba: {type(e).__name__}: {e}", ephemeral=True)


@app_commands.command(name="tierlistunban", description="Játékos visszavétele a tesztelésbe (admin csak).")
@app_commands.describe(
    name="A játékos neve a tierlistán"
)
async def tierlistunban(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)

    try:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Hiba.", ephemeral=True)
            return
        if not is_staff_member(interaction.user):
            await interaction.followup.send("Nincs jogosultságod ehhez a parancshoz.", ephemeral=True)
            return

        # Check if actually banned
        if not is_player_banned(name):
            await interaction.followup.send(
                f"❌ **{name}** nincs kitiltva.",
                ephemeral=True
            )
            return

        # Unban from bot
        unban_player(name)

        # Sync unban to website
        if WEBSITE_URL:
            await api_set_ban(username=name, banned=False)

        msg = f"✅ **{name}** vissza lett engedve a tesztelésbe."

        await interaction.followup.send(msg, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Hiba: {type(e).__name__}: {e}", ephemeral=True)


# Confirmation view for remove tierlist
class ConfirmRemoveView(discord.ui.View):
    def __init__(self, username: str, actual_username: str, moderator: discord.Member):
        super().__init__(timeout=60)
        self.username = username  # What the user typed
        self.actual_username = actual_username  # What's in the database
        self.moderator = moderator
        self.confirmed = False

    @discord.ui.button(label="Igen, törlöm", style=discord.ButtonStyle.danger, custom_id="confirm_remove_yes")
    async def confirm_yes(self, interaction: discord.Interaction, _button: discord.ui.Button):
        # Only the moderator who started the command can confirm
        if interaction.user.id != self.moderator.id:
            await interaction.response.send_message("❌ Csak a parancs indítója erősítheti meg.", ephemeral=True)
            return

        self.confirmed = True
        await interaction.response.defer()

        try:
            # Call the API to remove the player - use actual username from DB
            result = await api_remove_player(username=self.actual_username)
            status = result.get("status")
            data = result.get("data", {})

            if status == 200:
                removed_count = data.get("removedCount", 1)
                modes = data.get("modes", "")
                details = data.get("details", "")

                # Truncate if too long for embed
                desc = f"**{self.username}** sikeresen törölve lett a tierlistáról.\nMód: {modes}"
                if details:
                    if len(desc) + len(details) > 1500:
                        details = details[:1500 - len(desc)] + "..."
                    desc += f"\n{details}"

                embed = discord.Embed(
                    title="✅ Játékos eltávolítva a tierlistáról",
                    description=desc,
                    color=discord.Color.green()
                )
                embed.set_footer(text=f"Moderátor: {self.moderator.display_name}")

                await interaction.followup.send(embed=embed)
            else:
                error_msg = data.get("error", "Ismeretlen hiba")
                # Truncate error_msg to avoid Discord's 2000 character limit
                error_msg_str = truncate_message(str(error_msg), 1500)
                await interaction.followup.send(
                    f"❌ Hiba a törléskor: {error_msg_str}",
                    ephemeral=True
                )
        except Exception as e:
            await interaction.followup.send(
                f"❌ Hiba: {type(e).__name__}: {e}",
                ephemeral=True
            )

        self.stop()

    @discord.ui.button(label="Mégse", style=discord.ButtonStyle.secondary, custom_id="confirm_remove_no")
    async def confirm_no(self, interaction: discord.Interaction, _button: discord.ui.Button):
        # Only the moderator who started the command can cancel
        if interaction.user.id != self.moderator.id:
            await interaction.response.send_message("❌ Csak a parancs indítója mondhat le.", ephemeral=True)
            return

        await interaction.response.send_message("❌ Törlés megszüntetve.", ephemeral=True)
        self.stop()


@app_commands.command(name="removetierlist", description="Játékos eltávolítása a tierlistáról (admin csak, DANGER!)")
@app_commands.describe(
    name="A játékos neve a tierlistán (Minecraft név)"
)
async def removetierlist(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)

    try:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Hiba.", ephemeral=True)
            return
        if not is_staff_member(interaction.user):
            await interaction.followup.send("Nincs jogosultságod ehhez a parancshoz.", ephemeral=True)
            return

        if not WEBSITE_URL:
            await interaction.followup.send("⚠️ WEBSITE_URL nincs beállítva.", ephemeral=True)
            return

        # First, check if the player exists in the tierlist (case-sensitive)
        url = f"{WEBSITE_URL}/api/tests?username={name}"
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        async with http_session.get(url, headers=_auth_headers(), timeout=timeout) as resp:
            try:
                data = await resp.json()
            except Exception:
                data = {}

            if resp.status != 200:
                await interaction.followup.send(f"⚠️ Hiba a weboldal lekérésekor: {resp.status}", ephemeral=True)
                return

            tests = data.get("tests", [])

            # Filter for exact case-sensitive match
            exact_match_tests = [t for t in tests if t.get("username", "") == name]

            # If no exact match, check if there's a similar name with different case
            if not exact_match_tests:
                similar = [t for t in tests if t.get("username", "").lower() == name.lower()]
                if similar:
                    similar_names = ", ".join([f"`{t.get('username')}`" for t in similar])
                    await interaction.followup.send(
                        f"❌ **{name}** nincs a tierlistán.\n\n"
                        f"Hasonló név(ek) talált: {similar_names}\n"
                        f"Kérlek írd be a pontos nevet (a nagybetűk számítanak)!",
                        ephemeral=True
                    )
                else:
                    await interaction.followup.send(
                        f"❌ **{name}** nincs a tierlistán.",
                        ephemeral=True
                    )
                return

            # Use exact match
            tests = exact_match_tests
            actual_username = tests[0].get("username", "")

            # Show info about the player (limit to 1500 chars to avoid embed limits)
            modes_info = "\n".join([f"• **{t.get('gamemode', '?')}**: {t.get('rank', '?')} ({t.get('points', 0)}pt)" for t in tests])
            if len(modes_info) > 1500:
                modes_info = modes_info[:1500] + "\n... (több is van)"

        # Create confirmation embed
        embed = discord.Embed(
            title="⚠️ FIGYELMEZTETÉS - Törlés előtt!",
            description=f"Biztosan eltávolítod **{name}**-t a tierlistáról?\n\n"
                       f"**Jelenlegi tierlist bejegyzések:**\n{modes_info}\n\n"
                       f"❗ **EZ EGY VÉGÉGES MŰVELET!** A játékos minden gamemód-beli eredménye törlésre kerül.",
            color=discord.Color.red()
        )
        embed.set_footer(text=f"Kéri: {interaction.user.display_name}")

        # Send confirmation view
        view = ConfirmRemoveView(username=name, actual_username=name, moderator=interaction.user)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    except aiohttp.ClientError as e:
        await interaction.followup.send(f"⚠️ Web hiba: {type(e).__name__}: {e}", ephemeral=True)
    except asyncio.TimeoutError:
        await interaction.followup.send("⚠️ Web timeout (nem válaszolt 10 mp-en belül).", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Hiba: {type(e).__name__}: {e}", ephemeral=True)


@app_commands.command(name="bulkimport", description="Bulk import test results from file (admin only)")
@app_commands.describe(
    file="Text file with test results (one per line: username mode rank)"
)
async def bulkimport(interaction: discord.Interaction, file: discord.Attachment):
    """Bulk import test results from a text file - format: username mode rank (one per line)"""
    await interaction.response.defer(ephemeral=True)

    # Check if admin
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("Nincs jogosultságod ehhez.", ephemeral=True)
        return

    if not WEBSITE_URL:
        await interaction.followup.send("⚠️ WEBSITE_URL nincs beállítva.", ephemeral=True)
        return

    # Read file content
    try:
        content = await file.read()
        data = content.decode('utf-8')
    except Exception as e:
        await interaction.followup.send(f"❌ Hiba a fájl olvasásakor: {e}", ephemeral=True)
        return

    lines = data.strip().split('\n')
    success_count = 0
    error_count = 0
    errors = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) < 3:
            error_count += 1
            errors.append(f"Invalid format: {line}")
            continue

        username = parts[0]
        mode = parts[1].lower()
        rank = parts[2].upper()

        # Get proper display name for mode
        mode_display = get_gamemode_display_name(mode)

        # Get tester (use bot as tester)
        tester = interaction.user

        try:
            save = await api_post_test(username=username, mode=mode_display, rank=rank, tester=tester)
            if save.get("status") in [200, 201]:
                success_count += 1
            else:
                error_count += 1
                errors.append(f"Failed: {username} {mode} {rank}")
        except Exception as e:
            error_count += 1
            errors.append(f"Error: {username} - {str(e)[:50]}")

    result_msg = f"✅ Sikeres import: {success_count}\n❌ Sikertelen: {error_count}"
    if errors:
        result_msg += "\n\nHibák:\n" + "\n".join(errors[:10])
        if len(errors) > 10:
            result_msg += f"\n... és még {len(errors) - 10} hiba"

    await interaction.followup.send(result_msg, ephemeral=True)


@app_commands.command(name="cooldown", description="Megnézed a cooldownidat egy játékmódban, vagy egy másik játékos cooldownját (staff).")
@app_commands.describe(
    user="Játékos (ha üres, a sajátodat nézed meg)"
)
async def cooldown(interaction: discord.Interaction, user: discord.User = None):
    await interaction.response.defer(ephemeral=True)

    try:
        member = interaction.user
        is_staff = False

        # Check if user is staff (for viewing others' cooldowns)
        if user is not None:
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                await interaction.followup.send("Hiba: Guild context szükséges más játékos cooldownjának megtekintéséhez.", ephemeral=True)
                return
            is_staff = is_staff_member(interaction.user)

            if not is_staff:
                await interaction.followup.send("Nincs jogosultságod más játékos cooldownjának megtekintéséhez.", ephemeral=True)
                return

            target_member = user
        else:
            # Check own cooldown - check if banned first
            target_member = member

        # Check if player is banned from testing
        if WEBSITE_URL:
            try:
                player_name = target_member.display_name
                if hasattr(target_member, 'nick') and target_member.nick:
                    player_name = target_member.nick

                url = f"{WEBSITE_URL}/api/tests/ban?username={player_name}"
                timeout = aiohttp.ClientTimeout(total=5)
                async with http_session.get(url, headers=_auth_headers(), timeout=timeout) as resp:
                    if resp.status == 200:
                        ban_data = await resp.json()
                        if ban_data.get("banned"):
                            reason = ban_data.get("reason", "")
                            await interaction.followup.send(
                                f"❌ **{player_name}** ki van tiltva a tesztelésből!\n" +
                                (f"**Ok:** {reason}" if reason else "Nincs megadva ok."),
                                ephemeral=True
                            )
                            return
            except Exception:
                pass  # If ban check fails, continue

        # Check local ban (bot-side)
        if is_player_banned(target_member.display_name):
            ban_info = get_ban_info(target_member.display_name)
            if ban_info:
                expires_at = ban_info.get("expires_at", 0)
                if expires_at == 0:
                    await interaction.followup.send(
                        f"❌ **{target_member.display_name}** örökre ki van tiltva a tesztelésből!\n"
                        f"**Ok:** {ban_info.get('reason', 'Nincs megadva')}",
                        ephemeral=True
                    )
                else:
                    from datetime import datetime
                    exp_date = datetime.fromtimestamp(expires_at)
                    await interaction.followup.send(
                        f"❌ **{target_member.display_name}** ki van tiltva!\n"
                        f"**Lejárat:** {exp_date.strftime('%Y-%m-%d %H:%M')}\n"
                        f"**Ok:** {ban_info.get('reason', 'Nincs megadva')}",
                        ephemeral=True
                    )
                return

        # Build cooldown info for all modes
        data = _load_data()
        cooldowns = data.get("cooldowns", {}).get(str(target_member.id), {})

        embed = discord.Embed(
            title=f"⏳ Cooldown info - {target_member.display_name}",
            color=discord.Color.blurple()
        )

        mode_cooldowns = []
        for label, mode_key, _ in TICKET_TYPES:
            last_closed = float(cooldowns.get(mode_key, 0))
            if last_closed <= 0:
                mode_cooldowns.append(f"✅ **{label}**: Nincs cooldown")
            else:
                left = int((last_closed + COOLDOWN_SECONDS) - time.time())
                if left <= 0:
                    mode_cooldowns.append(f"✅ **{label}**: Kész vagy, már nyithatsz ticketet!")
                else:
                    days = left // (24 * 3600)
                    hours = (left % (24 * 3600)) // 3600
                    minutes = (left % 3600) // 60

                    if days > 0:
                        time_str = f"{days} nap {hours} óra"
                    elif hours > 0:
                        time_str = f"{hours} óra {minutes} perc"
                    else:
                        time_str = f"{minutes} perc"
                    mode_cooldowns.append(f"⏳ **{label}**: {time_str}")

        # Add global cooldown info
        global_last = data.get("cooldowns", {}).get(str(target_member.id), {}).get("_global", 0)
        if global_last > 0:
            left = int((global_last + COOLDOWN_SECONDS) - time.time())
            if left > 0:
                days = left // (24 * 3600)
                hours = (left % (24 * 3600)) // 3600
                mode_cooldowns.append(f"\n🌐 **Globális cooldown**: {days} nap {hours} óra")

        embed.description = "\n".join(mode_cooldowns)
        embed.set_footer(text=f"Cooldown időtartam: 30 nap")

        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Hiba: {type(e).__name__}: {e}", ephemeral=True)


@app_commands.command(name="link", description="Összekapcsolod a Minecraft fiókodat a Discord fiókoddal.")
@app_commands.describe(
    code="A Minecraftban kapott összekapcsolási kód (opcionális, ha még nincs kódod)"
)
async def link(interaction: discord.Interaction, code: str = None):
    await interaction.response.defer(ephemeral=True)

    # If no code provided (or empty), or if code doesn't belong to user, generate a new one
    code_valid = False
    if code and code != "":
        code_valid = await validate_link_code_for_user(interaction.user.id, code)

    if code is None or code == "" or not code_valid:
        try:
            # Check if user is already linked (try async first, then sync fallback)
            existing_link = get_linked_minecraft_name(interaction.user.id)
            if existing_link:
                description = f"**Minecraft:** `{existing_link}`\n**Discord:** {interaction.user.mention}\n\nA kettős fiók már össze van kapcsolva!"

                embed = discord.Embed(
                    title="⚠️ Már össze van kapcsolva!",
                    description=description,
                    color=discord.Color.orange()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            # Check if user already has a pending code - if so, remove it and generate new one
            existing_code = await get_pending_link_code_async(interaction.user.id)
            if existing_code:
                embed = discord.Embed(
                    title="⏳ Már van egy kódod!",
                    description=f"A meglévő kódod: `{existing_code}`\n\n"
                               f"**Minecraft szerver:** `45.140.164.183:25942`\n"
                               f"Ezt használd: `/link {existing_code}` a Minecraftban!\n"
                               f"Vagy várd meg amíg lejár és generálj újat.",
                    color=discord.Color.orange()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            # Generate new code
            new_code = await generate_link_code_async(interaction.user.id)

            # Send code via DM
            try:
                await interaction.user.send(
                    f"🎮 **Összekapcsolási kód:** `{new_code}`\n\n"
                    f"**Minecraft szerver:** `45.140.164.183:25942`\n"
                    f"Írd be a Minecraftban: `/link {new_code}`\n"
                    f"A kód {LINK_CODE_EXPIRY_MINUTES} percig érvényes."
                )
                dm_sent = True
            except:
                dm_sent = False

            embed = discord.Embed(
                title="✅ Kód generálva!",
                description=f"```\n{new_code}\n```\n"
                           f"**Minecraft szerver:** `45.140.164.183:25942`\n"
                           f"Írd be a Minecraftban: `/link {new_code}`\n"
                           f"A kód **{LINK_CODE_EXPIRY_MINUTES} percig** érvényes.",
                color=discord.Color.green()
            )
            if dm_sent:
                embed.add_field(
                    name="📬 DM elküldve!",
                    value="A kódot elküldtem privát üzenetben is!",
                    inline=False
                )
            else:
                embed.add_field(
                    name="⚠️ DM nem sikerült",
                    value="A kód itt látható, másold ki!",
                    inline=False
                )

            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        except Exception as e:
            # Log the error for debugging
            print(f"[LINK ERROR] {type(e).__name__}: {e}")
            await interaction.followup.send(
                f"❌ Hiba történt. Kérlek, próbáld újra!\n"
                f"Ha a hiba továbbra is fennáll, jelentsd a hibát.",
                ephemeral=True
            )
            return

    # If code IS provided and valid - show success!
    if code_valid:
        linked_name = get_linked_minecraft_name(interaction.user.id)
        embed = discord.Embed(
            title="✅ Fiók összekapcsolva!",
            description=f"**Minecraft:** `{linked_name}`\n"
                       f"**Discord:** {interaction.user.mention}\n\n"
                       f"A fiókok sikeresen össze lettek kapcsolva!",
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Code was provided but is invalid
    await interaction.followup.send(
        "❌ Érvénytelen kód!\n"
        f"Használd `/link` parancsot új kód generálásához.",
        ephemeral=True
    )


@app_commands.command(name="unlink", description="Leválasztod a Minecraft fiókodat a Discord fiókodról.")
async def unlink(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        # Check if linked
        existing = get_linked_minecraft_name(interaction.user.id)
        if not existing:
            await interaction.followup.send(
                "❌ Nincs összekapcsolva Minecraft fiók!\n"
                "Használd: `/link <név>` hogy összekapcsold.",
                ephemeral=True
            )
            return

        # Unlink
        unlink_minecraft_account(interaction.user.id)

        embed = discord.Embed(
            title="✅ Sikeres leválasztás!",
            description=f"A Minecraft fiókod (**{existing}**) le lett választva a Discord fiókodról.",
            color=discord.Color.green()
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Hiba: {type(e).__name__}: {e}", ephemeral=True)


@app_commands.command(name="mylink", description="Megnézed az összekapcsolt Minecraft fiókodat.")
async def mylink(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        linked = get_linked_minecraft_name(interaction.user.id)

        if not linked:
            await interaction.followup.send(
                "❌ Nincs összekapcsolva Minecraft fiók!\n"
                "Használd: `/link <név>` hogy összekapcsold.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            description=f"**Discord:** {interaction.user.mention}\n"
                       f"**Minecraft:** {linked}",
            color=discord.Color.blurple()
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Hiba: {type(e).__name__}: {e}", ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    try:
        error_msg = f"❌ Parancs hiba: {type(error).__name__}: {error}"

        # If already responded, use followup, else normal response
        if interaction.response.is_done():
            await interaction.followup.send(error_msg, ephemeral=True)
        else:
            await interaction.response.send_message(error_msg, ephemeral=True)
    except Exception:
        pass


# =========================
# SETUP / EVENTS
# =========================
async def wipe_global_commands_once():
    try:
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print("Global commands wiped.")
    except Exception as e:
        print("Failed to wipe global commands:", e)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")

    # Register persistent views only once (avoid duplicates on reconnect)
    if not hasattr(bot, '_persistent_views_added'):
        bot.add_view(TicketPanelView())
        bot.add_view(CloseTicketView(owner_id=0, mode_key=""))
        bot.add_view(QueuePanelView())
        bot.add_view(QueueActionView())  # Generic instance; gamemode derived from message
        bot.add_view(PingPanelView())
        bot._persistent_views_added = True

    # Load persisted queue panel message ID
    global QUEUE_PANEL_MESSAGE
    try:
        data = _load_data()
        panel_data = data.get("queue_panel_message")
        if panel_data and isinstance(panel_data, list) and len(panel_data) == 2:
            QUEUE_PANEL_MESSAGE = (panel_data[0], panel_data[1])
    except Exception as e:
        print(f"Error loading queue panel message: {e}")

    # Load persisted queue message IDs
    global QUEUE_MESSAGE_IDS
    try:
        data = _load_data()
        raw_ids = data.get("queue_message_ids", [])
        loaded = {}
        for entry in raw_ids:
            if isinstance(entry, list) and len(entry) == 2:
                try:
                    msg_id = int(entry[0])
                    gamemode = entry[1]
                    loaded[msg_id] = gamemode
                except (ValueError, TypeError):
                    continue
        QUEUE_MESSAGE_IDS = loaded
    except Exception as e:
        print(f"Error loading queue message IDs: {e}")

    # Rebuild queue message ID mapping after restart (in case persisted data is stale)
    if GUILD_ID:
        guild = bot.get_guild(GUILD_ID)
        if guild:
            await rebuild_queue_message_ids(guild)
            # After rebuilding, persist the fresh mapping
            _persist_queue_message_ids()

    guild = discord.Object(id=GUILD_ID) if GUILD_ID else None

    # Sync commands
    try:
        # Print all commands registered
        print(f"Registered commands: {len(bot.tree._global_commands)} global, {len(bot.tree._guild_commands)} guild")
        
        if guild:
            await bot.tree.sync(guild=guild)
            print(f"Slash commands synced to guild {GUILD_ID}")
        else:
            await bot.tree.sync()
            print("Slash commands synced globally")
    except Exception as e:
        import traceback
        print("Sync failed:", e)
        traceback.print_exc()


async def main():
    global http_session

    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing")

    # Initialize database
    await init_db()

    # Initialize http_session BEFORE starting health server
    http_session = aiohttp.ClientSession()

    # health server - must start AFTER http_session is ready
    asyncio.create_task(start_health_server())

    # queue maintenance task
    asyncio.create_task(queue_maintenance_task())

    # register commands - Use guild commands only (faster sync, avoids duplicates)
    if GUILD_ID:
        g = discord.Object(id=GUILD_ID)
        bot.tree.add_command(ticketpanel, guild=g)
        bot.tree.add_command(testresult, guild=g)
        bot.tree.add_command(tierlistnamechange, guild=g)
        bot.tree.add_command(profile, guild=g)
        bot.tree.add_command(spin, guild=g)
        bot.tree.add_command(retire, guild=g)
        bot.tree.add_command(unretire, guild=g)
        bot.tree.add_command(tierlistban, guild=g)
        bot.tree.add_command(tierlistunban, guild=g)
        bot.tree.add_command(removetierlist, guild=g)
        bot.tree.add_command(cooldown, guild=g)
        bot.tree.add_command(bulkimport, guild=g)
        bot.tree.add_command(queuepanel, guild=g)
        bot.tree.add_command(pingpanel, guild=g)
        bot.tree.add_command(closequeue, guild=g)
        bot.tree.add_command(link, guild=g)
        bot.tree.add_command(unlink, guild=g)
        bot.tree.add_command(mylink, guild=g)
    else:
        # Only register as global if no GUILD_ID
        bot.tree.add_command(ticketpanel)
        bot.tree.add_command(testresult)
        bot.tree.add_command(tierlistnamechange)
        bot.tree.add_command(profile)
        bot.tree.add_command(spin)
        bot.tree.add_command(retire)
        bot.tree.add_command(unretire)
        bot.tree.add_command(tierlistban)
        bot.tree.add_command(tierlistunban)
        bot.tree.add_command(removetierlist)
        bot.tree.add_command(cooldown)
        bot.tree.add_command(bulkimport)
        bot.tree.add_command(queuepanel)
        bot.tree.add_command(pingpanel)
        bot.tree.add_command(closequeue)
        bot.tree.add_command(link)
        bot.tree.add_command(unlink)
        bot.tree.add_command(mylink)

    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        if http_session:
            await http_session.close()
        await close_db()


async def db_upsert_test(username: str, mode: str, rank: str, tester_id: str, tester_name: str, ts: int) -> bool:
    """Upsert test using direct PostgreSQL connection (fallback when Supabase unavailable)"""
    global db_pool
    if not db_pool:
        return False
    try:
        async with db_pool.acquire() as conn:
            query = """
            INSERT INTO tests (username, mode, rank, "testerId", "testerName", ts)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (username, mode) DO UPDATE SET
                rank = EXCLUDED.rank,
                "testerId" = EXCLUDED."testerId",
                "testerName" = EXCLUDED."testerName",
                ts = EXCLUDED.ts
            """
            await conn.execute(query, username, mode, rank, tester_id, tester_name, ts)
            return True
    except Exception as e:
        print(f"DB upsert error: {e}")
        return False


async def db_delete_test(test_id: str) -> bool:
    """Delete test by ID using direct PostgreSQL connection"""
    global db_pool
    if not db_pool:
        return False
    try:
        async with db_pool.acquire() as conn:
            await conn.execute('DELETE FROM tests WHERE id = $1', int(test_id))
            return True
    except Exception as e:
        print(f"DB delete error: {e}")
        return False


if __name__ == "__main__":
    asyncio.run(main())