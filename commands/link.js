'use strict';
const { SlashCommandBuilder, EmbedBuilder } = require('discord.js');
const crypto = require('crypto');
const db = require('../database');
const { LINK_CODE_LENGTH, LINK_CODE_EXPIRY_MINUTES } = require('../config');

function generateCode(len) {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'; // no O/0 I/1 confusion
  let code = '';
  const bytes = crypto.randomBytes(len);
  for (let i = 0; i < len; i++) {
    code += chars[bytes[i] % chars.length];
  }
  return code;
}

const commands = {
  // /link
  link: {
    data: new SlashCommandBuilder()
      .setName('link')
      .setDescription('Link your Minecraft account to Discord')
      .addStringOption(o => o.setName('code').setDescription('Link code (leave empty to generate)').setRequired(false)),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });
      const code = interaction.options.getString('code');

      // If code provided, verify it
      if (code) {
        const discordIdFromCode = db.verifyCode(code);
        if (!discordIdFromCode) {
          return interaction.editReply('❌ Invalid or expired code. Use `/link` with no code to generate a new one.');
        }
        if (String(discordIdFromCode) !== String(interaction.user.id)) {
          return interaction.editReply('❌ This code does not belong to your account.');
        }
        const linked = db.getLinkedAccount(interaction.user.id);
        if (linked) {
          const embed = new EmbedBuilder()
            .setTitle('✅ Account Linked!')
            .setDescription(
              `**Minecraft:** \`${linked.minecraft_name}\`\n` +
              `**Discord:** ${interaction.user}\n\n` +
              `Your accounts are now linked!`
            )
            .setColor(0x2ecc71);
          return interaction.editReply({ embeds: [embed] });
        }
        return interaction.editReply('❌ Code verified but account not yet linked. The Minecraft server may not have confirmed linking yet.');
      }

      // No code: check if already linked
      const existing = db.getLinkedAccount(interaction.user.id);
      if (existing) {
        const embed = new EmbedBuilder()
          .setTitle('⚠️ Already Linked!')
          .setDescription(
            `**Minecraft:** \`${existing.minecraft_name}\`\n` +
            `**Discord:** ${interaction.user}\n\n` +
            `Your accounts are already linked!`
          )
          .setColor(0xe67e22);
        return interaction.editReply({ embeds: [embed] });
      }

      // Check for existing pending code
      const existingCode = db.getPendingCodeByDiscord(interaction.user.id);
      if (existingCode) {
        const embed = new EmbedBuilder()
          .setTitle('⏳ Existing Code')
          .setDescription(
            `You already have a pending code: \`${existingCode.code}\`\n\n` +
            `Use \`/link ${existingCode.code}\` in Minecraft to link your account.\n` +
            `Or wait for it to expire and generate a new one.`
          )
          .setColor(0xe67e22);
        return interaction.editReply({ embeds: [embed] });
      }

      // Generate new code
      const newCode = generateCode(LINK_CODE_LENGTH);
      const expiresAt = new Date(Date.now() + LINK_CODE_EXPIRY_MINUTES * 60 * 1000).toISOString();
      db.createPendingCode(interaction.user.id, newCode, expiresAt);

      // Try DM
      let dmSent = false;
      try {
        await interaction.user.send(
          `🎮 **Link Code:** \`${newCode}\`\n\n` +
          `Type in Minecraft: \`/link ${newCode}\`\n` +
          `Valid for **${LINK_CODE_EXPIRY_MINUTES} minutes**.`
        );
        dmSent = true;
      } catch {}

      const embed = new EmbedBuilder()
        .setTitle('✅ Code Generated!')
        .setDescription(
          `\`\`\`\n${newCode}\n\`\`\`\n` +
          `Type in Minecraft: \`/link ${newCode}\`\n` +
          `Valid for **${LINK_CODE_EXPIRY_MINUTES} minutes**.`
        )
        .setColor(0x2ecc71);

      if (dmSent) {
        embed.addFields({ name: '📬 DM Sent!', value: 'The code was also sent to your DMs!', inline: false });
      } else {
        embed.addFields({ name: '⚠️ DM Failed', value: 'Copy the code shown above!', inline: false });
      }

      await interaction.editReply({ embeds: [embed] });
    },
  },

  // /unlink
  unlink: {
    data: new SlashCommandBuilder()
      .setName('unlink')
      .setDescription('Unlink your Minecraft account'),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });

      const existing = db.getLinkedAccount(interaction.user.id);
      if (!existing) {
        return interaction.editReply('❌ No Minecraft account linked. Use `/link` to link one.');
      }

      db.unlinkAccount(interaction.user.id);

      const embed = new EmbedBuilder()
        .setTitle('✅ Unlinked!')
        .setDescription(`Your Minecraft account (**${existing.minecraft_name}**) has been unlinked.`)
        .setColor(0x2ecc71);

      await interaction.editReply({ embeds: [embed] });
    },
  },

  // /mylink
  mylink: {
    data: new SlashCommandBuilder()
      .setName('mylink')
      .setDescription('Show your linked Minecraft account'),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });

      const linked = db.getLinkedAccount(interaction.user.id);
      if (!linked) {
        return interaction.editReply('❌ No Minecraft account linked. Use `/link` to link one.');
      }

      const embed = new EmbedBuilder()
        .setDescription(
          `**Discord:** ${interaction.user}\n` +
          `**Minecraft:** ${linked.minecraft_name}`
        )
        .setColor(0x5865F2);

      await interaction.editReply({ embeds: [embed] });
    },
  },
};

module.exports = { commands };
