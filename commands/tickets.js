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
  RANKS,
  POINTS,
  TICKET_CATEGORY_ID,
  STAFF_ROLE_ID,
  TIER_RESULTS_CHANNEL_ID,
  getGamemodeDisplay,
  getTicketRoundsDisplay,
} = require('../config');
const db = require('../database');
const storage = require('../storage');
const { isStaff, canOpenTicket } = require('../permissions');

// Build the close ticket row (used by queue.js too)
function buildCloseTicketRow(ownerId, modeKey) {
  return new ActionRowBuilder().addComponents(
    new ButtonBuilder()
      .setCustomId(`ticket_close:${ownerId}:${modeKey}`)
      .setLabel('Close Ticket')
      .setStyle(ButtonStyle.Danger),
    new ButtonBuilder()
      .setCustomId(`ticket_give_tier:${ownerId}:${modeKey}`)
      .setLabel('Give Tier')
      .setStyle(ButtonStyle.Success),
  );
}

const commands = {
  // /ticketpanel
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

      // Max 5 buttons per row, 5 rows = 25 buttons
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

  // Must have linked account
  const linked = db.getLinkedAccount(interaction.user.id);
  if (!linked) {
    await interaction.reply({
      content: '❌ **Your Minecraft account is not linked!**\nUse `/link` to link your account first.',
      ephemeral: true,
    });
    return true;
  }

  // Ban check
  if (storage.isPlayerBanned(linked.minecraft_name)) {
    const info = storage.getBanInfo(linked.minecraft_name);
    await interaction.reply({
      content: `❌ You are banned from testing!\n${info && info.reason ? `**Reason:** ${info.reason}` : ''}`,
      ephemeral: true,
    });
    return true;
  }

  // Rank check
  const test = db.getTestByUsernameAndMode(linked.minecraft_name, getGamemodeDisplay(gamemode));
  const rank = test ? test.rank : 'Unranked';
  if (!canOpenTicket(rank)) {
    await interaction.reply({
      content: `❌ Opening a **${getGamemodeDisplay(gamemode)}** ticket requires at least **LT3** rank. Your rank: **${rank}**.`,
      ephemeral: true,
    });
    return true;
  }

  // Cooldown check
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

  // Check for existing open ticket
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

  // Create ticket channel
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

  // Parse from channel topic if available
  let actualOwnerId = ownerId;
  let actualMode = modeKey;
  if (channel.topic) {
    const ownerMatch = channel.topic.match(/owner=(\d+)/);
    const modeMatch = channel.topic.match(/mode=([^\s|]+)/);
    if (ownerMatch) actualOwnerId = ownerMatch[1];
    if (modeMatch) actualMode = modeMatch[1];
  }

  storage.setLastClosed(actualOwnerId, actualMode, Math.floor(Date.now() / 1000));
  storage.setOpenTicketChannelId(actualOwnerId, actualMode, null);

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

  // Build gamemode select (if mode unknown) + tier select
  const gamemodeOptions = GAMEMODES.map(gm =>
    new StringSelectMenuOptionBuilder()
      .setLabel(gm)
      .setValue(gm.toLowerCase())
      .setDefault(gm.toLowerCase() === actualMode)
  );

  const tierOptions = RANKS.filter(r => r !== 'Unranked').map(r =>
    new StringSelectMenuOptionBuilder().setLabel(r).setValue(r)
  );

  const gmSelect = new StringSelectMenuBuilder()
    .setCustomId(`give_tier_gm:${actualOwnerId}:${linked.minecraft_name}:${actualMode}`)
    .setPlaceholder('Gamemode...')
    .addOptions(gamemodeOptions);

  const tierSelect = new StringSelectMenuBuilder()
    .setCustomId(`give_tier_rank:${actualOwnerId}:${linked.minecraft_name}:${actualMode}`)
    .setPlaceholder('Achieved Rank...')
    .addOptions(tierOptions);

  const row1 = new ActionRowBuilder().addComponents(gmSelect);
  const row2 = new ActionRowBuilder().addComponents(tierSelect);

  await interaction.reply({ content: 'Select the gamemode and tier:', components: [row1, row2], ephemeral: true });
  return true;
}

async function handleGiveTierGmSelect(interaction) {
  if (!interaction.customId.startsWith('give_tier_gm:')) return false;
  // Just defer — user will pick rank next
  await interaction.deferUpdate();
  return true;
}

async function handleGiveTierRankSelect(interaction) {
  if (!interaction.customId.startsWith('give_tier_rank:')) return false;
  const parts = interaction.customId.split(':');
  const ownerId = parts[1];
  const minecraftName = parts[2];
  let modeKey = parts[3];

  // Try to get selected gamemode from the message's other select if present
  const selectedRank = interaction.values[0];

  // Check if there's a gamemode select in the message
  const msg = interaction.message;
  let selectedMode = modeKey;
  if (msg && msg.components) {
    for (const row of msg.components) {
      for (const comp of row.components) {
        if (comp.customId && comp.customId.startsWith('give_tier_gm:') && comp.type === 3) {
          // StringSelect, check values
          if (comp.values && comp.values.length > 0) {
            selectedMode = comp.values[0];
          }
        }
      }
    }
  }

  await interaction.deferUpdate();

  const tester = interaction.member;
  const modeDisplay = getGamemodeDisplay(selectedMode);

  // Prev rank
  const prev = db.getTestByUsernameAndMode(minecraftName, modeDisplay);
  const prevRank = prev ? prev.rank : 'Unranked';
  const prevPoints = POINTS[prevRank] || 0;
  const newPoints = POINTS[selectedRank] || 0;
  const diff = newPoints - prevPoints;

  // Upsert
  db.upsertTest({
    username: minecraftName,
    mode: modeDisplay,
    rank: selectedRank,
    testerId: String(tester.id),
    testerName: tester.displayName || tester.user.username,
    ts: Math.floor(Date.now() / 1000),
  });

  // Set cooldown
  storage.setLastClosed(ownerId, selectedMode, Math.floor(Date.now() / 1000));
  storage.setOpenTicketChannelId(ownerId, selectedMode, null);

  // Build embed
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

  // Send to results channel
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

  await interaction.followUp({
    content: `✅ Tier set: **${selectedRank}** for **${minecraftName}** in **${modeDisplay}**. Channel closes in 5 seconds.`,
    ephemeral: true,
  });

  // Close channel after 5 seconds
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
  handleGiveTier,
  handleGiveTierGmSelect,
  handleGiveTierRankSelect,
};
