import asyncio
import datetime
import io
import os
import time
from typing import Dict, Any, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    TICKET_TYPES, QUEUE_CHANNELS, QUEUE_PING_ROLES, TICKET_CREATE_CATEGORY_ID,
    STAFF_ROLE_ID, MODE_LIST, RANKS, WEBSITE_URL, POINTS,
    get_gamemode_display_name, get_gamemode_color,
    _choices_from_list, MONTHLY_REPORT_CHANNEL_ID, TRANSCRIPT_CHANNEL_ID,
)
from storage import (
    cooldown_left, get_linked_minecraft_name, set_last_closed,
    _load_data, _save_data,
    persist_queue_message_ids,
    set_active_session, get_active_session, clear_active_session,
    save_queue_last_session, get_queue_last_session,
    register_ticket, get_ticket_meta, set_ticket_exempt, set_ticket_warning_sent,
    remove_ticket, get_all_tickets, update_ticket_embed_msg,
    increment_tester_stat, get_leaderboard, reset_monthly_stats,
    get_monthly_quota, set_monthly_quota,
    get_monthly_quota_last_check, set_monthly_quota_last_check,
    get_all_tester_stats,
)
from permissions import is_staff_member, is_gamemode_tester_or_admin, can_join_queue, get_player_rank_for_mode
from api import api_get_tests, api_post_test, _auth_headers

REGIONS = ["NA", "EU", "AS", "SA", "AU"]
TIER_CHOICES = [app_commands.Choice(name=r, value=r) for r in RANKS if r != "Unranked"]

AUTO_CLOSE_SECONDS = 3 * 24 * 3600       # 3 days
WARN_BEFORE_SECONDS = 15 * 60            # 15 min warning before auto-close

# MODULE-LEVEL STATE
ACTIVE_QUEUES: Dict[str, Dict[str, Any]] = {}
QUEUE_MESSAGE_IDS: Dict[int, str] = {}
QUEUE_PANEL_MESSAGE = None  # (channel_id, message_id)


class QueuePlayer:
    def __init__(self, discord_id: int, minecraft_name: str):
        self.discord_id = discord_id
        self.minecraft_name = minecraft_name
        self.joined_at = time.time()


# ─── HELPERS ────────────────────────────────────────────────────────────────

def _ticket_age_emoji(created_at: float) -> str:
    age = time.time() - created_at
    if age < 7 * 24 * 3600:
        return "🟢"
    if age < 14 * 24 * 3600:
        return "🟠"
    return "🔴"


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
    embed.set_footer(text="Join Queue to enter • Leave Queue to exit")
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
        try:
            await message.edit(embed=_build_closed_embed(gamemode), view=None)
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
            print(f"Error scanning channel {channel_id}: {e}")
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


async def _export_transcript(channel: discord.TextChannel, mc_name: str, mode_key: str, tester: discord.Member):
    """Export HTML transcript and post to the configured transcript channel."""
    try:
        import chat_exporter
        transcript = await chat_exporter.export(channel)
        if not transcript:
            return
        transcript_bytes = transcript.encode()
        filename = f"transcript-{mc_name}-{mode_key}.html"
        file = discord.File(io.BytesIO(transcript_bytes), filename=filename)

        from main import bot
        dest_id = TRANSCRIPT_CHANNEL_ID
        if not dest_id:
            try:
                dest_id = int(os.getenv("TIER_RESULTS_CHANNEL_ID", "0"))
            except ValueError:
                pass
        dest_channel = bot.get_channel(dest_id) if dest_id else None
        if not dest_channel:
            dest_channel = (
                discord.utils.get(channel.guild.text_channels, name="transcripts")
                or discord.utils.get(channel.guild.text_channels, name="test-results")
            )
        if dest_channel:
            await dest_channel.send(
                f"📄 Transcript — **{mc_name}** ({mode_key}) tested by {tester.mention}",
                file=file,
            )
    except ImportError:
        pass
    except Exception as e:
        print(f"Transcript export error: {e}")


async def _process_test_close(
    interaction: discord.Interaction,
    owner_id: int,
    mc_name: str,
    mode_key: str,
    tier: str,
    score: str,
    notes: Optional[str],
    tester: discord.Member,
):
    """Handle the full close flow: post results, export transcript, update stats, close channel."""
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel

    mode_display = get_gamemode_display_name(mode_key)
    prev_rank = "Unranked"
    prev_points = 0

    if WEBSITE_URL:
        try:
            res = await api_get_tests(username=mc_name, mode=mode_key)
            if res.get("status") == 200:
                data = res.get("data", {})
                test = data.get("test") or (data.get("tests") or [None])[0]
                if test:
                    prev_rank = str(test.get("rank", "Unranked")) or "Unranked"
                    prev_points = POINTS.get(prev_rank, 0)
        except Exception:
            pass

    new_points = POINTS.get(tier, 0)
    diff = new_points - prev_points
    points_str = f"+{diff}" if diff > 0 else ("±0" if diff == 0 else str(diff))

    embed = discord.Embed(
        title=f"{mc_name} — Test Result 🏆",
        color=discord.Color.dark_grey(),
    )
    embed.set_thumbnail(url=f"https://minotar.net/helm/{mc_name}/128.png")
    embed.add_field(name="Tester", value=tester.mention, inline=True)
    embed.add_field(name="Gamemode", value=mode_display, inline=True)
    embed.add_field(name="Minecraft Name", value=mc_name, inline=True)
    embed.add_field(name="Score", value=score, inline=True)
    embed.add_field(name="Previous Rank", value=f"{prev_rank} ({prev_points} pts)", inline=True)
    embed.add_field(name="Achieved Rank", value=f"{tier} ({new_points} pts) {points_str}", inline=True)
    if notes:
        embed.add_field(name="Notes", value=notes, inline=False)

    # Post to results channel
    results_channel = None
    try:
        rc_id = int(os.getenv("TIER_RESULTS_CHANNEL_ID", "0"))
        if rc_id:
            results_channel = interaction.guild.get_channel(rc_id)
    except ValueError:
        pass
    if not results_channel:
        results_channel = (
            discord.utils.get(interaction.guild.text_channels, name="test-results")
            or discord.utils.get(interaction.guild.text_channels, name="teszteredmenyek")
            or discord.utils.get(interaction.guild.text_channels, name="eredmenyek")
        )
    if results_channel:
        await results_channel.send(embed=embed)

    # Export transcript before channel is deleted
    if isinstance(channel, discord.TextChannel):
        await _export_transcript(channel, mc_name, mode_key, tester)

    # Save rank to website
    result_msg = f"✅ Tier set: **{tier}**"
    if WEBSITE_URL:
        try:
            save = await api_post_test(username=mc_name, mode=mode_display, rank=tier, tester=tester)
            if save.get("status") in (200, 201):
                result_msg = f"✅ Tier set: **{tier}** — saved to website!"
        except Exception:
            pass

    # Stats & cooldown
    increment_tester_stat(tester.id, mode_key)
    set_last_closed(owner_id, mode_key, time.time())
    clear_active_session(mode_key)
    if isinstance(channel, discord.TextChannel):
        remove_ticket(channel.id)

    await interaction.followup.send(f"{result_msg}\nChannel closes in 5 seconds.", ephemeral=True)

    if isinstance(channel, discord.TextChannel):
        await asyncio.sleep(5)
        try:
            await channel.delete(reason="Test completed")
        except Exception:
            pass


# ─── REGION SELECT ───────────────────────────────────────────────────────────

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


# ─── CORE QUEUE LOGIC ────────────────────────────────────────────────────────

async def _do_open_queue(
    interaction: discord.Interaction,
    mode_key: str,
    mode_label: str,
    region: str,
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("Error: server only.", ephemeral=True)
        return

    if mode_key in ACTIVE_QUEUES:
        queue = ACTIVE_QUEUES[mode_key]
        if any(t.discord_id == interaction.user.id for t in queue.get("testers", [])):
            await interaction.followup.send(
                f"❌ You are already on duty for the **{mode_label}** queue.", ephemeral=True
            )
            return
        linked_mc = get_linked_minecraft_name(interaction.user.id) or "TESTER"
        ACTIVE_QUEUES[mode_key]["testers"].append(QueuePlayer(interaction.user.id, linked_mc))
        await update_queue_message(mode_key, interaction.guild)

        ping_role_id = QUEUE_PING_ROLES.get(mode_key)
        ping_text = f"<@&{ping_role_id}> " if ping_role_id else ""
        await interaction.followup.send(
            f"✅ You joined the existing **{mode_label}** queue as a tester ({region}).",
            ephemeral=True,
        )
        return

    channel_id = QUEUE_CHANNELS.get(mode_key)
    if not channel_id:
        await interaction.followup.send(f"❌ No channel configured for **{mode_label}**.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        await interaction.followup.send(f"❌ Queue channel not found.", ephemeral=True)
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
        f"✅ **{mode_label}** queue opened in {channel.mention} (Region: **{region}**)!\nUse `/next {mode_key}` to call players and `/stop {mode_key}` when you're done.",
        ephemeral=True,
    )
    await refresh_queue_panel(interaction.guild)


async def _do_close_queue(mode_key: str, guild: discord.Guild, closer_id: int):
    """Strip roles, clean up ACTIVE_QUEUES, update embed. Returns display name."""
    queue = ACTIVE_QUEUES.get(mode_key)
    if not queue:
        return

    queue_role = await get_queue_role(guild, mode_key)
    for p in queue.get("players", []):
        if queue_role:
            pm = guild.get_member(p.discord_id)
            if pm:
                try:
                    await pm.remove_roles(queue_role, reason="Queue closed")
                except Exception:
                    pass

    save_queue_last_session(mode_key)
    clear_active_session(mode_key)
    del ACTIVE_QUEUES[mode_key]

    # Edit message to closed embed
    try:
        from main import bot
        msg_id = next((mid for mid, gm in list(QUEUE_MESSAGE_IDS.items()) if gm == mode_key), None)
        if msg_id:
            ch_id = QUEUE_CHANNELS.get(mode_key)
            if ch_id:
                ch = bot.get_channel(ch_id)
                if ch and isinstance(ch, discord.TextChannel):
                    msg = await ch.fetch_message(msg_id)
                    await msg.edit(embed=_build_closed_embed(mode_key), view=None)
                    QUEUE_MESSAGE_IDS.pop(msg_id, None)
                    persist_queue_message_ids(QUEUE_MESSAGE_IDS)
    except Exception:
        pass

    await refresh_queue_panel(guild)


# ─── QUEUE ACTION BUTTONS ────────────────────────────────────────────────────

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
        if is_gamemode_tester_or_admin(member, gamemode):
            await interaction.response.send_message("❌ You are a tester for this gamemode.", ephemeral=True)
            return

        linked_mc = get_linked_minecraft_name(member.id)
        if not linked_mc:
            await interaction.response.send_message(
                "❌ Link your Minecraft account first with `/link`.", ephemeral=True
            )
            return

        cd_left = cooldown_left(member.id, gamemode)
        if cd_left > 0:
            days = cd_left // (24 * 3600)
            hours = (cd_left % (24 * 3600)) // 3600
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
            f"✅ You joined the **{get_gamemode_display_name(gamemode)}** queue! Position: **#{pos}**.",
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
                    f"✅ You left the **{get_gamemode_display_name(gamemode)}** queue.", ephemeral=True
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
            label="▶ Next Player",
            style=discord.ButtonStyle.primary,
            custom_id=f"queue_next_{gamemode}",
        )
        self.gamemode = gamemode

    async def callback(self, interaction: discord.Interaction):
        await _handle_next_player(interaction, self.gamemode)


async def _handle_next_player(interaction: discord.Interaction, gamemode: str):
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if not member:
        await interaction.response.send_message("Error: not a member.", ephemeral=True)
        return

    queue = ACTIVE_QUEUES.get(gamemode)
    if not queue:
        await interaction.response.send_message("❌ No active queue for this gamemode.", ephemeral=True)
        return
    if not queue["players"]:
        await interaction.response.send_message("❌ No players in the queue.", ephemeral=True)
        return
    if not is_staff_member(member) and not is_gamemode_tester_or_admin(member, gamemode):
        await interaction.response.send_message(
            "❌ Only testers or staff can call the next player.", ephemeral=True
        )
        return

    session = get_active_session(gamemode)
    if session:
        await interaction.response.send_message(
            f"❌ A session is already in progress for **{get_gamemode_display_name(gamemode)}**. Close the current ticket first.",
            ephemeral=True,
        )
        return

    next_player = queue["players"].pop(0)
    queue["called_players"].append(next_player.discord_id)

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
            member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
        }
        if STAFF_ROLE_ID:
            staff_role = guild.get_role(STAFF_ROLE_ID)
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True, manage_channels=True
                )

        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"owner={next_player.discord_id} | mode={gamemode} | mc={next_player.minecraft_name}",
            reason=f"Queue ticket for {next_player.minecraft_name}",
        )

        set_active_session(gamemode, member.id, next_player.discord_id, ticket_channel.id)

        display = get_gamemode_display_name(gamemode)
        meta = get_ticket_meta(ticket_channel.id)  # not yet registered
        # Build initial embed with 🟢 aging
        ticket_embed = discord.Embed(
            title=f"🟢 Testing Session — {display}",
            color=get_gamemode_color(gamemode),
        )
        ticket_embed.set_thumbnail(url=f"https://minotar.net/helm/{next_player.minecraft_name}/128.png")
        ticket_embed.add_field(name="Player", value=f"<@{next_player.discord_id}> (`{next_player.minecraft_name}`)", inline=False)
        ticket_embed.add_field(name="Tester", value=member.mention, inline=True)
        ticket_embed.add_field(name="Gamemode", value=display, inline=True)
        ticket_embed.add_field(name="Region", value=queue.get("region", "?"), inline=True)
        ticket_embed.add_field(
            name="Commands",
            value=(
                "`/close <tier> <score>` — finish and assign tier\n"
                "`/skip` — close as discontinued\n"
                "`/exempt` — prevent auto-close\n"
                "`/lock` — toggle player messaging\n"
                "`/add @user` / `/remove @user` — manage access"
            ),
            inline=False,
        )
        ticket_embed.set_footer(text="Auto-closes in 3 days unless exempted.")

        ticket_msg = await ticket_channel.send(
            content=f"<@{next_player.discord_id}>",
            embed=ticket_embed,
        )

        # Register ticket metadata
        register_ticket(ticket_channel.id, next_player.discord_id, member.id, gamemode, ticket_msg.id)

        # DM the player
        try:
            player_member = guild.get_member(next_player.discord_id)
            if player_member:
                await player_member.send(
                    f"✅ You've been called for **{display}** testing! Head to {ticket_channel.mention}."
                )
        except Exception:
            pass

        if interaction.response.is_done():
            await interaction.followup.send(
                f"✅ Called **{next_player.minecraft_name}** → {ticket_channel.mention}", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"✅ Called **{next_player.minecraft_name}** → {ticket_channel.mention}", ephemeral=True
            )

    except Exception as e:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ Error creating channel: {e}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Error creating channel: {e}", ephemeral=True)


class QueueActionView(discord.ui.View):
    def __init__(self, gamemode: str = ""):
        super().__init__(timeout=None)
        if gamemode:
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
            await interaction.response.send_message(
                f"✅ **{get_gamemode_display_name(self.gamemode)}** queue closed.", ephemeral=True
            )
            await _do_close_queue(self.gamemode, interaction.guild, member.id)
        else:
            await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("❌ Cancelled.", ephemeral=True)


# ─── PING PANEL ──────────────────────────────────────────────────────────────

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
            placeholder="Select queues to receive pings for...",
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
                    await member.add_roles(role, reason="Ping preference")
                    added.append(role.name)
                except Exception as e:
                    errors.append(f"Failed to add {role.name}: {e}")
            elif not should_have and has_role:
                try:
                    await member.remove_roles(role, reason="Ping preference")
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
            parts.append("Errors:\n" + "\n".join(errors))
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

        removed, errors = [], []
        for gm, role_id in QUEUE_PING_ROLES.items():
            role = member.guild.get_role(role_id)
            if not role:
                continue
            if any(r.id == role_id for r in member.roles):
                try:
                    await member.remove_roles(role, reason="Clear all pings")
                    removed.append(role.name)
                except Exception as e:
                    errors.append(f"Failed to remove {role.name}: {e}")

        parts = [f"❌ Removed: {', '.join(removed)}"] if removed else ["No pings enabled."]
        if errors:
            parts.append("Errors:\n" + "\n".join(errors))
        await interaction.response.send_message("\n".join(parts), ephemeral=True)


class PingPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PingRoleSelect())
        self.add_item(ClearAllPingsButton())


# ─── QUEUE PANEL ─────────────────────────────────────────────────────────────

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
                "❌ Only testers for this gamemode can open a queue.", ephemeral=True
            )
            return
        view = RegionSelectView(self.mode_key, self.mode_label)
        await interaction.response.send_message("Select your region:", view=view, ephemeral=True)


class QueuePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for label, key, _rid in TICKET_TYPES:
            self.add_item(QueueOpenButton(label=label, mode_key=key))


# ─── BACKGROUND TASKS ────────────────────────────────────────────────────────

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


async def ticket_lifecycle_task():
    """Auto-close tickets after 3 days, send warning 15 min before, update aging emoji."""
    from main import bot
    while not bot.is_closed():
        try:
            await asyncio.sleep(60)
            all_tickets = get_all_tickets()

            for channel_id_str, meta in list(all_tickets.items()):
                channel_id = int(channel_id_str)
                channel = bot.get_channel(channel_id)

                if not channel:
                    remove_ticket(channel_id)
                    continue

                created_at = meta.get("created_at", time.time())
                age = time.time() - created_at
                exempt = meta.get("exempt", False)
                warning_sent = meta.get("warning_sent", False)

                # Update aging emoji in channel name
                try:
                    new_emoji = _ticket_age_emoji(created_at)
                    name = channel.name
                    current_emoji = next((e for e in ["🟢", "🟠", "🔴"] if name.startswith(e)), None)
                    if current_emoji is None:
                        await channel.edit(name=f"{new_emoji}-{name}")
                    elif current_emoji != new_emoji:
                        await channel.edit(name=f"{new_emoji}{name[len(current_emoji):]}")
                except Exception:
                    pass

                # Update ticket embed if embed_msg_id is stored
                embed_msg_id = meta.get("embed_msg_id", 0)
                if embed_msg_id:
                    try:
                        embed_msg = await channel.fetch_message(embed_msg_id)
                        if embed_msg.embeds:
                            old_embed = embed_msg.embeds[0]
                            new_title = old_embed.title or ""
                            for old_e in ["🟢", "🟠", "🔴"]:
                                new_title = new_title.replace(old_e, "")
                            new_e = _ticket_age_emoji(created_at)
                            new_title = f"{new_e}{new_title.lstrip()}"
                            new_embed = old_embed.copy()
                            new_embed.title = new_title
                            await embed_msg.edit(embed=new_embed)
                    except Exception:
                        pass

                if exempt:
                    continue

                # Warning 15 min before close
                if age >= AUTO_CLOSE_SECONDS - WARN_BEFORE_SECONDS and not warning_sent:
                    try:
                        await channel.send(
                            "⚠️ This ticket will be **automatically closed in 15 minutes**. "
                            "Use `/exempt` to prevent this."
                        )
                        set_ticket_warning_sent(channel_id)
                    except Exception:
                        pass

                # Auto-close
                if age >= AUTO_CLOSE_SECONDS:
                    try:
                        gamemode = meta.get("gamemode", "")
                        clear_active_session(gamemode)
                        remove_ticket(channel_id)
                        await channel.send("🔒 Ticket auto-closed after 3 days of inactivity.")
                        await asyncio.sleep(3)
                        await channel.delete(reason="Auto-close: 3 day limit reached")
                    except Exception as e:
                        print(f"[TicketLifecycle] Auto-close error for {channel_id}: {e}")

        except Exception as e:
            print(f"[TicketLifecycle] Fatal: {e}")


async def monthly_quota_task():
    """On the 1st of each month, check tester quota and post report."""
    from main import bot
    while not bot.is_closed():
        try:
            await asyncio.sleep(3600)  # check hourly
            now = datetime.datetime.utcnow()
            current_month = now.strftime("%Y-%m")
            last_check = get_monthly_quota_last_check()

            if now.day == 1 and current_month != last_check:
                set_monthly_quota_last_check(current_month)
                await _run_monthly_quota_check(bot)
                reset_monthly_stats()
        except Exception as e:
            print(f"[MonthlyQuota] Fatal: {e}")


async def _run_monthly_quota_check(bot: commands.Bot):
    quota = get_monthly_quota()
    all_stats = get_all_tester_stats()
    leaderboard = get_leaderboard("monthly")

    report_channel = None
    if MONTHLY_REPORT_CHANNEL_ID:
        for guild in bot.guilds:
            ch = guild.get_channel(MONTHLY_REPORT_CHANNEL_ID)
            if ch:
                report_channel = ch
                break

    demoted = []
    met_quota = []

    for guild in bot.guilds:
        all_tester_role_ids = {role_id for _, _, role_id in TICKET_TYPES if role_id}

        for entry in all_stats.items():
            tester_id_str, stats = entry
            tester_id = int(tester_id_str)
            monthly_total = sum(stats.get("monthly", {}).values())
            member = guild.get_member(tester_id)
            if not member:
                continue

            tester_roles = [r for r in member.roles if r.id in all_tester_role_ids]
            if not tester_roles:
                continue

            if monthly_total < quota:
                demoted.append((member, monthly_total, tester_roles))
                for role in tester_roles:
                    try:
                        await member.remove_roles(role, reason=f"Monthly quota not met ({monthly_total}/{quota})")
                    except Exception:
                        pass
            else:
                met_quota.append((member, monthly_total))

    if report_channel:
        prev_month = (datetime.datetime.utcnow().replace(day=1) - datetime.timedelta(days=1)).strftime("%B %Y")
        embed = discord.Embed(
            title=f"📊 Monthly Tester Report — {prev_month}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Quota", value=f"{quota} tests/month", inline=True)

        lb_lines = []
        for i, entry in enumerate(leaderboard[:10], 1):
            member_obj = report_channel.guild.get_member(entry["tester_id"])
            name = member_obj.display_name if member_obj else str(entry["tester_id"])
            lb_lines.append(f"**{i}.** {name} — {entry['total']} tests")
        if lb_lines:
            embed.add_field(name="🏆 Top Testers", value="\n".join(lb_lines), inline=False)

        if demoted:
            dem_lines = [f"{m.mention} ({count} tests)" for m, count, _ in demoted]
            embed.add_field(
                name=f"⬇️ Demoted ({len(demoted)})",
                value="\n".join(dem_lines) or "None",
                inline=False,
            )

        await report_channel.send(embed=embed)


# ─── COG ─────────────────────────────────────────────────────────────────────

class QueueCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Admin/Panel commands ──────────────────────────────────────────────────

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
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Error: server only.", ephemeral=True)
            return
        embed = discord.Embed(
            title="🔔 Queue Ping Settings",
            description="Select the queues you want to be notified for:",
            color=discord.Color.blue(),
        )
        await interaction.followup.send(embed=embed, view=PingPanelView())

    # ── Tester workflow commands ──────────────────────────────────────────────

    @app_commands.command(name="start", description="Open a queue and go on duty as a tester.")
    @app_commands.describe(gamemode="Gamemode to open queue for.", region="Your region.")
    @app_commands.choices(
        gamemode=_choices_from_list(MODE_LIST),
        region=[app_commands.Choice(name=r, value=r) for r in REGIONS],
    )
    async def start(self, interaction: discord.Interaction, gamemode: app_commands.Choice[str], region: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Error: server only.", ephemeral=True)
            return

        mode_key = gamemode.value.lower()
        mode_label = gamemode.name

        if not is_gamemode_tester_or_admin(interaction.user, mode_key):
            await interaction.followup.send("❌ You are not a tester for this gamemode.", ephemeral=True)
            return

        await _do_open_queue(interaction, mode_key, mode_label, region.value)

    @app_commands.command(name="stop", description="Go off duty and close your queue.")
    @app_commands.describe(gamemode="Gamemode queue to close.")
    @app_commands.choices(gamemode=_choices_from_list(MODE_LIST))
    async def stop(self, interaction: discord.Interaction, gamemode: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Error.", ephemeral=True)
            return

        mode_key = gamemode.value.lower()
        queue = ACTIVE_QUEUES.get(mode_key)
        if not queue:
            await interaction.followup.send(f"❌ The **{gamemode.name}** queue is not open.", ephemeral=True)
            return

        testers = queue.get("testers", [])
        caller_tester = next((t for t in testers if t.discord_id == interaction.user.id), None)

        if not caller_tester and not is_staff_member(interaction.user):
            await interaction.followup.send("❌ You are not on duty for this queue.", ephemeral=True)
            return

        if caller_tester:
            testers.remove(caller_tester)

        if not testers:
            # No testers left — close the queue
            await interaction.followup.send(
                f"✅ You went off duty. No testers remain — closing the **{gamemode.name}** queue.",
                ephemeral=True,
            )
            await _do_close_queue(mode_key, interaction.guild, interaction.user.id)
        else:
            await update_queue_message(mode_key, interaction.guild)
            await interaction.followup.send(
                f"✅ You went off duty for **{gamemode.name}**. "
                f"{len(testers)} tester(s) still active.",
                ephemeral=True,
            )

    @app_commands.command(name="next", description="Call the next player in queue.")
    @app_commands.describe(gamemode="Gamemode queue to call from.")
    @app_commands.choices(gamemode=_choices_from_list(MODE_LIST))
    async def next_player_cmd(self, interaction: discord.Interaction, gamemode: app_commands.Choice[str]):
        await _handle_next_player(interaction, gamemode.value.lower())

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

        await _do_close_queue(mode_key, interaction.guild, interaction.user.id)
        await interaction.followup.send(f"✅ **{gamemode.name}** queue closed.", ephemeral=True)

    # ── In-ticket commands ────────────────────────────────────────────────────

    @app_commands.command(name="close", description="Close a testing ticket and assign a tier.")
    @app_commands.describe(
        tier="Tier to assign to the player.",
        score="Match score (e.g. 8-5).",
        notes="Optional test notes.",
    )
    @app_commands.choices(tier=TIER_CHOICES)
    async def close_ticket(
        self,
        interaction: discord.Interaction,
        tier: app_commands.Choice[str],
        score: str,
        notes: Optional[str] = None,
    ):
        channel = interaction.channel
        member = interaction.user
        if not isinstance(member, discord.Member) or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Error: must be used in a server text channel.", ephemeral=True)
            return

        if not is_staff_member(member) and not any(
            is_gamemode_tester_or_admin(member, key) for _, key, _ in TICKET_TYPES
        ):
            await interaction.response.send_message("❌ You don't have permission to close tickets.", ephemeral=True)
            return

        owner_id, mode_key, mc_name = _parse_topic(channel.topic or "")
        if not owner_id or not mode_key:
            await interaction.response.send_message(
                "❌ This doesn't appear to be a testing ticket channel.", ephemeral=True
            )
            return
        if not mc_name:
            from storage import get_linked_minecraft_name as _get_mc
            mc_name = _get_mc(owner_id) or "Unknown"

        await _process_test_close(
            interaction, owner_id, mc_name, mode_key,
            tier.value, score, notes, member
        )

    @app_commands.command(name="skip", description="Close a ticket as discontinued (no tier assigned).")
    @app_commands.describe(reason="Optional reason for skipping.")
    async def skip(self, interaction: discord.Interaction, reason: Optional[str] = None):
        channel = interaction.channel
        member = interaction.user
        if not isinstance(member, discord.Member) or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Error: must be used in a server text channel.", ephemeral=True)
            return
        if not is_staff_member(member) and not any(
            is_gamemode_tester_or_admin(member, key) for _, key, _ in TICKET_TYPES
        ):
            await interaction.response.send_message("❌ You don't have permission to skip tickets.", ephemeral=True)
            return

        owner_id, mode_key, mc_name = _parse_topic(channel.topic or "")
        if not owner_id or not mode_key:
            await interaction.response.send_message("❌ Not a testing ticket channel.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"🚫 Ticket closed as **Discontinued**{f' — {reason}' if reason else ''}. Channel closes in 5 seconds.",
            ephemeral=False,
        )

        clear_active_session(mode_key)
        remove_ticket(channel.id)
        # No cooldown applied on skip
        await asyncio.sleep(5)
        try:
            await channel.delete(reason="Ticket skipped/discontinued")
        except Exception:
            pass

    @app_commands.command(name="exempt", description="Exempt this ticket from the 3-day auto-close.")
    async def exempt(self, interaction: discord.Interaction):
        channel = interaction.channel
        member = interaction.user
        if not isinstance(member, discord.Member) or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Error.", ephemeral=True)
            return
        if not is_staff_member(member):
            await interaction.response.send_message("❌ Staff only.", ephemeral=True)
            return

        meta = get_ticket_meta(channel.id)
        if not meta:
            await interaction.response.send_message("❌ This is not a tracked testing ticket.", ephemeral=True)
            return

        currently_exempt = meta.get("exempt", False)
        set_ticket_exempt(channel.id, not currently_exempt)
        if currently_exempt:
            await interaction.response.send_message("✅ Auto-close re-enabled for this ticket.", ephemeral=True)
        else:
            await interaction.response.send_message("✅ This ticket is now **exempt** from auto-close.", ephemeral=True)

    @app_commands.command(name="add", description="Add a user to this ticket channel.")
    @app_commands.describe(user="The user to add.")
    async def add_user(self, interaction: discord.Interaction, user: discord.Member):
        channel = interaction.channel
        member = interaction.user
        if not isinstance(member, discord.Member) or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Error.", ephemeral=True)
            return
        if not is_staff_member(member) and not any(
            is_gamemode_tester_or_admin(member, key) for _, key, _ in TICKET_TYPES
        ):
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return

        await channel.set_permissions(
            user,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        )
        await interaction.response.send_message(f"✅ Added {user.mention} to this ticket.", ephemeral=True)

    @app_commands.command(name="remove", description="Remove a user from this ticket channel.")
    @app_commands.describe(user="The user to remove.")
    async def remove_user(self, interaction: discord.Interaction, user: discord.Member):
        channel = interaction.channel
        member = interaction.user
        if not isinstance(member, discord.Member) or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Error.", ephemeral=True)
            return
        if not is_staff_member(member) and not any(
            is_gamemode_tester_or_admin(member, key) for _, key, _ in TICKET_TYPES
        ):
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return

        owner_id, _, _ = _parse_topic(channel.topic or "")
        if user.id == owner_id:
            await interaction.response.send_message("❌ Cannot remove the ticket owner.", ephemeral=True)
            return

        await channel.set_permissions(user, overwrite=None)
        await interaction.response.send_message(f"✅ Removed {user.mention} from this ticket.", ephemeral=True)

    @app_commands.command(name="lock", description="Toggle player messaging in this ticket channel.")
    async def lock(self, interaction: discord.Interaction):
        channel = interaction.channel
        member = interaction.user
        if not isinstance(member, discord.Member) or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Error.", ephemeral=True)
            return
        if not is_staff_member(member) and not any(
            is_gamemode_tester_or_admin(member, key) for _, key, _ in TICKET_TYPES
        ):
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return

        owner_id, _, _ = _parse_topic(channel.topic or "")
        if not owner_id:
            await interaction.response.send_message("❌ Not a testing ticket channel.", ephemeral=True)
            return

        owner_member = channel.guild.get_member(owner_id)
        if not owner_member:
            await interaction.response.send_message("❌ Cannot find ticket owner.", ephemeral=True)
            return

        current_perms = channel.permissions_for(owner_member)
        if current_perms.send_messages:
            await channel.set_permissions(owner_member, send_messages=False, view_channel=True, read_message_history=True)
            await interaction.response.send_message("🔒 Player messaging locked.", ephemeral=False)
        else:
            await channel.set_permissions(owner_member, send_messages=True, view_channel=True, read_message_history=True)
            await interaction.response.send_message("🔓 Player messaging unlocked.", ephemeral=False)

    @app_commands.command(name="rename", description="Rename this ticket channel.")
    @app_commands.describe(name="New channel name.")
    async def rename(self, interaction: discord.Interaction, name: str):
        channel = interaction.channel
        member = interaction.user
        if not isinstance(member, discord.Member) or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Error.", ephemeral=True)
            return
        if not is_staff_member(member) and not any(
            is_gamemode_tester_or_admin(member, key) for _, key, _ in TICKET_TYPES
        ):
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return

        safe_name = name.lower().replace(" ", "-")[:50]
        await channel.edit(name=safe_name)
        await interaction.response.send_message(f"✅ Renamed to `{safe_name}`.", ephemeral=True)

    # ── Info/utility commands ─────────────────────────────────────────────────

    @app_commands.command(name="queuepos", description="Check your position in a queue.")
    @app_commands.describe(gamemode="The gamemode to check.")
    @app_commands.choices(gamemode=_choices_from_list(MODE_LIST))
    async def queuepos(self, interaction: discord.Interaction, gamemode: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        mode_key = gamemode.value.lower()
        queue = ACTIVE_QUEUES.get(mode_key)

        if not queue:
            await interaction.followup.send(
                f"❌ The **{gamemode.name}** queue is not currently open.", ephemeral=True
            )
            return

        for i, p in enumerate(queue["players"]):
            if p.discord_id == interaction.user.id:
                total = len(queue["players"])
                await interaction.followup.send(
                    f"You are **#{i + 1}** of **{total}** in the **{gamemode.name}** queue.",
                    ephemeral=True,
                )
                return

        await interaction.followup.send(
            f"You are not in the **{gamemode.name}** queue.", ephemeral=True
        )

    @app_commands.command(name="pending", description="Show all players currently in queues (staff only).")
    async def pending(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not is_staff_member(interaction.user):
            await interaction.followup.send("❌ Staff only.", ephemeral=True)
            return

        if not ACTIVE_QUEUES:
            await interaction.followup.send("No queues are currently open.", ephemeral=True)
            return

        embed = discord.Embed(title="📋 Active Queues", color=discord.Color.blurple())
        for gm, queue in ACTIVE_QUEUES.items():
            display = get_gamemode_display_name(gm)
            players = queue.get("players", [])
            testers = queue.get("testers", [])
            if not players and not testers:
                continue
            guild = interaction.guild
            def fmt(p):
                m = guild.get_member(p.discord_id) if guild else None
                return f"{m.display_name if m else p.discord_id} ({p.minecraft_name})"

            player_lines = [f"{i+1}. {fmt(p)}" for i, p in enumerate(players)] or ["None"]
            tester_lines = [fmt(t) for t in testers] or ["None"]
            embed.add_field(
                name=f"{display} — {queue.get('region', '?')}",
                value=(
                    f"**Testers:** {', '.join(tester_lines)}\n"
                    f"**Queue ({len(players)}):**\n" + "\n".join(player_lines)
                ),
                inline=False,
            )

        if not embed.fields:
            await interaction.followup.send("No players are queued.", ephemeral=True)
            return
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Stats commands ────────────────────────────────────────────────────────

    @app_commands.command(name="leaderboard", description="Show the tester leaderboard.")
    @app_commands.describe(period="Monthly or all-time stats.")
    @app_commands.choices(period=[
        app_commands.Choice(name="Monthly", value="monthly"),
        app_commands.Choice(name="All Time", value="alltime"),
    ])
    async def leaderboard(self, interaction: discord.Interaction, period: app_commands.Choice[str] = None):
        await interaction.response.defer()
        stat_type = period.value if period else "monthly"
        label = "Monthly" if stat_type == "monthly" else "All Time"
        lb = get_leaderboard(stat_type)

        if not lb:
            await interaction.followup.send(f"No {label.lower()} tester stats yet.", ephemeral=True)
            return

        embed = discord.Embed(title=f"🏆 Tester Leaderboard — {label}", color=discord.Color.gold())
        lines = []
        for i, entry in enumerate(lb[:15], 1):
            member = interaction.guild.get_member(entry["tester_id"]) if interaction.guild else None
            name = member.display_name if member else str(entry["tester_id"])
            lines.append(f"**{i}.** {name} — {entry['total']} tests")
        embed.description = "\n".join(lines)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="quota", description="Set the monthly tester quota (staff only).")
    @app_commands.describe(amount="Number of tests required per month.")
    async def quota(self, interaction: discord.Interaction, amount: int):
        await interaction.response.defer(ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not is_staff_member(interaction.user):
            await interaction.followup.send("❌ Staff only.", ephemeral=True)
            return
        if amount < 1:
            await interaction.followup.send("❌ Quota must be at least 1.", ephemeral=True)
            return
        set_monthly_quota(amount)
        await interaction.followup.send(f"✅ Monthly quota set to **{amount}** tests.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(QueueCog(bot))
