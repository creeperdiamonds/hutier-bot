'use strict';
const {
  SlashCommandBuilder,
  EmbedBuilder,
  ButtonBuilder,
  ButtonStyle,
  ActionRowBuilder,
} = require('discord.js');
const { isAdmin } = require('../permissions');

const RUBRICS = {
  sword: {
    name: 'Sword',
    kitImage: 'https://i.imgur.com/wTxkbpa.png',
    text: `**__Sword__**

**HT1 Testing**
Phase 1: Beat two LT2 opponents with the score 20-5 or better
Phase 2: Beat two HT2 opponents with the score 20-10 or better
Phase 3: Beat two/one opponents in the same Tier as you with the score of 20-15 or better
Phase 4: Beat the HT1 Player. If successful, you will steal their title.

**LT1 Testing**
Phase 1: Beat two opponents in the lower tier below you with at least a 20-10 overall score.
Phase 2: Beat two opponents in the same Tier as you with at least a 20-14 overall score.
Phase 3: Achieve an equal or better total score against 1-2 players in the Tier you are testing for. You must get a score of 16-20 each.

**HT2 Testing**
Phase 1: Defeat two opponents in your current tier with a minimum score of 10-6 or better.
Phase 2: Compete against two HT2 players, and achieve an equal or better overall score. You must get a minimum of 7 rounds on each opponent.

**LT2 Testing**
Phase 1: Defeat two HT3 opponents with a minimum score of 10-6 or better.
Phase 2: Compete against 2 LT2 players, and achieve an equal or better overall score. You must get a minimum of 7 rounds on each opponent.

**Note**
All LT2+ Fights must be spectated by a Staff (Helper+)

**HT3 Testing (Queue)**
Phase 1: Defeat one LT3 with a minimum score of 6-3 or better (First to 10).
Phase 2: Beat the HT3 tester in First to 10.

**Evaluation Tests (LT3 and below)**
Fights will be FT6
Minimum score for LT3: 4-6 against LT3 Tester
Score for LT3: 6-5 or better
Score for Bridge to HT3 / Direct Eval: 6-3 or better`,
  },
  axe: {
    name: 'Axe',
    kitImage: 'https://media.discordapp.net/attachments/1409604228128706580/1452652277327462522/wa2qnyW.png',
    text: `**__Axe__**

**HT1 Testing**
Phase 1: Beat two opponents in the Tier below you with at least a 20-10 score each (40-20 overall)
Phase 2: Beat two/one opponents in the same Tier as you in a first to 40 format with at least 40-30 each.
Phase 3: Beat the HT1 Player in a first to 40 format. If successful, you will steal their title.

**LT1 Testing**
Phase 1: Beat two HT2 opponents with a score of 20-15 or better each (40-30 overall)
Phase 2: Fight against two/one players in the Tier you are testing for in a first to 40 format and achieve a score of 34-40 each or better.

**HT2 Testing**
Phase 1: Defeat two LT2 opponents with a score of 20-15 each or better (overall 40-30)
Phase 2: Fight against two HT2 players. Achieving at least 16-20 or better (overall 32-40).

**LT2 Testing**
Phase 1: Beat two HT3 opponents with a score of 20-15 each or better (overall 40-30)
Phase 2: Fight 2 LT2 players and achieve a score of 16-20 each or better (overall 32-40)

**Note**
All LT2+ Fights must be spectated by a Staff (Helper+)

**HT3 Testing (Queue)**
Phase 1: Defeat 1 LT3 with a minimum score of 10-5 or better.
Phase 2: You will be paired against a HT3 tester, who you must beat in a First to 20 to achieve HT3.

**Evaluation Tests (LT3 and below)**
Fights will be FT10
Minimum score for LT3: 8-10 against LT3 Tester
Score for LT3: 10-9 or better
Score for Bridge to HT3 / Direct Eval: 10-5 or better`,
  },
  mace: {
    name: 'Mace',
    kitImage: 'https://media.discordapp.net/attachments/1409604228128706580/1452652276438274152/dwawadadwwadawd.png',
    text: `**__Mace__**

**HT1 Testing**
Phase 1: Beat two opponents in the Tier below you with at least a 4-2 score each (8-4 overall)
Phase 2: Beat two/one opponents in the same Tier as you with at least 8-5 overall
Phase 3: Beat the HT1 Player. If successful, you will steal their title.

**LT1 Testing**
Phase 1: Beat two LT2 opponents with a score of 4-2 or better each (8-4 overall)
Phase 2: Beat two/one HT2 opponents with an overall score of 8-5 or better
Phase 3: Fight against one/two LT1 opponents with a score of 3-4 or better each.

**HT2 Testing**
Phase 1: Defeat two LT2 opponents with a score of 4-2 each or better (overall 8-4)
Phase 2: Fight against two HT2 players. Achieving at least 3-4 or better (overall 6-8).

**LT2 Testing**
Phase 1: Beat two HT3 opponents with a score of 4-2 each or better (overall 8-4)
Phase 2: Fight 2 LT2 players and achieve a score of 3-4 each or better (overall 6-8)

**Note**
All LT2+ Fights must be spectated by a Staff (Helper+)

**HT3 Testing (Queue)**
Phase 1: Defeat 1 LT3 with a minimum score of 3-1.
Phase 2: Beat the HT3 in a FT4

**Evaluation Tests (LT3 and below)**
Fights will be FT3
Minimum score for LT3: 2-3 against LT3 Tester
Score for LT3: 3-2 or better
Score for Bridge to HT3 / Direct Eval: 3-1 or better`,
  },
  uhc: {
    name: 'UHC',
    kitImage: 'https://i.imgur.com/AT0iJN9.png',
    text: `**__UHC__**

**HT1 Testing**
Phase 1: Beat two LT2 opponents with a score of 10-2 each or better (20-4 overall or better)
Phase 2: Beat two HT2 opponents with a score of 10-4 each or better (20-8 overall or better)
Phase 3: Beat two LT1 opponents with a score of 10-7 each or better (20-14 overall or better)
Phase 4: Beat the HT1 Player. If successful, you will steal their title.

**LT1 Testing**
Phase 1: Beat 2 LT2 opponents with a score of 10-5 each or better (20-10 overall or better)
Phase 2: Beat two HT2 opponents with a score 10-7 or better each (20-14 overall or better)
Phase 3: Achieve an equal or better overall score against one/two players in the Tier you are testing for.

**HT2 Testing**
Phase 1: Beat two LT2 opponents with a score of 20-14 or better overall (true score) FT10
Phase 2: Compete against two HT2 players, and achieve 14-20 overall score.

**LT2 Testing**
Phase 1: Beat 2 HT3 opponents with an overall score of 20-14 or better
Phase 2: Fight 2 LT2 players, achieve an equal or better overall score, getting a score of 14-20 or better on each opponent

**Note**
All LT2+ Fights must be spectated by a Staff (Helper+)

**HT3 Testing (Queue)**
Phase 1: Defeat 1 LT3 with a minimum score of 6-3 or better.
Phase 2: You will be paired against a HT3 tester, who you must beat in a FT10 to achieve HT3.

**Evaluation Tests (LT3 and below)**
Fights will be FT6
Minimum score for LT3: 4-6 against LT3 Tester
Score for LT3: 6-5 or better
Score for Bridge to HT3 / Direct Eval: 6-3 or better`,
  },
  pot: {
    name: 'Pot',
    kitImage: 'https://i.imgur.com/3Y1cSjq.png',
    text: `**__Potion__**

**HT1 Testing**
Phase 1: Beat two LT2 opponents
Phase 2: Beat two HT2 opponents
Phase 3: Beat two/one opponents in the same Tier as you
Phase 4: Beat the HT1 Player. If successful, you will steal their title.

**LT1 Testing**
Phase 1: Beat two LT2 opponents
Phase 2: Beat two opponents in the same Tier as you
Phase 3: Achieve an equal or better overall score against 1 player in the Tier you are testing for. You must get a minimum of 3-4 on that opponent.

**HT2 Testing**
Phase 1: Defeat two opponents in your current tier.
Phase 2: Compete against two HT2 players, and achieve an equal or better overall score. You must get a minimum of 3 rounds on each opponent.

**LT2 Testing**
Phase 1: Beat two opponents in your current tier
Phase 2: Compete against 2 LT2 players, and achieve an equal or better overall score. You must get a minimum of 3 rounds on each opponent.

**Note**
All LT2+ Fights must be spectated by a Staff (Helper+)

**HT3 Testing (Queue)**
Phase 1: Defeat 1 LT3 in a FT4 with a minimum score of 3-1 or better
Phase 2: Beat HT3 tester in First to 4 to achieve HT3

**Evaluation Tests (LT3 and below)**
Fights will be FT3
Minimum score for LT3: 2-3 against LT3 Tester
Score for LT3: 3-2 or better
Score for Bridge to HT3 / Direct Eval: 3-1 or better`,
  },
  nethpot: {
    name: 'Nethpot',
    kitImage: 'https://i.imgur.com/RyHWR7F.png',
    text: `**__Netherite Potion__**

**HT1 Testing**
Phase 1: Beat two LT2 opponents
Phase 2: Beat two HT2 opponents
Phase 3: Beat two/one opponents in the same Tier as you
Phase 4: Beat the HT1 Player. If successful, you will steal their title.

**LT1 Testing**
Phase 1: Beat two LT2 opponents
Phase 2: Beat two opponents in the same Tier as you
Phase 3: Achieve an equal or better overall score against 1 player in the Tier you are testing for. You must get a minimum of 3-4 on that opponent.

**HT2 Testing**
Phase 1: Defeat two opponents in your current tier.
Phase 2: Compete against two HT2 players, and achieve an equal or better overall score. You must get a minimum of 3 rounds on each opponent.

**LT2 Testing**
Phase 1: Beat two opponents in your current tier
Phase 2: Compete against 2 LT2 players, and achieve an equal or better overall score. You must get a minimum of 3 rounds on each opponent.

**Note**
All LT2+ Fights must be spectated by a Staff (Helper+)

**HT3 Testing (Queue)**
Phase 1: Defeat 1 LT3 in a FT4 with a minimum score of 3-1 or better
Phase 2: Beat HT3 tester in First to 4 to achieve HT3

**Evaluation Tests (LT3 and below)**
Fights will be FT3
Minimum score for LT3: 2-3 against LT3 Tester
Score for LT3: 3-2 or better
Score for Bridge to HT3 / Direct Eval: 3-1 or better`,
  },
  smp: {
    name: 'SMP',
    kitImage: 'https://i.imgur.com/L6b5d4m.png',
    text: `**__SMP__**

**HT1 Testing**
Phase 1: Beat two LT2 opponents (4-1 each or better, 8-2 overall)
Phase 2: Beat two HT2 opponents (4-2 each or better, 8-4 overall)
Phase 3: Beat two LT1 opponents (4-2 each or better, 8-4 overall)
Phase 4: Beat the HT1 Player. If successful, you will steal their title.

**LT1 Testing**
Phase 1: Beat two LT2 opponents with a score of 4-1 or better each (8-2 overall)
Phase 2: Beat two HT2 opponents with a score of 4-2 or better each (8-4 overall)
Phase 3: Fight against two/one players in the Tier you are testing for and achieve a score of 3-4 each or better.

**HT2 Testing**
Phase 1: Defeat 2 LT2 opponents with a minimum score of 4-2 or better
Phase 2: Fight 2 HT2 players and achieve a score of 3-4 on each opponent.

**LT2 Testing**
Phase 1: Beat two HT3 opponents with a score of 4-2 each
Phase 2: Fight 2 LT2 players and achieve a score of 3-4 or better.

**Note**
All LT2+ Fights must be spectated by a Staff (Helper+)

**HT3 Testing (Queue)**
Phase 1: Defeat 1 LT3 with a minimum score of 3-1 or better.
Phase 2: You will be paired against a HT3 tester, who you must beat in a First to 4 to achieve HT3.

**Evaluation Tests (LT3 and below)**
Fights will be FT3
Minimum score for LT3: 2-3 against LT3 Tester
Score for LT3: 3-2 or better`,
  },
  vanilla: {
    name: 'Vanilla',
    kitImage: 'https://i.imgur.com/Gl2LJtY.png',
    text: `**__Vanilla__**

**HT1 Testing**
Phase 1: Beat two LT2 opponents
Phase 2: Beat two HT2 opponents
Phase 3: Beat two LT1 opponents
Phase 4: Beat the HT1 Player. If successful, you will steal their title.

**LT1 Testing**
Phase 1: Beat two LT2 opponents
Phase 2: Beat two HT2 opponents
Phase 3: Achieve a score of 3-4 each against one/two players in the Tier you are testing for.

**HT2 Testing**
Phase 1: Beat two LT2 opponents in First to 4.
Phase 2: Compete against two HT2 players, and achieve an equal or better overall score. You must get a minimum of the score 3-4 on each opponent.

**LT2 Testing**
Phase 1: Beat two HT3 opponents in a First to 4.
Phase 2: Compete against 2 LT2 players, you must get a score of 3-4 on each opponent.

**Note**
All LT2+ Fights must be spectated by a Staff (Helper+)

**HT3 Testing (Queue)**
Phase 1: Defeat 1 LT3 with a minimum score of 3-1 or better.
Phase 2: You will be paired against a HT3 tester, who you must beat in a First to 3 to achieve HT3.

**Evaluation Tests (LT3 and below)**
Fights will be FT3
Minimum score for LT3: 2-3 against LT3 Tester
Score for LT3: 3-2 or better
Score for Bridge to HT3 / Direct Eval: 3-1 or better`,
  },
};

const RUBRIC_GAMEMODES = ['sword', 'axe', 'mace', 'uhc', 'pot', 'nethpot', 'smp', 'vanilla'];

const commands = {
  setuprubric: {
    data: new SlashCommandBuilder()
      .setName('setuprubric')
      .setDescription('Post the ranked rubric panel in this channel (admin only)'),

    async execute(interaction) {
      await interaction.deferReply({ ephemeral: true });
      if (!isAdmin(interaction.member)) {
        return interaction.editReply('❌ Admin only.');
      }

      const embed = new EmbedBuilder()
        .setTitle('Ranked Rubric')
        .setDescription(
          '**These are the ranked rubrics for all game modes.**\n\n' +
          'Being tested/testing means you agree to **follow the rubrics correctly.** ' +
          'Tests should be done **according** to the Rubrics and TierList Rules.\n' +
          'Failure to follow this will result in your test being **invalidated** by higher staff.'
        )
        .setColor(0x5865F2)
        .setFooter({ text: 'SM Tierlist' });

      const rows = [];
      let row = new ActionRowBuilder();
      let count = 0;
      for (const key of RUBRIC_GAMEMODES) {
        if (count > 0 && count % 5 === 0) {
          rows.push(row);
          row = new ActionRowBuilder();
        }
        row.addComponents(
          new ButtonBuilder()
            .setCustomId(`rubric_btn:${key}`)
            .setLabel(RUBRICS[key].name)
            .setStyle(ButtonStyle.Primary)
        );
        count++;
      }
      if (count % 5 !== 0 || count === 0) rows.push(row);

      await interaction.channel.send({ embeds: [embed], components: rows });
      await interaction.editReply('✅ Rubric panel posted.');
    },
  },
};

async function handleRubricButton(interaction) {
  if (!interaction.customId.startsWith('rubric_btn:')) return false;
  const key = interaction.customId.split(':')[1];

  const rubric = RUBRICS[key];
  if (!rubric) {
    await interaction.reply({ content: '❌ No rubric for this gamemode.', ephemeral: true });
    return true;
  }

  const textEmbed = new EmbedBuilder()
    .setTitle(rubric.name)
    .setDescription(rubric.text.length > 4096 ? rubric.text.slice(0, 4093) + '...' : rubric.text)
    .setColor(0x5865F2)
    .setFooter({ text: 'SM Tierlist' });

  const kitEmbed = new EmbedBuilder()
    .setTitle(`${rubric.name} Kit`)
    .setColor(0x2f3136)
    .setFooter({ text: 'SM Tierlist' });

  if (rubric.kitImage) kitEmbed.setImage(rubric.kitImage);

  await interaction.reply({ embeds: [textEmbed, kitEmbed], ephemeral: true });
  return true;
}

module.exports = { commands, handleRubricButton };
