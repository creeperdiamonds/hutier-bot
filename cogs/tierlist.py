import asyncio
import os
import time
from datetime import datetime
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from config import (
    TICKET_TYPES, RANKS, POINTS, MODE_LIST,
    WEBSITE_URL, HTTP_TIMEOUT_SECONDS,
    TIER_ROLES,
    get_gamemode_display_name,
    _choices_from_list,
    truncate_message,
)
from storage import (
    get_linked_minecraft_name,
    get_linked_minecraft_name_async,
    get_discord_by_minecraft_async,
    get_all_linked_accounts_async,
    generate_link_code_async,
    get_pending_link_code_async,
    validate_link_code_for_user,
    unlink_minecraft_account,
    is_player_banned, get_ban_info, ban_player, unban_player,
    _load_data,
    cooldown_left, set_last_closed,
    LINK_CODE_EXPIRY_MINUTES,
)
from permissions import is_staff_member, get_player_rank_for_mode, can_open_ticket
from api import (
    api_get_tests, api_post_test, api_rename_player,
    api_set_ban, api_remove_player,
    autocomplete_testresult_username,
    http_session as _http_session, _auth_headers,
)
from database import USE_SUPABASE_API, db_pool, supabase_delete, supabase_update, db_delete_test


# UI VIEWS (module level)

class ConfirmRemoveView(discord.ui.View):
    def __init__(self, username: str, actual_username: str, moderator: discord.Member):
        super().__init__(timeout=60)
        self.username = username  # What the user typed
        self.actual_username = actual_username  # What's in the database
        self.moderator = moderator
        self.confirmed = False

    @discord.ui.button(label="Yes, delete", style=discord.ButtonStyle.danger, custom_id="confirm_remove_yes")
    async def confirm_yes(self, interaction: discord.Interaction, _button: discord.ui.Button):
        # Only the moderator who started the command can confirm
        if interaction.user.id != self.moderator.id:
            await interaction.response.send_message("Only the command invoker can confirm.", ephemeral=True)
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
                desc = f"**{self.username}** has been successfully removed from the tierlist.\nMode: {modes}"
                if details:
                    if len(desc) + len(details) > 1500:
                        details = details[:1500 - len(desc)] + "..."
                    desc += f"\n{details}"

                embed = discord.Embed(
                    title="✅ Player Removed from Tierlist",
                    description=desc,
                    color=discord.Color.green()
                )
                embed.set_footer(text=f"Moderator: {self.moderator.display_name}")

                await interaction.followup.send(embed=embed)
            else:
                error_msg = data.get("error", "Unknown error")
                # Truncate error_msg to avoid Discord's 2000 character limit
                error_msg_str = truncate_message(str(error_msg), 1500)
                await interaction.followup.send(
                    f"❌ Error during deletion: {error_msg_str}",
                    ephemeral=True
                )
        except Exception as e:
            await interaction.followup.send(
                f"❌ Error: {type(e).__name__}: {e}",
                ephemeral=True
            )

        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="confirm_remove_no")
    async def confirm_no(self, interaction: discord.Interaction, _button: discord.ui.Button):
        # Only the moderator who started the command can cancel
        if interaction.user.id != self.moderator.id:
            await interaction.response.send_message("Only the command invoker can cancel.", ephemeral=True)
            return

        await interaction.response.send_message("❌ Deletion cancelled.", ephemeral=True)
        self.stop()


TRANSLATE_LANGUAGES = [
    app_commands.Choice(name="English", value="en"),
    app_commands.Choice(name="Spanish", value="es"),
    app_commands.Choice(name="French", value="fr"),
    app_commands.Choice(name="German", value="de"),
    app_commands.Choice(name="Italian", value="it"),
    app_commands.Choice(name="Portuguese", value="pt"),
    app_commands.Choice(name="Russian", value="ru"),
    app_commands.Choice(name="Japanese", value="ja"),
    app_commands.Choice(name="Korean", value="ko"),
    app_commands.Choice(name="Chinese (Simplified)", value="zh-CN"),
    app_commands.Choice(name="Chinese (Traditional)", value="zh-TW"),
    app_commands.Choice(name="Arabic", value="ar"),
    app_commands.Choice(name="Hindi", value="hi"),
    app_commands.Choice(name="Dutch", value="nl"),
    app_commands.Choice(name="Polish", value="pl"),
    app_commands.Choice(name="Turkish", value="tr"),
    app_commands.Choice(name="Swedish", value="sv"),
    app_commands.Choice(name="Hungarian", value="hu"),
    app_commands.Choice(name="Romanian", value="ro"),
    app_commands.Choice(name="Czech", value="cs"),
    app_commands.Choice(name="Greek", value="el"),
    app_commands.Choice(name="Ukrainian", value="uk"),
    app_commands.Choice(name="Indonesian", value="id"),
    app_commands.Choice(name="Vietnamese", value="vi"),
    app_commands.Choice(name="Thai", value="th"),
]

_LANG_NAMES = {c.value: c.name for c in TRANSLATE_LANGUAGES}


async def _google_translate(text: str, target: str) -> str:
    import urllib.parse
    url = (
        "https://translate.googleapis.com/translate_a/single"
        f"?client=gtx&sl=auto&tl={target}&dt=t&q={urllib.parse.quote(text)}"
    )
    import api as api_module
    session = api_module.http_session
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        data = await resp.json(content_type=None)
    return "".join(part[0] for part in data[0] if part[0])


# TIER TAGGER HELPERS

_ALL_TIER_ROLE_IDS: set[int] = set()


def _refresh_tier_role_ids() -> None:
    """Rebuild the cached set of all tier role IDs (called once at startup)."""
    _ALL_TIER_ROLE_IDS.clear()
    _ALL_TIER_ROLE_IDS.update(rid for rid in TIER_ROLES.values() if rid)


async def _apply_tier_role(member: discord.Member, new_rank: str) -> str:
    """
    Remove every tier role the member currently holds, then assign the role
    that corresponds to new_rank. Returns a human-readable status string.
    """
    if not _ALL_TIER_ROLE_IDS:
        return "⚠️ No tier roles are configured (set TIER_ROLE_<RANK> env vars)."

    to_remove = [r for r in member.roles if r.id in _ALL_TIER_ROLE_IDS]
    if to_remove:
        try:
            await member.remove_roles(*to_remove, reason="Tier tagger: rank update")
        except discord.Forbidden:
            return "❌ Missing permissions to remove tier roles."

    new_role_id = TIER_ROLES.get(new_rank, 0)
    if not new_role_id:
        return f"⚠️ No role configured for **{new_rank}** — role not assigned."

    new_role = member.guild.get_role(new_role_id)
    if not new_role:
        return f"⚠️ Role ID {new_role_id} for **{new_rank}** not found in server."

    try:
        await member.add_roles(new_role, reason=f"Tier tagger: {new_rank}")
    except discord.Forbidden:
        return "❌ Missing permissions to assign tier role."

    return f"✅ Tagged as **{new_rank}** ({new_role.name})"


# COG

class TierlistCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        _refresh_tier_role_ids()

    @app_commands.command(name="testresult", description="Record a Minecraft tier test result and save to the website.")
    @app_commands.describe(
        username="Minecraft name (used for the skin on the website).",
        tester="Tester (Discord user).",
        gamemode="Gamemode.",
        rank="Achieved rank (e.g. LT3 / HT3)."
    )
    @app_commands.autocomplete(username=autocomplete_testresult_username)
    @app_commands.choices(
        gamemode=_choices_from_list(MODE_LIST),
        rank=_choices_from_list(RANKS)
    )
    async def testresult(
        self,
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
                await interaction.followup.send("Error.", ephemeral=True)
                return
            if not is_staff_member(interaction.user):
                await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
                return
            if interaction.channel is None:
                await interaction.followup.send("Error: no channel.", ephemeral=True)
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
                title=f"{display_username}'s Test Result 🏆",
                color=discord.Color.dark_grey()
            )
            embed.set_thumbnail(url=skin_url)
            embed.add_field(name="Tester:", value=tester.mention, inline=False)
            embed.add_field(name="Gamemode:", value=display_mode, inline=False)
            embed.add_field(name="Minecraft Name:", value=display_username, inline=False)
            embed.add_field(name="Previous Rank:", value=display_prev_rank, inline=False)
            embed.add_field(name="Achieved Rank:", value=display_rank_val, inline=False)

            # Only send to the results channel (eredmenyek), not the command channel
            # ALWAYS save to website first (UPsert)
            if not WEBSITE_URL:
                await interaction.followup.send("⚠️ WEBSITE_URL is not set, cannot save to website.", ephemeral=True)
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

            # Tier tagger: find and tag the tested player
            tag_status = ""
            if _ALL_TIER_ROLE_IDS and save_ok:
                try:
                    discord_id = await get_discord_by_minecraft_async(username)
                    if discord_id:
                        target_member = interaction.guild.get_member(discord_id)
                        if target_member:
                            tag_status = await _apply_tier_role(target_member, rank_val)
                        else:
                            tag_status = f"⚠️ Player not in server (Discord ID: {discord_id})"
                    else:
                        tag_status = "⚠️ Minecraft account not linked — role not assigned."
                except Exception as e:
                    tag_status = f"⚠️ Tier tagger error: {e}"

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
                    msg = (
                        f"✅ Result saved!\nPrevious: **{prev_rank}** → Achieved: **{rank_val}** | "
                        f"{'+' if diff>=0 else ''}{diff} pts"
                    )
                    if tag_status:
                        msg += f"\n{tag_status}"
                    await interaction.followup.send(msg, ephemeral=True)
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
                msg = (
                    f"✅ Saved + website updated.\nPrevious: **{prev_rank}** → Achieved: **{rank_val}** | "
                    f"{'+' if diff>=0 else ''}{diff} pts"
                )
                if tag_status:
                    msg += f"\n{tag_status}"
                await interaction.followup.send(msg, ephemeral=True)
            else:
                # Truncate save_data to avoid Discord's 2000 character limit
                save_data_str = truncate_message(str(save_data), 1500)
                await interaction.followup.send(
                    f"⚠️ Save error towards website (status {save_status}) | {save_data_str}",
                    ephemeral=True
                )

        except aiohttp.ClientError as e:
            await interaction.followup.send(f"⚠️ Website error: {type(e).__name__}: {e}", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("⚠️ Website timeout (no response within 10 seconds).", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("❌ Cannot send messages/embeds here (Missing Permissions).", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="tierlistnamechange", description="Change a player's name on the tierlist (admin only).")
    @app_commands.describe(
        oldname="The current name on the tierlist.",
        newname="The new name to appear on the tierlist."
    )
    async def tierlistnamechange(self, interaction: discord.Interaction, oldname: str, newname: str):
        await interaction.response.defer(ephemeral=True)

        try:
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                await interaction.followup.send("Error.", ephemeral=True)
                return
            if not is_staff_member(interaction.user):
                await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
                return

            # Call the website API to rename the player
            if not WEBSITE_URL:
                await interaction.followup.send("⚠️ WEBSITE_URL is not set.", ephemeral=True)
                return

            # Pre-delete any conflicting tests for newname in modes that oldname has
            import api as api_module
            try:
                # Fetch old player's tests to get their modes
                old_tests_url = f"{WEBSITE_URL}/api/tests?username={oldname}"
                async with api_module.http_session.get(old_tests_url, headers=_auth_headers(), timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)) as old_resp:
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
                    async with api_module.http_session.get(new_tests_url, headers=_auth_headers(), timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)) as new_resp:
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
                                                async with api_module.http_session.delete(del_url, headers=_auth_headers(), timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)) as d_resp:
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

                msg = f"✅ Successfully renamed: **{oldname}** → **{newname}**\nUpdated: {updated_count} entries (all gamemodes)"

                await interaction.followup.send(msg, ephemeral=True)
            elif status == 404:
                await interaction.followup.send(
                    f"❌ Player not found: **{oldname}**",
                    ephemeral=True
                )
            elif status == 401 or status == 403:
                await interaction.followup.send(
                    "❌ You don't have permission to use this command.",
                    ephemeral=True
                )
            else:
                # Truncate data to avoid Discord's 2000 character limit
                data_str = truncate_message(str(data), 1500)
                await interaction.followup.send(
                    f"⚠️ Error (status {status}): {data_str}",
                    ephemeral=True
                )

        except aiohttp.ClientError as e:
            await interaction.followup.send(f"⚠️ Website error: {type(e).__name__}: {e}", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("⚠️ Website timeout (no response within 10 seconds).", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="profile", description="View a player's tiers from the tierlist.")
    @app_commands.describe(
        name="The player's name on the tierlist."
    )
    async def profile(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=False)

        try:
            if not WEBSITE_URL:
                await interaction.followup.send("⚠️ WEBSITE_URL is not set.", ephemeral=True)
                return

            import api as api_module
            # Use the new API endpoint that supports filtering by username only
            url = f"{WEBSITE_URL}/api/tests?username={name}"
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
            async with api_module.http_session.get(url, headers=_auth_headers(), timeout=timeout) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    data = {}

                if resp.status != 200:
                    await interaction.followup.send(f"⚠️ Error fetching from website: {resp.status}", ephemeral=True)
                    return

                tests = data.get("tests", [])

                if not tests:
                    await interaction.followup.send(f"❌ No results found for: **{name}**", ephemeral=False)
                    return

                # Get global rank by fetching all tests and sorting
                all_url = f"{WEBSITE_URL}/api/tests"
                async with api_module.http_session.get(all_url, headers=_auth_headers(), timeout=timeout) as all_resp:
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

                    for idx, (nm, pts) in enumerate(sorted_players, 1):
                        if nm == player_username:
                            global_rank = idx
                            break

                # Build embed
                display_name = tests[0].get('username', name)

                embed = discord.Embed(
                    title=f"{display_name}'s Profile",
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
                rank_info = f"**Total Points:** {total_points}"
                if global_rank:
                    rank_info += f"\n**Global Rank:** #{global_rank}"

                embed.add_field(name="Statistics", value=rank_info, inline=False)

                # Skin
                skin_url = f"https://minotar.net/helm/{tests[0].get('username', name)}/128.png"
                embed.set_thumbnail(url=skin_url)

                await interaction.followup.send(embed=embed)

        except aiohttp.ClientError as e:
            await interaction.followup.send(f"⚠️ Website error: {type(e).__name__}: {e}", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("⚠️ Website timeout (no response within 10 seconds).", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="spin", description="Pick a random player from the given gamemode and tier.")
    @app_commands.describe(
        gamemode="The gamemode (e.g. sword, pot, smp).",
        tier="The tier (e.g. ht3, lt1).",
        sajat="Include self in roll (default: no)"
    )
    @app_commands.choices(
        gamemode=_choices_from_list(MODE_LIST),
        tier=_choices_from_list(RANKS)
    )
    async def spin(self, interaction: discord.Interaction, gamemode: app_commands.Choice[str], tier: app_commands.Choice[str], sajat: bool = False):
        await interaction.response.defer(ephemeral=False)

        try:
            if not is_staff_member(interaction.user):
                await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
                return

            if not WEBSITE_URL:
                await interaction.followup.send("⚠️ WEBSITE_URL is not set.", ephemeral=True)
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

            import api as api_module
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
            async with api_module.http_session.get(url, headers=_auth_headers(), timeout=timeout) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    data = {}

                if resp.status != 200:
                    await interaction.followup.send(f"⚠️ Error fetching from website: {resp.status}", ephemeral=True)
                    return

                player = data.get("player")

                if not player:
                    await interaction.followup.send("❌ No results found for this gamemode and tier.", ephemeral=False)
                    return

                username = player.get("username")
                rank = player.get("rank")

                embed = discord.Embed(
                    title="🎲 Random Player",
                    description=f"**{username}** ({rank})",
                    color=discord.Color.gold()
                )

                skin_url = f"https://minotar.net/helm/{username}/128.png"
                embed.set_thumbnail(url=skin_url)

                await interaction.followup.send(embed=embed)

        except aiohttp.ClientError as e:
            await interaction.followup.send(f"⚠️ Website error: {type(e).__name__}: {e}", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("⚠️ Website timeout (no response within 10 seconds).", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="retire", description="Retire a player in a gamemode (admin only, Tier 2+ only).")
    @app_commands.describe(
        name="The player's name on the tierlist.",
        gamemode="The gamemode."
    )
    @app_commands.choices(
        gamemode=_choices_from_list(MODE_LIST)
    )
    async def retire(self, interaction: discord.Interaction, name: str, gamemode: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)

        try:
            if not interaction.user.guild_permissions.administrator:
                await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
                return

            if not WEBSITE_URL:
                await interaction.followup.send("⚠️ WEBSITE_URL is not set.", ephemeral=True)
                return

            import api as api_module
            # First, check the player's current rank to ensure they are Tier 2
            url = f"{WEBSITE_URL}/api/tests?username={name}&gamemode={gamemode.value}"
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
            async with api_module.http_session.get(url, headers=_auth_headers(), timeout=timeout) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    data = {}

                if resp.status != 200:
                    await interaction.followup.send(f"⚠️ Error fetching from website: {resp.status}", ephemeral=True)
                    return

                test = data.get("test")
                if not test:
                    await interaction.followup.send(
                        f"❌ Player not found: **{name}** in this gamemode ({gamemode.value}).",
                        ephemeral=True
                    )
                    return

                current_rank = test.get("rank", "")
                # Check if Tier 2
                if current_rank not in ["LT2", "HT2"]:
                    await interaction.followup.send(
                        f"❌ Only Tier 2 (LT2/HT2) players can be retired. **{name}**'s current rank: **{current_rank}**.",
                        ephemeral=True
                    )
                    return

            # Call the website API to retire (upsert with R prefix)
            retire_url = f"{WEBSITE_URL}/api/tests"
            payload = {
                "username": name,
                "gamemode": gamemode.value,
                "rank": f"R{current_rank}",
                "points": POINTS.get(current_rank, 0),  # Keep same points
                "retired": True
            }

            async with api_module.http_session.post(retire_url, json=payload, headers=_auth_headers(), timeout=timeout) as retire_resp:
                try:
                    retire_data = await retire_resp.json()
                except Exception:
                    retire_data = {}

                if retire_resp.status == 200:
                    msg = f"✅ Retired! **{name}** ({gamemode.value}) is now **R{current_rank}**."

                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    # Truncate retire_data to avoid Discord's 2000 character limit
                    retire_data_str = truncate_message(str(retire_data), 1500)
                    await interaction.followup.send(
                        f"⚠️ Error: {retire_resp.status} - {retire_data_str}",
                        ephemeral=True
                    )

        except aiohttp.ClientError as e:
            await interaction.followup.send(f"⚠️ Website error: {type(e).__name__}: {e}", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("⚠️ Website timeout.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="unretire", description="Un-retire a player (admin only).")
    @app_commands.describe(
        name="The player's name on the tierlist.",
        gamemode="The gamemode."
    )
    @app_commands.choices(
        gamemode=_choices_from_list(MODE_LIST)
    )
    async def unretire(self, interaction: discord.Interaction, name: str, gamemode: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)

        try:
            if not interaction.user.guild_permissions.administrator:
                await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
                return

            if not WEBSITE_URL:
                await interaction.followup.send("⚠️ WEBSITE_URL is not set.", ephemeral=True)
                return

            import api as api_module
            # First, get current rank to remove R prefix
            url = f"{WEBSITE_URL}/api/tests?username={name}&gamemode={gamemode.value}"
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
            async with api_module.http_session.get(url, headers=_auth_headers(), timeout=timeout) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    data = {}

                if resp.status != 200:
                    await interaction.followup.send(f"⚠️ Error fetching from website: {resp.status}", ephemeral=True)
                    return

                test = data.get("test")
                if not test:
                    await interaction.followup.send(
                        f"❌ Player not found: **{name}** in this gamemode ({gamemode.value}).",
                        ephemeral=True
                    )
                    return

                current_rank = test.get("rank", "")
                if not current_rank.startswith("R"):
                    await interaction.followup.send(
                        f"❌ The player is not retired in this gamemode.",
                        ephemeral=True
                    )
                    return

                original_rank = current_rank[1:]  # Remove R prefix

            # Upsert back to original rank
            post_url = f"{WEBSITE_URL}/api/tests"
            payload = {
                "username": name,
                "gamemode": gamemode.value,
                "rank": original_rank,
                "points": POINTS.get(original_rank, 0)
            }

            async with api_module.http_session.post(post_url, json=payload, headers=_auth_headers(), timeout=timeout) as post_resp:
                try:
                    post_data = await post_resp.json()
                except Exception:
                    post_data = {}

                if post_resp.status == 200:
                    msg = f"✅ Unretired! **{name}** ({gamemode.value}) has returned to the tierlist ({original_rank})."

                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    # Truncate post_data to avoid Discord's 2000 character limit
                    post_data_str = truncate_message(str(post_data), 1500)
                    await interaction.followup.send(
                        f"⚠️ Error: {post_resp.status} - {post_data_str}",
                        ephemeral=True
                    )

        except aiohttp.ClientError as e:
            await interaction.followup.send(f"⚠️ Website error: {type(e).__name__}: {e}", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("⚠️ Website timeout.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="tierlistban", description="Ban a player from testing (admin only).")
    @app_commands.describe(
        name="The player's name on the tierlist.",
        days="Ban duration in days (0 = permanent ban).",
        reason="Reason for ban (optional)."
    )
    async def tierlistban(self, interaction: discord.Interaction, name: str, days: int, reason: str = ""):
        await interaction.response.defer(ephemeral=True)

        try:
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                await interaction.followup.send("Error.", ephemeral=True)
                return
            if not is_staff_member(interaction.user):
                await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
                return

            # Check if already banned
            if is_player_banned(name):
                ban_info = get_ban_info(name)
                if ban_info:
                    expires_at = ban_info.get("expires_at", 0)
                    if expires_at == 0:
                        await interaction.followup.send(
                            f"❌ **{name}** is already permanently banned.",
                            ephemeral=True
                        )
                    else:
                        exp_date = datetime.fromtimestamp(expires_at)
                        await interaction.followup.send(
                            f"❌ **{name}** is already banned. Expires: {exp_date.strftime('%Y-%m-%d %H:%M')}",
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
                msg = f"✅ **{name}** has been permanently banned from testing."
            else:
                msg = f"✅ **{name}** has been banned for {days} days."

            if reason:
                msg += f"\n**Reason:** {reason}"

            await interaction.followup.send(msg, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="tierlistunban", description="Unban a player from testing (admin only).")
    @app_commands.describe(
        name="The player's name on the tierlist."
    )
    async def tierlistunban(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)

        try:
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                await interaction.followup.send("Error.", ephemeral=True)
                return
            if not is_staff_member(interaction.user):
                await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
                return

            # Check if actually banned
            if not is_player_banned(name):
                await interaction.followup.send(
                    f"❌ **{name}** is not banned.",
                    ephemeral=True
                )
                return

            # Unban from bot
            unban_player(name)

            # Sync unban to website
            if WEBSITE_URL:
                await api_set_ban(username=name, banned=False)

            msg = f"✅ **{name}** has been unbanned from testing."

            await interaction.followup.send(msg, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="removetierlist", description="Remove a player from the tierlist (admin only, DANGER!).")
    @app_commands.describe(
        name="The player's name on the tierlist (Minecraft name)."
    )
    async def removetierlist(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)

        try:
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                await interaction.followup.send("Error.", ephemeral=True)
                return
            if not is_staff_member(interaction.user):
                await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
                return

            if not WEBSITE_URL:
                await interaction.followup.send("⚠️ WEBSITE_URL is not set.", ephemeral=True)
                return

            import api as api_module
            # First, check if the player exists in the tierlist (case-sensitive)
            url = f"{WEBSITE_URL}/api/tests?username={name}"
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
            async with api_module.http_session.get(url, headers=_auth_headers(), timeout=timeout) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    data = {}

                if resp.status != 200:
                    await interaction.followup.send(f"⚠️ Error fetching from website: {resp.status}", ephemeral=True)
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
                            f"❌ **{name}** is not on the tierlist.\n\n"
                            f"Similar name(s) found: {similar_names}\n"
                            f"Please enter the exact name (case-sensitive)!",
                            ephemeral=True
                        )
                    else:
                        await interaction.followup.send(
                            f"❌ **{name}** is not on the tierlist.",
                            ephemeral=True
                        )
                    return

                # Use exact match
                tests = exact_match_tests
                actual_username = tests[0].get("username", "")

                # Show info about the player (limit to 1500 chars to avoid embed limits)
                modes_info = "\n".join([f"• **{t.get('gamemode', '?')}**: {t.get('rank', '?')} ({t.get('points', 0)}pt)" for t in tests])
                if len(modes_info) > 1500:
                    modes_info = modes_info[:1500] + "\n... (more)"

            # Create confirmation embed
            embed = discord.Embed(
                title="⚠️ WARNING — Before Deletion!",
                description=f"Are you sure you want to remove **{name}** from the tierlist?\n\n"
                           f"**Current tierlist entries:**\n{modes_info}\n\n"
                           f"❗ **THIS IS A PERMANENT ACTION!** All of the player's results across every gamemode will be deleted.",
                color=discord.Color.red()
            )
            embed.set_footer(text=f"Requested by: {interaction.user.display_name}")

            # Send confirmation view
            view = ConfirmRemoveView(username=name, actual_username=name, moderator=interaction.user)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except aiohttp.ClientError as e:
            await interaction.followup.send(f"⚠️ Website error: {type(e).__name__}: {e}", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("⚠️ Website timeout (no response within 10 seconds).", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="bulkimport", description="Bulk import test results from file (admin only)")
    @app_commands.describe(
        file="Text file with test results (one per line: username mode rank)"
    )
    async def bulkimport(self, interaction: discord.Interaction, file: discord.Attachment):
        """Bulk import test results from a text file - format: username mode rank (one per line)"""
        await interaction.response.defer(ephemeral=True)

        # Check if admin
        if not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("No permission.", ephemeral=True)
            return

        if not WEBSITE_URL:
            await interaction.followup.send("⚠️ WEBSITE_URL is not set.", ephemeral=True)
            return

        # Read file content
        try:
            content = await file.read()
            data = content.decode('utf-8')
        except Exception as e:
            await interaction.followup.send(f"❌ Error reading file: {e}", ephemeral=True)
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

        result_msg = f"✅ Successful import: {success_count}\n❌ Failed: {error_count}"
        if errors:
            result_msg += "\n\nErrors:\n" + "\n".join(errors[:10])
            if len(errors) > 10:
                result_msg += f"\n... and {len(errors) - 10} more errors"

        await interaction.followup.send(result_msg, ephemeral=True)

    @app_commands.command(name="cooldown", description="Check your cooldowns, or another player's (staff only).")
    @app_commands.describe(
        user="Player (if empty, shows your own)."
    )
    async def cooldown(self, interaction: discord.Interaction, user: discord.User = None):
        await interaction.response.defer(ephemeral=True)

        try:
            from config import COOLDOWN_SECONDS, TICKET_TYPES
            member = interaction.user
            is_staff = False

            # Check if user is staff (for viewing others' cooldowns)
            if user is not None:
                if not interaction.guild or not isinstance(interaction.user, discord.Member):
                    await interaction.followup.send("Error: Guild context is required to view another player's cooldowns.", ephemeral=True)
                    return
                is_staff = is_staff_member(interaction.user)

                if not is_staff:
                    await interaction.followup.send("You don't have permission to view another player's cooldowns.", ephemeral=True)
                    return

                target_member = user
            else:
                # Check own cooldown - check if banned first
                target_member = member

            # Check if player is banned from testing
            import api as api_module
            if WEBSITE_URL:
                try:
                    player_name = target_member.display_name
                    if hasattr(target_member, 'nick') and target_member.nick:
                        player_name = target_member.nick

                    url = f"{WEBSITE_URL}/api/tests/ban?username={player_name}"
                    timeout = aiohttp.ClientTimeout(total=5)
                    async with api_module.http_session.get(url, headers=_auth_headers(), timeout=timeout) as resp:
                        if resp.status == 200:
                            ban_data = await resp.json()
                            if ban_data.get("banned"):
                                reason = ban_data.get("reason", "")
                                await interaction.followup.send(
                                    f"❌ **{player_name}** is banned from testing!\n" +
                                    (f"**Reason:** {reason}" if reason else "No reason provided."),
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
                            f"❌ **{target_member.display_name}** is permanently banned from testing!\n"
                            f"**Reason:** {ban_info.get('reason', 'None')}",
                            ephemeral=True
                        )
                    else:
                        exp_date = datetime.fromtimestamp(expires_at)
                        await interaction.followup.send(
                            f"❌ **{target_member.display_name}** is banned!\n"
                            f"**Expires:** {exp_date.strftime('%Y-%m-%d %H:%M')}\n"
                            f"**Reason:** {ban_info.get('reason', 'None')}",
                            ephemeral=True
                        )
                    return

            # Build cooldown info for all modes
            data = _load_data()
            cooldowns = data.get("cooldowns", {}).get(str(target_member.id), {})

            embed = discord.Embed(
                title=f"⏳ Cooldown Info - {target_member.display_name}",
                color=discord.Color.blurple()
            )

            mode_cooldowns = []
            for label, mode_key, _ in TICKET_TYPES:
                last_closed = float(cooldowns.get(mode_key, 0))
                if last_closed <= 0:
                    mode_cooldowns.append(f"✅ **{label}**: No cooldown")
                else:
                    left = int((last_closed + COOLDOWN_SECONDS) - time.time())
                    if left <= 0:
                        mode_cooldowns.append(f"✅ **{label}**: Ready, you can open a ticket now!")
                    else:
                        days = left // (24 * 3600)
                        hours = (left % (24 * 3600)) // 3600
                        minutes = (left % 3600) // 60

                        if days > 0:
                            time_str = f"{days}d {hours}h"
                        elif hours > 0:
                            time_str = f"{hours}h {minutes}m"
                        else:
                            time_str = f"{minutes}m"
                        mode_cooldowns.append(f"⏳ **{label}**: {time_str}")

            # Add global cooldown info
            global_last = data.get("cooldowns", {}).get(str(target_member.id), {}).get("_global", 0)
            if global_last > 0:
                left = int((global_last + COOLDOWN_SECONDS) - time.time())
                if left > 0:
                    days = left // (24 * 3600)
                    hours = (left % (24 * 3600)) // 3600
                    mode_cooldowns.append(f"\n🌐 **Global Cooldown**: {days}d {hours}h")

            embed.description = "\n".join(mode_cooldowns)
            embed.set_footer(text=f"Cooldown duration: 30 days")

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="link", description="Link your Minecraft account to your Discord.")
    @app_commands.describe(
        code="The link code from Minecraft (optional if you don't have one yet)."
    )
    async def link(self, interaction: discord.Interaction, code: str = None):
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
                    description = f"**Minecraft:** `{existing_link}`\n**Discord:** {interaction.user.mention}\n\nThe accounts are already linked!"

                    embed = discord.Embed(
                        title="⚠️ Already Linked!",
                        description=description,
                        color=discord.Color.orange()
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return

                # Check if user already has a pending code - if so, remove it and generate new one
                existing_code = await get_pending_link_code_async(interaction.user.id)
                if existing_code:
                    embed = discord.Embed(
                        title="⏳ You already have a code!",
                        description=f"Your existing code: `{existing_code}`\n\n"
                                   f"**Minecraft Server:** `45.140.164.183:25942`\n"
                                   f"Use this: `/link {existing_code}` in Minecraft!\n"
                                   f"Or wait for it to expire and generate a new one.",
                        color=discord.Color.orange()
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return

                # Generate new code
                new_code = await generate_link_code_async(interaction.user.id)

                # Send code via DM
                try:
                    await interaction.user.send(
                        f"🎮 **Link Code:** `{new_code}`\n\n"
                        f"**Minecraft Server:** `45.140.164.183:25942`\n"
                        f"Type in Minecraft: `/link {new_code}`\n"
                        f"The code is valid for {LINK_CODE_EXPIRY_MINUTES} minutes."
                    )
                    dm_sent = True
                except Exception:
                    dm_sent = False

                embed = discord.Embed(
                    title="✅ Code Generated!",
                    description=f"```\n{new_code}\n```\n"
                               f"**Minecraft Server:** `45.140.164.183:25942`\n"
                               f"Type in Minecraft: `/link {new_code}`\n"
                               f"The code is valid for **{LINK_CODE_EXPIRY_MINUTES} minutes**.",
                    color=discord.Color.green()
                )
                if dm_sent:
                    embed.add_field(
                        name="📬 DM Sent!",
                        value="The code was also sent to your DMs!",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="⚠️ DM Failed",
                        value="The code is shown here, copy it!",
                        inline=False
                    )

                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            except Exception as e:
                # Log the error for debugging
                print(f"[LINK ERROR] {type(e).__name__}: {e}")
                await interaction.followup.send(
                    f"❌ An error occurred. Please try again!\n"
                    f"If the error persists, please report it.",
                    ephemeral=True
                )
                return

        # If code IS provided and valid - show success!
        if code_valid:
            linked_name = get_linked_minecraft_name(interaction.user.id)
            embed = discord.Embed(
                title="✅ Account Linked!",
                description=f"**Minecraft:** `{linked_name}`\n"
                           f"**Discord:** {interaction.user.mention}\n\n"
                           f"The accounts have been successfully linked!",
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Code was provided but is invalid
        await interaction.followup.send(
            "❌ Invalid code!\n"
            f"Use `/link` to generate a new code.",
            ephemeral=True
        )

    @app_commands.command(name="unlink", description="Unlink your Minecraft account from your Discord.")
    async def unlink(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            # Check if linked
            existing = get_linked_minecraft_name(interaction.user.id)
            if not existing:
                await interaction.followup.send(
                    "❌ No Minecraft account linked!\n"
                    "Use `/link` to link your account.",
                    ephemeral=True
                )
                return

            # Unlink
            unlink_minecraft_account(interaction.user.id)

            embed = discord.Embed(
                title="✅ Unlinked!",
                description=f"Your Minecraft account (**{existing}**) has been unlinked from your Discord.",
                color=discord.Color.green()
            )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="mylink", description="View your linked Minecraft account.")
    async def mylink(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            linked = get_linked_minecraft_name(interaction.user.id)

            if not linked:
                await interaction.followup.send(
                    "❌ No Minecraft account linked!\n"
                    "Use `/link` to link your account.",
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
            await interaction.followup.send(f"❌ Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="synctiertags", description="Re-tag all linked players with their current highest tier role (admin only).")
    async def synctiertags(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Error.", ephemeral=True)
            return
        if not is_staff_member(interaction.user):
            await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
            return
        if not _ALL_TIER_ROLE_IDS:
            await interaction.followup.send(
                "⚠️ No tier roles are configured. Set `TIER_ROLE_<RANK>` environment variables first.",
                ephemeral=True
            )
            return

        await interaction.followup.send("🔄 Syncing tier tags for all linked players…", ephemeral=True)

        accounts = await get_all_linked_accounts_async()
        if not accounts:
            await interaction.followup.send("No linked accounts found.", ephemeral=True)
            return

        tagged = 0
        skipped = 0
        errors = []

        from database import USE_SUPABASE_API, supabase_select

        for account in accounts:
            discord_id: int = account["discord_id"]
            mc_name: str = account["minecraft_name"]

            member = interaction.guild.get_member(discord_id)
            if not member:
                skipped += 1
                continue

            # Determine best rank: query all tests from Supabase, pick highest by POINTS
            best_rank = "Unranked"
            try:
                if USE_SUPABASE_API:
                    rows = await supabase_select("tests", {"username": mc_name})
                    for row in rows:
                        r = row.get("rank", "Unranked")
                        if POINTS.get(r, 0) > POINTS.get(best_rank, 0):
                            best_rank = r
                else:
                    # Fallback: query website API per mode (slow but correct)
                    for label, key, _ in TICKET_TYPES:
                        try:
                            res = await api_get_tests(mc_name, key)
                            if res.get("status") == 200:
                                data = res.get("data", {})
                                test = data.get("test") or (data.get("tests") or [None])[0]
                                if test:
                                    r = str(test.get("rank", "Unranked"))
                                    if POINTS.get(r, 0) > POINTS.get(best_rank, 0):
                                        best_rank = r
                        except Exception:
                            pass
            except Exception as e:
                errors.append(f"{mc_name}: rank lookup error — {e}")
                skipped += 1
                continue

            try:
                await _apply_tier_role(member, best_rank)
                tagged += 1
            except Exception as e:
                errors.append(f"{mc_name}: role assignment failed — {e}")
                skipped += 1

        summary = f"✅ Sync complete. Tagged: **{tagged}** | Skipped: **{skipped}**"
        if errors:
            err_text = "\n".join(errors[:10])
            if len(errors) > 10:
                err_text += f"\n…and {len(errors) - 10} more."
            summary += f"\n\n**Errors:**\n{err_text}"
        await interaction.followup.send(summary, ephemeral=True)

    @app_commands.command(name="translate", description="Translate text into any supported language.")
    @app_commands.describe(
        text="The text to translate.",
        language="Target language.",
    )
    @app_commands.choices(language=TRANSLATE_LANGUAGES)
    async def translate(self, interaction: discord.Interaction, text: str, language: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await _google_translate(text, language.value)
            lang_name = _LANG_NAMES.get(language.value, language.name)
            embed = discord.Embed(
                title=f"Translation → {lang_name}",
                color=discord.Color.blurple()
            )
            embed.add_field(name="Original", value=text[:1000], inline=False)
            embed.add_field(name="Translated", value=result[:1000], inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Translation failed: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TierlistCog(bot))
