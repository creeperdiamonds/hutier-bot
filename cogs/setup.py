import discord
from discord import app_commands
from discord.ext import commands
import os
import re


TIER_RANKS = ["LT5", "HT5", "LT4", "HT4", "LT3", "HT3", "LT2", "HT2", "LT1", "HT1"]

GAMEMODE_KEYS = [
    "sword", "axe", "mace", "uhc", "pot", "nethpot", "smp", "vanilla",
    "creeper", "cart", "diasmp", "ogvanilla", "shieldlessuhc",
    "spearmace", "spearelytra", "diapot", "SMP", "crystal"
]


class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _env_path(self) -> str:
        return os.path.join(os.path.dirname(__file__), "..", ".env")

    def _load_env_file(self) -> dict:
        env_values = {}
        env_path = self._env_path()
        if os.path.exists(env_path):
            try:
                with open(env_path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, value = line.split("=", 1)
                            env_values[key.strip()] = value.strip()
            except Exception:
                pass
        return env_values

    def _write_env_updates(self, updates: dict) -> int:
        """Update specific keys in .env in-place. Appends new keys at end. Returns count changed."""
        env_path = self._env_path()
        if not os.path.exists(env_path):
            return 0

        with open(env_path, "r") as f:
            raw = f.read()

        remaining = dict(updates)
        new_lines = []
        changed = 0

        for line in raw.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in remaining:
                    new_val = remaining.pop(key)
                    new_lines.append(f"{key}={new_val}")
                    changed += 1
                    continue
            new_lines.append(line)

        if remaining:
            new_lines.append("")
            new_lines.append("# Auto-added by /detect")
            for key, val in remaining.items():
                new_lines.append(f"{key}={val}")
                changed += 1

        with open(env_path, "w") as f:
            f.write("\n".join(new_lines))
            if raw.endswith("\n"):
                f.write("\n")

        return changed

    def _env_tier_gamemodes(self, env: dict) -> list[str]:
        """Return gamemodes that already have TIER_ROLE_* entries in .env."""
        modes = set()
        for key in env:
            m = re.match(r"^TIER_ROLE_([A-Z0-9]+)_", key)
            if m:
                modes.add(m.group(1).lower())
        return sorted(modes)

    def _get_missing_tier_roles(self, found: dict, managed_modes: list[str]) -> list[tuple[str, str]]:
        missing = []
        for mode in managed_modes:
            for rank in TIER_RANKS:
                if rank not in found.get(mode, {}):
                    missing.append((mode, rank))
        return missing

    @app_commands.command(name="detect", description="Scan server, create missing tier roles, and update .env")
    @app_commands.checks.has_permissions(administrator=True)
    async def detect(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This command only works in a server", ephemeral=True)
            return

        detected = await self._scan_server(guild)
        existing_env = self._load_env_file()
        managed_modes = self._env_tier_gamemodes(existing_env)
        missing_roles = self._get_missing_tier_roles(detected["gamemode_tier_roles"], managed_modes)

        detected["existing_env"] = existing_env
        detected["missing_roles"] = missing_roles
        detected["managed_modes"] = managed_modes

        summary = self._generate_summary(detected, guild.id)
        view = ConfirmView(interaction.user, has_missing=bool(missing_roles))
        await interaction.followup.send(summary, view=view, ephemeral=True)
        await view.wait()

        if view.result == "cancel":
            await interaction.followup.send("❌ Setup cancelled", ephemeral=True)
            return

        create_roles = view.result == "create_and_save"
        env_updates = {}
        created_count = 0
        failed_roles = []

        if create_roles and missing_roles:
            progress_msg = await interaction.followup.send(
                f"Creating {len(missing_roles)} missing tier roles...", ephemeral=True
            )
            for mode, rank in missing_roles:
                role_name = f"{mode}-{rank.lower()}"
                try:
                    role = await guild.create_role(name=role_name, reason="/detect auto-create")
                    env_key = f"TIER_ROLE_{mode.upper()}_{rank.upper()}"
                    env_updates[env_key] = str(role.id)
                    detected["gamemode_tier_roles"].setdefault(mode, {})[rank] = role.id
                    created_count += 1
                except Exception as e:
                    failed_roles.append(f"{role_name} ({e})")

        # Include all found tier roles in the .env update
        for mode, ranks in detected["gamemode_tier_roles"].items():
            for rank, role_id in ranks.items():
                env_key = f"TIER_ROLE_{mode.upper()}_{rank.upper()}"
                if env_key not in env_updates:
                    env_updates[env_key] = str(role_id)

        updated_count = self._write_env_updates(env_updates)

        lines = []
        if create_roles:
            lines.append(f"✅ Created **{created_count}** tier roles")
        if failed_roles:
            lines.append(f"⚠️ Failed: {', '.join(failed_roles[:5])}")
        lines.append(f"✅ Updated **{updated_count}** keys in `.env`")
        lines.append("\nRestart the bot to load the new role IDs.")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    async def _scan_server(self, guild: discord.Guild) -> dict:
        detected = {
            "guild_id": guild.id,
            "queue_channels": {},
            "ping_roles": {},
            "gamemode_tier_roles": {},
            "staff_role": None,
            "ticket_category": None,
        }

        for channel in guild.text_channels:
            name = channel.name.lower()
            for mode in GAMEMODE_KEYS:
                if f"{mode}-queue" in name:
                    detected["queue_channels"][mode] = channel.id
                    break
            if "ticket" in name and channel.category:
                detected["ticket_category"] = channel.category.id

        rank_variants = [r.lower() for r in TIER_RANKS]

        for role in guild.roles:
            name = role.name.lower()

            for mode in GAMEMODE_KEYS:
                for rank in rank_variants:
                    if f"{mode}-{rank}" in name or f"{rank}-{mode}" in name:
                        detected["gamemode_tier_roles"].setdefault(mode, {})[rank.upper()] = role.id
                        break

            for mode in GAMEMODE_KEYS:
                if f"{mode}-ping" in name:
                    detected["ping_roles"][mode] = role.id
                    break

            if "staff" in name and not detected["staff_role"]:
                detected["staff_role"] = role.id

        return detected

    def _generate_summary(self, detected: dict, guild_id: int) -> str:
        existing_env = detected.get("existing_env", {})
        missing_roles = detected.get("missing_roles", [])
        managed_modes = detected.get("managed_modes", [])
        lines = ["🔍 **Server Scan Results**\n"]

        lines.append(f"**Guild ID:** `{detected['guild_id']}`")

        staff = detected["staff_role"]
        if staff:
            existing = int(existing_env.get("STAFF_ROLE_ID", "0") or "0")
            tag = "✅" if existing == staff else "⚠️ updating"
            lines.append(f"**Staff Role:** `{staff}` {tag}")
        else:
            lines.append("**Staff Role:** ❌ Not found")

        ticket = detected["ticket_category"]
        if ticket:
            existing = int(existing_env.get("TICKET_CATEGORY_ID", "0") or "0")
            tag = "✅" if existing == ticket else "⚠️ updating"
            lines.append(f"**Ticket Category:** `{ticket}` {tag}")
        else:
            lines.append("**Ticket Category:** ❌ Not found")

        lines.append(f"\n**Queue Channels:** {len(detected['queue_channels'])}")
        for mode, cid in sorted(detected["queue_channels"].items()):
            existing = int(existing_env.get(f"QUEUE_CHANNEL_{mode.upper()}", "0") or "0")
            tag = "✅" if existing == cid else ("🆕" if existing == 0 else "⚠️")
            lines.append(f"  {tag} {mode}: `{cid}`")

        lines.append(f"\n**Ping Roles:** {len(detected['ping_roles'])}")
        for mode, rid in sorted(detected["ping_roles"].items()):
            existing = int(existing_env.get(f"QUEUE_PING_ROLE_{mode.upper()}", "0") or "0")
            tag = "✅" if existing == rid else ("🆕" if existing == 0 else "⚠️")
            lines.append(f"  {tag} {mode}: `{rid}`")

        found_count = sum(len(r) for r in detected["gamemode_tier_roles"].values())
        total_managed = len(managed_modes) * len(TIER_RANKS)
        lines.append(f"\n**Tier Roles:** {found_count} found · {len(missing_roles)} missing")
        if managed_modes:
            lines.append(f"  Managed gamemodes: {', '.join(managed_modes)}")
        if missing_roles:
            # Group missing by mode for compact display
            by_mode: dict[str, list[str]] = {}
            for mode, rank in missing_roles:
                by_mode.setdefault(mode, []).append(rank)
            lines.append("  **Would create:**")
            for mode, ranks in sorted(by_mode.items()):
                lines.append(f"    {mode}: {', '.join(ranks)}")

        lines.append("\n**Legend:** ✅ No change · 🆕 New · ⚠️ Will update")
        return "\n".join(lines)


class ConfirmView(discord.ui.View):
    def __init__(self, user: discord.User, has_missing: bool):
        super().__init__(timeout=300)
        self.user = user
        self.result = "cancel"
        if not has_missing:
            # No missing roles — disable the create button
            self.create_and_save_btn.disabled = True
            self.create_and_save_btn.label = "Create Roles + Save .env (none missing)"

    @discord.ui.button(label="Create Roles + Save .env", style=discord.ButtonStyle.green)
    async def create_and_save_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ Not your button", ephemeral=True)
            return
        self.result = "create_and_save"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Save .env Only", style=discord.ButtonStyle.blurple)
    async def save_only_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ Not your button", ephemeral=True)
            return
        self.result = "save_only"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ Not your button", ephemeral=True)
            return
        self.result = "cancel"
        await interaction.response.defer()
        self.stop()


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))
