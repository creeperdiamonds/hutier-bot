import os
import discord
from typing import List

# UTILITY

def truncate_message(text: str, max_length: int = 1900) -> str:
    """Truncate a message to fit Discord's 2000 character limit with safety margin"""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."


# ENV / CONFIG
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

LINK_CODE_LENGTH = 8  # 6-8 characters
LINK_CODE_EXPIRY_MINUTES = 10

# Tier role IDs per gamemode — set TIER_ROLE_<GAMEMODE>_<RANK> env vars
# E.g. TIER_ROLE_SWORD_LT3=123456789  →  players who achieve LT3 in Sword get @sword-LT3 role
# Structure: { "gamemode": { "rank": role_id } }
GAMEMODE_TIER_ROLES: dict[str, dict[str, int]] = {}

# Dynamically load tier roles from env vars for each gamemode
# Format: TIER_ROLE_<GAMEMODE>_<RANK>=role_id
# Example: TIER_ROLE_SWORD_HT1=123456789, TIER_ROLE_NETHPOT_LT2=987654321
RANKS_FOR_ENV = ["Unranked", "LT5", "HT5", "LT4", "HT4", "LT3", "HT3", "LT2", "HT2", "LT1", "HT1"]
ALL_GAMEMODES = [
    "sword", "axe", "mace", "uhc", "pot", "nethpot", "smp", "vanilla",
    "creeper", "cart", "diasmp", "ogvanilla", "shieldlessuhc",
    "spearmace", "spearelytra", "diapot", "SMP", "crystal"
]

for gamemode in ALL_GAMEMODES:
    GAMEMODE_TIER_ROLES[gamemode] = {}
    for rank in RANKS_FOR_ENV:
        env_key = f"TIER_ROLE_{gamemode.upper()}_{rank.upper()}"
        role_id = int(os.getenv(env_key, "0"))
        if role_id != 0:
            GAMEMODE_TIER_ROLES[gamemode][rank] = role_id

# Legacy support: Global tier roles (if no gamemode-specific ones exist)
TIER_ROLES: dict[str, int] = {
    "Unranked": int(os.getenv("TIER_ROLE_UNRANKED", "0")),
    "LT5":      int(os.getenv("TIER_ROLE_LT5",      "0")),
    "HT5":      int(os.getenv("TIER_ROLE_HT5",      "0")),
    "LT4":      int(os.getenv("TIER_ROLE_LT4",      "0")),
    "HT4":      int(os.getenv("TIER_ROLE_HT4",      "0")),
    "LT3":      int(os.getenv("TIER_ROLE_LT3",      "0")),
    "HT3":      int(os.getenv("TIER_ROLE_HT3",      "0")),
    "LT2":      int(os.getenv("TIER_ROLE_LT2",      "0")),
    "HT2":      int(os.getenv("TIER_ROLE_HT2",      "0")),
    "LT1":      int(os.getenv("TIER_ROLE_LT1",      "0")),
    "HT1":      int(os.getenv("TIER_ROLE_HT1",      "0")),
}


def get_tier_role(gamemode: str, rank: str) -> int:
    """Get tier role ID for a specific gamemode and rank.

    First checks gamemode-specific roles, then falls back to global roles.
    Returns 0 if not configured.
    """
    # Try gamemode-specific first
    if gamemode in GAMEMODE_TIER_ROLES and rank in GAMEMODE_TIER_ROLES[gamemode]:
        return GAMEMODE_TIER_ROLES[gamemode][rank]

    # Fall back to global
    if rank in TIER_ROLES:
        return TIER_ROLES[rank]

    return 0


# CONSTANTS
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
    ("SMP", "SMP", 0),
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
    "SMP": ("FT4", "FT3", "FT2"),  # FT2 if lose round
    "crystal": ("FT4", "FT3", None),
}


def get_ticket_rounds_display(mode_key: str) -> str:
    """Get the display string for required rounds based on gamemode"""
    rounds = TICKET_ROUNDS.get(mode_key.lower())
    if not rounds:
        return "FT4"

    default_ft, lt3_ft, loss_ft = rounds

    if loss_ft:
        return f"{default_ft}, below LT3: {lt3_ft}, if you lose a round against the tester: {loss_ft}"
    else:
        return f"{default_ft}, below LT3: {lt3_ft}"


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
    "SMP": "SMP",
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
    "SMP": 0x8b0000,      # Dark Red (nether)
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
    "SMP": "🟥",
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


# QUEUE CHANNELS
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
    "SMP": 0,
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
    "SMP": 0,
    "crystal": 0,
}

# Category where ticket channels will be created
TICKET_CREATE_CATEGORY_ID = 1495038336744689674


def _choices_from_list(values):
    from discord import app_commands
    return [app_commands.Choice(name=v, value=v) for v in values]
