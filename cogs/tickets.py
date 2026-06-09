import asyncio
import os
import time

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    TICKET_TYPES, RANKS, POINTS,
    TICKET_CATEGORY_ID, STAFF_ROLE_ID,
    WEBSITE_URL,
    get_gamemode_display_name, get_ticket_rounds_display, normalize_gamemode,
)
from storage import (
    get_open_ticket_channel_id, set_open_ticket_channel_id,
    cooldown_left, set_last_closed,
    get_linked_minecraft_name,
    clear_active_session,
)
from permissions import is_staff_member, is_gamemode_tester_or_admin, can_open_ticket, get_player_rank_for_mode
from api import api_get_tests, api_post_test, http_session as _http_session, _auth_headers

TICKET_CREATION_LOCKS: dict = {}


async def _delete_channel_after(channel: discord.TextChannel, delay: int = 5):
    await asyncio.sleep(delay)
    try:
        await channel.delete(reason="Ticket closed")
    except discord.Forbidden:
        try:
            await channel.send(
                "❌ Cannot delete the channel (Missing Permissions). "
                "Give the bot **Manage Channels** permission in this category."
            )
        except Exception:
            pass
    except Exception:
        pass


def _parse_topic(topic: str):
    """Return (owner_id, mode_key, mc_name) from a channel topic string."""
    owner_id, mode_key, mc_name = 0, "", None
    if "owner=" in topic:
        try:
            owner_id = int(topic.split("owner=")[1].split("|")[0].strip())
        except (ValueError, IndexError):
            pass
    if "mode=" in topic:
        try:
            mode_key = topic.split("mode=")[1].split("|")[0].strip()
        except (ValueError, IndexError):
            pass
    if "mc=" in topic:
        try:
            mc_name = topic.split("mc=")[1].split("|")[0].strip()
        except (ValueError, IndexError):
            pass
    return owner_id, mode_key, mc_name


# GIVE TIER — sequential selection (step 1: gamemode, step 2: rank)

async def _process_tier_result(
    interaction: discord.Interaction,
    owner_id: int,
    mc_name: str,
    mode_key: str,
    selected_rank: str,
    tester: discord.Member,
):
    mode_display = get_gamemode_display_name(mode_key)

    prev_rank = "Unranked"
    prev_points = 0
    if WEBSITE_URL:
        try:
            mode_param = normalize_gamemode(mode_key)
            res = await api_get_tests(username=mc_name, mode=mode_param)
            if res.get("status") == 200:
                data = res.get("data", {})
                test = data.get("test")
                tests = data.get("tests", [])
                target = None
                if test:
                    target = test
                elif tests:
                    best_test, best_pts = None, -1
                    for t in tests:
                        t_mode = str(t.get("gamemode", "")).lower()
                        t_rank = str(t.get("rank", "Unranked"))
                        t_pts = POINTS.get(t_rank, 0)
                        if t_mode == mode_param and t_pts > best_pts:
                            best_pts = t_pts
                            best_test = t
                    target = best_test
                if target:
                    prev_rank = str(target.get("rank", "Unranked")) or "Unranked"
                    prev_points = POINTS.get(prev_rank, 0)
        except Exception as e:
            print(f"Error fetching previous rank: {e}")

    new_points = POINTS.get(selected_rank, 0)
    diff = new_points - prev_points
    points_str = f"+{diff}" if diff > 0 else ("±0" if diff == 0 else str(diff))

    embed = discord.Embed(
        title=f"{mc_name} Test Result 🏆",
        color=discord.Color.dark_grey(),
    )
    embed.set_thumbnail(url=f"https://minotar.net/helm/{mc_name}/128.png")
    embed.add_field(name="Tester:", value=tester.mention, inline=False)
    embed.add_field(name="Gamemode:", value=mode_display, inline=False)
    embed.add_field(name="Minecraft Name:", value=mc_name, inline=False)
    embed.add_field(name="Previous Rank:", value=f"{prev_rank} ({prev_points} pts)", inline=False)
    embed.add_field(name="Achieved Rank:", value=f"{selected_rank} ({new_points} pts)", inline=False)
    embed.add_field(name="Points:", value=points_str, inline=False)

    # Post to tier results channel
    tier_channel_id = 0
    try:
        tier_channel_id = int(os.getenv("TIER_RESULTS_CHANNEL_ID", "0"))
    except ValueError:
        pass

    if not tier_channel_id:
        tier_channel = (
            discord.utils.get(interaction.guild.text_channels, name="teszteredmenyek")
            or discord.utils.get(interaction.guild.text_channels, name="test-results")
            or discord.utils.get(interaction.guild.text_channels, name="eredmenyek")
        )
    else:
        tier_channel = interaction.guild.get_channel(tier_channel_id)

    if tier_channel:
        await tier_channel.send(embed=embed)

    # Save to website
    if WEBSITE_URL:
        try:
            mode_to_save = get_gamemode_display_name(mode_key)
            save = await api_post_test(username=mc_name, mode=mode_to_save, rank=selected_rank, tester=tester)
            save_ok = save.get("status") in (200, 201)
            result_msg = "✅ Tier set and saved to the website!" if save_ok else "✅ Tier set (website save failed)"
        except Exception as e:
            result_msg = f"✅ Tier set: **{selected_rank}** (website error: {e})"
    else:
        result_msg = f"✅ Tier set: **{selected_rank}**"

    # Apply cooldown and clear session
    set_last_closed(owner_id, mode_key, time.time())
    set_open_ticket_channel_id(owner_id, mode_key, None)
    clear_active_session(mode_key)

    await interaction.followup.send(
        f"{result_msg}\nChannel closes in 5 seconds.", ephemeral=True
    )

    if interaction.channel and isinstance(interaction.channel, discord.TextChannel):
        asyncio.create_task(_delete_channel_after(interaction.channel, 5))


class GiveTierRankSelect(discord.ui.Select):
    def __init__(self, owner_id: int, mc_name: str, mode_key: str, tester: discord.Member):
        self.owner_id = owner_id
        self.mc_name = mc_name
        self.mode_key = mode_key
        self.tester = tester
        options = [discord.SelectOption(label=r, value=r) for r in RANKS if r != "Unranked"]
        super().__init__(placeholder="Step 2 — Select achieved rank...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_rank = self.values[0]
        await interaction.response.defer(ephemeral=True)
        await _process_tier_result(interaction, self.owner_id, self.mc_name, self.mode_key, selected_rank, self.tester)


class GiveTierRankView(discord.ui.View):
    def __init__(self, owner_id: int, mc_name: str, mode_key: str, tester: discord.Member):
        super().__init__(timeout=60)
        self.add_item(GiveTierRankSelect(owner_id, mc_name, mode_key, tester))


class GiveTierGameModeSelect(discord.ui.Select):
    def __init__(self, owner_id: int, mc_name: str, current_mode: str, tester: discord.Member):
        self.owner_id = owner_id
        self.mc_name = mc_name
        self.tester = tester
        options = [
            discord.SelectOption(label=label, value=key, default=(key == current_mode))
            for label, key, _rid in TICKET_TYPES
        ]
        super().__init__(placeholder="Step 1 — Select gamemode...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_gm = self.values[0]
        mode_display = get_gamemode_display_name(selected_gm)
        rank_view = GiveTierRankView(self.owner_id, self.mc_name, selected_gm, self.tester)
        await interaction.response.edit_message(
            content=f"Gamemode: **{mode_display}**\nStep 2 — Select the achieved rank:",
            view=rank_view,
        )


class GiveTierGameModeView(discord.ui.View):
    def __init__(self, owner_id: int, mc_name: str, current_mode: str, tester: discord.Member):
        super().__init__(timeout=60)
        self.add_item(GiveTierGameModeSelect(owner_id, mc_name, current_mode, tester))


# MAIN TICKET VIEW

class CloseTicketView(discord.ui.View):
    def __init__(self, owner_id: int, mode_key: str):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.mode_key = mode_key

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="tierlist_close_ticket")
    async def close(self, interaction: discord.Interaction, _button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Error: not a text channel.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Error: member not found.", ephemeral=True)
            return

        owner_id, mode_key, mc_name = _parse_topic(channel.topic or "")

        if member.id != owner_id and not is_staff_member(member):
            await interaction.response.send_message("You don't have permission to close this ticket.", ephemeral=True)
            return

        await interaction.response.send_message("✅ Closing ticket... deleting channel in 5 seconds.", ephemeral=True)

        set_last_closed(owner_id, mode_key, time.time())
        set_open_ticket_channel_id(owner_id, mode_key, None)
        clear_active_session(mode_key)

        asyncio.create_task(_delete_channel_after(channel, 5))

    @discord.ui.button(label="Dismiss (No Cooldown)", style=discord.ButtonStyle.secondary, custom_id="tierlist_dismiss_ticket")
    async def dismiss(self, interaction: discord.Interaction, _button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Error: not a text channel.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Error: member not found.", ephemeral=True)
            return

        if not is_staff_member(member):
            await interaction.response.send_message("❌ Staff only can dismiss tickets.", ephemeral=True)
            return

        await interaction.response.send_message(
            "✅ Dismissing ticket (no cooldown applied)... deleting channel in 5 seconds.", ephemeral=True
        )

        owner_id, mode_key, mc_name = _parse_topic(channel.topic or "")

        # No cooldown — just clear state
        set_open_ticket_channel_id(owner_id, mode_key, None)
        clear_active_session(mode_key)

        asyncio.create_task(_delete_channel_after(channel, 5))

    @discord.ui.button(label="Give Tier", style=discord.ButtonStyle.success, custom_id="tierlist_give_tier")
    async def give_tier(self, interaction: discord.Interaction, _button: discord.ui.Button):
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Error: member not found.", ephemeral=True)
            return

        if not is_staff_member(member):
            await interaction.response.send_message("You don't have permission to give tiers.", ephemeral=True)
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Error: not a text channel.", ephemeral=True)
            return

        owner_id, mode_key, mc_name = _parse_topic(channel.topic or "")

        if owner_id == 0:
            await interaction.response.send_message("Error: cannot find the ticket owner.", ephemeral=True)
            return

        linked_minecraft = get_linked_minecraft_name(owner_id)
        if not linked_minecraft:
            await interaction.response.send_message(
                "❌ The player has no linked account! Their Minecraft name is unknown.", ephemeral=True
            )
            return

        view = GiveTierGameModeView(owner_id, linked_minecraft, mode_key, member)
        await interaction.response.send_message("Step 1 — Select the gamemode:", view=view, ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for label, mode_key, _rid in TICKET_TYPES:
            self.add_item(TicketButton(label=label, mode_key=mode_key))


class TicketButton(discord.ui.Button):
    def __init__(self, label: str, mode_key: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=f"tierlist_ticket_{mode_key}")
        self.mode_key = mode_key

    async def callback(self, interaction: discord.Interaction):
        import api as api_module
        guild = interaction.guild
        member = interaction.user

        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("Error: guild/member not available.", ephemeral=True)
            return

        linked_minecraft = get_linked_minecraft_name(member.id)
        if not linked_minecraft:
            await interaction.response.send_message(
                "❌ **Your Minecraft account is not linked!**\n\n"
                "Use the `/link` command in Discord, then `/link <code>` in Minecraft to link your account.",
                ephemeral=True,
            )
            return

        if WEBSITE_URL:
            try:
                import aiohttp
                player_name = member.nick or member.display_name
                url = f"{WEBSITE_URL}/api/tests/ban?username={player_name}"
                timeout = aiohttp.ClientTimeout(total=5)
                async with api_module.http_session.get(url, headers=_auth_headers(), timeout=timeout) as resp:
                    if resp.status == 200:
                        ban_data = await resp.json()
                        if ban_data.get("banned"):
                            reason = ban_data.get("reason", "")
                            await interaction.response.send_message(
                                "❌ You are banned from testing!\n" + (f"**Reason:** {reason}" if reason else ""),
                                ephemeral=True,
                            )
                            return
            except Exception:
                pass

        player_rank = await get_player_rank_for_mode(linked_minecraft, self.mode_key)
        if not can_open_ticket(player_rank):
            await interaction.response.send_message(
                f"❌ Opening a **{get_gamemode_display_name(self.mode_key)}** ticket requires at least **LT3** rank. "
                f"Your current rank: **{player_rank}**.",
                ephemeral=True,
            )
            return

        left = cooldown_left(member.id, self.mode_key)
        if left > 0:
            days = left // (24 * 3600)
            hours = (left % (24 * 3600)) // 3600
            await interaction.response.send_message(
                f"⏳ **Cooldown**: you can open a new ticket for **{get_gamemode_display_name(self.mode_key)}** "
                f"in **{days}d {hours}h**.",
                ephemeral=True,
            )
            return

        lock_key = (member.id, self.mode_key)
        if lock_key not in TICKET_CREATION_LOCKS:
            TICKET_CREATION_LOCKS[lock_key] = asyncio.Lock()
        async with TICKET_CREATION_LOCKS[lock_key]:
            existing_channel_id = get_open_ticket_channel_id(member.id, self.mode_key)
            if existing_channel_id:
                ch = guild.get_channel(existing_channel_id)
                if ch:
                    await interaction.response.send_message(
                        "You already have an open ticket for this gamemode. 🔒", ephemeral=True
                    )
                    return
                else:
                    set_open_ticket_channel_id(member.id, self.mode_key, None)

            category = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
            if TICKET_CATEGORY_ID and not isinstance(category, discord.CategoryChannel):
                await interaction.response.send_message(
                    "❌ Ticket category is invalid. Check the TICKET_CATEGORY_ID setting.", ephemeral=True
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
                    topic=f"Tierlist ticket | owner={member.id} | mode={self.mode_key} | mc={linked_minecraft}",
                    reason="Tierlist ticket created",
                )
            except discord.Forbidden:
                await interaction.response.send_message(
                    "❌ Missing permission to create channels. Give the bot **Manage Channels** permission.",
                    ephemeral=True,
                )
                return

            set_open_ticket_channel_id(member.id, self.mode_key, channel.id)

            rounds_display = get_ticket_rounds_display(self.mode_key)
            display_mode = get_gamemode_display_name(self.mode_key)

            embed = discord.Embed(
                title="Test Request",
                description="A tester will be with you soon.",
                color=discord.Color.blurple(),
            )
            embed.set_thumbnail(url=f"https://minotar.net/helm/{linked_minecraft}/128.png")
            embed.add_field(name="Gamemode", value=display_mode, inline=True)
            embed.add_field(name="Minecraft Name", value=f"`{linked_minecraft}`", inline=True)
            embed.add_field(name="Rounds", value=rounds_display, inline=False)
            embed.add_field(name="Player", value=member.mention, inline=True)

            await channel.send(
                embed=embed,
                view=CloseTicketView(owner_id=member.id, mode_key=self.mode_key),
            )
            await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=False)


# COG

class TicketsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ticketpanel", description="Post the ticket panel.")
    async def ticketpanel(self, interaction: discord.Interaction):
        await interaction.response.defer()

        try:
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                await interaction.followup.send("Error.", ephemeral=True)
                return
            if interaction.channel is None:
                await interaction.followup.send("Error: no channel.", ephemeral=True)
                return

            embed = discord.Embed(
                title="Test Request",
                description="Click one of the buttons below to request a test for that gamemode.",
                color=discord.Color.blurple(),
            )

            await interaction.channel.send(embed=embed, view=TicketPanelView())
            await interaction.followup.send("✅ Ticket panel posted.", ephemeral=True)

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Cannot send messages here (Missing Permissions).", ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {type(e).__name__}: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketsCog(bot))
