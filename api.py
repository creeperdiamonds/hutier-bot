import asyncio
import time
from typing import Dict, Any, Optional

import aiohttp
import discord

from config import WEBSITE_URL, BOT_API_KEY, HTTP_TIMEOUT_SECONDS
from config import get_gamemode_display_name
from database import db_pool, USE_SUPABASE_API, supabase_upsert, db_upsert_test

# Module-level http session — set by main before bot starts
http_session: Optional[aiohttp.ClientSession] = None


# WEBSITE API
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


async def api_post_test(username: str, mode: str, rank: str, tester: discord.Member, account_type: str = None) -> Dict[str, Any]:
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
            ts=int(time.time()),
            account_type=account_type
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
            "accountType": account_type,
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
                        "accountType": account_type,
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
        "accountType": account_type,
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


async def api_get_player_tiers(username: str) -> Dict[str, Any]:
    """Get all tier results for a player across every gamemode. Returns {mode: rank} dict."""
    from database import USE_SUPABASE_API, supabase_select
    from config import POINTS

    if USE_SUPABASE_API:
        try:
            rows = await supabase_select("tests", {"username": username})
            if rows is not None:
                tiers = {r["mode"]: r["rank"] for r in rows if "mode" in r and "rank" in r}
                best = max(tiers.values(), key=lambda r: POINTS.get(r, 0), default="Unranked") if tiers else "Unranked"
                best_mode = next((m for m, r in tiers.items() if r == best), None)
                return {"status": 200, "data": {"tiers": tiers, "highest": best, "highest_gamemode": best_mode}}
        except Exception as e:
            print(f"[API] Supabase player tiers error: {e}")

    if db_pool is not None:
        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT mode, rank FROM tests WHERE LOWER(username) = LOWER($1)", username
                )
                tiers = {r["mode"]: r["rank"] for r in rows}
                best = max(tiers.values(), key=lambda r: POINTS.get(r, 0), default="Unranked") if tiers else "Unranked"
                best_mode = next((m for m, r in tiers.items() if r == best), None)
                return {"status": 200, "data": {"tiers": tiers, "highest": best, "highest_gamemode": best_mode}}
        except Exception as e:
            print(f"[API] DB player tiers error: {e}")

    return {"status": 404, "data": {"error": "No database configured"}}


async def api_get_gamemode_leaderboard(gamemode: str) -> Dict[str, Any]:
    """Get all players ranked in a specific gamemode, sorted by rank."""
    from database import USE_SUPABASE_API, supabase_select
    from config import POINTS

    mode_display = get_gamemode_display_name(gamemode)

    if USE_SUPABASE_API:
        try:
            rows = await supabase_select("tests", {"mode": mode_display})
            if rows is not None:
                players = sorted(
                    [{"username": r["username"], "rank": r["rank"]} for r in rows if "username" in r],
                    key=lambda p: POINTS.get(p["rank"], 0),
                    reverse=True,
                )
                return {"status": 200, "data": {"gamemode": mode_display, "players": players}}
        except Exception as e:
            print(f"[API] Supabase leaderboard error: {e}")

    if db_pool is not None:
        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT username, rank FROM tests WHERE LOWER(mode) = LOWER($1)", mode_display
                )
                players = [{"username": r["username"], "rank": r["rank"]} for r in rows]
                return {"status": 200, "data": {"gamemode": mode_display, "players": players}}
        except Exception as e:
            print(f"[API] DB leaderboard error: {e}")

    return {"status": 404, "data": {"error": "No database configured"}}


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


async def autocomplete_testresult_username(interaction: discord.Interaction, current: str) -> list:
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
            from discord import app_commands
            matches = [u for u in usernames if current.lower() in u.lower()]
            return [app_commands.Choice(name=u, value=u) for u in matches[:25]]
    except Exception:
        return []
