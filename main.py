import asyncio
import traceback
from dotenv import load_dotenv

load_dotenv()

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import api
import database
import cogs.queue as queue_module
from config import DISCORD_TOKEN, GUILD_ID
from cogs.queue import rebuild_queue_message_ids
from cogs.tickets import CloseTicketView, TicketPanelView
from cogs.queue import QueueActionView, QueuePanelView, PingPanelView
from server import start_health_server
from storage import _load_data, persist_queue_message_ids

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")

    # List all guilds the bot is in
    print(f"\nBot is in {len(bot.guilds)} guild(s):")
    for guild in bot.guilds:
        print(f"  - {guild.name} (ID: {guild.id})")
    print()

    if not hasattr(bot, "_startup_completed"):
        bot.add_view(TicketPanelView())
        bot.add_view(CloseTicketView(owner_id=0, mode_key=""))
        bot.add_view(QueuePanelView())
        bot.add_view(QueueActionView())
        bot.add_view(PingPanelView())

        await _restore_queue_state()
        await _sync_commands()
        asyncio.create_task(queue_module.queue_maintenance_task())

        bot._startup_completed = True


async def _restore_queue_state():
    data = _load_data()

    panel_data = data.get("queue_panel_message")
    if panel_data and isinstance(panel_data, list) and len(panel_data) == 2:
        queue_module.QUEUE_PANEL_MESSAGE = (panel_data[0], panel_data[1])

    loaded = {}
    for entry in data.get("queue_message_ids", []):
        if isinstance(entry, list) and len(entry) == 2:
            try:
                loaded[int(entry[0])] = entry[1]
            except (ValueError, TypeError):
                continue
    queue_module.QUEUE_MESSAGE_IDS = loaded

    if GUILD_ID:
        guild = bot.get_guild(GUILD_ID)
        if guild:
            await rebuild_queue_message_ids(guild)
            persist_queue_message_ids(queue_module.QUEUE_MESSAGE_IDS)


async def _sync_commands():
    try:
        print("\n=== Syncing Slash Commands ===")

        # Show commands before sync
        all_cmds = list(bot.tree._get_all_commands())
        print(f"Commands to sync: {len(all_cmds)}")
        for cmd in all_cmds:
            print(f"  - /{cmd.qualified_name}")

        if GUILD_ID:
            print(f"\nSyncing to guild: {GUILD_ID}")
            g = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=g)
            synced = await bot.tree.sync(guild=g)
            print(f"✅ Successfully synced {len(synced)} commands!")
        else:
            print("\nSyncing globally...")
            synced = await bot.tree.sync()
            print(f"✅ Successfully synced {len(synced)} commands!")
        print()
    except Exception as e:
        print(f"❌ Sync failed: {e}")
        traceback.print_exc()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    try:
        msg = f"❌ Parancs hiba: {type(error).__name__}: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


async def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing")

    await database.init_db()
    api.http_session = aiohttp.ClientSession()

    try:
        async with bot:
            cogs = ["cogs.tickets", "cogs.queue", "cogs.tierlist", "cogs.setup"]
            print("Loading cogs...")
            for cog in cogs:
                try:
                    await bot.load_extension(cog)
                    print(f"  ✅ {cog}")
                except Exception as e:
                    print(f"  ❌ {cog}: {e}")
                    traceback.print_exc()

            # Show all registered commands before starting
            print("\n=== Registered Commands ===")
            all_cmds = bot.tree._get_all_commands()
            for cmd in all_cmds:
                print(f"  ✅ /{cmd.qualified_name}")
            print(f"Total: {len(all_cmds)} commands\n")

            asyncio.create_task(start_health_server(bot))

            await bot.start(DISCORD_TOKEN)
    finally:
        if api.http_session and not api.http_session.closed:
            await api.http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
