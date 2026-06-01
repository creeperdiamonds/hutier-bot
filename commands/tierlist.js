'use strict';
const {
  SlashCommandBuilder,
  EmbedBuilder,
  ButtonBuilder,
  ButtonStyle,
  ActionRowBuilder,
  AttachmentBuilder,
} = require('discord.js');
const {
  RANKS, POINTS, GAMEMODES, TIERS,
  TIER_RESULTS_CHANNEL_ID, COOLDOWN_SECONDS,
  getGamemodeDisplay, getTierRoleId, GAMEMODE_TIER_ROLES,
  getRankName, getRankProgress,
} = require('../config');
const db = require('../database');
const storage = require('../storage');
const { isStaff, isAdmin } = require('../permissions');

// Build choice arrays
const gamemodeChoices = GAMEMODES.map(g => ({ name: g, value: g.toLowerCase() }));
const rankChoices = RANKS.filter(r => r !== 'Unranked').map(r => ({ name: r, value: r }));
const allRankChoices = RANKS.map(r => ({ name: r, value: r }));

// Helper: get all tier role IDs
function getAllTierRoleIds() {
  const ids = new Set();
  for (const ranks of Object.values(GAMEMODE_TIER_ROLES)) {
    for (const id of Object.values(ranks)) {
      if (id) ids.add(id);
    }
  }
  return ids;
}

// Helper: apply tier role for a gamemode
async function applyTierRole(member, gamemode, rank) {
  const allIds = getAllTierRoleIds();
  if (allIds.size === 0) return '⚠️ No tier roles configured.';

  // Remove all tier roles for this gamemode
  const gamemodeRoleIds = new Set(Object.values(GAMEMODE_TIER_ROLES[gamemode.toLowerCase()] || {}));
  const toRemove = member.roles.cache.filter(r => gamemodeRoleIds.has(r.id));
  if (toRemove.size > 0) {
    try {
      await member.roles.remove([...toRemove.values()], 'Tier update');
    } catch (e) {
      return `❌ Missing permissions to remove tier roles: ${e.message}`;
    }
  }

  const newRoleId = getTierRoleId(gamemode, rank);
  if (!newRoleId) return `⚠️ No role configured for **${rank}** in **${getGamemodeDisplay(gamemode)}**.`;

  const newRole = member.guild.roles.cache.get(newRoleId);
  if (!newRole) return `⚠️ Role ID ${newRoleId} not found in server.`;

  try {
    await member.roles.add(newRole, `Tier: ${rank} in ${getGamemodeDisplay(gamemode)}`);
    return `✅ Tagged as **${rank}** in **${getGamemodeDisplay(gamemode)}** (${newRole.name})`;
  } catch (e) {
    return `❌ Missing permissions to assign tier role: ${e.message}`;
  }
}

// Helper: send result embed to tier results channel
async function sendResultEmbed(guild, embed) {
  if (!TIER_RESULTS_CHANNEL_ID) return;
  const ch = guild.channels.cache.get(TIER_RESULTS_CHANNEL_ID);
  if (ch && ch.isTextBased()) {
    await ch.send({ embeds: [embed] });
  }
}

const commands = {
  // /testresult
  testresult: {
    data: new SlashCommandBuilder()
      .setName('testresult')
      .setDescription('Record a tier test result (staff only)')
      .addStringOption(o => o.setName('username').setDescription('Minecraft username').setRequired(true))
      .addUserOption(o => o.setName('tester').setDescription('Tester (Discord user)').setRequired(true))
      .addStringOption(o => o.setName('gamemode').setDescription('Gamemode').setRequired(true)
        .addChoices(...gamemodeChoices))
      .addStringOption(o => o.setName('rank').setDescription('Achieved rank').setRequired(true)
        .addChoices(...rankChoices)),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });
      if (!isStaff(interaction.member)) {
        return interaction.editReply('❌ Staff only.');
      }

      const username = interaction.options.getString('username');
      const tester = interaction.options.getUser('tester');
      const testerMember = interaction.options.getMember('tester');
      const gamemode = interaction.options.getString('gamemode');
      const rank = interaction.options.getString('rank');

      // Get previous rank
      const prev = db.getTestByUsernameAndMode(username, getGamemodeDisplay(gamemode));
      const prevRank = prev ? prev.rank : 'Unranked';
      const prevPoints = POINTS[prevRank] || 0;
      const newPoints = POINTS[rank] || 0;
      const diff = newPoints - prevPoints;

      // Upsert to DB
      const ok = db.upsertTest({
        username,
        mode: getGamemodeDisplay(gamemode),
        rank,
        testerId: tester.id,
        testerName: testerMember ? testerMember.displayName : tester.username,
        ts: Math.floor(Date.now() / 1000),
      });

      if (!ok) {
        return interaction.editReply('❌ Failed to save test result to database.');
      }

      // Track tester activity
      db.incrementTesterStat(tester.id, testerMember ? testerMember.displayName : tester.username);

      // Set cooldown for owner if in a ticket channel
      const ch = interaction.channel;
      if (ch && ch.topic && ch.topic.includes('owner=')) {
        const m = ch.topic.match(/owner=(\d+)/);
        if (m) {
          storage.setLastClosed(m[1], gamemode, Math.floor(Date.now() / 1000));
        }
      }

      // Build embed
      const embed = new EmbedBuilder()
        .setTitle(`${username}'s Test Result 🏆`)
        .setColor(0x2f3136)
        .setThumbnail(`https://minotar.net/helm/${username}/128.png`)
        .addFields(
          { name: 'Tester:', value: `<@${tester.id}>`, inline: false },
          { name: 'Gamemode:', value: getGamemodeDisplay(gamemode), inline: false },
          { name: 'Minecraft Name:', value: username, inline: false },
          { name: 'Previous Rank:', value: prevRank, inline: false },
          { name: 'Achieved Rank:', value: rank, inline: false },
        );

      // Send to results channel
      await sendResultEmbed(interaction.guild, embed);

      // Apply tier role if linked
      let tagStatus = '';
      const linked = db.getLinkedAccountByMinecraft(username);
      if (linked) {
        const member = interaction.guild.members.cache.get(linked.discord_id);
        if (member) {
          tagStatus = await applyTierRole(member, gamemode, rank);
        } else {
          tagStatus = `⚠️ Player not in server (Discord ID: ${linked.discord_id})`;
        }
      } else {
        tagStatus = '⚠️ No linked account — role not assigned.';
      }

      const sign = diff >= 0 ? '+' : '';
      let msg = `✅ Result saved!\nPrev: **${prevRank}** → **${rank}** | ${sign}${diff} pts`;
      if (tagStatus) msg += `\n${tagStatus}`;
      await interaction.editReply(msg);
    },
  },

  // /profile
  profile: {
    data: new SlashCommandBuilder()
      .setName('profile')
      .setDescription('View a player\'s tiers')
      .addStringOption(o => o.setName('name').setDescription('Player name').setRequired(true)),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: false });
      const name = interaction.options.getString('name');
      const tests = db.getTestsByUsername(name);

      if (!tests.length) {
        return interaction.editReply(`❌ No results found for **${name}**.`);
      }

      // Sort by points desc
      tests.sort((a, b) => (POINTS[b.rank] || 0) - (POINTS[a.rank] || 0));

      let totalPoints = 0;
      const modeLines = [];
      for (const t of tests) {
        const p = POINTS[t.rank] || 0;
        totalPoints += p;
        modeLines.push(`**${t.mode}**: ${t.rank} (${p}pt)`);
      }

      // Global rank
      const all = db.getAllTests();
      const playerTotals = {};
      for (const t of all) {
        playerTotals[t.username] = (playerTotals[t.username] || 0) + (POINTS[t.rank] || 0);
      }
      const sorted = Object.entries(playerTotals).sort((a, b) => b[1] - a[1]);
      const rankIdx = sorted.findIndex(([n]) => n.toLowerCase() === name.toLowerCase());
      const globalRank = rankIdx >= 0 ? rankIdx + 1 : null;

      const displayName = tests[0].username;
      const rankName = getRankName(totalPoints);
      const rankProgress = getRankProgress(totalPoints);

      const embed = new EmbedBuilder()
        .setTitle(`${displayName}'s Profile`)
        .setColor(0x5865F2)
        .setThumbnail(`https://minotar.net/helm/${displayName}/128.png`)
        .setDescription(modeLines.join('\n'));

      let stats = `**Total Points:** ${totalPoints}\n**Rank:** ${rankName}`;
      if (globalRank) stats += `\n**Global Rank:** #${globalRank}`;
      if (rankProgress) stats += `\n**Progress:** ${rankProgress}`;
      embed.addFields({ name: 'Statistics', value: stats, inline: false });

      await interaction.editReply({ embeds: [embed] });
    },
  },

  // /spin
  spin: {
    data: new SlashCommandBuilder()
      .setName('spin')
      .setDescription('Pick a random player from a gamemode and tier (staff only)')
      .addStringOption(o => o.setName('gamemode').setDescription('Gamemode').setRequired(true)
        .addChoices(...gamemodeChoices))
      .addStringOption(o => o.setName('tier').setDescription('Tier').setRequired(true)
        .addChoices(...rankChoices)),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: false });
      if (!isStaff(interaction.member)) {
        return interaction.editReply('❌ Staff only.');
      }

      const gamemode = interaction.options.getString('gamemode');
      const tier = interaction.options.getString('tier');

      const players = db.getTestsByModeAndRank(getGamemodeDisplay(gamemode), tier);
      if (!players.length) {
        return interaction.editReply('❌ No players found for this gamemode and tier.');
      }

      const picked = players[Math.floor(Math.random() * players.length)];
      const embed = new EmbedBuilder()
        .setTitle('🎲 Random Player')
        .setDescription(`**${picked.username}** (${picked.rank})`)
        .setColor(0xF1C40F)
        .setThumbnail(`https://minotar.net/helm/${picked.username}/128.png`);

      await interaction.editReply({ embeds: [embed] });
    },
  },

  // /tierlistnamechange
  tierlistnamechange: {
    data: new SlashCommandBuilder()
      .setName('tierlistnamechange')
      .setDescription('Rename a player in the database (staff only)')
      .addStringOption(o => o.setName('oldname').setDescription('Current name').setRequired(true))
      .addStringOption(o => o.setName('newname').setDescription('New name').setRequired(true)),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });
      if (!isStaff(interaction.member)) {
        return interaction.editReply('❌ Staff only.');
      }

      const oldName = interaction.options.getString('oldname');
      const newName = interaction.options.getString('newname');

      const existing = db.getTestsByUsername(oldName);
      if (!existing.length) {
        return interaction.editReply(`❌ Player not found: **${oldName}**`);
      }

      const ok = db.renamePlayer(oldName, newName);
      if (!ok) {
        return interaction.editReply('❌ Failed to rename player.');
      }

      await interaction.editReply(`✅ Renamed **${oldName}** → **${newName}** (${existing.length} entries updated)`);
    },
  },

  // /retire
  retire: {
    data: new SlashCommandBuilder()
      .setName('retire')
      .setDescription('Retire a player (admin only, LT2/HT2 only)')
      .addStringOption(o => o.setName('name').setDescription('Player name').setRequired(true))
      .addStringOption(o => o.setName('gamemode').setDescription('Gamemode').setRequired(true)
        .addChoices(...gamemodeChoices)),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });
      if (!isAdmin(interaction.member)) {
        return interaction.editReply('❌ Admin only.');
      }

      const name = interaction.options.getString('name');
      const gamemode = interaction.options.getString('gamemode');
      const modeDisplay = getGamemodeDisplay(gamemode);

      const test = db.getTestByUsernameAndMode(name, modeDisplay);
      if (!test) {
        return interaction.editReply(`❌ Player **${name}** not found in **${modeDisplay}**.`);
      }

      if (!['LT2', 'HT2'].includes(test.rank)) {
        return interaction.editReply(`❌ Only LT2/HT2 players can be retired. Current rank: **${test.rank}**.`);
      }

      const retiredRank = `R${test.rank}`;
      db.upsertTest({
        username: test.username,
        mode: modeDisplay,
        rank: retiredRank,
        testerId: test.testerId,
        testerName: test.testerName,
        ts: Math.floor(Date.now() / 1000),
      });

      await interaction.editReply(`✅ **${name}** (${modeDisplay}) is now retired as **${retiredRank}**.`);
    },
  },

  // /unretire
  unretire: {
    data: new SlashCommandBuilder()
      .setName('unretire')
      .setDescription('Unretire a player (admin only)')
      .addStringOption(o => o.setName('name').setDescription('Player name').setRequired(true))
      .addStringOption(o => o.setName('gamemode').setDescription('Gamemode').setRequired(true)
        .addChoices(...gamemodeChoices)),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });
      if (!isAdmin(interaction.member)) {
        return interaction.editReply('❌ Admin only.');
      }

      const name = interaction.options.getString('name');
      const gamemode = interaction.options.getString('gamemode');
      const modeDisplay = getGamemodeDisplay(gamemode);

      const test = db.getTestByUsernameAndMode(name, modeDisplay);
      if (!test) {
        return interaction.editReply(`❌ Player **${name}** not found in **${modeDisplay}**.`);
      }

      if (!test.rank.startsWith('R')) {
        return interaction.editReply(`❌ Player is not retired in this gamemode.`);
      }

      const originalRank = test.rank.slice(1);
      db.upsertTest({
        username: test.username,
        mode: modeDisplay,
        rank: originalRank,
        testerId: test.testerId,
        testerName: test.testerName,
        ts: Math.floor(Date.now() / 1000),
      });

      await interaction.editReply(`✅ **${name}** (${modeDisplay}) has returned to the tierlist as **${originalRank}**.`);
    },
  },

  // /tierlistban
  tierlistban: {
    data: new SlashCommandBuilder()
      .setName('tierlistban')
      .setDescription('Ban a player from testing (staff only)')
      .addStringOption(o => o.setName('name').setDescription('Player name').setRequired(true))
      .addIntegerOption(o => o.setName('days').setDescription('Days (0 = permanent)').setRequired(true))
      .addStringOption(o => o.setName('reason').setDescription('Reason').setRequired(false)),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });
      if (!isStaff(interaction.member)) {
        return interaction.editReply('❌ Staff only.');
      }

      const name = interaction.options.getString('name');
      const days = interaction.options.getInteger('days');
      const reason = interaction.options.getString('reason') || '';

      if (storage.isPlayerBanned(name)) {
        const info = storage.getBanInfo(name);
        if (info) {
          const expStr = info.permanent ? 'permanent' : `expires <t:${info.expires_at}:R>`;
          return interaction.editReply(`❌ **${name}** is already banned (${expStr}).`);
        }
      }

      storage.banPlayer(name, days, reason);

      let msg = days === 0
        ? `✅ **${name}** has been permanently banned from testing.`
        : `✅ **${name}** has been banned for **${days}** days.`;
      if (reason) msg += `\n**Reason:** ${reason}`;

      await interaction.editReply(msg);
    },
  },

  // /tierlistunban
  tierlistunban: {
    data: new SlashCommandBuilder()
      .setName('tierlistunban')
      .setDescription('Unban a player (staff only)')
      .addStringOption(o => o.setName('name').setDescription('Player name').setRequired(true)),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });
      if (!isStaff(interaction.member)) {
        return interaction.editReply('❌ Staff only.');
      }

      const name = interaction.options.getString('name');
      if (!storage.isPlayerBanned(name)) {
        return interaction.editReply(`❌ **${name}** is not banned.`);
      }

      storage.unbanPlayer(name);
      await interaction.editReply(`✅ **${name}** has been unbanned.`);
    },
  },

  // /removetierlist
  removetierlist: {
    data: new SlashCommandBuilder()
      .setName('removetierlist')
      .setDescription('Remove a player from the database (staff only, irreversible)')
      .addStringOption(o => o.setName('name').setDescription('Player name').setRequired(true)),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });
      if (!isStaff(interaction.member)) {
        return interaction.editReply('❌ Staff only.');
      }

      const name = interaction.options.getString('name');
      const tests = db.getTestsByUsername(name);

      if (!tests.length) {
        return interaction.editReply(`❌ Player **${name}** not found in the database.`);
      }

      const modesInfo = tests
        .map(t => `• **${t.mode}**: ${t.rank} (${POINTS[t.rank] || 0}pt)`)
        .join('\n')
        .slice(0, 1500);

      const embed = new EmbedBuilder()
        .setTitle('⚠️ WARNING — Confirm Deletion')
        .setDescription(
          `Are you sure you want to remove **${name}** from the tierlist?\n\n` +
          `**Current entries:**\n${modesInfo}\n\n` +
          `❗ **THIS IS PERMANENT!**`
        )
        .setColor(0xe74c3c)
        .setFooter({ text: `Requested by: ${interaction.user.displayName}` });

      const confirm = new ButtonBuilder()
        .setCustomId(`remove_confirm:${name}`)
        .setLabel('Yes, delete')
        .setStyle(ButtonStyle.Danger);
      const cancel = new ButtonBuilder()
        .setCustomId('remove_cancel')
        .setLabel('Cancel')
        .setStyle(ButtonStyle.Secondary);

      const row = new ActionRowBuilder().addComponents(confirm, cancel);
      await interaction.editReply({ embeds: [embed], components: [row] });
    },
  },

  // /bulkimport
  bulkimport: {
    data: new SlashCommandBuilder()
      .setName('bulkimport')
      .setDescription('Bulk import from text file (admin only): username mode rank per line')
      .addAttachmentOption(o => o.setName('file').setDescription('Text file').setRequired(true)),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });
      if (!isAdmin(interaction.member)) {
        return interaction.editReply('❌ Admin only.');
      }

      const attachment = interaction.options.getAttachment('file');
      let text;
      try {
        const { default: fetch } = await import('node-fetch');
        const res = await fetch(attachment.url);
        text = await res.text();
      } catch (e) {
        return interaction.editReply(`❌ Failed to download file: ${e.message}`);
      }

      const lines = text.trim().split('\n').filter(Boolean);
      let successCount = 0;
      let errorCount = 0;
      const errors = [];

      for (const line of lines) {
        const parts = line.trim().split(/\s+/);
        if (parts.length < 3) {
          errorCount++;
          errors.push(`Invalid format: ${line.slice(0, 50)}`);
          continue;
        }
        const [username, mode, rank] = parts;
        const modeDisplay = getGamemodeDisplay(mode.toLowerCase());
        const rankUpper = rank.toUpperCase();

        if (!RANKS.includes(rankUpper)) {
          errorCount++;
          errors.push(`Unknown rank: ${rankUpper} for ${username}`);
          continue;
        }

        const ok = db.upsertTest({
          username,
          mode: modeDisplay || mode,
          rank: rankUpper,
          testerId: String(interaction.user.id),
          testerName: interaction.user.displayName || interaction.user.username,
          ts: Math.floor(Date.now() / 1000),
        });

        if (ok) successCount++;
        else { errorCount++; errors.push(`DB error: ${username} ${modeDisplay} ${rankUpper}`); }
      }

      let msg = `✅ Imported: **${successCount}** | ❌ Failed: **${errorCount}**`;
      if (errors.length) {
        msg += '\n\nErrors:\n' + errors.slice(0, 10).join('\n');
        if (errors.length > 10) msg += `\n...and ${errors.length - 10} more.`;
      }
      await interaction.editReply(msg);
    },
  },

  // /synctiertags
  synctiertags: {
    data: new SlashCommandBuilder()
      .setName('synctiertags')
      .setDescription('Re-apply tier roles to all linked players (staff only)'),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });
      if (!isStaff(interaction.member)) {
        return interaction.editReply('❌ Staff only.');
      }

      const allIds = getAllTierRoleIds();
      if (allIds.size === 0) {
        return interaction.editReply('⚠️ No tier roles configured.');
      }

      await interaction.editReply('🔄 Syncing tier roles for all linked players...');

      const accounts = db.getAllLinkedAccounts();
      let tagged = 0, skipped = 0;
      const errors = [];

      for (const account of accounts) {
        const member = interaction.guild.members.cache.get(account.discord_id);
        if (!member) { skipped++; continue; }

        // Get best rank across all modes
        const tests = db.getTestsByUsername(account.minecraft_name);
        if (!tests.length) { skipped++; continue; }

        // Find highest rank per gamemode and apply each
        const byMode = {};
        for (const t of tests) {
          const mode = t.mode.toLowerCase();
          if (!byMode[mode] || (POINTS[t.rank] || 0) > (POINTS[byMode[mode]] || 0)) {
            byMode[mode] = t.rank;
          }
        }

        let success = true;
        for (const [mode, rank] of Object.entries(byMode)) {
          if (rank.startsWith('R')) continue; // retired
          try {
            await applyTierRole(member, mode, rank);
          } catch (e) {
            errors.push(`${account.minecraft_name}/${mode}: ${e.message}`);
            success = false;
          }
        }
        if (success) tagged++; else skipped++;
      }

      let summary = `✅ Sync complete. Tagged: **${tagged}** | Skipped: **${skipped}**`;
      if (errors.length) {
        summary += '\n\n**Errors:**\n' + errors.slice(0, 10).join('\n');
        if (errors.length > 10) summary += `\n...and ${errors.length - 10} more.`;
      }

      await interaction.followUp({ content: summary, ephemeral: true });
    },
  },

  // /testingleaderboard
  testingleaderboard: {
    data: new SlashCommandBuilder()
      .setName('testingleaderboard')
      .setDescription('View the tester activity leaderboard'),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: false });

      const now = new Date();
      const monthKey = `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, '0')}`;
      const monthName = now.toLocaleString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' });

      const alltime = db.getTesterStats('alltime');
      const monthly = db.getTesterStats(monthKey);

      const medals = ['🥇', '🥈', '🥉'];

      function formatRows(rows) {
        if (!rows.length) return 'No data yet.';
        return rows.slice(0, 10).map((row, i) => {
          const medal = medals[i] || `#${i + 1}`;
          const member = interaction.guild.members.cache.get(row.tester_id);
          const name = member ? `<@${row.tester_id}>` : `**${row.tester_name}**`;
          return `${medal} ${name} — **${row.count}** tests`;
        }).join('\n');
      }

      const embed = new EmbedBuilder()
        .setTitle('Testing Leaderboard')
        .setColor(0xFFD700)
        .addFields(
          { name: 'All Time', value: formatRows(alltime), inline: false },
          { name: '​', value: '━━━━━━━━━━━━━━━━━━━━━━━━━━━━', inline: false },
          { name: `${monthName} Leaderboard`, value: formatRows(monthly), inline: false },
        )
        .setFooter({ text: `SM Tierlist • ${monthName}` });

      await interaction.editReply({ embeds: [embed] });
    },
  },

  // /resetcooldown
  resetcooldown: {
    data: new SlashCommandBuilder()
      .setName('resetcooldown')
      .setDescription('Reset a player\'s cooldown for a gamemode (staff only)')
      .addUserOption(o => o.setName('user').setDescription('Discord user').setRequired(true))
      .addStringOption(o => o.setName('gamemode').setDescription('Gamemode (omit for all)').setRequired(false)
        .addChoices(...GAMEMODES.map(g => ({ name: g, value: g.toLowerCase() })))),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });
      if (!isStaff(interaction.member)) {
        return interaction.editReply('❌ Staff only.');
      }

      const targetUser = interaction.options.getUser('user');
      const gamemode = interaction.options.getString('gamemode');

      if (gamemode) {
        storage.setLastClosed(targetUser.id, gamemode, 0);
        const targetName = (interaction.guild.members.cache.get(targetUser.id) || { displayName: targetUser.username }).displayName;
        await interaction.editReply(`✅ Reset **${getGamemodeDisplay(gamemode)}** cooldown for **${targetName}**.`);
      } else {
        for (const gm of GAMEMODES) {
          storage.setLastClosed(targetUser.id, gm.toLowerCase(), 0);
        }
        const targetName = (interaction.guild.members.cache.get(targetUser.id) || { displayName: targetUser.username }).displayName;
        await interaction.editReply(`✅ Reset **all** cooldowns for **${targetName}**.`);
      }
    },
  },

  // /cooldown
  cooldown: {
    data: new SlashCommandBuilder()
      .setName('cooldown')
      .setDescription('Check cooldowns per gamemode')
      .addUserOption(o => o.setName('user').setDescription('User (staff only, default: yourself)').setRequired(false)),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });

      let targetId = interaction.user.id;
      let targetName = interaction.user.displayName || interaction.user.username;
      const targetUser = interaction.options.getUser('user');

      if (targetUser) {
        if (!isStaff(interaction.member)) {
          return interaction.editReply('❌ Only staff can view other players\' cooldowns.');
        }
        targetId = targetUser.id;
        const targetMember = interaction.guild.members.cache.get(targetUser.id);
        targetName = targetMember ? targetMember.displayName : targetUser.username;
      }

      // Check ban
      if (storage.isPlayerBanned(targetName)) {
        const info = storage.getBanInfo(targetName);
        const expStr = info && info.permanent ? 'permanent' : (info ? `<t:${info.expires_at}:R>` : 'unknown');
        return interaction.editReply(
          `❌ **${targetName}** is banned from testing!\n` +
          (info && info.reason ? `**Reason:** ${info.reason}\n` : '') +
          `**Expires:** ${expStr}`
        );
      }

      const cooldowns = storage.getUserCooldowns(targetId);
      const now = Math.floor(Date.now() / 1000);
      const lines = [];

      for (const gm of GAMEMODES) {
        const modeKey = gm.toLowerCase();
        const last = parseFloat(cooldowns[modeKey] || 0);
        if (!last) {
          lines.push(`✅ **${gm}**: No cooldown`);
        } else {
          const left = Math.floor(last + COOLDOWN_SECONDS - now);
          if (left <= 0) {
            lines.push(`✅ **${gm}**: Ready!`);
          } else {
            const d = Math.floor(left / 86400);
            const h = Math.floor((left % 86400) / 3600);
            const m = Math.floor((left % 3600) / 60);
            const timeStr = d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`;
            lines.push(`⏳ **${gm}**: ${timeStr}`);
          }
        }
      }

      const embed = new EmbedBuilder()
        .setTitle(`⏳ Cooldowns — ${targetName}`)
        .setColor(0x5865F2)
        .setDescription(lines.join('\n'))
        .setFooter({ text: 'Cooldown: 30 days' });

      await interaction.editReply({ embeds: [embed] });
    },
  },
};

// Button handlers for removetierlist confirmation
async function handleRemoveConfirm(interaction) {
  if (!interaction.customId.startsWith('remove_confirm:')) return false;
  const name = interaction.customId.split(':').slice(1).join(':');

  // Only the original invoker can confirm (check message author via interaction)
  const msg = interaction.message;
  if (msg.interaction && msg.interaction.user.id !== interaction.user.id) {
    await interaction.reply({ content: 'Only the command invoker can confirm.', ephemeral: true });
    return true;
  }

  await interaction.deferUpdate();

  const ok = db.deleteTestsByUsername(name);
  if (!ok) {
    await interaction.followUp({ content: `❌ Failed to delete **${name}**.`, ephemeral: true });
    return true;
  }

  const embed = new EmbedBuilder()
    .setTitle('✅ Player Removed from Tierlist')
    .setDescription(`**${name}** has been removed from all gamemodes.`)
    .setColor(0x2ecc71);

  await interaction.editReply({ embeds: [embed], components: [] });
  return true;
}

async function handleRemoveCancel(interaction) {
  if (interaction.customId !== 'remove_cancel') return false;
  await interaction.reply({ content: '❌ Deletion cancelled.', ephemeral: true });
  try {
    await interaction.message.edit({ components: [] });
  } catch {}
  return true;
}

module.exports = {
  commands,
  handleRemoveConfirm,
  handleRemoveCancel,
  applyTierRole,
  sendResultEmbed,
};
