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
)
from permissions import is_staff_member, is_gamemode_tester_or_admin, can_open_ticket, get_player_rank_for_mode
from api import api_get_tests, api_post_test, http_session as _http_session, _auth_headers

# Module-level dict for ticket creation locks (avoids circular refs in views)
TICKET_CREATION_LOCKS: dict = {}


# UI VIEWS (module level)

class CloseTicketView(discord.ui.View):
    def __init__(self, owner_id: int, mode_key: str):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.mode_key = mode_key

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="tierlist_close_ticket")
    async def close(self, interaction: discord.Interaction, _button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Error: this is not a text channel.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Error: member not found.", ephemeral=True)
            return

        topic = channel.topic or ""
        owner_id = 0
        if "owner=" in topic:
            try:
                owner_id = int(topic.split("owner=")[1].split("|")[0].strip())
            except (ValueError, IndexError):
                owner_id = 0

        if member.id != owner_id and not is_staff_member(member):
            await interaction.response.send_message("You don't have permission to close this ticket.", ephemeral=True)
            return

        await interaction.response.send_message("✅ Closing ticket... deleting channel in 3 seconds.", ephemeral=True)

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
            await channel.delete(reason="Tierlist ticket closed")
        except discord.Forbidden:
            try:
                await channel.send("❌ Cannot delete the channel (Missing Permissions). Give the bot **Manage Channels** permission in the category too.")
            except Exception:
                pass
        except Exception:
            pass

    @discord.ui.button(label="Give Tier", style=discord.ButtonStyle.success, custom_id="tierlist_give_tier")
    async def give_tier(self, interaction: discord.Interaction, _button: discord.ui.Button):
        """Give tier to the ticket owner - only for staff"""
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Error: member not found.", ephemeral=True)
            return

        if not is_staff_member(member):
            await interaction.response.send_message("You don't have permission to give tiers.", ephemeral=True)
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Error: this is not a text channel.", ephemeral=True)
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
            await interaction.response.send_message("Error: cannot find the ticket owner.", ephemeral=True)
            return

        linked_minecraft = get_linked_minecraft_name(owner_id)
        if not linked_minecraft:
            await interaction.response.send_message("❌ The player has no linked account! Their Minecraft name is unknown.", ephemeral=True)
            return

        tier_select = TierSelectView(owner_id, linked_minecraft, mode_key, member)
        await interaction.response.send_message("Select the gamemode and tier:", view=tier_select, ephemeral=True)


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
        super().__init__(placeholder="Gamemode...", options=options, custom_id="gamemode_select")
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
        super().__init__(placeholder="Achieved Rank...", options=options, custom_id="tier_select")

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
            await interaction.response.send_message("Error: cannot find the Discord user.", ephemeral=True)
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
            title=f"{display_mc} Test Result 🏆",
            color=discord.Color.dark_grey()
        )
        embed.set_thumbnail(url=skin_url)
        embed.add_field(name="Tester:", value=tester.mention, inline=False)
        embed.add_field(name="Gamemode:", value=display_mode, inline=False)
        embed.add_field(name="Minecraft Name:", value=display_mc, inline=False)
        embed.add_field(name="Previous Rank:", value=f"{display_prev_rank} ({prev_points} pts)", inline=False)
        embed.add_field(name="Achieved Rank:", value=f"{display_selected_tier} ({new_points} pts)", inline=False)
        embed.add_field(name="Points:", value=points_str, inline=False)

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
                    await interaction.response.send_message(f"✅ Tier set: **{selected_tier}** and saved to the website!", ephemeral=True)
                else:
                    await interaction.response.send_message(f"✅ Tier set: **{selected_tier}** (website save failed)", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"✅ Tier set: **{selected_tier}** (website error: {e})", ephemeral=True)
        else:
            await interaction.response.send_message(f"✅ Tier set: **{selected_tier}**", ephemeral=True)


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

        # Check if user has a linked Minecraft account
        linked_minecraft = get_linked_minecraft_name(member.id)
        if not linked_minecraft:
            await interaction.response.send_message(
                "❌ **Your Minecraft account is not linked!**\n\n"
                "Use the `/link` command in Discord, then `/link <code>` in Minecraft to link your account. "
                "Only players with linked accounts can open tickets!",
                ephemeral=True
            )
            return

        # Check if player is banned
        player_name = member.display_name
        if member.nick:
            player_name = member.nick

        if WEBSITE_URL:
            try:
                import aiohttp
                url = f"{WEBSITE_URL}/api/tests/ban?username={player_name}"
                timeout = aiohttp.ClientTimeout(total=5)
                async with api_module.http_session.get(url, headers=_auth_headers(), timeout=timeout) as resp:
                    if resp.status == 200:
                        ban_data = await resp.json()
                        if ban_data.get("banned"):
                            reason = ban_data.get("reason", "")
                            await interaction.response.send_message(
                                f"❌ You are banned from testing!\n" +
                                (f"**Reason:** {reason}" if reason else ""),
                                ephemeral=True
                            )
                            return
            except Exception:
                pass

        # Rank check
        player_rank = await get_player_rank_for_mode(linked_minecraft, self.mode_key)
        if not can_open_ticket(player_rank):
            await interaction.response.send_message(
                f"❌ Opening a **{get_gamemode_display_name(self.mode_key)}** ticket requires at least **LT3** rank. "
                f"Your current rank: **{player_rank}**.",
                ephemeral=True
            )
            return

        # Cooldown check
        left = cooldown_left(member.id, self.mode_key)
        if left > 0:
            days = left // (24 * 3600)
            hours = (left % (24 * 3600)) // 3600
            await interaction.response.send_message(
                f"⏳ **Cooldown**: you can open a new ticket for this gamemode ({self.mode_key}) in **{days}d {hours}h**.",
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
                    await interaction.response.send_message("You already have an open ticket for this gamemode. 🔒", ephemeral=True)
                    return
                else:
                    set_open_ticket_channel_id(member.id, self.mode_key, None)

            category = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
            if TICKET_CATEGORY_ID and not isinstance(category, discord.CategoryChannel):
                await interaction.response.send_message(
                    "❌ Ticket category is invalid. Check the TICKET_CATEGORY_ID setting.",
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
                    topic=f"Tierlist ticket | owner={member.id} | mode={self.mode_key} | mc={linked_minecraft}",
                    reason="Tierlist ticket created"
                )
            except discord.Forbidden:
                await interaction.response.send_message(
                    "❌ Missing permission to create channels. Give the bot **Manage Channels** permission (including the category).",
                    ephemeral=True
                )
                return

            set_open_ticket_channel_id(member.id, self.mode_key, channel.id)

            ping_text = ""

            rounds_display = get_ticket_rounds_display(self.mode_key)

            description = "Click one of the buttons below to request a test for the gamemode shown on the button."

            embed = discord.Embed(
                title="Test Request",
                description=description,
                color=discord.Color.blurple()
            )

            display_mode = get_gamemode_display_name(self.mode_key)

            embed.add_field(name="Gamemode", value=display_mode, inline=True)
            embed.add_field(name="Minecraft Name", value=f"`{linked_minecraft}`", inline=True)

            embed.add_field(name="Rounds", value=rounds_display, inline=False)
            embed.add_field(name="Player", value=member.mention, inline=True)

            await channel.send(content=ping_text, embed=embed, view=CloseTicketView(owner_id=member.id, mode_key=self.mode_key))
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

            description = "Click one of the buttons below to request a test for the gamemode shown on the button."

            embed = discord.Embed(
                title="Test Request",
                description=description,
                color=discord.Color.blurple()
            )

            await interaction.channel.send(embed=embed, view=TicketPanelView())
            await interaction.followup.send("✅ Ticket panel posted.", ephemeral=True)

        except discord.Forbidden:
            await interaction.followup.send("❌ Cannot send messages here (Missing Permissions). Grant the bot write permission in this channel.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {type(e).__name__}: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketsCog(bot))
