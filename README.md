# SM Tiers Discord Bot

A Hungarian Minecraft tier-list Discord bot that manages tier tests, queues, player profiles, and account linking.

## Features

- **Ticket system** — Players open a test ticket per gamemode; staff close it and record the result
- **Queue system** — Testers open/close queues per gamemode with join/leave/next buttons and ping role notifications
- **Tier list** — Test results are saved to a Supabase database and synced to a website
- **Player profiles** — View all tiers for a given Minecraft username
- **Minecraft account linking** — Players link their Minecraft account to their Discord via a verification code
- **Admin tools** — Retire, ban, unban, rename, and bulk-import players

## Rank System

```
Unranked → LT5 / HT5 → LT4 / HT4 → LT3 / HT3 → LT2 / HT2 → LT1 / HT1
```

## Supported Gamemodes

Vanilla, UHC, Pot, NethPot, SMP, Sword, Axe, Mace, Cart, Creeper, DiaSMP, OGVanilla, ShieldlessUHC, SpearMace, SpearElytra, DiaPot, SMP, Crystal

## Slash Commands

| Command | Description | Who |
|---------|-------------|-----|
| `/ticketpanel` | Post the ticket panel in a channel | Staff |
| `/queuepanel` | Post the queue panel (open queues from here) | Staff |
| `/pingpanel` | Set up ping role notifications for queues | Anyone |
| `/closequeue` | Manually close a queue | Staff / queue opener |
| `/testresult` | Record a tier test result and save it to the website | Staff |
| `/profile` | View a player's tiers on the tier list | Anyone |
| `/spin` | Pick a random player from a given gamemode and tier | Anyone |
| `/cooldown` | Check ticket cooldowns (own or another player's) | Anyone / Staff |
| `/link` | Link your Minecraft account to Discord | Anyone |
| `/unlink` | Unlink your Minecraft account | Anyone |
| `/mylink` | View your currently linked Minecraft account | Anyone |
| `/tierlistnamechange` | Rename a player on the tier list | Admin |
| `/retire` | Retire a player in a gamemode (Tier 2+ only) | Admin |
| `/unretire` | Un-retire a player | Admin |
| `/tierlistban` | Ban a player from testing | Admin |
| `/tierlistunban` | Unban a player | Admin |
| `/removetierlist` | Permanently remove a player from the tier list | Admin |
| `/bulkimport` | Bulk-import test results from a file | Admin |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | Yes | Discord bot token (also accepted as `BOT_TOKEN` or `TOKEN`) |
| `GUILD_ID` | Yes | Discord server (guild) ID |
| `STAFF_ROLE_ID` | Yes | Role ID that grants staff permissions |
| `TICKET_CATEGORY_ID` | Yes | Category ID where ticket channels are created |
| `SUPABASE_URL` | Yes* | Supabase project URL |
| `SUPABASE_KEY` | Yes* | Supabase anon/service key |
| `WEBSITE_URL` | Yes | Website URL (e.g. `https://neontiers.vercel.app`) |
| `BOT_API_KEY` | Yes | Shared secret between the bot and the website |
| `MINECRAFT_API_URL` | No | Minecraft verification API URL (default: `http://localhost:8080`) |
| `EXTRA_STAFF_ROLE_IDS` | No | Additional role ID with staff permissions |
| `ALLOWED_USER_IDS` | No | Comma-separated Discord user IDs with staff permissions |
| `DATABASE_URL` | No | PostgreSQL URL (legacy fallback if Supabase REST API is not used) |
| `SUPABASE_PG_URL` | No | Supabase direct PostgreSQL URL (legacy fallback) |
| `WIPE_GLOBAL_COMMANDS` | No | Set to `1` to wipe all global slash commands on startup |

*`SUPABASE_URL` + `SUPABASE_KEY` are required for the recommended Supabase REST API mode.

## Setup

### Prerequisites

- Python 3.11+
- A Discord application and bot token
- A Supabase project with the following tables:
  - `linked_accounts` (`discord_id BIGINT UNIQUE`, `minecraft_name VARCHAR`, `linked_at TIMESTAMPTZ`)
  - `pending_codes` (`discord_id BIGINT`, `code VARCHAR(8)`, `created_at TIMESTAMPTZ`, `expires_at TIMESTAMPTZ`, `used BOOLEAN`)

### Install dependencies

```bash
pip install -r requirements.txt
```

### Run locally

```bash
export DISCORD_TOKEN=your_token_here
export GUILD_ID=your_guild_id
# ... other env vars
python main.py
```

### Deploy (Heroku / Railway)

The `Procfile` configures a worker dyno:

```
worker: python main.py
```

Set all required environment variables in your platform's dashboard, then deploy.

## Adding New Gamemodes

Three places in `main.py` need to be updated when adding a gamemode:

1. **`TICKET_TYPES`** — add `("DisplayName", "key", tester_role_id)`
2. **`QUEUE_CHANNELS`** — add `"key": channel_id`
3. **`QUEUE_PING_ROLES`** — add `"key": ping_role_id`

Also add the gamemode to `TICKET_ROUNDS`, `GAMEMODE_DISPLAY_NAMES`, `GAMEMODE_COLORS`, and `GAMEMODE_INDICATORS` for full support.

> The three gamemodes currently missing IDs are **DiaPot**, **SMP**, and **Crystal** — replace the `0` placeholders with real Discord IDs.
    