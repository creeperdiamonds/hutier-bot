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
} = require('discord.js');
const {
  GAMEMODES,
  QUEUE_CHANNELS,
  QUEUE_PING_ROLES,
  TICKET_CATEGORY_ID,
  STAFF_ROLE_ID,
  getGamemodeDisplay,
  getGamemodeColor,
  COOLDOWN_SECONDS,
} = require('../config');
const db = require('../database');
const storage = require('../storage');
const { isStaff, isGamemodeTester, canJoinQueue } = require('../permissions');

// In-memory queue state
// ACTIVE_QUEUES[gamemode] = { openedBy, openedAt, players: [{discordId, minecraftName}], testers: [...], calledPlayers: [], messageId, channelId }
const ACTIVE_QUEUES = {};

// Map: messageId -> gamemode (persisted in storage)
let QUEUE_MESSAGE_IDS = {};

function loadState() {
  QUEUE_MESSAGE_IDS = storage.loadQueueMessageIds();
  const qpm = storage.getQueuePanelMessage();
  return qpm;
}

// Build queue embed
function buildQueueEmbed(gamemode, queue, guild) {
  const display = getGamemodeDisplay(gamemode);
  const color = getGamemodeColor(gamemode);
  const indicator = '🟢';

  const playerLines = queue.players.map(p => {
    const member = guild.members.cache.get(p.discordId);
    const nick = member ? member.displayName : p.discordId;
    return `${nick} (${p.minecraftName})`;
  });
  const testerLines = queue.testers.map(t => {
    const member = guild.members.cache.get(t.discordId);
    const nick = member ? member.displayName : t.discordId;
    return `${nick} (${t.minecraftName})`;
  });

  return new EmbedBuilder()
    .setTitle(`${indicator} ${display} Queue`)
    .setDescription(`Players: **${queue.players.length}** | Testers: **${queue.testers.length}**`)
    .setColor(color)
    .addFields(
      { name: 'Players', value: playerLines.join('\n') || 'Nobody in queue yet.', inline: false },
      { name: 'Testers', value: testerLines.join('\n') || 'No testers.', inline: false },
    );
}

function buildClosedEmbed(gamemode) {
  return new EmbedBuilder()
    .setTitle(`🔴 ${getGamemodeDisplay(gamemode)} Queue`)
    .setDescription('The queue is closed.')
    .setColor(getGamemodeColor(gamemode));
}

// Build persistent action row for a queue message
function buildQueueActionRow(gamemode) {
  return new ActionRowBuilder().addComponents(
    new ButtonBuilder()
      .setCustomId(`queue_join:${gamemode}`)
      .setLabel('Join Queue')
      .setStyle(ButtonStyle.Success),
    new ButtonBuilder()
      .setCustomId(`queue_leave:${gamemode}`)
      .setLabel('Leave Queue')
      .setStyle(ButtonStyle.Danger),
    new ButtonBuilder()
      .setCustomId(`queue_close:${gamemode}`)
      .setLabel('❌ Close Queue')
      .setStyle(ButtonStyle.Secondary),
    new ButtonBuilder()
      .setCustomId(`queue_next:${gamemode}`)
      .setLabel('Next Player')
      .setStyle(ButtonStyle.Primary),
  );
}

// Update queue message
async function updateQueueMessage(gamemode, bot) {
  const queue = ACTIVE_QUEUES[gamemode];
  const msgId = Object.keys(QUEUE_MESSAGE_IDS).find(id => QUEUE_MESSAGE_IDS[id] === gamemode);
  if (!msgId) return;

  const channelId = QUEUE_CHANNELS[gamemode];
  if (!channelId) return;

  const channel = bot.channels.cache.get(channelId);
  if (!channel || !channel.isTextBased()) return;

  try {
    const msg = await channel.messages.fetch(msgId);
    if (!queue) {
      await msg.edit({ embeds: [buildClosedEmbed(gamemode)], components: [] });
      delete QUEUE_MESSAGE_IDS[msgId];
      storage.persistQueueMessageIds(QUEUE_MESSAGE_IDS);
    } else {
      await msg.edit({
        embeds: [buildQueueEmbed(gamemode, queue, channel.guild)],
        components: [buildQueueActionRow(gamemode)],
      });
    }
  } catch (e) {
    console.error(`[Queue] updateQueueMessage error [${gamemode}]:`, e.message);
    delete QUEUE_MESSAGE_IDS[msgId];
    storage.persistQueueMessageIds(QUEUE_MESSAGE_IDS);
  }
}

const commands = {
  // /queuepanel
  queuepanel: {
    data: new SlashCommandBuilder()
      .setName('queuepanel')
      .setDescription('Post an Open Queue button in each gamemode\'s dedicated channel (staff only)'),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });
      if (!isStaff(interaction.member)) {
        return interaction.editReply('❌ Staff only.');
      }

      const guild = interaction.guild;
      const posted = [];
      const skipped = [];

      for (const gm of GAMEMODES) {
        const modeKey = gm.toLowerCase();
        const channelId = QUEUE_CHANNELS[modeKey];
        if (!channelId) { skipped.push(gm); continue; }

        const channel = guild.channels.cache.get(channelId);
        if (!channel || !channel.isTextBased()) { skipped.push(gm); continue; }

        const embed = new EmbedBuilder()
          .setTitle(`${gm} Queue`)
          .setDescription('Testers: click below to open the queue.\nPlayers: click **Join Queue** once it\'s open.')
          .setColor(getGamemodeColor(modeKey));

        const row = new ActionRowBuilder().addComponents(
          new ButtonBuilder()
            .setCustomId(`queue_open:${modeKey}`)
            .setLabel(`Open ${gm} Queue`)
            .setStyle(ButtonStyle.Primary),
        );

        try {
          await channel.send({ embeds: [embed], components: [row] });
          posted.push(gm);
        } catch (e) {
          skipped.push(gm);
          console.error(`[Queue] Failed to post panel in ${gm} channel:`, e.message);
        }
      }

      let reply = `✅ Posted panels in **${posted.length}** channels: ${posted.join(', ')}`;
      if (skipped.length) reply += `\n⚠️ Skipped (no channel or no access): ${skipped.join(', ')}`;
      await interaction.editReply(reply);
    },
  },

  // /closequeue
  closequeue: {
    data: new SlashCommandBuilder()
      .setName('closequeue')
      .setDescription('Force close a queue (staff only)')
      .addStringOption(o => o.setName('gamemode').setDescription('Gamemode').setRequired(true)
        .addChoices(...GAMEMODES.map(g => ({ name: g, value: g.toLowerCase() })))),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });

      const gamemode = interaction.options.getString('gamemode');
      const queue = ACTIVE_QUEUES[gamemode];

      if (!queue) {
        // Try to clean up stale message
        const msgId = Object.keys(QUEUE_MESSAGE_IDS).find(id => QUEUE_MESSAGE_IDS[id] === gamemode);
        if (msgId) {
          const channelId = QUEUE_CHANNELS[gamemode];
          if (channelId) {
            const ch = interaction.guild.channels.cache.get(channelId);
            if (ch && ch.isTextBased()) {
              try {
                const msg = await ch.messages.fetch(msgId);
                await msg.edit({ embeds: [buildClosedEmbed(gamemode)], components: [] });
              } catch {}
            }
          }
          delete QUEUE_MESSAGE_IDS[msgId];
          storage.persistQueueMessageIds(QUEUE_MESSAGE_IDS);
        }
        return interaction.editReply(`❌ The **${getGamemodeDisplay(gamemode)}** queue is not open.`);
      }

      if (!isStaff(interaction.member) && queue.openedBy !== interaction.user.id) {
        return interaction.editReply('❌ Only the queue opener or staff can close it.');
      }

      delete ACTIVE_QUEUES[gamemode];
      await updateQueueMessage(gamemode, interaction.client);
      await interaction.editReply(`✅ **${getGamemodeDisplay(gamemode)}** queue closed.`);
    },
  },

  // /myqueue
  myqueue: {
    data: new SlashCommandBuilder()
      .setName('myqueue')
      .setDescription('See all queues you are currently in'),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });

      const lines = [];
      for (const [gamemode, queue] of Object.entries(ACTIVE_QUEUES)) {
        const pi = queue.players.findIndex(p => p.discordId === interaction.user.id);
        const ti = queue.testers.findIndex(t => t.discordId === interaction.user.id);
        if (pi >= 0) lines.push(`**${getGamemodeDisplay(gamemode)}**: Position #${pi + 1} of ${queue.players.length}`);
        else if (ti >= 0) lines.push(`**${getGamemodeDisplay(gamemode)}**: Tester`);
      }

      if (!lines.length) {
        return interaction.editReply('You are not in any queues.');
      }

      const embed = new EmbedBuilder()
        .setTitle('Your Queue Positions')
        .setDescription(lines.join('\n'))
        .setColor(0x5865F2);

      await interaction.editReply({ embeds: [embed] });
    },
  },

  // /removeplayer
  removeplayer: {
    data: new SlashCommandBuilder()
      .setName('removeplayer')
      .setDescription('Remove a specific player from a queue (staff/tester only)')
      .addStringOption(o => o.setName('gamemode').setDescription('Gamemode').setRequired(true)
        .addChoices(...GAMEMODES.map(g => ({ name: g, value: g.toLowerCase() })))),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });

      const gamemode = interaction.options.getString('gamemode');
      const queue = ACTIVE_QUEUES[gamemode];

      if (!queue) {
        return interaction.editReply(`❌ **${getGamemodeDisplay(gamemode)}** queue is not open.`);
      }

      if (!isStaff(interaction.member) && !isGamemodeTester(interaction.member, gamemode)) {
        return interaction.editReply('❌ Staff or testers only.');
      }

      if (!queue.players.length) {
        return interaction.editReply('❌ No players in this queue.');
      }

      const options = queue.players.map((p, i) => {
        const member = interaction.guild.members.cache.get(p.discordId);
        const nick = member ? member.displayName : p.discordId;
        return new StringSelectMenuOptionBuilder()
          .setLabel(`#${i + 1} — ${nick} (${p.minecraftName})`)
          .setValue(p.discordId);
      });

      const select = new StringSelectMenuBuilder()
        .setCustomId(`queue_remove_player:${gamemode}`)
        .setPlaceholder('Select player to remove...')
        .addOptions(options);

      const row = new ActionRowBuilder().addComponents(select);
      await interaction.editReply({ content: 'Select a player to remove:', components: [row] });
    },
  },

  // /clearqueue
  clearqueue: {
    data: new SlashCommandBuilder()
      .setName('clearqueue')
      .setDescription('Clear all players from a queue (staff only)')
      .addStringOption(o => o.setName('gamemode').setDescription('Gamemode').setRequired(true)
        .addChoices(...GAMEMODES.map(g => ({ name: g, value: g.toLowerCase() })))),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });
      if (!isStaff(interaction.member)) {
        return interaction.editReply('❌ Staff only.');
      }

      const gamemode = interaction.options.getString('gamemode');
      const queue = ACTIVE_QUEUES[gamemode];

      if (!queue) {
        return interaction.editReply(`❌ **${getGamemodeDisplay(gamemode)}** queue is not open.`);
      }

      const count = queue.players.length;
      queue.players = [];
      await updateQueueMessage(gamemode, interaction.client);

      const channelId = QUEUE_CHANNELS[gamemode];
      if (channelId) {
        const ch = interaction.guild.channels.cache.get(channelId);
        if (ch && ch.isTextBased()) {
          await ch.send(`📋 Queue cleared by <@${interaction.user.id}>. **${count}** player(s) were removed.`).catch(() => {});
        }
      }

      await interaction.editReply(`✅ Cleared **${count}** player(s) from the **${getGamemodeDisplay(gamemode)}** queue.`);
    },
  },

  // /pingpanel
  pingpanel: {
    data: new SlashCommandBuilder()
      .setName('pingpanel')
      .setDescription('Post a panel for users to subscribe to queue ping roles'),

    async execute(interaction) {
      await interaction.deferReply();

      const options = GAMEMODES
        .filter(gm => QUEUE_PING_ROLES[gm.toLowerCase()])
        .map(gm =>
          new StringSelectMenuOptionBuilder()
            .setLabel(gm)
            .setValue(gm.toLowerCase())
            .setDescription(`Pings for ${gm} queue`)
        );

      const select = new StringSelectMenuBuilder()
        .setCustomId('ping_select')
        .setPlaceholder('Select queues to receive pings for...')
        .setMinValues(0)
        .setMaxValues(options.length)
        .addOptions(options);

      const clearBtn = new ButtonBuilder()
        .setCustomId('ping_clear_all')
        .setLabel('❌ Clear All Pings')
        .setStyle(ButtonStyle.Danger);

      const row1 = new ActionRowBuilder().addComponents(select);
      const row2 = new ActionRowBuilder().addComponents(clearBtn);

      const embed = new EmbedBuilder()
        .setTitle('🔔 Queue Ping Settings')
        .setDescription('Select the queues you want to be notified for:')
        .setColor(0x3498db);

      await interaction.editReply({ embeds: [embed], components: [row1, row2] });
    },
  },
};

// --- Button & Select Handlers ---

async function handleQueueOpen(interaction) {
  if (!interaction.customId.startsWith('queue_open:')) return false;
  const gamemode = interaction.customId.split(':')[1];

  await interaction.deferReply({ ephemeral: true });

  if (!interaction.guild || !interaction.member) {
    await interaction.editReply('❌ Server only.');
    return true;
  }

  if (!isGamemodeTester(interaction.member, gamemode)) {
    await interaction.editReply('❌ Only testers for this gamemode can open a queue.');
    return true;
  }

  if (ACTIVE_QUEUES[gamemode]) {
    await interaction.editReply(`❌ **${getGamemodeDisplay(gamemode)}** queue is already open!`);
    return true;
  }

  const linked = db.getLinkedAccount(interaction.user.id);
  const mcName = linked ? linked.minecraft_name : 'TESTER';

  ACTIVE_QUEUES[gamemode] = {
    openedBy: interaction.user.id,
    openedAt: Date.now(),
    players: [],
    testers: [{ discordId: interaction.user.id, minecraftName: mcName }],
    calledPlayers: [],
  };

  const channelId = QUEUE_CHANNELS[gamemode];
  if (!channelId) {
    await interaction.editReply(`❌ No channel configured for **${getGamemodeDisplay(gamemode)}**.`);
    delete ACTIVE_QUEUES[gamemode];
    return true;
  }

  const channel = interaction.guild.channels.cache.get(channelId);
  if (!channel || !channel.isTextBased()) {
    await interaction.editReply(`❌ Queue channel not found (${channelId}).`);
    delete ACTIVE_QUEUES[gamemode];
    return true;
  }

  const pingRoleId = QUEUE_PING_ROLES[gamemode];
  const pingText = pingRoleId ? `<@&${pingRoleId}> ` : '';

  const embed = buildQueueEmbed(gamemode, ACTIVE_QUEUES[gamemode], interaction.guild);
  embed.setDescription('The queue is open! Click the buttons below to join.');

  const msg = await channel.send({
    content: pingText || undefined,
    embeds: [embed],
    components: [buildQueueActionRow(gamemode)],
  });

  QUEUE_MESSAGE_IDS[msg.id] = gamemode;
  storage.persistQueueMessageIds(QUEUE_MESSAGE_IDS);

  await interaction.editReply(`✅ **${getGamemodeDisplay(gamemode)}** queue opened!`);
  return true;
}

async function handleQueueJoin(interaction) {
  if (!interaction.customId.startsWith('queue_join:')) return false;
  const gamemode = interaction.customId.split(':')[1];

  const queue = ACTIVE_QUEUES[gamemode];
  if (!queue) {
    await interaction.reply({ content: '❌ This queue is not open.', ephemeral: true });
    return true;
  }

  const member = interaction.member;
  if (!member) {
    await interaction.reply({ content: '❌ Not in a server.', ephemeral: true });
    return true;
  }

  // Already in queue?
  if (queue.players.some(p => p.discordId === interaction.user.id) ||
      queue.testers.some(t => t.discordId === interaction.user.id)) {
    await interaction.reply({ content: '❌ You\'re already in the queue!', ephemeral: true });
    return true;
  }

  // Linked account?
  const linked = db.getLinkedAccount(interaction.user.id);
  if (!linked) {
    await interaction.reply({ content: '❌ Link your Minecraft account first with `/link`.', ephemeral: true });
    return true;
  }

  // Tester path
  if (isGamemodeTester(member, gamemode)) {
    queue.testers.push({ discordId: interaction.user.id, minecraftName: linked.minecraft_name });
    await updateQueueMessage(gamemode, interaction.client);
    await interaction.reply({ content: `✅ You joined the **${getGamemodeDisplay(gamemode)}** queue as a tester!`, ephemeral: true });
    return true;
  }

  // Player path: cooldown check
  const cdLeft = storage.cooldownLeft(interaction.user.id, gamemode);
  if (cdLeft > 0) {
    const d = Math.floor(cdLeft / 86400);
    const h = Math.floor((cdLeft % 86400) / 3600);
    await interaction.reply({
      content: `❌ You have **${d}d ${h}h** cooldown remaining for **${getGamemodeDisplay(gamemode)}**.`,
      ephemeral: true,
    });
    return true;
  }

  // Rank check
  const test = db.getTestByUsernameAndMode(linked.minecraft_name, getGamemodeDisplay(gamemode));
  const rank = test ? test.rank : 'Unranked';
  if (!canJoinQueue(rank)) {
    await interaction.reply({
      content: `❌ Only players ranked LT5–HT4 can join the queue. Your rank: **${rank}**.`,
      ephemeral: true,
    });
    return true;
  }

  queue.players.push({ discordId: interaction.user.id, minecraftName: linked.minecraft_name });
  await updateQueueMessage(gamemode, interaction.client);
  await interaction.reply({ content: `✅ You joined the **${getGamemodeDisplay(gamemode)}** queue!`, ephemeral: true });
  return true;
}

async function handleQueueLeave(interaction) {
  if (!interaction.customId.startsWith('queue_leave:')) return false;
  const gamemode = interaction.customId.split(':')[1];

  const queue = ACTIVE_QUEUES[gamemode];
  if (!queue) {
    await interaction.reply({ content: '❌ This queue is not open.', ephemeral: true });
    return true;
  }

  const pi = queue.players.findIndex(p => p.discordId === interaction.user.id);
  if (pi >= 0) {
    queue.players.splice(pi, 1);
    await updateQueueMessage(gamemode, interaction.client);
    await interaction.reply({ content: `✅ You left the **${getGamemodeDisplay(gamemode)}** queue.`, ephemeral: true });
    return true;
  }

  const ti = queue.testers.findIndex(t => t.discordId === interaction.user.id);
  if (ti >= 0) {
    queue.testers.splice(ti, 1);
    await updateQueueMessage(gamemode, interaction.client);
    await interaction.reply({ content: `✅ You left the **${getGamemodeDisplay(gamemode)}** queue.`, ephemeral: true });
    return true;
  }

  await interaction.reply({ content: '❌ You are not in this queue.', ephemeral: true });
  return true;
}

async function handleQueueClose(interaction) {
  if (!interaction.customId.startsWith('queue_close:')) return false;
  const gamemode = interaction.customId.split(':')[1];

  const queue = ACTIVE_QUEUES[gamemode];
  if (!queue) {
    await interaction.reply({ content: '❌ This queue is already closed.', ephemeral: true });
    return true;
  }

  if (!isStaff(interaction.member) && queue.openedBy !== interaction.user.id) {
    await interaction.reply({ content: '❌ Only the queue opener or staff can close it.', ephemeral: true });
    return true;
  }

  // Confirm buttons
  const confirmBtn = new ButtonBuilder()
    .setCustomId(`queue_close_confirm:${gamemode}`)
    .setLabel('Yes, close it')
    .setStyle(ButtonStyle.Danger);
  const cancelBtn = new ButtonBuilder()
    .setCustomId('queue_close_cancel')
    .setLabel('Cancel')
    .setStyle(ButtonStyle.Secondary);

  const row = new ActionRowBuilder().addComponents(confirmBtn, cancelBtn);
  await interaction.reply({
    content: `Are you sure you want to close the **${getGamemodeDisplay(gamemode)}** queue?`,
    components: [row],
    ephemeral: true,
  });
  return true;
}

async function handleQueueCloseConfirm(interaction) {
  if (!interaction.customId.startsWith('queue_close_confirm:')) return false;
  const gamemode = interaction.customId.split(':')[1];

  const queue = ACTIVE_QUEUES[gamemode];
  if (queue) {
    if (!isStaff(interaction.member) && queue.openedBy !== interaction.user.id) {
      await interaction.reply({ content: '❌ Only the queue opener or staff can close it.', ephemeral: true });
      return true;
    }
    delete ACTIVE_QUEUES[gamemode];
  }

  await updateQueueMessage(gamemode, interaction.client);
  await interaction.update({ content: `✅ **${getGamemodeDisplay(gamemode)}** queue closed.`, components: [] });
  return true;
}

async function handleQueueCloseCancel(interaction) {
  if (interaction.customId !== 'queue_close_cancel') return false;
  await interaction.update({ content: '❌ Cancelled.', components: [] });
  return true;
}

async function handleQueueNext(interaction) {
  if (!interaction.customId.startsWith('queue_next:')) return false;
  const gamemode = interaction.customId.split(':')[1];

  const queue = ACTIVE_QUEUES[gamemode];
  if (!queue) {
    await interaction.reply({ content: '❌ This queue is not open.', ephemeral: true });
    return true;
  }

  if (!isStaff(interaction.member) && queue.openedBy !== interaction.user.id) {
    await interaction.reply({ content: '❌ Only the queue opener or staff can call next player.', ephemeral: true });
    return true;
  }

  if (!queue.players.length) {
    await interaction.reply({ content: '❌ No more players in queue.', ephemeral: true });
    return true;
  }

  const nextPlayer = queue.players.shift();
  queue.calledPlayers.push(nextPlayer.discordId);

  await updateQueueMessage(gamemode, interaction.client);

  // Create ticket channel
  const guild = interaction.guild;
  const categoryId = TICKET_CATEGORY_ID;
  const category = categoryId ? guild.channels.cache.get(categoryId) : null;

  if (!category || category.type !== ChannelType.GuildCategory) {
    await interaction.reply({ content: '❌ Ticket category not found.', ephemeral: true });
    return true;
  }

  const channelName = `${gamemode}-${nextPlayer.minecraftName}`.toLowerCase().replace(/[^a-z0-9-]/g, '-').slice(0, 50);

  const permOverwrites = [
    { id: guild.id, deny: [PermissionFlagsBits.ViewChannel] },
    {
      id: nextPlayer.discordId,
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
    const ticketChannel = await guild.channels.create({
      name: channelName,
      type: ChannelType.GuildText,
      parent: category.id,
      permissionOverwrites: permOverwrites,
      topic: `owner=${nextPlayer.discordId} | mode=${gamemode} | mc=${nextPlayer.minecraftName}`,
      reason: `Queue ticket for ${nextPlayer.minecraftName}`,
    });

    const embed = new EmbedBuilder()
      .setTitle('Test Request')
      .setDescription(
        `**Player:** ${nextPlayer.minecraftName}\n` +
        `**Gamemode:** ${getGamemodeDisplay(gamemode)}\n` +
        `**Discord:** <@${nextPlayer.discordId}>`
      )
      .setColor(0x5865F2)
      .setThumbnail(`https://minotar.net/helm/${nextPlayer.minecraftName}/128.png`);

    const { buildCloseTicketRow } = require('./tickets');
    await ticketChannel.send({
      embeds: [embed],
      components: [buildCloseTicketRow(nextPlayer.discordId, gamemode)],
    });

    await interaction.reply({
      content: `✅ Called **${nextPlayer.minecraftName}** → ${ticketChannel}`,
      ephemeral: true,
    });
  } catch (e) {
    console.error('[Queue] Error creating ticket channel:', e);
    await interaction.reply({ content: `❌ Error creating ticket channel: ${e.message}`, ephemeral: true });
  }

  return true;
}

async function handleRemovePlayerSelect(interaction) {
  if (!interaction.customId.startsWith('queue_remove_player:')) return false;
  const gamemode = interaction.customId.split(':')[1];

  const queue = ACTIVE_QUEUES[gamemode];
  if (!queue) {
    await interaction.update({ content: '❌ Queue no longer active.', components: [] });
    return true;
  }

  const targetId = interaction.values[0];
  const pi = queue.players.findIndex(p => p.discordId === targetId);
  if (pi < 0) {
    await interaction.update({ content: '❌ Player not found in queue (already removed?)', components: [] });
    return true;
  }

  const removed = queue.players.splice(pi, 1)[0];
  await updateQueueMessage(gamemode, interaction.client);

  const member = interaction.guild.members.cache.get(targetId);
  const name = member ? member.displayName : removed.minecraftName;
  await interaction.update({
    content: `✅ Removed **${name}** from the **${getGamemodeDisplay(gamemode)}** queue.`,
    components: [],
  });
  return true;
}

async function handlePingSelect(interaction) {
  if (interaction.customId !== 'ping_select') return false;

  const selected = new Set(interaction.values);
  const member = interaction.member;
  const guild = interaction.guild;
  const added = [], removed = [], errors = [];

  for (const [gm, roleId] of Object.entries(QUEUE_PING_ROLES)) {
    if (!roleId) continue;
    const role = guild.roles.cache.get(roleId);
    if (!role) continue;
    const hasRole = member.roles.cache.has(roleId);
    const shouldHave = selected.has(gm);

    if (shouldHave && !hasRole) {
      try { await member.roles.add(role, 'Ping preference'); added.push(role.name); }
      catch (e) { errors.push(`Failed to add ${role.name}: ${e.message}`); }
    } else if (!shouldHave && hasRole) {
      try { await member.roles.remove(role, 'Ping preference'); removed.push(role.name); }
      catch (e) { errors.push(`Failed to remove ${role.name}: ${e.message}`); }
    }
  }

  const parts = [];
  if (added.length) parts.push(`✅ Added: ${added.join(', ')}`);
  if (removed.length) parts.push(`❌ Removed: ${removed.join(', ')}`);
  if (!added.length && !removed.length) parts.push('No changes.');
  if (errors.length) parts.push('Errors:\n' + errors.join('\n'));

  await interaction.reply({ content: parts.join('\n'), ephemeral: true });
  return true;
}

async function handlePingClearAll(interaction) {
  if (interaction.customId !== 'ping_clear_all') return false;

  const member = interaction.member;
  const guild = interaction.guild;
  const removed = [], errors = [];

  for (const [, roleId] of Object.entries(QUEUE_PING_ROLES)) {
    if (!roleId) continue;
    const role = guild.roles.cache.get(roleId);
    if (!role) continue;
    if (member.roles.cache.has(roleId)) {
      try { await member.roles.remove(role, 'Clear all pings'); removed.push(role.name); }
      catch (e) { errors.push(e.message); }
    }
  }

  const parts = removed.length ? [`❌ Removed: ${removed.join(', ')}`] : ['No ping roles to remove.'];
  if (errors.length) parts.push('Errors:\n' + errors.join('\n'));

  await interaction.reply({ content: parts.join('\n'), ephemeral: true });
  return true;
}

module.exports = {
  commands,
  ACTIVE_QUEUES,
  QUEUE_MESSAGE_IDS,
  loadState,
  updateQueueMessage,
  handleQueueOpen,
  handleQueueJoin,
  handleQueueLeave,
  handleQueueClose,
  handleQueueCloseConfirm,
  handleQueueCloseCancel,
  handleQueueNext,
  handleRemovePlayerSelect,
  handlePingSelect,
  handlePingClearAll,
};
