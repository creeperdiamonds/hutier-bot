'use strict';
const {
  SlashCommandBuilder, EmbedBuilder, ButtonBuilder, ButtonStyle,
  ActionRowBuilder, StringSelectMenuBuilder, StringSelectMenuOptionBuilder,
  ChannelType, PermissionFlagsBits,
} = require('discord.js');
const {
  GAMEMODES, QUEUE_CHANNELS, QUEUE_PING_ROLES, TICKET_CATEGORY_ID, STAFF_ROLE_ID,
  getGamemodeDisplay, getGamemodeColor, COOLDOWN_SECONDS,
} = require('../config');
const db = require('../database');
const storage = require('../storage');
const { isStaff, isGamemodeTester, canJoinQueue } = require('../permissions');

function resolveQueueChannel(gamemode) {
  return storage.getQueueChannels()[gamemode] || QUEUE_CHANNELS[gamemode] || null;
}

const REGIONS = ['NA', 'EU', 'AS', 'SA', 'AU'];

function buildOpenEmbed(gamemode, guild) {
  const display = getGamemodeDisplay(gamemode);
  const color = getGamemodeColor(gamemode);
  const region = storage.getQueueRegion(gamemode) || '??';

  const players = storage.getQueue(gamemode);
  const testers = storage.getActiveTesters(gamemode);

  const playerLines = players.map((uid, i) => {
    const linked = db.getLinkedAccount(uid);
    const member = guild ? guild.members.cache.get(uid) : null;
    const nick = member ? member.displayName : uid;
    const mc = linked ? linked.minecraft_name : '?';
    return `**${i + 1}.** ${nick} (${mc})`;
  });

  const testerLines = testers.map(uid => {
    const linked = db.getLinkedAccount(uid);
    const member = guild ? guild.members.cache.get(uid) : null;
    const nick = member ? member.displayName : uid;
    const mc = linked ? linked.minecraft_name : '?';
    return `${nick} (${mc})`;
  });

  return new EmbedBuilder()
    .setTitle(`🟢 ${display} Queue — ${region}`)
    .setColor(color)
    .addFields(
      {
        name: `Players (${players.length})`,
        value: playerLines.join('\n') || 'Nobody in queue yet.',
        inline: false,
      },
      {
        name: `Active Testers (${testers.length})`,
        value: testerLines.join('\n') || 'No testers.',
        inline: false,
      },
    )
    .setFooter({ text: 'Click Join Queue to enter • Leave Queue to exit' });
}

function buildClosedEmbed(gamemode) {
  const display = getGamemodeDisplay(gamemode);
  const lastSession = storage.getLastSession(gamemode);
  const descLines = [
    'No testers are currently available.',
    lastSession ? `Last session: ${lastSession}` : null,
    'Check back later.',
  ].filter(Boolean);

  return new EmbedBuilder()
    .setTitle(`🔴 ${display} Queue — Closed`)
    .setDescription(descLines.join('\n'))
    .setColor(0xe74c3c);
}

function buildPublicRow(gamemode) {
  return new ActionRowBuilder().addComponents(
    new ButtonBuilder()
      .setCustomId(`queue_join:${gamemode}`)
      .setLabel('Join Queue')
      .setStyle(ButtonStyle.Success),
    new ButtonBuilder()
      .setCustomId(`queue_leave:${gamemode}`)
      .setLabel('Leave Queue')
      .setStyle(ButtonStyle.Danger),
  );
}

async function refreshPublicEmbed(gamemode, client, guild) {
  const openMsg = storage.getQueueOpenMessage(gamemode);
  if (!openMsg) return;

  const { channel_id, message_id } = openMsg;
  const channel = client.channels.cache.get(channel_id);
  if (!channel || !channel.isTextBased()) return;

  try {
    const msg = await channel.messages.fetch(message_id);
    await msg.edit({
      embeds: [buildOpenEmbed(gamemode, guild || channel.guild)],
      components: [buildPublicRow(gamemode)],
    });
  } catch (e) {
    console.error(`[Queue] refreshPublicEmbed error [${gamemode}]:`, e.message);
  }
}

async function getQueueRole(guild, gamemode) {
  const display = getGamemodeDisplay(gamemode);
  const roleName = `${display} Queue`;
  await guild.roles.fetch().catch(() => {});
  return guild.roles.cache.find(r => r.name === roleName) || null;
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
        const channelId = resolveQueueChannel(modeKey);
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
      .setDescription('Remove yourself from active testers, or force-close a queue (staff only)')
      .addStringOption(o => o.setName('gamemode').setDescription('Gamemode').setRequired(true)
        .addChoices(...GAMEMODES.map(g => ({ name: g, value: g.toLowerCase() })))),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });

      const gamemode = interaction.options.getString('gamemode');
      const testers = storage.getActiveTesters(gamemode);

      if (!testers.length) {
        return interaction.editReply(`❌ The **${getGamemodeDisplay(gamemode)}** queue is not open.`);
      }

      const isTester = testers.includes(String(interaction.user.id));
      if (!isTester && !isStaff(interaction.member)) {
        return interaction.editReply('❌ Only active testers or staff can close the queue.');
      }

      // Remove the calling tester (or if staff, just remove them anyway)
      storage.removeActiveTester(gamemode, interaction.user.id);
      const remaining = storage.getActiveTesters(gamemode);

      if (remaining.length > 0) {
        // Others still active — just refresh embed
        await refreshPublicEmbed(gamemode, interaction.client, interaction.guild);
        return interaction.editReply(
          `✅ You left the **${getGamemodeDisplay(gamemode)}** queue. **${remaining.length}** tester(s) still active.`
        );
      }

      // Last tester — close the queue
      storage.saveLastSession(gamemode);
      storage.clearQueueRegion(gamemode);
      storage.clearActiveTesters(gamemode);

      // Edit open message in place to closed embed, save as closed message
      const openMsg = storage.getQueueOpenMessage(gamemode);
      if (openMsg) {
        const { channel_id, message_id } = openMsg;
        const ch = interaction.guild.channels.cache.get(channel_id);
        if (ch && ch.isTextBased()) {
          try {
            const msg = await ch.messages.fetch(message_id);
            await msg.edit({ embeds: [buildClosedEmbed(gamemode)], components: [] });
            storage.saveClosedMessage(gamemode, channel_id, message_id);
          } catch (e) {
            console.error(`[Queue] closequeue edit error [${gamemode}]:`, e.message);
          }
        }
        storage.clearQueueOpenMessage(gamemode);
      }

      // Clear the waitlist and strip queue roles from all players
      const players = storage.getQueue(gamemode);
      const queueRole = await getQueueRole(interaction.guild, gamemode);
      for (const uid of players) {
        if (queueRole) {
          const member = interaction.guild.members.cache.get(uid);
          if (member) await member.roles.remove(queueRole, 'Queue closed').catch(() => {});
        }
      }
      storage.clearQueue(gamemode);

      await interaction.editReply(`✅ **${getGamemodeDisplay(gamemode)}** queue closed.`);
    },
  },

  // /callnext
  callnext: {
    data: new SlashCommandBuilder()
      .setName('callnext')
      .setDescription('Call the next player from the queue and create a ticket channel')
      .addStringOption(o => o.setName('gamemode').setDescription('Gamemode').setRequired(true)
        .addChoices(...GAMEMODES.map(g => ({ name: g, value: g.toLowerCase() })))),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });

      const gamemode = interaction.options.getString('gamemode');

      // Must be an active tester or staff
      const testers = storage.getActiveTesters(gamemode);
      if (!testers.length) {
        return interaction.editReply(`❌ The **${getGamemodeDisplay(gamemode)}** queue is not open.`);
      }
      if (!isStaff(interaction.member) && !isGamemodeTester(interaction.member, gamemode)) {
        return interaction.editReply('❌ Only testers or staff can call next.');
      }

      // Block if active session already in progress
      const session = storage.getActiveSession(gamemode);
      if (session) {
        return interaction.editReply(`❌ A session is already in progress for **${getGamemodeDisplay(gamemode)}**. Close the current ticket first.`);
      }

      const players = storage.getQueue(gamemode);
      if (!players.length) {
        return interaction.editReply('❌ No players in the queue.');
      }

      const nextUserId = players[0];
      const linked = db.getLinkedAccount(nextUserId);
      const mcName = linked ? linked.minecraft_name : 'Unknown';

      // Remove from queue and strip queue role
      storage.removeFromQueue(gamemode, nextUserId);
      const queueRole = await getQueueRole(interaction.guild, gamemode);
      if (queueRole) {
        const playerMember = interaction.guild.members.cache.get(nextUserId);
        if (playerMember) await playerMember.roles.remove(queueRole, 'Called from queue').catch(() => {});
      }

      // Create ticket channel
      const guild = interaction.guild;
      const category = TICKET_CATEGORY_ID ? guild.channels.cache.get(TICKET_CATEGORY_ID) : null;
      if (!category || category.type !== ChannelType.GuildCategory) {
        await refreshPublicEmbed(gamemode, interaction.client, guild);
        return interaction.editReply('❌ Ticket category not found.');
      }

      const channelName = `${gamemode}-${mcName}`.toLowerCase().replace(/[^a-z0-9-]/g, '-').slice(0, 50);

      const permOverwrites = [
        { id: guild.id, deny: [PermissionFlagsBits.ViewChannel] },
        {
          id: nextUserId,
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
          topic: `owner=${nextUserId} | mode=${gamemode} | mc=${mcName}`,
          reason: `Queue ticket for ${mcName}`,
        });

        storage.setActiveSession(gamemode, interaction.user.id, nextUserId, ticketChannel.id);

        const embed = new EmbedBuilder()
          .setTitle('Test Request')
          .setDescription(
            `**Player:** ${mcName}\n` +
            `**Gamemode:** ${getGamemodeDisplay(gamemode)}\n` +
            `**Discord:** <@${nextUserId}>`
          )
          .setColor(0x5865F2)
          .setThumbnail(`https://minotar.net/helm/${mcName}/128.png`);

        const { buildCloseTicketRow } = require('./tickets');
        await ticketChannel.send({
          content: `<@${nextUserId}>`,
          embeds: [embed],
          components: [buildCloseTicketRow(nextUserId, gamemode)],
        });

        await refreshPublicEmbed(gamemode, interaction.client, guild);

        await interaction.editReply(`✅ Called **${mcName}** → ${ticketChannel}`);
      } catch (e) {
        console.error('[Queue] callnext error creating ticket:', e);
        await interaction.editReply(`❌ Error creating ticket channel: ${e.message}`);
      }
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
      for (const gm of GAMEMODES) {
        const modeKey = gm.toLowerCase();
        const players = storage.getQueue(modeKey);
        const testers = storage.getActiveTesters(modeKey);

        const pi = players.indexOf(String(interaction.user.id));
        const isTester = testers.includes(String(interaction.user.id));

        if (pi >= 0) {
          lines.push(`**${getGamemodeDisplay(modeKey)}**: Position #${pi + 1} of ${players.length}`);
        } else if (isTester) {
          lines.push(`**${getGamemodeDisplay(modeKey)}**: Active Tester`);
        }
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
      const testers = storage.getActiveTesters(gamemode);

      if (!testers.length) {
        return interaction.editReply(`❌ **${getGamemodeDisplay(gamemode)}** queue is not open.`);
      }

      if (!isStaff(interaction.member) && !isGamemodeTester(interaction.member, gamemode)) {
        return interaction.editReply('❌ Staff or testers only.');
      }

      const players = storage.getQueue(gamemode);
      if (!players.length) {
        return interaction.editReply('❌ No players in this queue.');
      }

      const options = await Promise.all(players.map(async (uid, i) => {
        const linked = db.getLinkedAccount(uid);
        const member = interaction.guild.members.cache.get(uid);
        const nick = member ? member.displayName : uid;
        const mc = linked ? linked.minecraft_name : '?';
        return new StringSelectMenuOptionBuilder()
          .setLabel(`#${i + 1} — ${nick} (${mc})`.slice(0, 100))
          .setValue(uid);
      }));

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
      const testers = storage.getActiveTesters(gamemode);

      if (!testers.length) {
        return interaction.editReply(`❌ **${getGamemodeDisplay(gamemode)}** queue is not open.`);
      }

      const players = storage.getQueue(gamemode);
      const count = players.length;

      // Strip queue role from all players
      const queueRole = await getQueueRole(interaction.guild, gamemode);
      for (const uid of players) {
        if (queueRole) {
          const member = interaction.guild.members.cache.get(uid);
          if (member) await member.roles.remove(queueRole, 'Queue cleared').catch(() => {});
        }
      }

      storage.clearQueue(gamemode);
      await refreshPublicEmbed(gamemode, interaction.client, interaction.guild);

      const channelId = resolveQueueChannel(gamemode);
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

  // Check not already active tester
  const testers = storage.getActiveTesters(gamemode);
  if (testers.includes(String(interaction.user.id))) {
    await interaction.editReply(`❌ You are already on duty for the **${getGamemodeDisplay(gamemode)}** queue.`);
    return true;
  }

  // Show region select
  const options = REGIONS.map(r =>
    new StringSelectMenuOptionBuilder().setLabel(r).setValue(r)
  );
  const select = new StringSelectMenuBuilder()
    .setCustomId(`queue_region:${gamemode}`)
    .setPlaceholder('Select a region...')
    .addOptions(options);

  const row = new ActionRowBuilder().addComponents(select);
  await interaction.editReply({ content: 'Select your region:', components: [row] });
  return true;
}

async function handleRegionSelect(interaction) {
  if (!interaction.customId.startsWith('queue_region:')) return false;
  const gamemode = interaction.customId.split(':')[1];

  const region = interaction.values[0];

  const existingTesters = storage.getActiveTesters(gamemode);
  const queueAlreadyOpen = existingTesters.length > 0;

  storage.addActiveTester(gamemode, interaction.user.id);
  storage.setQueueRegion(gamemode, region);

  if (queueAlreadyOpen) {
    // Already open — just join and refresh
    await refreshPublicEmbed(gamemode, interaction.client, interaction.guild);
    await interaction.update({
      content: `✅ You joined the existing **${getGamemodeDisplay(gamemode)}** queue (${region}).`,
      components: [],
    });
    return true;
  }

  // First tester — open the queue
  // Delete old closed message if one exists
  const closedMsg = storage.getClosedMessage(gamemode);
  if (closedMsg) {
    const { channel_id, message_id } = closedMsg;
    const ch = interaction.guild.channels.cache.get(channel_id);
    if (ch && ch.isTextBased()) {
      try {
        const msg = await ch.messages.fetch(message_id);
        await msg.delete();
      } catch {}
    }
    storage.clearClosedMessage(gamemode);
  }

  const channelId = resolveQueueChannel(gamemode);
  if (!channelId) {
    storage.removeActiveTester(gamemode, interaction.user.id);
    await interaction.update({
      content: `❌ No channel configured for **${getGamemodeDisplay(gamemode)}**.`,
      components: [],
    });
    return true;
  }

  const channel = interaction.guild.channels.cache.get(channelId);
  if (!channel || !channel.isTextBased()) {
    storage.removeActiveTester(gamemode, interaction.user.id);
    await interaction.update({
      content: `❌ Queue channel not found (${channelId}).`,
      components: [],
    });
    return true;
  }

  const pingRoleId = QUEUE_PING_ROLES[gamemode];
  const pingText = pingRoleId ? `<@&${pingRoleId}>` : undefined;

  const embed = buildOpenEmbed(gamemode, interaction.guild);
  const msg = await channel.send({
    content: pingText,
    embeds: [embed],
    components: [buildPublicRow(gamemode)],
  });

  storage.saveQueueOpenMessage(gamemode, channel.id, msg.id);

  await interaction.update({
    content: `✅ **${getGamemodeDisplay(gamemode)}** queue opened in ${channel} (Region: ${region})!`,
    components: [],
  });
  return true;
}

async function handleQueueJoin(interaction) {
  if (!interaction.customId.startsWith('queue_join:')) return false;
  const gamemode = interaction.customId.split(':')[1];

  const testers = storage.getActiveTesters(gamemode);
  if (!testers.length) {
    await interaction.reply({ content: '❌ This queue is not open.', ephemeral: true });
    return true;
  }

  const member = interaction.member;
  if (!member) {
    await interaction.reply({ content: '❌ Not in a server.', ephemeral: true });
    return true;
  }

  // Testers cannot join as players
  if (isGamemodeTester(member, gamemode)) {
    await interaction.reply({ content: '❌ You are a tester for this gamemode.', ephemeral: true });
    return true;
  }

  // Already in queue?
  const players = storage.getQueue(gamemode);
  if (players.includes(String(interaction.user.id))) {
    await interaction.reply({ content: '❌ You\'re already in the queue!', ephemeral: true });
    return true;
  }

  // Already an active tester?
  if (testers.includes(String(interaction.user.id))) {
    await interaction.reply({ content: '❌ You are already on duty as a tester.', ephemeral: true });
    return true;
  }

  // Linked account?
  const linked = db.getLinkedAccount(interaction.user.id);
  if (!linked) {
    await interaction.reply({ content: '❌ Link your Minecraft account first with `/link`.', ephemeral: true });
    return true;
  }

  // Cooldown check
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

  storage.addToQueue(gamemode, interaction.user.id);

  // Assign queue role
  const queueRole = await getQueueRole(interaction.guild, gamemode);
  if (queueRole) {
    await member.roles.add(queueRole, 'Joined queue').catch(() => {});
  }

  await refreshPublicEmbed(gamemode, interaction.client, interaction.guild);

  const updatedPlayers = storage.getQueue(gamemode);
  const pos = updatedPlayers.indexOf(String(interaction.user.id)) + 1;
  await interaction.reply({
    content: `✅ You joined the **${getGamemodeDisplay(gamemode)}** queue! You are **#${pos}** of **${updatedPlayers.length}**.`,
    ephemeral: true,
  });
  return true;
}

async function handleQueueLeave(interaction) {
  if (!interaction.customId.startsWith('queue_leave:')) return false;
  const gamemode = interaction.customId.split(':')[1];

  const players = storage.getQueue(gamemode);
  if (!players.includes(String(interaction.user.id))) {
    await interaction.reply({ content: '❌ You are not in this queue.', ephemeral: true });
    return true;
  }

  storage.removeFromQueue(gamemode, interaction.user.id);

  // Strip queue role
  const queueRole = await getQueueRole(interaction.guild, gamemode);
  if (queueRole && interaction.member) {
    await interaction.member.roles.remove(queueRole, 'Left queue').catch(() => {});
  }

  await refreshPublicEmbed(gamemode, interaction.client, interaction.guild);
  await interaction.reply({ content: `✅ You left the **${getGamemodeDisplay(gamemode)}** queue.`, ephemeral: true });
  return true;
}

async function handleRemovePlayerSelect(interaction) {
  if (!interaction.customId.startsWith('queue_remove_player:')) return false;
  const gamemode = interaction.customId.split(':')[1];

  const testers = storage.getActiveTesters(gamemode);
  if (!testers.length) {
    await interaction.update({ content: '❌ Queue no longer active.', components: [] });
    return true;
  }

  const targetId = interaction.values[0];
  const players = storage.getQueue(gamemode);
  if (!players.includes(targetId)) {
    await interaction.update({ content: '❌ Player not found in queue (already removed?)', components: [] });
    return true;
  }

  storage.removeFromQueue(gamemode, targetId);

  // Strip queue role
  const queueRole = await getQueueRole(interaction.guild, gamemode);
  if (queueRole) {
    const targetMember = interaction.guild.members.cache.get(targetId);
    if (targetMember) await targetMember.roles.remove(queueRole, 'Removed from queue').catch(() => {});
  }

  await refreshPublicEmbed(gamemode, interaction.client, interaction.guild);

  const targetMember = interaction.guild.members.cache.get(targetId);
  const name = targetMember ? targetMember.displayName : targetId;
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

async function handleMemberLeave(member) {
  const userId = String(member.id);

  for (const gm of GAMEMODES) {
    const modeKey = gm.toLowerCase();

    // Remove from player queue
    const players = storage.getQueue(modeKey);
    if (players.includes(userId)) {
      storage.removeFromQueue(modeKey, userId);
      console.log(`[Queue] Auto-removed ${member.displayName || userId} from ${modeKey} player queue (left server)`);
    }

    // Remove from active testers
    const testers = storage.getActiveTesters(modeKey);
    if (testers.includes(userId)) {
      storage.removeActiveTester(modeKey, userId);
      const remaining = storage.getActiveTesters(modeKey);
      console.log(`[Queue] Auto-removed ${member.displayName || userId} from ${modeKey} testers (left server)`);

      if (remaining.length === 0) {
        // Last tester left — close the queue state
        storage.saveLastSession(modeKey);
        storage.clearQueueRegion(modeKey);
        storage.clearQueue(modeKey);
        // We can't refresh the embed here without guild context, just log
        console.log(`[Queue] Last tester left for ${modeKey}, queue auto-closed.`);
      }
    }
  }
}

function loadState() {
  // No-op: state is now fully persisted in data.json via storage functions
}

module.exports = {
  commands,
  loadState,
  handleMemberLeave,
  handleQueueOpen,
  handleRegionSelect,
  handleQueueJoin,
  handleQueueLeave,
  handleRemovePlayerSelect,
  handlePingSelect,
  handlePingClearAll,
  refreshPublicEmbed,
};
