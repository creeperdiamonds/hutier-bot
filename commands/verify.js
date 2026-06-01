'use strict';
const {
  SlashCommandBuilder,
  EmbedBuilder,
  ButtonBuilder,
  ButtonStyle,
  ActionRowBuilder,
  StringSelectMenuBuilder,
  StringSelectMenuOptionBuilder,
  ModalBuilder,
  TextInputBuilder,
  TextInputStyle,
} = require('discord.js');
const db = require('../database');
const { isAdmin } = require('../permissions');

const commands = {
  setupverify: {
    data: new SlashCommandBuilder()
      .setName('setupverify')
      .setDescription('Post the account verification panel in this channel (admin only)'),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });
      if (!isAdmin(interaction.member)) {
        return interaction.editReply('❌ Admin only.');
      }

      const existing = db.getLinkedAccount(interaction.user.id);

      const embed = new EmbedBuilder()
        .setTitle('Account Verification')
        .setDescription(
          '**Link your Minecraft account to gain access to testing queues and tickets.**\n\n' +
          '**Step 1** — Click **Verify Account** below\n' +
          '**Step 2** — Enter your Minecraft IGN\n' +
          '**Step 3** — Select your account type (Premium or Cracked)\n\n' +
          '> If you already verified, clicking the button lets you **update** your IGN or account type.'
        )
        .setColor(0x5865F2)
        .setFooter({ text: 'SM Tierlist | Verification' });

      const btn = new ButtonBuilder()
        .setCustomId('verify_btn')
        .setLabel('Verify Account')
        .setStyle(ButtonStyle.Success);

      const row = new ActionRowBuilder().addComponents(btn);
      await interaction.channel.send({ embeds: [embed], components: [row] });
      await interaction.editReply('✅ Verification panel posted.');
    },
  },

  // Staff command to check a user's verification status
  verifycheck: {
    data: new SlashCommandBuilder()
      .setName('verifycheck')
      .setDescription('Check a player\'s linked account and type (staff only)')
      .addUserOption(o => o.setName('user').setDescription('Discord user').setRequired(true)),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });

      const { isStaff } = require('../permissions');
      if (!isStaff(interaction.member)) {
        return interaction.editReply('❌ Staff only.');
      }

      const target = interaction.options.getUser('user');
      const linked = db.getLinkedAccount(target.id);

      if (!linked) {
        return interaction.editReply(`❌ <@${target.id}> has not verified their account.`);
      }

      const embed = new EmbedBuilder()
        .setTitle('Account Info')
        .setColor(0x5865F2)
        .setThumbnail(`https://minotar.net/helm/${linked.minecraft_name}/128.png`)
        .addFields(
          { name: 'Discord', value: `<@${target.id}>`, inline: true },
          { name: 'Minecraft IGN', value: `\`${linked.minecraft_name}\``, inline: true },
          { name: 'Account Type', value: linked.account_type || 'Unknown', inline: true },
          { name: 'Linked At', value: linked.linked_at || 'Unknown', inline: false },
        );

      await interaction.editReply({ embeds: [embed] });
    },
  },
};

// --- Interaction Handlers ---

// Step 1: Player clicks "Verify Account" → show modal for IGN
async function handleVerifyButton(interaction) {
  if (interaction.customId !== 'verify_btn') return false;

  const existing = db.getLinkedAccount(interaction.user.id);

  const modal = new ModalBuilder()
    .setCustomId('verify_modal')
    .setTitle('Account Verification');

  const ignInput = new TextInputBuilder()
    .setCustomId('verify_ign')
    .setLabel('Minecraft IGN')
    .setStyle(TextInputStyle.Short)
    .setPlaceholder('e.g. Notch')
    .setRequired(true)
    .setMinLength(3)
    .setMaxLength(16);

  if (existing) ignInput.setValue(existing.minecraft_name);

  modal.addComponents(new ActionRowBuilder().addComponents(ignInput));
  await interaction.showModal(modal);
  return true;
}

// Step 2: Modal submitted → store IGN, show account type select
async function handleVerifyModal(interaction) {
  if (interaction.customId !== 'verify_modal') return false;

  const ign = interaction.fields.getTextInputValue('verify_ign').trim();

  // Basic IGN validation (alphanumeric + underscore, 3-16 chars)
  if (!/^[a-zA-Z0-9_]{3,16}$/.test(ign)) {
    await interaction.reply({
      content: '❌ Invalid Minecraft IGN. Must be 3–16 characters, letters/numbers/underscores only.',
      ephemeral: true,
    });
    return true;
  }

  // Check if that IGN is already linked to a DIFFERENT Discord account
  const otherAccount = db.getLinkedAccountByMinecraft(ign);
  if (otherAccount && otherAccount.discord_id !== interaction.user.id) {
    await interaction.reply({
      content: `❌ **\`${ign}\`** is already linked to another Discord account.`,
      ephemeral: true,
    });
    return true;
  }

  const select = new StringSelectMenuBuilder()
    .setCustomId(`verify_type:${ign}`)
    .setPlaceholder('Select your account type...')
    .addOptions(
      new StringSelectMenuOptionBuilder()
        .setLabel('Premium')
        .setValue('Premium')
        .setDescription('Paid Minecraft account (Mojang/Microsoft)'),
      new StringSelectMenuOptionBuilder()
        .setLabel('Cracked')
        .setValue('Cracked')
        .setDescription('Offline / cracked Minecraft account'),
    );

  const row = new ActionRowBuilder().addComponents(select);

  await interaction.reply({
    content: `IGN: **\`${ign}\`**\nNow select your account type:`,
    components: [row],
    ephemeral: true,
  });
  return true;
}

// Step 3: Account type selected → save to DB, assign Verified role
async function handleVerifyTypeSelect(interaction) {
  if (!interaction.customId.startsWith('verify_type:')) return false;

  const ign = interaction.customId.split(':').slice(1).join(':');
  const accountType = interaction.values[0];

  db.linkAccount(interaction.user.id, ign, accountType);

  const embed = new EmbedBuilder()
    .setTitle('✅ Verification Complete')
    .setColor(0x2ecc71)
    .setThumbnail(`https://minotar.net/helm/${ign}/128.png`)
    .addFields(
      { name: 'Minecraft IGN', value: `\`${ign}\``, inline: true },
      { name: 'Account Type', value: accountType, inline: true },
    )
    .setFooter({ text: 'You can re-verify anytime to update your info.' });

  await interaction.update({
    embeds: [embed],
    components: [],
  });
  return true;
}

module.exports = {
  commands,
  handleVerifyButton,
  handleVerifyModal,
  handleVerifyTypeSelect,
};
