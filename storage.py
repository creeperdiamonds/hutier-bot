import os
import json
import time
import random
import string
import asyncio
import datetime
from typing import Dict, Any, List, Optional

from config import DATA_FILE, COOLDOWN_SECONDS, LINK_CODE_LENGTH, LINK_CODE_EXPIRY_MINUTES
from database import (
    USE_SUPABASE_API, db_pool,
    supabase_select, supabase_insert, supabase_upsert, supabase_delete, supabase_update,
    supabase_select_sync, supabase_insert_sync,
)

# LINK_CODE_LENGTH and LINK_CODE_EXPIRY_MINUTES imported from config above


# STORAGE
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


def persist_queue_message_ids(queue_message_ids: dict):
    """Save queue_message_ids to data.json"""
    try:
        data = _load_data()
        # Convert to list of [msg_id, gamemode] for JSON
        ids_list = [[k, v] for k, v in queue_message_ids.items()]
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


# LINK SYSTEM (Discord -> Minecraft Account Linking) - Database Version

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


async def get_all_linked_accounts_async() -> List[Dict[str, Any]]:
    """Return all linked accounts as a list of {discord_id: int, minecraft_name: str}."""
    if USE_SUPABASE_API:
        try:
            rows = await supabase_select("linked_accounts")
            return [{"discord_id": int(r["discord_id"]), "minecraft_name": r["minecraft_name"]} for r in rows]
        except Exception as e:
            print(f"Error fetching all linked accounts from Supabase: {e}")

    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch("SELECT discord_id, minecraft_name FROM linked_accounts")
                return [{"discord_id": r["discord_id"], "minecraft_name": r["minecraft_name"]} for r in rows]
        except Exception as e:
            print(f"Error fetching all linked accounts from DB: {e}")

    data = _load_link_data()
    return [{"discord_id": int(k), "minecraft_name": v} for k, v in data.items()]


# PENDING CODES - Database versions

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


# ACTIVE SESSIONS (in-memory, per-gamemode)
_ACTIVE_SESSIONS: dict = {}


def set_active_session(gamemode: str, tester_id: int, player_id: int, channel_id: int) -> None:
    _ACTIVE_SESSIONS[gamemode] = {
        "tester_id": tester_id,
        "player_id": player_id,
        "channel_id": channel_id,
    }


def get_active_session(gamemode: str) -> Optional[dict]:
    return _ACTIVE_SESSIONS.get(gamemode)


def clear_active_session(gamemode: str) -> None:
    _ACTIVE_SESSIONS.pop(gamemode, None)


# QUEUE LAST SESSION (in-memory, per-gamemode)
_QUEUE_LAST_SESSIONS: dict = {}


def save_queue_last_session(gamemode: str) -> None:
    import datetime
    _QUEUE_LAST_SESSIONS[gamemode] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


def get_queue_last_session(gamemode: str) -> Optional[str]:
    return _QUEUE_LAST_SESSIONS.get(gamemode)


# BAN SYSTEM
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
