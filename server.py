import os
import traceback

import discord
from discord.ext import commands
from aiohttp import web

from storage import verify_link_code_async, link_minecraft_account_async
from api import api_get_player_tiers, api_get_gamemode_leaderboard
import api as api_module


async def start_health_server(bot: commands.Bot):
    """Start the aiohttp health/API server"""

    app = web.Application()

    async def health(_request):
        return web.Response(text="ok")

    print("Health server endpoints registered")

    # API endpoint for Minecraft link code verification
    async def verify_link(request):
        print(f"verify_link called: code={request.query.get('code')}, minecraft={request.query.get('minecraft')}")
        try:
            # Get code from query params
            code = request.query.get("code", "")
            minecraft_name = request.query.get("minecraft", "")

            if not code or not minecraft_name:
                return web.json_response({"success": False, "error": "Missing code or minecraft parameter"}, status=400)

            print(f"Verifying code: {code}")
            # Verify the code
            discord_id = await verify_link_code_async(code.upper())
            print(f"Verified: discord_id={discord_id}")

            if discord_id is None:
                return web.json_response({"success": False, "error": "Invalid or expired code"}, status=400)

            # Ensure http_session is available for linking
            if api_module.http_session is None:
                import aiohttp
                print("Creating http_session")
                api_module.http_session = aiohttp.ClientSession()

            # Link the Minecraft account to the Discord account
            print(f"Linking account: {discord_id} -> {minecraft_name}")
            await link_minecraft_account_async(discord_id, minecraft_name)

            # Send confirmation DM to the user
            try:
                user = await bot.fetch_user(discord_id)
                if user:
                    embed = discord.Embed(
                        title="Account linked successfully!",
                        description=f"Your Discord account has been linked to your **Minecraft** account!\n\n**Minecraft name:** `{minecraft_name}`",
                        color=discord.Color.green()
                    )
                    await user.send(embed=embed)
            except Exception as e:
                print(f"Could not send DM: {e}")

            return web.json_response({"success": True, "discord_id": discord_id, "minecraft": minecraft_name})
        except Exception as e:
            print(f"verify_link error: {e}")
            traceback.print_exc()
            return web.json_response({"success": False, "error": str(e)}, status=500)

        # Send confirmation DM to the user
        try:
            user = await bot.fetch_user(discord_id)
            if user:
                embed = discord.Embed(
                    title="✅ Account linked successfully!",
                    description=f"Your Discord account has been linked to your **Minecraft** account!\n\n"
                               f"**Minecraft name:** `{minecraft_name}`\n"
                               f"**Linked:** Permanently!",
                    color=discord.Color.green()
                )
                embed.set_footer(text="You can now use the tier list!")
                await user.send(embed=embed)
        except Exception as e:
            print(f"Could not send DM to user: {e}")

        return web.json_response({
            "success": True,
            "discord_id": discord_id,
            "minecraft": minecraft_name
        })

    # Public tier API — no auth required (read-only)

    async def get_player_tier(request):
        """GET /api/v1/tier/{username}
        Returns all tiers + highest rank for a player.
        Response: {"username": "Steve", "tiers": {"Vanilla": "LT3"}, "highest": "LT3", "highest_gamemode": "Vanilla"}
        """
        username = request.match_info["username"]
        if not username or len(username) > 64:
            return web.json_response({"error": "Invalid username"}, status=400)
        result = await api_get_player_tiers(username)
        if result["status"] != 200:
            return web.json_response({"error": result["data"].get("error", "Not found")}, status=404)
        data = result["data"]
        return web.json_response({
            "username": username,
            "tiers": data.get("tiers", {}),
            "highest": data.get("highest", "Unranked"),
            "highest_gamemode": data.get("highest_gamemode"),
        })

    async def get_player_gamemode_tier(request):
        """GET /api/v1/tier/{username}/{gamemode}
        Returns the rank for a specific gamemode.
        Response: {"username": "Steve", "gamemode": "Vanilla", "rank": "LT3"}
        """
        username = request.match_info["username"]
        gamemode = request.match_info["gamemode"]
        if not username or len(username) > 64:
            return web.json_response({"error": "Invalid username"}, status=400)
        result = await api_get_player_tiers(username)
        if result["status"] != 200:
            return web.json_response({"error": "Player not found"}, status=404)
        tiers = result["data"].get("tiers", {})
        from config import get_gamemode_display_name
        mode_display = get_gamemode_display_name(gamemode)
        rank = tiers.get(mode_display) or tiers.get(gamemode)
        if rank is None:
            return web.json_response({"username": username, "gamemode": mode_display, "rank": "Unranked"})
        return web.json_response({"username": username, "gamemode": mode_display, "rank": rank})

    async def get_leaderboard(request):
        """GET /api/v1/leaderboard/{gamemode}
        Returns all ranked players in a gamemode, sorted highest first.
        Response: {"gamemode": "Vanilla", "players": [{"username": "Steve", "rank": "LT1"}, ...]}
        """
        gamemode = request.match_info["gamemode"]
        result = await api_get_gamemode_leaderboard(gamemode)
        if result["status"] != 200:
            return web.json_response({"error": result["data"].get("error", "Not found")}, status=404)
        return web.json_response(result["data"])

    app.router.add_get("/health", health)
    app.router.add_get("/api/link/verify", verify_link)
    app.router.add_get("/api/v1/tier/{username}", get_player_tier)
    app.router.add_get("/api/v1/tier/{username}/{gamemode}", get_player_gamemode_tier)
    app.router.add_get("/api/v1/leaderboard/{gamemode}", get_leaderboard)

    runner = web.AppRunner(app)
    await runner.setup()

    base_port = int(os.getenv("PORT", "8080"))
    for port in range(base_port, base_port + 10):
        try:
            site = web.TCPSite(runner, "0.0.0.0", port)
            await site.start()
            print(f"Health server running on 0.0.0.0:{port}")
            return
        except OSError:
            continue
    print(f"WARNING: Could not start health server — ports {base_port}-{base_port + 9} all in use")
