import asyncio
import time
from typing import Dict, Any, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    TICKET_TYPES, QUEUE_CHANNELS, QUEUE_PING_ROLES, TICKET_CREATE_CATEGORY_ID,
    STAFF_ROLE_ID, MODE_LIST,
    get_gamemode_display_name, get_gamemode_indicator, get_gamemode_color,
    _choices_from_list,
)
from storage import (
    cooldown_left, get_linked_minecraft_name,
    _load_data, _save_data,
    persist_queue_message_ids,
    set_active_session, get_active_session, clear_active_session,
    save_queue_last_session, get_queue_last_session,
)
from permissions import is_staff_member, is_gamemode_tester_or_admin, can_join_queue, get_player_rank_for_mode
from cogs.tickets import CloseTicketView

REGIONS = ['NA', 'EU', 'AS', 'SA', 'AU']

# MODULE-LEVEL STATE
ACTIVE_QUEUES: Dict[str, Dict[str, Any]] = {}
QUEUE_MESSAGE_IDS: Dict[int, str] = {}
QUEUE_PANEL_MESSAGE = None  # (channel_id, message_id)


class QueuePlayer:
    def __init__(self, discord_id: int, minecraft_name: str):
        self.discord_id = discord_id
        self.minecraft_name = minecraft_name
        self.joined_at = time.time()


# HELPERS

async def get_queue_role(guild: discord.Guild, gamemode: str) -> Optional[discord.Role]:
    display = get_gamemode_display_name(gamemode)
    return discord.utils.get(guild.roles, name=f"{display} Queue")


def _build_open_embed(gamemode: str, guild: Optional[discord.Guild] = None) -> discord.Embed:
    display = get_gamemode_display_name(gamemode)
    queue = ACTIVE_QUEUES.get(gamemode, {})
    region = queue.get("region", "??")
    players: List[QueuePlayer] = queue.get("players", [])
    testers: List[QueuePlayer] = queue.get("testers", [])

    def fmt(p: QueuePlayer) -> str:
        member = guild.get_member(p.discord_id) if guild else None
        nick = member.display_name if member else str(p.discord_id)
        return f"{nick} ({p.minecraft_name})"

    player_text = "\n".join(
        f"**{i + 1}.** {fmt(p)}" for i, p in enumerate(players)
    ) or "Nobody in queue yet."

    tester_text = "\n".join(fmt(t) for t in testers) or "No testers."

    embed = discord.Embed(
        title=f"🟢 {display} Queue — {region}",
        color=get_gamemode_color(gamemode),
    )
    embed.add_field(name=f"Players ({len(players)})", value=player_text, inline=False)
    embed.add_field(name=f"Active Testers ({len(testers)})", value=tester_text, inline=False)
    embed.set_footer(text="Click Join Queue to enter • Leave Queue to exit")
    return embed


def _build_closed_embed(gamemode: str) -> discord.Embed:
    display = get_gamemode_display_name(gamemode)
    last = get_queue_last_session(gamemode)
    lines = ["No testers are currently available."]
    if last:
        lines.append(f"Last session: {last}")
    lines.append("Check back later.")
    return discord.Embed(
        title=f"🔴 {display} Queue — Closed",
        description="\n".join(lines),
        color=discord.Color(0xe74c3c),
    )


async def update_queue_message(gamemode: str, guild: Optional[discord.Guild] = None):
    from main import bot
    channel_id = QUEUE_CHANNELS.get(gamemode)
    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        return

    msg_id = next((mid for mid, gm in QUEUE_MESSAGE_IDS.items() if gm == gamemode), None)
    if not msg_id:
        return

    try:
        message = await channel.fetch_message(msg_id)
    except discord.NotFound:
        QUEUE_MESSAGE_IDS.pop(msg_id, None)
        persist_queue_message_ids(QUEUE_MESSAGE_IDS)
        return
    except Exception:
        return

    g = guild or channel.guild
    queue = ACTIVE_QUEUES.get(gamemode)
    if not queue:
        embed = _build_closed_embed(gamemode)
        try:
            await message.edit(embed=embed, view=None)
            QUEUE_MESSAGE_IDS.pop(msg_id, None)
            persist_queue_message_ids(QUEUE_MESSAGE_IDS)
        except Exception:
            pass
        return

    try:
        await message.edit(embed=_build_open_embed(gamemode, g), view=QueueActionView(gamemode))
    except Exception as e:
        print(f"Queue update error [{gamemode}]: {e}")


async def rebuild_queue_message_ids(guild):
    global QUEUE_MESSAGE_IDS
    QUEUE_MESSAGE_IDS.clear()
    for gamemode, channel_id in QUEUE_CHANNELS.items():
        channel = guild.get_channel(channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            continue
        try:
            async for msg in channel.history(limit=200):
                if msg.embeds and msg.components and msg.embeds[0].title:
                    title = msg.embeds[0].title
                    if "Queue" in title and "Panel" not in title:
                        QUEUE_MESSAGE_IDS[msg.id] = gamemode
                        break
        except Exception as e:
            print(f"Error scanning channel {channel_id} for queue message: {e}")
    persist_queue_message_ids(QUEUE_MESSAGE_IDS)


async def refresh_queue_panel(guild):
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
            title="Open Queue",
            description="Click a button below to open a queue for your gamemode.",
            color=discord.Color.blurple(),
        )
        await msg.edit(embed=embed, view=QueuePanelView())
    except discord.NotFound:
        QUEUE_PANEL_MESSAGE = None
        data = _load_data()
        data["queue_panel_message"] = None
        _save_data(data)
    except Exception as e:
        print(f"Error refreshing queue panel: {e}")


async def queue_maintenance_task():
    from main import bot
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


# REGION SELECT (shown after clicking Open Queue)

class RegionSelectMenu(discord.ui.Select):
    def __init__(self, mode_key: str, mode_label: str):
        self.mode_key = mode_key
        self.mode_label = mode_label
        options = [discord.SelectOption(label=r, value=r) for r in REGIONS]
        super().__init__(placeholder="Select your region...", options=options)

    async def callback(self, interaction: discord.Interaction):
        region = self.values[0]
        await interaction.response.defer(ephemeral=True)
        await _do_open_queue(interaction, self.mode_key, self.mode_label, region)


class RegionSelectView(discord.ui.View):
    def __init__(self, mode_key: str, mode_label: str):
        super().__init__(timeout=60)
        self.add_item(RegionSelectMenu(mode_key, mode_label))


async def _do_open_queue(
    interaction: discord.Interaction,
    mode_key: str,
    mode_label: str,
    region: str,
):
    """Actually open the queue after region is chosen."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("Error: server only.", ephemeral=True)
        return

    if mode_key in ACTIVE_QUEUES:
        # Queue already open — just add this tester and refresh
        linked_mc = get_linked_minecraft_name(interaction.user.id) or "TESTER"
        ACTIVE_QUEUES[mode_key]["testers"].append(
            QueuePlayer(interaction.user.id, linked_mc)
        )
        await update_queue_message(mode_key, interaction.guild)
        await interaction.followup.send(
            f"✅ You joined the existing **{mode_label}** queue ({region}).",
            ephemeral=True,
        )
        return

    channel_id = QUEUE_CHANNELS.get(mode_key)
    if not channel_id:
        await interaction.followup.send(f"❌ No channel configured for **{mode_label}**.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        await interaction.followup.send(f"❌ Queue channel not found ({channel_id}).", ephemeral=True)
        return

    linked_mc = get_linked_minecraft_name(interaction.user.id) or "TESTER"
    ACTIVE_QUEUES[mode_key] = {
        "opened_by": interaction.user.id,
        "opened_at": time.time(),
        "region": region,
        "players": [],
        "testers": [QueuePlayer(interaction.user.id, linked_mc)],
        "called_players": [],
    }

    ping_role_id = QUEUE_PING_ROLES.get(mode_key)
    ping_text = f"<@&{ping_role_id}> " if ping_role_id else ""

    embed = _build_open_embed(mode_key, interaction.guild)
    view = QueueActionView(mode_key)
    message = await channel.send(content=ping_text, embed=embed, view=view)
    QUEUE_MESSAGE_IDS[message.id] = mode_key
    persist_queue_message_ids(QUEUE_MESSAGE_IDS)

    await interaction.followup.send(
        f"✅ **{mode_label}** queue opened in {channel.mention} (Region: {region})!",
        ephemeral=True,
    )
    await refresh_queue_panel(interaction.guild)


# PER-GAMEMODE BUTTON CLASSES

class JoinQueueButton(discord.ui.Button):
    def __init__(self, gamemode: str):
        super().__init__(
            label="Join Queue",
            style=discord.ButtonStyle.success,
            custom_id=f"queue_join_{gamemode}",
        )
        self.gamemode = gamemode

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            await interaction.response.send_message("Error: not a member.", ephemeral=True)
            return

        gamemode = self.gamemode
        queue = ACTIVE_QUEUES.get(gamemode)
        if not queue:
            await interaction.response.send_message("❌ The queue is not open.", ephemeral=True)
            return

        if any(p.discord_id == member.id for p in queue["players"]):
            await interaction.response.send_message("You're already in the queue!", ephemeral=True)
            return
        if any(t.discord_id == member.id for t in queue.get("testers", [])):
            await interaction.response.send_message("❌ You are a tester for this gamemode.", ephemeral=True)
            return

        linked_mc = get_linked_minecraft_name(member.id)
        if not linked_mc:
            await interaction.response.send_message(
                "❌ Link your Minecraft account first with `/link`.", ephemeral=True
            )
            return

        if is_gamemode_tester_or_admin(member, gamemode):
            await interaction.response.send_message(
                "❌ You are a tester for this gamemode.", ephemeral=True
            )
            return

        cd_left = cooldown_left(member.id, gamemode)
        if cd_left > 0:
            days = cd_left // (24 * 60 * 60)
            hours = (cd_left % (24 * 60 * 60)) // (60 * 60)
            await interaction.response.send_message(
                f"❌ **{days}d {hours}h** cooldown remaining for **{get_gamemode_display_name(gamemode)}**.",
                ephemeral=True,
            )
            return

        player_rank = await get_player_rank_for_mode(linked_mc, gamemode)
        if not can_join_queue(player_rank):
            await interaction.response.send_message(
                f"❌ Only players ranked **LT5–HT4** can join the queue. Your rank: **{player_rank}**.",
                ephemeral=True,
            )
            return

        queue["players"].append(QueuePlayer(member.id, linked_mc))

        queue_role = await get_queue_role(member.guild, gamemode)
        if queue_role:
            try:
                await member.add_roles(queue_role, reason="Joined queue")
            except Exception:
                pass

        await update_queue_message(gamemode, member.guild)

        pos = len(queue["players"])
        await interaction.response.send_message(
            f"✅ You joined the **{get_gamemode_display_name(gamemode)}** queue! "
            f"You are **#{pos}** of **{pos}**.",
            ephemeral=True,
        )


class LeaveQueueButton(discord.ui.Button):
    def __init__(self, gamemode: str):
        super().__init__(
            label="Leave Queue",
            style=discord.ButtonStyle.danger,
            custom_id=f"queue_leave_{gamemode}",
        )
        self.gamemode = gamemode

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            await interaction.response.send_message("Error: not a member.", ephemeral=True)
            return

        gamemode = self.gamemode
        queue = ACTIVE_QUEUES.get(gamemode)
        if not queue:
            await interaction.response.send_message("❌ The queue doesn't exist.", ephemeral=True)
            return

        for i, p in enumerate(queue["players"]):
            if p.discord_id == member.id:
                queue["players"].pop(i)
                queue_role = await get_queue_role(member.guild, gamemode)
                if queue_role:
                    try:
                        await member.remove_roles(queue_role, reason="Left queue")
                    except Exception:
                        pass
                await update_queue_message(gamemode, member.guild)
                await interaction.response.send_message(
                    f"✅ You left the **{get_gamemode_display_name(gamemode)}** queue!", ephemeral=True
                )
                return

        await interaction.response.send_message("You're not in the queue.", ephemeral=True)


class CloseQueueButton(discord.ui.Button):
    def __init__(self, gamemode: str):
        super().__init__(
            label="❌ Close Queue",
            style=discord.ButtonStyle.secondary,
            custom_id=f"queue_close_{gamemode}",
        )
        self.gamemode = gamemode

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            await interaction.response.send_message("Error: not a member.", ephemeral=True)
            return

        gamemode = self.gamemode
        queue = ACTIVE_QUEUES.get(gamemode)
        if not queue:
            await interaction.response.send_message("❌ The queue is already closed.", ephemeral=True)
            return

        if not is_staff_member(member) and queue["opened_by"] != member.id:
            await interaction.response.send_message(
                "Only the tester who opened the queue or staff can close it.", ephemeral=True
            )
            return

        view = ConfirmCloseQueueView(gamemode)
        await interaction.response.send_message(
            f"Are you sure you want to close the **{get_gamemode_display_name(gamemode)}** queue?",
            view=view,
            ephemeral=True,
        )


class NextPlayerButton(discord.ui.Button):
    def __init__(self, gamemode: str):
        super().__init__(
            label="Next Player",
            style=discord.ButtonStyle.primary,
            custom_id=f"queue_next_{gamemode}",
        )
        self.gamemode = gamemode

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            await interaction.response.send_message("Error: not a member.", ephemeral=True)
            return

        gamemode = self.gamemode
        queue = ACTIVE_QUEUES.get(gamemode)
        if not queue or not queue["players"]:
            await interaction.response.send_message("❌ No players in the queue.", ephemeral=True)
            return

        if not is_staff_member(member) and queue["opened_by"] != member.id:
            await interaction.response.send_message(
                "Only the tester who opened the queue or staff can call the next player.", ephemeral=True
            )
            return

        # Block if session already in progress
        session = get_active_session(gamemode)
        if session:
            await interaction.response.send_message(
                f"❌ A session is already in progress for **{get_gamemode_display_name(gamemode)}**. "
                f"Close the current ticket first.",
                ephemeral=True,
            )
            return

        next_player = queue["players"].pop(0)
        queue["called_players"].append(next_player.discord_id)

        # Remove queue role from called player
        queue_role = await get_queue_role(interaction.guild, gamemode)
        if queue_role:
            player_member = interaction.guild.get_member(next_player.discord_id)
            if player_member:
                try:
                    await player_member.remove_roles(queue_role, reason="Called from queue")
                except Exception:
                    pass

        await update_queue_message(gamemode, interaction.guild)

        guild = interaction.guild
        category = guild.get_channel(TICKET_CREATE_CATEGORY_ID)
        if not category or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("❌ Ticket category not found.", ephemeral=True)
            return

        channel_name = f"{gamemode}-{next_player.minecraft_name}".lower().replace(" ", "-")[:50]
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.get_member(next_player.discord_id): discord.PermissionOverwrite(
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
                topic=f"owner={next_player.discord_id} | mode={gamemode} | mc={next_player.minecraft_name}",
                reason=f"Queue ticket for {next_player.minecraft_name}",
            )

            set_active_session(gamemode, member.id, next_player.discord_id, channel.id)

            display = get_gamemode_display_name(gamemode)
            embed = discord.Embed(
                title="Testing Channel Created",
                description=f"<@{member.id}> → a testing channel has been created for <@{next_player.discord_id}>",
                color=discord.Color.blurple(),
            )
            embed.set_thumbnail(url=f"https://minotar.net/helm/{next_player.minecraft_name}/128.png")
            embed.add_field(name="Name", value=next_player.minecraft_name, inline=True)
            embed.add_field(name="Gamemode", value=display, inline=True)
            embed.add_field(name="Region", value=queue.get("region", "Default"), inline=True)

            ticket_view = CloseTicketView(owner_id=next_player.discord_id, mode_key=gamemode)
            await channel.send(
                content=f"<@{next_player.discord_id}>",
                embed=embed,
                view=ticket_view,
            )

            # DM the called player
            try:
                player_member = guild.get_member(next_player.discord_id)
                if player_member:
                    await player_member.send(
                        f"✅ You have been called for **{display}** testing! "
                        f"Head to {channel.mention} in the server."
                    )
            except Exception:
                pass

            await interaction.response.send_message(
                f"✅ Called **{next_player.minecraft_name}** → {channel.mention}", ephemeral=True
            )

        except Exception as e:
            await interaction.response.send_message(f"❌ Error creating channel: {e}", ephemeral=True)


class QueueActionView(discord.ui.View):
    def __init__(self, gamemode: str):
        super().__init__(timeout=None)
        self.gamemode = gamemode
        self.add_item(JoinQueueButton(gamemode))
        self.add_item(LeaveQueueButton(gamemode))
        self.add_item(CloseQueueButton(gamemode))
        self.add_item(NextPlayerButton(gamemode))


class ConfirmCloseQueueView(discord.ui.View):
    def __init__(self, gamemode: str):
        super().__init__(timeout=30)
        self.gamemode = gamemode

    @discord.ui.button(label="Yes, close it", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        from main import bot
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Error.", ephemeral=True)
            return

        queue = ACTIVE_QUEUES.get(self.gamemode)
        if queue:
            if queue["opened_by"] != member.id and not is_staff_member(member):
                await interaction.response.send_message(
                    "Only the tester who opened the queue can close it.", ephemeral=True
                )
                return

            # Strip queue role from all waiting players
            if interaction.guild:
                queue_role = await get_queue_role(interaction.guild, self.gamemode)
                for p in queue["players"]:
                    if queue_role:
                        pm = interaction.guild.get_member(p.discord_id)
                        if pm:
                            try:
                                await pm.remove_roles(queue_role, reason="Queue closed")
                            except Exception:
                                pass

            save_queue_last_session(self.gamemode)
            clear_active_session(self.gamemode)
            del ACTIVE_QUEUES[self.gamemode]

            await interaction.response.send_message(
                f"✅ **{get_gamemode_display_name(self.gamemode)}** queue closed.", ephemeral=True
            )
        else:
            await interaction.response.defer(ephemeral=True)

        if interaction.guild:
            await refresh_queue_panel(interaction.guild)

        # Edit the open message to closed embed
        try:
            msg_id = next((mid for mid, gm in list(QUEUE_MESSAGE_IDS.items()) if gm == self.gamemode), None)
            if msg_id:
                channel_id = QUEUE_CHANNELS.get(self.gamemode)
                if channel_id:
                    channel = bot.get_channel(channel_id)
                    if channel and isinstance(channel, discord.TextChannel):
                        msg = await channel.fetch_message(msg_id)
                        await msg.edit(embed=_build_closed_embed(self.gamemode), view=None)
                        QUEUE_MESSAGE_IDS.pop(msg_id, None)
                        persist_queue_message_ids(QUEUE_MESSAGE_IDS)
        except Exception:
            pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("❌ Cancelled.", ephemeral=True)


class PingRoleSelect(discord.ui.Select):
    def __init__(self, selected_gamemodes: List[str] = None):
        self.selected_gamemodes = selected_gamemodes or []
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                description=f"Ping notifications for {label} queue",
                default=key in self.selected_gamemodes,
            )
            for label, key, _rid in TICKET_TYPES
        ]
        super().__init__(
            placeholder="Select queues to receive pings for... (empty = disable all)",
            min_values=0,
            max_values=len(TICKET_TYPES),
            options=options,
            custom_id="ping_queue_select",
        )

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Error: not a member.", ephemeral=True)
            return

        guild = member.guild
        selected_gms = set(self.values)
        added, removed, errors = [], [], []

        for gm, role_id in QUEUE_PING_ROLES.items():
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
                    errors.append(f"Failed to add {role.name}: {e}")
            elif not should_have and has_role:
                try:
                    await member.remove_roles(role, reason="Ping preference via /pingpanel")
                    removed.append(role.name)
                except Exception as e:
                    errors.append(f"Failed to remove {role.name}: {e}")

        parts = []
        if added:
            parts.append(f"✅ Added: {', '.join(added)}")
        if removed:
            parts.append(f"❌ Removed: {', '.join(removed)}")
        if not added and not removed:
            parts.append("No changes.")
        if errors:
            parts.append("\nErrors:\n" + "\n".join(errors))

        await interaction.response.send_message("\n".join(parts), ephemeral=True)


class ClearAllPingsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="❌ Clear All Pings",
            style=discord.ButtonStyle.danger,
            custom_id="clear_all_pings",
        )

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Error: not a member.", ephemeral=True)
            return

        guild = member.guild
        removed, errors = [], []

        for gm, role_id in QUEUE_PING_ROLES.items():
            role = guild.get_role(role_id)
            if not role:
                continue
            if any(r.id == role_id for r in member.roles):
                try:
                    await member.remove_roles(role, reason="Ping preference clear all")
                    removed.append(role.name)
                except Exception as e:
                    errors.append(f"Failed to remove {role.name}: {e}")

        parts = []
        if removed:
            parts.append(f"❌ Removed: {', '.join(removed)}")
        else:
            parts.append("No pings enabled.")
        if errors:
            parts.append("\nErrors:\n" + "\n".join(errors))

        await interaction.response.send_message("\n".join(parts), ephemeral=True)


class PingPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PingRoleSelect())
        self.add_item(ClearAllPingsButton())


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
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Error: server only.", ephemeral=True)
            return

        if not is_gamemode_tester_or_admin(interaction.user, self.mode_key):
            await interaction.response.send_message(
                "❌ Only testers for this gamemode can open a queue.",
                ephemeral=True,
            )
            return

        # If already an active tester in this queue, block
        queue = ACTIVE_QUEUES.get(self.mode_key)
        if queue and any(t.discord_id == interaction.user.id for t in queue.get("testers", [])):
            await interaction.response.send_message(
                f"❌ You are already on duty for the **{self.mode_label}** queue.",
                ephemeral=True,
            )
            return

        # Show region select
        view = RegionSelectView(self.mode_key, self.mode_label)
        await interaction.response.send_message("Select your region:", view=view, ephemeral=True)


# COG

class QueueCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="queuepanel", description="Post the queue panel (staff only).")
    async def queuepanel(self, interaction: discord.Interaction):
        global QUEUE_PANEL_MESSAGE
        await interaction.response.defer(ephemeral=True)

        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Error.", ephemeral=True)
            return
        if not is_staff_member(interaction.user):
            await interaction.followup.send("No permission.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Open Queue",
            description="Click a button below to open a queue for your gamemode.",
            color=discord.Color.blurple(),
        )

        message = await interaction.channel.send(embed=embed, view=QueuePanelView())
        QUEUE_PANEL_MESSAGE = (interaction.channel.id, message.id)

        data = _load_data()
        data["queue_panel_message"] = [interaction.channel.id, message.id]
        _save_data(data)

        await interaction.followup.send("✅ Panel sent!", ephemeral=True)

    @app_commands.command(name="pingpanel", description="Set up ping notifications for queues.")
    async def pingpanel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                await interaction.followup.send("Error: server only.", ephemeral=True)
                return

            embed = discord.Embed(
                title="🔔 Queue Ping Settings",
                description="Select the queues you want to be notified for:",
                color=discord.Color.blue(),
            )
            await interaction.followup.send(embed=embed, view=PingPanelView())
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="closequeue", description="Close a queue (staff or queue opener).")
    @app_commands.describe(gamemode="The queue's gamemode.")
    @app_commands.choices(gamemode=_choices_from_list(MODE_LIST))
    async def closequeue(self, interaction: discord.Interaction, gamemode: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)

        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Error.", ephemeral=True)
            return

        mode_key = gamemode.value.lower()
        queue = ACTIVE_QUEUES.get(mode_key)

        if not queue:
            await interaction.followup.send(f"❌ The **{gamemode.name}** queue is not open.", ephemeral=True)
            return

        if queue.get("opened_by") != interaction.user.id and not is_staff_member(interaction.user):
            await interaction.followup.send(
                "Only the tester who opened the queue or staff can close it.", ephemeral=True
            )
            return

        # Strip queue roles
        queue_role = await get_queue_role(interaction.guild, mode_key)
        for p in queue["players"]:
            if queue_role:
                pm = interaction.guild.get_member(p.discord_id)
                if pm:
                    try:
                        await pm.remove_roles(queue_role, reason="Queue closed")
                    except Exception:
                        pass

        save_queue_last_session(mode_key)
        clear_active_session(mode_key)
        del ACTIVE_QUEUES[mode_key]

        await interaction.followup.send(f"✅ **{gamemode.name}** queue closed.", ephemeral=True)
        await refresh_queue_panel(interaction.guild)

        try:
            msg_id = next((mid for mid, gm in list(QUEUE_MESSAGE_IDS.items()) if gm == mode_key), None)
            if msg_id:
                channel_id = QUEUE_CHANNELS.get(mode_key)
                if channel_id:
                    channel = interaction.guild.get_channel(channel_id)
                    if channel and isinstance(channel, discord.TextChannel):
                        msg = await channel.fetch_message(msg_id)
                        await msg.edit(embed=_build_closed_embed(mode_key), view=None)
                        QUEUE_MESSAGE_IDS.pop(msg_id, None)
                        persist_queue_message_ids(QUEUE_MESSAGE_IDS)
        except Exception as e:
            print(f"Error updating queue message on close: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(QueueCog(bot))
