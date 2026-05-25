'use strict';
const {
  SlashCommandBuilder,
  EmbedBuilder,
  ButtonBuilder,
  ButtonStyle,
  ActionRowBuilder,
} = require('discord.js');
const fs = require('fs');
const path = require('path');
const { GAMEMODES, RANKS, TIERS } = require('../config');
const { isAdmin } = require('../permissions');

const GAMEMODE_KEYS = GAMEMODES.map(g => g.toLowerCase());
const TIER_RANKS = RANKS.filter(r => r !== 'Unranked');

function loadEnvFile() {
  const envPath = path.join(__dirname, '..', '.env');
  if (!fs.existsSync(envPath)) return {};
  const vals = {};
  try {
    const raw = fs.readFileSync(envPath, 'utf8');
    for (const line of raw.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#') || !trimmed.includes('=')) continue;
      const [key, ...rest] = trimmed.split('=');
      vals[key.trim()] = rest.join('=').trim();
    }
  } catch {}
  return vals;
}

function writeEnvUpdates(updates) {
  const envPath = path.join(__dirname, '..', '.env');
  if (!fs.existsSync(envPath)) return 0;

  let raw = fs.readFileSync(envPath, 'utf8');
  const remaining = { ...updates };
  let changed = 0;

  const newLines = [];
  for (const line of raw.split('\n')) {
    const trimmed = line.trim();
    if (trimmed && !trimmed.startsWith('#') && trimmed.includes('=')) {
      const key = trimmed.split('=')[0].trim();
      if (key in remaining) {
        newLines.push(`${key}=${remaining[key]}`);
        delete remaining[key];
        changed++;
        continue;
      }
    }
    newLines.push(line);
  }

  if (Object.keys(remaining).length > 0) {
    newLines.push('', '# Auto-added by /detect');
    for (const [k, v] of Object.entries(remaining)) {
      newLines.push(`${k}=${v}`);
      changed++;
    }
  }

  fs.writeFileSync(envPath, newLines.join('\n'), 'utf8');
  return changed;
}

async function scanServer(guild) {
  const detected = {
    guildId: guild.id,
    queueChannels: {},
    pingRoles: {},
    gamemodeTierRoles: {},
    staffRole: null,
    ticketCategory: null,
  };

  // Channels
  for (const channel of guild.channels.cache.values()) {
    const name = channel.name.toLowerCase();
    for (const mode of GAMEMODE_KEYS) {
      if (name.includes(`${mode}-queue`) || name.includes(`${mode}queue`)) {
        detected.queueChannels[mode] = channel.id;
        break;
      }
    }
    if (name.includes('ticket') && channel.parent) {
      detected.ticketCategory = channel.parent.id;
    }
  }

  // Roles
  const rankVariants = TIER_RANKS.map(r => r.toLowerCase());
  for (const role of guild.roles.cache.values()) {
    const name = role.name.toLowerCase();

    // Tier roles
    for (const mode of GAMEMODE_KEYS) {
      for (const rank of rankVariants) {
        if (name.includes(`${mode}-${rank}`) || name.includes(`${rank}-${mode}`)) {
          if (!detected.gamemodeTierRoles[mode]) detected.gamemodeTierRoles[mode] = {};
          detected.gamemodeTierRoles[mode][rank.toUpperCase()] = role.id;
          break;
        }
      }
    }

    // Ping roles
    for (const mode of GAMEMODE_KEYS) {
      if (name.includes(`${mode}-ping`) || name.includes(`${mode}ping`)) {
        detected.pingRoles[mode] = role.id;
        break;
      }
    }

    // Staff role
    if (!detected.staffRole && name.includes('staff')) {
      detected.staffRole = role.id;
    }
  }

  return detected;
}

function generateSummary(detected, existingEnv, missingRoles, managedModes) {
  const lines = ['🔍 **Server Scan Results**\n'];
  lines.push(`**Guild ID:** \`${detected.guildId}\``);

  if (detected.staffRole) {
    const tag = existingEnv.STAFF_ROLE_ID === detected.staffRole ? '✅' : '⚠️ updating';
    lines.push(`**Staff Role:** \`${detected.staffRole}\` ${tag}`);
  } else {
    lines.push('**Staff Role:** ❌ Not found');
  }

  if (detected.ticketCategory) {
    const tag = existingEnv.TICKET_CATEGORY_ID === detected.ticketCategory ? '✅' : '⚠️ updating';
    lines.push(`**Ticket Category:** \`${detected.ticketCategory}\` ${tag}`);
  } else {
    lines.push('**Ticket Category:** ❌ Not found');
  }

  lines.push(`\n**Queue Channels:** ${Object.keys(detected.queueChannels).length}`);
  for (const [mode, cid] of Object.entries(detected.queueChannels)) {
    const tag = existingEnv[`QUEUE_CHANNEL_${mode.toUpperCase()}`] === cid ? '✅' : '🆕';
    lines.push(`  ${tag} ${mode}: \`${cid}\``);
  }

  const foundCount = Object.values(detected.gamemodeTierRoles).reduce((n, r) => n + Object.keys(r).length, 0);
  lines.push(`\n**Tier Roles:** ${foundCount} found · ${missingRoles.length} missing`);
  if (managedModes.length) lines.push(`  Managed gamemodes: ${managedModes.join(', ')}`);
  if (missingRoles.length) {
    const byMode = {};
    for (const [mode, rank] of missingRoles) {
      if (!byMode[mode]) byMode[mode] = [];
      byMode[mode].push(rank);
    }
    lines.push('  **Would create:**');
    for (const [mode, ranks] of Object.entries(byMode)) {
      lines.push(`    ${mode}: ${ranks.join(', ')}`);
    }
  }

  lines.push('\n**Legend:** ✅ No change · 🆕 New · ⚠️ Will update');
  return lines.join('\n');
}

const commands = {
  detect: {
    data: new SlashCommandBuilder()
      .setName('detect')
      .setDescription('Scan server for tier roles and queue channels (admin only)'),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });

      if (!isAdmin(interaction.member)) {
        return interaction.editReply('❌ Admin only.');
      }

      const guild = interaction.guild;
      if (!guild) return interaction.editReply('❌ Server only.');

      const detected = await scanServer(guild);
      const existingEnv = loadEnvFile();

      // Find managed modes (those with TIER_ROLE_* in env)
      const managedModes = [];
      for (const key of Object.keys(existingEnv)) {
        const m = key.match(/^TIER_ROLE_([A-Z0-9]+)_/);
        if (m) {
          const mode = m[1].toLowerCase();
          if (!managedModes.includes(mode)) managedModes.push(mode);
        }
      }

      // Find missing roles
      const missingRoles = [];
      for (const mode of managedModes) {
        for (const rank of TIER_RANKS) {
          if (!(detected.gamemodeTierRoles[mode] || {})[rank]) {
            missingRoles.push([mode, rank]);
          }
        }
      }

      const summary = generateSummary(detected, existingEnv, missingRoles, managedModes);

      const createBtn = new ButtonBuilder()
        .setCustomId('detect_create_and_save')
        .setLabel('Create Roles + Save .env')
        .setStyle(ButtonStyle.Success)
        .setDisabled(!missingRoles.length);
      const saveBtn = new ButtonBuilder()
        .setCustomId('detect_save_only')
        .setLabel('Save .env Only')
        .setStyle(ButtonStyle.Primary);
      const cancelBtn = new ButtonBuilder()
        .setCustomId('detect_cancel')
        .setLabel('Cancel')
        .setStyle(ButtonStyle.Danger);

      const row = new ActionRowBuilder().addComponents(createBtn, saveBtn, cancelBtn);

      // Store detected state for button handler
      interaction.client._detectState = {
        detected, existingEnv, missingRoles, managedModes, userId: interaction.user.id,
      };

      await interaction.editReply({ content: summary, components: [row] });
    },
  },
};

async function handleDetectButton(interaction) {
  if (!['detect_create_and_save', 'detect_save_only', 'detect_cancel'].includes(interaction.customId)) return false;

  const state = interaction.client._detectState;
  if (!state || state.userId !== interaction.user.id) {
    await interaction.reply({ content: '❌ Not your button or state expired.', ephemeral: true });
    return true;
  }

  await interaction.deferUpdate();

  if (interaction.customId === 'detect_cancel') {
    await interaction.editReply({ content: '❌ Setup cancelled.', components: [] });
    return true;
  }

  const { detected, existingEnv, missingRoles, managedModes } = state;
  const envUpdates = {};
  let createdCount = 0;
  const failedRoles = [];

  // Create missing roles if requested
  if (interaction.customId === 'detect_create_and_save' && missingRoles.length) {
    for (const [mode, rank] of missingRoles) {
      const roleName = `${mode}-${rank.toLowerCase()}`;
      try {
        const role = await interaction.guild.roles.create({ name: roleName, reason: '/detect auto-create' });
        const envKey = `TIER_ROLE_${mode.toUpperCase()}_${rank.toUpperCase()}`;
        envUpdates[envKey] = role.id;
        if (!detected.gamemodeTierRoles[mode]) detected.gamemodeTierRoles[mode] = {};
        detected.gamemodeTierRoles[mode][rank] = role.id;
        createdCount++;
      } catch (e) {
        failedRoles.push(`${roleName} (${e.message})`);
      }
    }
  }

  // Include all found tier roles
  for (const [mode, ranks] of Object.entries(detected.gamemodeTierRoles)) {
    for (const [rank, roleId] of Object.entries(ranks)) {
      const envKey = `TIER_ROLE_${mode.toUpperCase()}_${rank.toUpperCase()}`;
      if (!(envKey in envUpdates)) envUpdates[envKey] = roleId;
    }
  }

  if (detected.staffRole) envUpdates.STAFF_ROLE_ID = detected.staffRole;
  if (detected.ticketCategory) envUpdates.TICKET_CATEGORY_ID = detected.ticketCategory;

  const updatedCount = writeEnvUpdates(envUpdates);

  const lines = [];
  if (interaction.customId === 'detect_create_and_save') {
    lines.push(`✅ Created **${createdCount}** tier roles`);
  }
  if (failedRoles.length) lines.push(`⚠️ Failed: ${failedRoles.slice(0, 5).join(', ')}`);
  lines.push(`✅ Updated **${updatedCount}** keys in \`.env\``);
  lines.push('\nRestart the bot to load new role IDs.');

  await interaction.editReply({ content: lines.join('\n'), components: [] });
  return true;
}

module.exports = { commands, handleDetectButton };
