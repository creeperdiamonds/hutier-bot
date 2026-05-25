from typing import Optional
import discord

from config import (
    DEBUG_ALLOWED_USERS, DEBUG_ALLOWED_ROLES,
    ALLOWED_USER_IDS, STAFF_ROLE_ID, EXTRA_STAFF_ROLE_IDS,
    TICKET_TYPES, POINTS, WEBSITE_URL,
)


# PERMISSIONS
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
        from api import api_get_tests
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
