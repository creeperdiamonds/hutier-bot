'use strict';
require('dotenv').config();

const {
  Client,
  GatewayIntentBits,
  Partials,
  REST,
  Routes,
  Events,
  Collection,
} = require('discord.js');

const { DISCORD_TOKEN, GUILD_ID } = require('./config');
const { initDb } = require('./database');
const { startServer } = require('./server');
const { runMigration } = require('./migrate');
const storage = require('./storage');

// Import all command modules
const tierlistModule = require('./commands/tierlist');
const linkModule = require('./commands/link');
const queueModule = require('./commands/queue');
const ticketsModule = require('./commands/tickets');
const setupModule = require('./commands/setup');
const rubricModule = require('./commands/rubric');
const verifyModule = require('./commands/verify');

// Create client
const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.GuildMembers,
    GatewayIntentBits.GuildModeration,
    GatewayIntentBits.MessageContent,
    GatewayIntentBits.DirectMessages,
  ],
  partials: [Partials.Channel, Partials.Message],
});

// Build command collection
client.commands = new Collection();

const allModules = [tierlistModule, linkModule, queueModule, ticketsModule, setupModule, rubricModule, verifyModule];
for (const mod of allModules) {
  for (const [name, cmd] of Object.entries(mod.commands)) {
    client.commands.set(name, cmd);
  }
}

// Register slash commands
async function registerCommands() {
  const rest = new REST({ version: '10' }).setToken(DISCORD_TOKEN);
  const commandData = [...client.commands.values()].map(cmd => cmd.data.toJSON());

  try {
    if (GUILD_ID) {
      await rest.put(Routes.applicationGuildCommands(client.user.id, GUILD_ID), { body: commandData });
      console.log(`[Commands] Registered ${commandData.length} guild commands to ${GUILD_ID}`);
    } else {
      await rest.put(Routes.applicationCommands(client.user.id), { body: commandData });
      console.log(`[Commands] Registered ${commandData.length} global commands`);
    }
  } catch (e) {
    console.error('[Commands] Failed to register:', e);
  }
}

// --- Event: Ready ---
client.once(Events.ClientReady, async (c) => {
  console.log(`[Bot] Logged in as ${c.user.tag}`);

  initDb();
  await registerCommands();
  queueModule.loadState();
  await startServer(c);

  // Run migration if a sync channel exists (deletes itself when done)
  const db = require('./database');
  const guild = GUILD_ID ? c.guilds.cache.get(GUILD_ID) : c.guilds.cache.first();
  if (guild) {
    await runMigration(guild, db).catch(e => console.error('[Migrate] Error:', e));
  }

  console.log('[Bot] Ready!');
});

// --- Event: GuildMemberRemove (auto-remove from queues) ---
client.on(Events.GuildMemberRemove, async (member) => {
  await queueModule.handleMemberLeave(member).catch(() => {});
});

// --- Event: ChannelDelete (auto-clear active session) ---
client.on(Events.ChannelDelete, async (channel) => {
  try {
    const session = storage.findActiveSessionByChannel(channel.id);
    if (session) {
      console.log(`[Queue] Ticket channel ${channel.id} deleted — clearing active session for ${session.gamemode}`);
      storage.clearActiveSession(session.gamemode);
      storage.saveLastSession(session.gamemode);
    }
  } catch (e) {
    console.error('[Queue] ChannelDelete handler error:', e);
  }
});

// --- Event: Interaction ---
client.on(Events.InteractionCreate, async (interaction) => {
  try {
    if (interaction.isChatInputCommand()) {
      const cmd = client.commands.get(interaction.commandName);
      if (!cmd) return;
      await cmd.execute(interaction);
      return;
    }

    if (interaction.isButton()) {
      const id = interaction.customId;

      // Tierlist remove confirm/cancel
      if (await tierlistModule.handleRemoveConfirm(interaction)) return;
      if (await tierlistModule.handleRemoveCancel(interaction)) return;

      // Queue buttons
      if (await queueModule.handleQueueOpen(interaction)) return;
      if (await queueModule.handleQueueJoin(interaction)) return;
      if (await queueModule.handleQueueLeave(interaction)) return;
      if (await queueModule.handlePingClearAll(interaction)) return;

      // Ticket buttons
      if (await ticketsModule.handleTicketOpen(interaction)) return;
      if (await ticketsModule.handleTicketClose(interaction)) return;
      if (await ticketsModule.handleTicketDismiss(interaction)) return;
      if (await ticketsModule.handleGiveTier(interaction)) return;

      // Rubric buttons
      if (await rubricModule.handleRubricButton(interaction)) return;

      // Verify button
      if (await verifyModule.handleVerifyButton(interaction)) return;

      // Setup buttons
      if (await setupModule.handleDetectButton(interaction)) return;

      console.warn(`[Interaction] Unhandled button: ${id}`);
      return;
    }

    if (interaction.isModalSubmit()) {
      if (await verifyModule.handleVerifyModal(interaction)) return;
      return;
    }

    if (interaction.isStringSelectMenu()) {
      const id = interaction.customId;

      if (await queueModule.handleRegionSelect(interaction)) return;
      if (await queueModule.handlePingSelect(interaction)) return;
      if (await queueModule.handleRemovePlayerSelect(interaction)) return;
      if (await ticketsModule.handleGiveTierGmSelect(interaction)) return;
      if (await ticketsModule.handleGiveTierRankSelect(interaction)) return;
      if (await verifyModule.handleVerifyTypeSelect(interaction)) return;

      console.warn(`[Interaction] Unhandled select: ${id}`);
      return;
    }
  } catch (e) {
    console.error('[Interaction] Error handling interaction:', e);
    try {
      const errMsg = { content: `❌ An error occurred: ${e.message}`, ephemeral: true };
      if (interaction.deferred || interaction.replied) {
        await interaction.followUp(errMsg);
      } else {
        await interaction.reply(errMsg);
      }
    } catch {}
  }
});

// --- Error handling ---
client.on(Events.Error, (e) => {
  console.error('[Client] Error:', e);
});

process.on('unhandledRejection', (reason) => {
  console.error('[Process] Unhandled rejection:', reason);
});

// --- Start ---
if (!DISCORD_TOKEN) {
  console.error('[Bot] DISCORD_TOKEN is not set. Exiting.');
  process.exit(1);
}

client.login(DISCORD_TOKEN).catch(e => {
  console.error('[Bot] Login failed:', e);
  process.exit(1);
});
