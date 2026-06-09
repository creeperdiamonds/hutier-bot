'use strict';
const {
  SlashCommandBuilder,
  EmbedBuilder,
  ButtonBuilder,
  ButtonStyle,
  ActionRowBuilder,
  StringSelectMenuBuilder,
  StringSelectMenuOptionBuilder,
  ChannelType,
  PermissionFlagsBits,
  AttachmentBuilder,
} = require('discord.js');
const {
  GAMEMODES,
  RANKS,
  POINTS,
  TICKET_CATEGORY_ID,
  STAFF_ROLE_ID,
  TIER_RESULTS_CHANNEL_ID,
  TEST_LOGS_CHANNEL_ID,
  getGamemodeDisplay,
  getTicketRoundsDisplay,
} = require('../config');
const db = require('../database');
const storage = require('../storage');
const { isStaff, isGamemodeTester, canOpenTicket } = require('../permissions');

// Save a transcript of the channel to the test logs channel
async function saveTranscript(channel, guild, metadata) {
  if (!TEST_LOGS_CHANNEL_ID) return;
  const logChannel = guild.channels.cache.get(TEST_LOGS_CHANNEL_ID);
  if (!logChannel || !logChannel.isTextBased()) return;

  try {
    const messages = await channel.messages.fetch({ limit: 100 });
    const sorted = [...messages.values()].reverse();

    const lines = sorted.map(msg => {
      const time = new Date(msg.createdTimestamp).toISOString().replace('T', ' ').slice(0, 19);
      const author = msg.member ? msg.member.displayName : (msg.author ? msg.author.username : 'Unknown');
      let content = msg.content || '';
      if (msg.embeds.length > 0) content += (content ? ' ' : '') + `[${msg.embeds.length} embed(s)]`;
      if (msg.attachments.size > 0) content += (content ? ' ' : '') + `[${msg.attachments.size} file(s)]`;
      return `[${time}] ${author}: ${content}`;
    });

    const headerLines = [
      `# Transcript: ${channel.name}`,
      metadata.gamemode ? `Gamemode: ${metadata.gamemode}` : null,
      metadata.player ? `Player: ${metadata.player}` : null,
      metadata.tester ? `Tester: ${metadata.tester}` : null,
      metadata.result ? `Result: ${metadata.result}` : null,
      '',
    ].filter(l => l !== null);

    const text = headerLines.join('\n') + lines.join('\n');
    const buf = Buffer.from(text, 'utf8');
    const file = new AttachmentBuilder(buf, { name: `transcript-${channel.name}.txt` });

    const logEmbed = new EmbedBuilder()
      .setTitle('Test Session Transcript')
      .setColor(metadata.result ? 0x2ecc71 : 0x95a5a6)
      .setFooter({ text: new Date().toUTCString() });

    for (const [key, val] of Object.entries(metadata)) {
      if (val) logEmbed.addFields({ name: key.charAt(0).toUpperCase() + key.slice(1), value: val, inline: true });
    }

    await logChannel.send({ embeds: [logEmbed], files: [file] });
  } catch (e) {
    console.error('[Tickets] Transcript save error:', e.message);
  }
}

function buildCloseTicketRow(ownerId, modeKey) {
  return new ActionRowBuilder().addComponents(
    new ButtonBuilder()
      .setCustomId(`ticket_close:${ownerId}:${modeKey}`)
      .setLabel('Close Ticket')
      .setStyle(ButtonStyle.Danger),
    new ButtonBuilder()
      .setCustomId(`ticket_dismiss:${ownerId}:${modeKey}`)
      .setLabel('Dismiss (No Cooldown)')
      .setStyle(ButtonStyle.Secondary),
    new ButtonBuilder()
      .setCustomId(`ticket_give_tier:${ownerId}:${modeKey}`)
      .setLabel('Give Tier')
      .setStyle(ButtonStyle.Success),
  );
}

const commands = {
  // /closeticket — close active session ticket by gamemode (slash command)
  closeticket: {
    data: new SlashCommandBuilder()
      .setName('closeticket')
      .setDescription('Close the active test ticket for a gamemode')
      .addStringOption(o => o.setName('gamemode').setDescription('Gamemode').setRequired(true)
        .addChoices(...GAMEMODES.map(g => ({ name: g, value: g.toLowerCase() })))),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });
      const gamemode = interaction.options.getString('gamemode');

      if (!isGamemodeTester(interaction.member, gamemode)) {
        return interaction.editReply('❌ Only the gamemode tester or staff can close this ticket.');
      }

      const session = storage.getActiveSession(gamemode);
      if (!session) {
        return interaction.editReply(`❌ No active ticket found for **${getGamemodeDisplay(gamemode)}**.`);
      }

      const guild = interaction.guild;
      const ticketChannel = guild.channels.cache.get(session.channel_id);
      const linked = db.getLinkedAccount(session.player_id);
      const mcName = linked ? linked.minecraft_name : session.player_id;

      storage.setLastClosed(session.player_id, gamemode, Math.floor(Date.now() / 1000));
      storage.setOpenTicketChannelId(session.player_id, gamemode, null);
      storage.clearActiveSession(gamemode);

      if (ticketChannel) {
        await saveTranscript(ticketChannel, guild, {
          gamemode: getGamemodeDisplay(gamemode),
          player: mcName,
          tester: interaction.member.displayName,
          result: 'Closed via /closeticket',
        });
        try { await ticketChannel.send(`Ticket closed by ${interaction.user}.`); } catch {}
        setTimeout(async () => { try { await ticketChannel.delete('Ticket closed'); } catch {} }, 3000);
      }

      await interaction.editReply(`✅ Closed **${getGamemodeDisplay(gamemode)}** ticket.`);
    },
  },

  // /dismissticket — close without applying cooldown
  dismissticket: {
    data: new SlashCommandBuilder()
      .setName('dismissticket')
      .setDescription('Dismiss ticket without applying cooldown (no-show)')
      .addStringOption(o => o.setName('gamemode').setDescription('Gamemode').setRequired(true)
        .addChoices(...GAMEMODES.map(g => ({ name: g, value: g.toLowerCase() })))),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });
      const gamemode = interaction.options.getString('gamemode');

      if (!isGamemodeTester(interaction.member, gamemode)) {
        return interaction.editReply('❌ Only the gamemode tester or staff can dismiss this ticket.');
      }

      const session = storage.getActiveSession(gamemode);
      if (!session) {
        return interaction.editReply(`❌ No active ticket for **${getGamemodeDisplay(gamemode)}**.`);
      }

      const guild = interaction.guild;
      const ticketChannel = guild.channels.cache.get(session.channel_id);
      const linked = db.getLinkedAccount(session.player_id);
      const mcName = linked ? linked.minecraft_name : session.player_id;

      // No cooldown
      storage.setOpenTicketChannelId(session.player_id, gamemode, null);
      storage.clearActiveSession(gamemode);

      if (ticketChannel) {
        await saveTranscript(ticketChannel, guild, {
          gamemode: getGamemodeDisplay(gamemode),
          player: mcName,
          tester: interaction.member.displayName,
          result: 'Dismissed (no-show, no cooldown)',
        });
        try { await ticketChannel.send(`Ticket dismissed by ${interaction.user} — no cooldown applied.`); } catch {}
        setTimeout(async () => { try { await ticketChannel.delete('Ticket dismissed'); } catch {} }, 3000);
      }

      await interaction.editReply(`✅ Dismissed **${getGamemodeDisplay(gamemode)}** ticket (no cooldown).`);
    },
  },

  // /closetest — close current ticket channel with 5s delay
  closetest: {
    data: new SlashCommandBuilder()
      .setName('closetest')
      .setDescription('Close the current test ticket channel (5-second delay)'),

    async execute(interaction) {
      if (!isStaff(interaction.member)) {
        return interaction.reply({ content: '❌ Staff or testers only.', ephemeral: true });
      }

      const session = storage.findActiveSessionByChannel(interaction.channelId);
      if (!session) {
        return interaction.reply({ content: '❌ This channel is not an active test session.', ephemeral: true });
      }

      await interaction.reply({ content: '⏳ Closing ticket in 5 seconds...' });

      const { gamemode } = session;
      const linked = db.getLinkedAccount(session.player_id);
      const mcName = linked ? linked.minecraft_name : session.player_id;

      storage.setLastClosed(session.player_id, gamemode, Math.floor(Date.now() / 1000));
      storage.setOpenTicketChannelId(session.player_id, gamemode, null);
      storage.clearActiveSession(gamemode);

      await saveTranscript(interaction.channel, interaction.guild, {
        gamemode: getGamemodeDisplay(gamemode),
        player: mcName,
        tester: interaction.member.displayName,
        result: 'Closed via /closetest',
      });

      setTimeout(async () => { try { await interaction.channel.delete('Ticket closed via /closetest'); } catch {} }, 5000);
    },
  },

  // /forceclosetest — immediately delete the current ticket channel
  forceclosetest: {
    data: new SlashCommandBuilder()
      .setName('forceclosetest')
      .setDescription('Force-close the current test ticket channel immediately'),

    async execute(interaction) {
      if (!isStaff(interaction.member)) {
        return interaction.reply({ content: '❌ Staff or testers only.', ephemeral: true });
      }

      const session = storage.findActiveSessionByChannel(interaction.channelId);
      if (!session) {
        return interaction.reply({ content: '❌ This channel is not an active test session.', ephemeral: true });
      }

      const { gamemode } = session;
      const linked = db.getLinkedAccount(session.player_id);
      const mcName = linked ? linked.minecraft_name : session.player_id;

      storage.setLastClosed(session.player_id, gamemode, Math.floor(Date.now() / 1000));
      storage.setOpenTicketChannelId(session.player_id, gamemode, null);
      storage.clearActiveSession(gamemode);

      await saveTranscript(interaction.channel, interaction.guild, {
        gamemode: getGamemodeDisplay(gamemode),
        player: mcName,
        tester: interaction.member.displayName,
        result: 'Force-closed via /forceclosetest',
      });

      await interaction.reply({ content: '🔴 Force-closing...' });
      setTimeout(async () => { try { await interaction.channel.delete('Force-closed'); } catch {} }, 3000);
    },
  },

  // /add — add a user to the current ticket channel
  add: {
    data: new SlashCommandBuilder()
      .setName('add')
      .setDescription('Add a user to this ticket channel')
      .addUserOption(o => o.setName('user').setDescription('User to add').setRequired(true)),

    async execute(interaction) {
      if (!isStaff(interaction.member)) {
        return interaction.reply({ content: '❌ Staff or testers only.', ephemeral: true });
      }
      if (!storage.findActiveSessionByChannel(interaction.channelId)) {
        return interaction.reply({ content: '❌ This channel is not an active test session.', ephemeral: true });
      }

      const user = interaction.options.getUser('user');
      try {
        await interaction.channel.permissionOverwrites.create(user.id, {
          ViewChannel: true,
          SendMessages: true,
          ReadMessageHistory: true,
        });
        await interaction.reply({ content: `✅ <@${user.id}> has been added to the ticket.` });
      } catch (e) {
        await interaction.reply({ content: `❌ Failed to add user: ${e.message}`, ephemeral: true });
      }
    },
  },

  // /remove — remove a user from the current ticket channel
  remove: {
    data: new SlashCommandBuilder()
      .setName('remove')
      .setDescription('Remove a user from this ticket channel')
      .addUserOption(o => o.setName('user').setDescription('User to remove').setRequired(true)),

    async execute(interaction) {
      if (!isStaff(interaction.member)) {
        return interaction.reply({ content: '❌ Staff or testers only.', ephemeral: true });
      }
      if (!storage.findActiveSessionByChannel(interaction.channelId)) {
        return interaction.reply({ content: '❌ This channel is not an active test session.', ephemeral: true });
      }

      const user = interaction.options.getUser('user');
      try {
        await interaction.channel.permissionOverwrites.create(user.id, {
          ViewChannel: false,
          SendMessages: false,
        });
        await interaction.reply({ content: `✅ <@${user.id}> has been removed from the ticket.` });
      } catch (e) {
        await interaction.reply({ content: `❌ Failed to remove user: ${e.message}`, ephemeral: true });
      }
    },
  },

  // /passeval — rename ticket channel to passeval-{username}
  passeval: {
    data: new SlashCommandBuilder()
      .setName('passeval')
      .setDescription('Mark a player as passing eval (renames this channel)')
      .addUserOption(o => o.setName('user').setDescription('Player who passed').setRequired(true)),

    async execute(interaction) {
      if (!isStaff(interaction.member)) {
        return interaction.reply({ content: '❌ Staff or testers only.', ephemeral: true });
      }
      if (!storage.findActiveSessionByChannel(interaction.channelId)) {
        return interaction.reply({ content: '❌ This channel is not an active test session.', ephemeral: true });
      }

      const user = interaction.options.getUser('user');
      try {
        await interaction.channel.edit({ name: `passeval-${user.username}`.slice(0, 100) });
        await interaction.reply({ content: `✅ <@${user.id}> has passed eval!` });
      } catch (e) {
        await interaction.reply({ content: `❌ Failed to rename channel: ${e.message}`, ephemeral: true });
      }
    },
  },

  ticketpanel: {
    data: new SlashCommandBuilder()
      .setName('ticketpanel')
      .setDescription('Post ticket panel buttons for each gamemode (staff only)'),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });

      const embed = new EmbedBuilder()
        .setTitle('Test Request')
        .setDescription('Click one of the buttons below to request a test for that gamemode.')
        .setColor(0x5865F2);

      const rows = [];
      let currentRow = new ActionRowBuilder();
      let count = 0;

      for (const gm of GAMEMODES) {
        if (count > 0 && count % 5 === 0) {
          if (rows.length >= 4) break;
          rows.push(currentRow);
          currentRow = new ActionRowBuilder();
        }
        currentRow.addComponents(
          new ButtonBuilder()
            .setCustomId(`ticket_open:${gm.toLowerCase()}`)
            .setLabel(gm)
            .setStyle(ButtonStyle.Primary)
        );
        count++;
      }
      if (count % 5 !== 0 || count === 0) rows.push(currentRow);

      await interaction.channel.send({ embeds: [embed], components: rows });
      await interaction.editReply('✅ Ticket panel posted.');
    },
  },
};

// --- Button Handlers ---

async function handleTicketOpen(interaction) {
  if (!interaction.customId.startsWith('ticket_open:')) return false;
  const gamemode = interaction.customId.split(':')[1];

  const guild = interaction.guild;
  const member = interaction.member;
  if (!guild || !member) {
    await interaction.reply({ content: '❌ Server only.', ephemeral: true });
    return true;
  }

  const linked = db.getLinkedAccount(interaction.user.id);
  if (!linked) {
    await interaction.reply({
      content: '❌ **Your Minecraft account is not linked!**\nUse `/link` to link your account first.',
      ephemeral: true,
    });
    return true;
  }

  if (storage.isPlayerBanned(linked.minecraft_name)) {
    const info = storage.getBanInfo(linked.minecraft_name);
    await interaction.reply({
      content: `❌ You are banned from testing!\n${info && info.reason ? `**Reason:** ${info.reason}` : ''}`,
      ephemeral: true,
    });
    return true;
  }

  const test = db.getTestByUsernameAndMode(linked.minecraft_name, getGamemodeDisplay(gamemode));
  const rank = test ? test.rank : 'Unranked';
  if (!canOpenTicket(rank)) {
    await interaction.reply({
      content: `❌ Opening a **${getGamemodeDisplay(gamemode)}** ticket requires at least **LT3** rank. Your rank: **${rank}**.`,
      ephemeral: true,
    });
    return true;
  }

  const cdLeft = storage.cooldownLeft(interaction.user.id, gamemode);
  if (cdLeft > 0) {
    const d = Math.floor(cdLeft / 86400);
    const h = Math.floor((cdLeft % 86400) / 3600);
    await interaction.reply({
      content: `⏳ You have **${d}d ${h}h** cooldown remaining for **${getGamemodeDisplay(gamemode)}**.`,
      ephemeral: true,
    });
    return true;
  }

  const existingChannelId = storage.getOpenTicketChannelId(interaction.user.id, gamemode);
  if (existingChannelId) {
    const existingCh = guild.channels.cache.get(existingChannelId);
    if (existingCh) {
      await interaction.reply({ content: `❌ You already have an open ticket: ${existingCh}`, ephemeral: true });
      return true;
    } else {
      storage.setOpenTicketChannelId(interaction.user.id, gamemode, null);
    }
  }

  const categoryId = TICKET_CATEGORY_ID;
  const category = categoryId ? guild.channels.cache.get(categoryId) : null;
  if (categoryId && (!category || category.type !== ChannelType.GuildCategory)) {
    await interaction.reply({ content: '❌ Ticket category not configured correctly.', ephemeral: true });
    return true;
  }

  const safeName = (interaction.member.displayName || interaction.user.username)
    .toLowerCase().replace(/[^a-z0-9]/g, '-').slice(0, 32);
  const channelName = `${gamemode}-${safeName}`.slice(0, 50);

  const permOverwrites = [
    { id: guild.id, deny: [PermissionFlagsBits.ViewChannel] },
    {
      id: interaction.user.id,
      allow: [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.SendMessages, PermissionFlagsBits.ReadMessageHistory],
    },
  ];
  if (STAFF_ROLE_ID) {
    permOverwrites.push({
      id: STAFF_ROLE_ID,
      allow: [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.SendMessages, PermissionFlagsBits.ReadMessageHistory, PermissionFlagsBits.ManageChannels],
    });
  }

  try {
    const channel = await guild.channels.create({
      name: channelName,
      type: ChannelType.GuildText,
      parent: category ? category.id : undefined,
      permissionOverwrites: permOverwrites,
      topic: `Tierlist ticket | owner=${interaction.user.id} | mode=${gamemode} | mc=${linked.minecraft_name}`,
      reason: 'Tierlist ticket',
    });

    storage.setOpenTicketChannelId(interaction.user.id, gamemode, channel.id);

    const roundsDisplay = getTicketRoundsDisplay(gamemode);
    const embed = new EmbedBuilder()
      .setTitle('Test Request')
      .setDescription('A tester will be with you soon.')
      .setColor(0x5865F2)
      .setThumbnail(`https://minotar.net/helm/${linked.minecraft_name}/128.png`)
      .addFields(
        { name: 'Gamemode', value: getGamemodeDisplay(gamemode), inline: true },
        { name: 'Minecraft Name', value: `\`${linked.minecraft_name}\``, inline: true },
        { name: 'Rounds', value: roundsDisplay, inline: false },
        { name: 'Player', value: `<@${interaction.user.id}>`, inline: true },
      );

    await channel.send({
      embeds: [embed],
      components: [buildCloseTicketRow(interaction.user.id, gamemode)],
    });

    await interaction.reply({ content: `✅ Ticket created: ${channel}`, ephemeral: false });
  } catch (e) {
    console.error('[Tickets] Error creating channel:', e);
    await interaction.reply({ content: `❌ Failed to create ticket channel: ${e.message}`, ephemeral: true });
  }

  return true;
}

async function handleTicketClose(interaction) {
  if (!interaction.customId.startsWith('ticket_close:')) return false;
  const parts = interaction.customId.split(':');
  const ownerId = parts[1];
  const modeKey = parts[2];

  const channel = interaction.channel;
  if (!channel || !channel.isTextBased()) {
    await interaction.reply({ content: '❌ Not a text channel.', ephemeral: true });
    return true;
  }

  const member = interaction.member;
  if (interaction.user.id !== ownerId && !isStaff(member)) {
    await interaction.reply({ content: '❌ You don\'t have permission to close this ticket.', ephemeral: true });
    return true;
  }

  await interaction.reply({ content: '✅ Closing ticket in 5 seconds...', ephemeral: true });

  let actualOwnerId = ownerId;
  let actualMode = modeKey;
  let mcName = null;
  if (channel.topic) {
    const ownerMatch = channel.topic.match(/owner=(\d+)/);
    const modeMatch = channel.topic.match(/mode=([^\s|]+)/);
    const mcMatch = channel.topic.match(/mc=([^\s|]+)/);
    if (ownerMatch) actualOwnerId = ownerMatch[1];
    if (modeMatch) actualMode = modeMatch[1];
    if (mcMatch) mcName = mcMatch[1];
  }

  storage.setLastClosed(actualOwnerId, actualMode, Math.floor(Date.now() / 1000));
  storage.setOpenTicketChannelId(actualOwnerId, actualMode, null);
  storage.clearActiveSession(actualMode);

  const testerName = interaction.member ? interaction.member.displayName : interaction.user.username;
  await saveTranscript(channel, interaction.guild, {
    gamemode: getGamemodeDisplay(actualMode),
    player: mcName || actualOwnerId,
    tester: testerName,
    result: 'Closed (no result)',
  });

  setTimeout(async () => {
    try {
      await channel.delete('Ticket closed');
    } catch (e) {
      console.error('[Tickets] Error deleting channel:', e.message);
      try {
        await channel.send('❌ Cannot delete channel (Missing Permissions). Give bot Manage Channels in this category.');
      } catch {}
    }
  }, 5000);

  return true;
}

async function handleTicketDismiss(interaction) {
  if (!interaction.customId.startsWith('ticket_dismiss:')) return false;
  const parts = interaction.customId.split(':');
  const ownerId = parts[1];
  const modeKey = parts[2];

  const channel = interaction.channel;
  if (!channel || !channel.isTextBased()) {
    await interaction.reply({ content: '❌ Not a text channel.', ephemeral: true });
    return true;
  }

  if (!isStaff(interaction.member)) {
    await interaction.reply({ content: '❌ Staff only can dismiss tickets.', ephemeral: true });
    return true;
  }

  await interaction.reply({ content: '✅ Dismissing ticket (no cooldown applied) in 5 seconds...', ephemeral: true });

  let actualOwnerId = ownerId;
  let actualMode = modeKey;
  let mcName = null;
  if (channel.topic) {
    const ownerMatch = channel.topic.match(/owner=(\d+)/);
    const modeMatch = channel.topic.match(/mode=([^\s|]+)/);
    const mcMatch = channel.topic.match(/mc=([^\s|]+)/);
    if (ownerMatch) actualOwnerId = ownerMatch[1];
    if (modeMatch) actualMode = modeMatch[1];
    if (mcMatch) mcName = mcMatch[1];
  }

  // Clear open ticket state but do NOT apply cooldown
  storage.setOpenTicketChannelId(actualOwnerId, actualMode, null);
  storage.clearActiveSession(actualMode);

  const testerName = interaction.member ? interaction.member.displayName : interaction.user.username;
  await saveTranscript(channel, interaction.guild, {
    gamemode: getGamemodeDisplay(actualMode),
    player: mcName || actualOwnerId,
    tester: testerName,
    result: 'Dismissed (no-show, no cooldown)',
  });

  setTimeout(async () => {
    try {
      await channel.delete('Ticket dismissed');
    } catch (e) {
      console.error('[Tickets] Error deleting channel:', e.message);
    }
  }, 5000);

  return true;
}

// Step 1: staff clicks "Give Tier" → shows gamemode select
async function handleGiveTier(interaction) {
  if (!interaction.customId.startsWith('ticket_give_tier:')) return false;
  const parts = interaction.customId.split(':');
  const ownerId = parts[1];
  const modeKey = parts[2];

  if (!isStaff(interaction.member)) {
    await interaction.reply({ content: '❌ Staff only.', ephemeral: true });
    return true;
  }

  const channel = interaction.channel;
  let actualOwnerId = ownerId;
  let actualMode = modeKey;
  if (channel && channel.topic) {
    const ownerMatch = channel.topic.match(/owner=(\d+)/);
    const modeMatch = channel.topic.match(/mode=([^\s|]+)/);
    if (ownerMatch) actualOwnerId = ownerMatch[1];
    if (modeMatch) actualMode = modeMatch[1];
  }

  const linked = db.getLinkedAccount(actualOwnerId);
  if (!linked) {
    await interaction.reply({ content: '❌ The player has no linked Minecraft account.', ephemeral: true });
    return true;
  }

  const gamemodeOptions = GAMEMODES.map(gm =>
    new StringSelectMenuOptionBuilder()
      .setLabel(gm)
      .setValue(gm.toLowerCase())
      .setDefault(gm.toLowerCase() === actualMode)
  );

  const gmSelect = new StringSelectMenuBuilder()
    .setCustomId(`give_tier_gm:${actualOwnerId}:${linked.minecraft_name}`)
    .setPlaceholder('Select gamemode...')
    .addOptions(gamemodeOptions);

  const row = new ActionRowBuilder().addComponents(gmSelect);
  await interaction.reply({ content: 'Step 1 — Select the gamemode:', components: [row], ephemeral: true });
  return true;
}

// Step 2: gamemode picked → show rank select with gamemode baked into customId
async function handleGiveTierGmSelect(interaction) {
  if (!interaction.customId.startsWith('give_tier_gm:')) return false;
  const parts = interaction.customId.split(':');
  const ownerId = parts[1];
  const minecraftName = parts[2];

  const selectedGm = interaction.values[0];

  const tierOptions = RANKS.filter(r => r !== 'Unranked').map(r =>
    new StringSelectMenuOptionBuilder().setLabel(r).setValue(r)
  );

  const tierSelect = new StringSelectMenuBuilder()
    .setCustomId(`give_tier_rank:${ownerId}:${minecraftName}:${selectedGm}`)
    .setPlaceholder('Select achieved rank...')
    .addOptions(tierOptions);

  const row = new ActionRowBuilder().addComponents(tierSelect);
  await interaction.update({
    content: `Gamemode: **${getGamemodeDisplay(selectedGm)}**\nStep 2 — Select the rank:`,
    components: [row],
  });
  return true;
}

// Step 3: rank picked → process result
async function handleGiveTierRankSelect(interaction) {
  if (!interaction.customId.startsWith('give_tier_rank:')) return false;
  const parts = interaction.customId.split(':');
  const ownerId = parts[1];
  const minecraftName = parts[2];
  const selectedMode = parts[3];

  const selectedRank = interaction.values[0];

  await interaction.deferUpdate();

  const tester = interaction.member;
  const modeDisplay = getGamemodeDisplay(selectedMode);

  const prev = db.getTestByUsernameAndMode(minecraftName, modeDisplay);
  const prevRank = prev ? prev.rank : 'Unranked';
  const prevPoints = POINTS[prevRank] || 0;
  const newPoints = POINTS[selectedRank] || 0;
  const diff = newPoints - prevPoints;

  db.upsertTest({
    username: minecraftName,
    mode: modeDisplay,
    rank: selectedRank,
    testerId: String(tester.id),
    testerName: tester.displayName || tester.user.username,
    ts: Math.floor(Date.now() / 1000),
  });

  // Track tester activity
  db.incrementTesterStat(tester.id, tester.displayName || tester.user.username);

  storage.setLastClosed(ownerId, selectedMode, Math.floor(Date.now() / 1000));
  storage.setOpenTicketChannelId(ownerId, selectedMode, null);
  storage.clearActiveSession(selectedMode);

  const embed = new EmbedBuilder()
    .setTitle(`${minecraftName} Test Result 🏆`)
    .setColor(0x2f3136)
    .setThumbnail(`https://minotar.net/helm/${minecraftName}/128.png`)
    .addFields(
      { name: 'Tester:', value: `<@${tester.id}>`, inline: false },
      { name: 'Gamemode:', value: modeDisplay, inline: false },
      { name: 'Minecraft Name:', value: minecraftName, inline: false },
      { name: 'Previous Rank:', value: `${prevRank} (${prevPoints} pts)`, inline: false },
      { name: 'Achieved Rank:', value: `${selectedRank} (${newPoints} pts)`, inline: false },
      { name: 'Points:', value: diff >= 0 ? `+${diff}` : String(diff), inline: false },
    );

  if (TIER_RESULTS_CHANNEL_ID) {
    const guild = interaction.guild;
    const ch = guild.channels.cache.get(TIER_RESULTS_CHANNEL_ID);
    if (ch && ch.isTextBased()) {
      await ch.send({ embeds: [embed] }).catch(e => console.error('[Tickets] Results channel error:', e.message));
    }
  }

  // Apply tier role
  const linkedAccount = db.getLinkedAccount(ownerId);
  if (linkedAccount) {
    const targetMember = interaction.guild.members.cache.get(ownerId);
    if (targetMember) {
      const { applyTierRole } = require('./tierlist');
      await applyTierRole(targetMember, selectedMode, selectedRank).catch(e =>
        console.error('[Tickets] Role assign error:', e.message)
      );
    }
  }

  // Save transcript
  if (interaction.channel) {
    await saveTranscript(interaction.channel, interaction.guild, {
      gamemode: modeDisplay,
      player: minecraftName,
      tester: tester.displayName || tester.user.username,
      result: `${prevRank} → ${selectedRank} (${diff >= 0 ? '+' : ''}${diff} pts)`,
    });
  }

  await interaction.followUp({
    content: `✅ Tier set: **${selectedRank}** for **${minecraftName}** in **${modeDisplay}**. Channel closes in 5 seconds.`,
    ephemeral: true,
  });

  if (interaction.channel) {
    setTimeout(async () => {
      try { await interaction.channel.delete('Tier given, ticket closed'); } catch {}
    }, 5000);
  }

  return true;
}

module.exports = {
  commands,
  buildCloseTicketRow,
  handleTicketOpen,
  handleTicketClose,
  handleTicketDismiss,
  handleGiveTier,
  handleGiveTierGmSelect,
  handleGiveTierRankSelect,
};
