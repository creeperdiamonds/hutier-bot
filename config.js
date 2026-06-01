'use strict';
require('dotenv').config();

const DISCORD_TOKEN = process.env.DISCORD_TOKEN || process.env.BOT_TOKEN || process.env.TOKEN;
const GUILD_ID = process.env.GUILD_ID || '';
const STAFF_ROLE_ID = process.env.STAFF_ROLE_ID || '';
const EXTRA_STAFF_ROLE_IDS = (process.env.EXTRA_STAFF_ROLE_IDS || '')
  .split(',').map(s => s.trim()).filter(Boolean);
const ALLOWED_USER_IDS = (process.env.ALLOWED_USER_IDS || '')
  .split(',').map(s => s.trim()).filter(Boolean);
const TICKET_CATEGORY_ID = process.env.TICKET_CATEGORY_ID || '';
const TIER_RESULTS_CHANNEL_ID = process.env.TIER_RESULTS_CHANNEL_ID || '';
const WEBSITE_URL = (process.env.WEBSITE_URL || '').replace(/\/$/, '');
const BOT_API_KEY = process.env.BOT_API_KEY || '';
const DB_PATH = process.env.DB_PATH || require('path').join(__dirname, '..', 'tierlist.db');
const COOLDOWN_SECONDS = parseInt(process.env.COOLDOWN_SECONDS || String(30 * 24 * 60 * 60), 10);
const TEST_LOGS_CHANNEL_ID = process.env.TEST_LOGS_CHANNEL_ID || '';
const VERIFIED_ROLE_ID = process.env.VERIFIED_ROLE_ID || '';
const LINK_CODE_LENGTH = 8;
const LINK_CODE_EXPIRY_MINUTES = 10;
const DATA_FILE = require('path').join(__dirname, 'data.json');
const BANS_FILE = require('path').join(__dirname, 'bans.json');

const GAMEMODES = [
  'Sword', 'Axe', 'Mace', 'UHC', 'Pot', 'Nethpot', 'SMP', 'Vanilla',
  'Creeper', 'Cart', 'DiaSMP', 'OGVanilla', 'ShieldlessUHC',
  'SpearMace', 'SpearElytra', 'DiaPot', 'Crystal'
];

// Keys (lowercase) for internal use
const GAMEMODE_KEYS = GAMEMODES.map(g => g.toLowerCase());

const TIERS = ['HT1', 'LT1', 'HT2', 'LT2', 'HT3', 'LT3', 'HT4', 'LT4', 'HT5', 'LT5'];
const RANKS = ['Unranked', 'LT5', 'HT5', 'LT4', 'HT4', 'LT3', 'HT3', 'LT2', 'HT2', 'LT1', 'HT1'];

const POINTS = {
  Unranked: 0,
  LT5: 1, HT5: 2,
  LT4: 3, HT4: 4,
  LT3: 6, HT3: 8,
  LT2: 10, HT2: 12,
  LT1: 14, HT1: 18,
};

// Display name mapping (lowercase key -> display name)
const GAMEMODE_DISPLAY = {
  sword: 'Sword', axe: 'Axe', mace: 'Mace', uhc: 'UHC', pot: 'Pot',
  nethpot: 'Nethpot', smp: 'SMP', vanilla: 'Vanilla', creeper: 'Creeper',
  cart: 'Cart', diasmp: 'DiaSMP', ogvanilla: 'OGVanilla',
  shieldlessuhc: 'ShieldlessUHC', spearmace: 'SpearMace', spearelytra: 'SpearElytra',
  diapot: 'DiaPot', crystal: 'Crystal',
};

function getGamemodeDisplay(key) {
  if (!key) return key;
  return GAMEMODE_DISPLAY[key.toLowerCase()] || key;
}

function normalizeGamemode(mode) {
  if (!mode) return mode;
  return mode.toLowerCase().trim();
}

// Tier role IDs: TIER_ROLE_{GAMEMODE}_{RANK}
const GAMEMODE_TIER_ROLES = {};
for (const gm of GAMEMODES) {
  GAMEMODE_TIER_ROLES[gm.toLowerCase()] = {};
  for (const rank of RANKS) {
    if (rank === 'Unranked') continue;
    const envKey = `TIER_ROLE_${gm.toUpperCase()}_${rank.toUpperCase()}`;
    const roleId = process.env[envKey];
    if (roleId && roleId !== '0') {
      GAMEMODE_TIER_ROLES[gm.toLowerCase()][rank] = roleId;
    }
  }
}

function getTierRoleId(gamemode, rank) {
  const gm = (gamemode || '').toLowerCase();
  const roles = GAMEMODE_TIER_ROLES[gm] || {};
  return roles[rank] || null;
}

// Queue channels and ping roles from the Python config
const QUEUE_CHANNELS = {
  sword: '1495038486120632410',
  axe: '1495038602751774730',
  mace: '1495038625719783586',
  uhc: '1495038706103484487',
  pot: '1495038741465792553',
  nethpot: '1495038766769897482',
  smp: '1495038799800176660',
  vanilla: '1495038839591534834',
  creeper: '1495038857597681818',
  cart: '1495038915453779982',
  diasmp: '1495038938640027760',
  spearelytra: '1495038976988545206',
  spearmace: '1495038999876600008',
  shieldlessuhc: '1495039115119296572',
  ogvanilla: '1495039145330872341',
};

const QUEUE_PING_ROLES = {
  sword: '1495043729017278525',
  axe: '1495043913583558758',
  mace: '1495043981959237752',
  uhc: '1495044042612805754',
  pot: '1495044102730022942',
  nethpot: '1495044163194847322',
  smp: '1495044237551472893',
  vanilla: '1495044315272052929',
  creeper: '1495044383425171506',
  cart: '1495044436403556443',
  diasmp: '1495044514992095333',
  shieldlessuhc: '1495044593211670711',
  ogvanilla: '1495044664502386698',
  spearelytra: '1495044732680667247',
  spearmace: '1495044798472781944',
};

// Tester role IDs per gamemode
const GAMEMODE_TESTER_ROLES = {
  vanilla: '1469763891226480926',
  uhc: '1469765994988704030',
  pot: '1469763780593324032',
  nethpot: '1469763817218117697',
  smp: '1469764274955223161',
  sword: '1469763677141074125',
  axe: '1469763738889486518',
  mace: '1469763612452196375',
  cart: '1469763920871952435',
  creeper: '1469764200812249180',
  diasmp: '1469763946968911893',
  ogvanilla: '1469764329460203571',
  shieldlessuhc: '1469766017243807865',
  spearmace: '1469968704203788425',
  spearelytra: '1469968762575912970',
};

const GAMEMODE_COLORS = {
  mace: 0x808080, sword: 0x3498db, vanilla: 0x9b59b6, uhc: 0xe67e22,
  pot: 0xe74c3c, nethpot: 0xc0392b, smp: 0x2ecc71, axe: 0x8b4513,
  cart: 0xf1c40f, creeper: 0x27ae60, diasmp: 0x1abc9c, ogvanilla: 0x8e44ad,
  shieldlessuhc: 0xd35400, spearmace: 0x16a085, spearelytra: 0x2980b9,
  diapot: 0x00bcd4, crystal: 0x9b59b6,
};

function getGamemodeColor(gamemode) {
  return GAMEMODE_COLORS[(gamemode || '').toLowerCase()] || 0x5865F2;
}

const RANK_THRESHOLDS = [
  [350, 'Combat Grandmaster'],
  [200, 'Combat Master'],
  [100, 'Combat Ace'],
  [50, 'Combat Cadet'],
  [20, 'Combat Rookie'],
];

function getRankName(totalPoints) {
  for (const [threshold, name] of RANK_THRESHOLDS) {
    if (totalPoints >= threshold) return name;
  }
  return 'Unranked';
}

function getRankProgress(totalPoints) {
  if (totalPoints >= 350) return '🎉 Max Rank — Combat Grandmaster';
  for (const [threshold, name] of RANK_THRESHOLDS) {
    if (totalPoints < threshold) return `${threshold - totalPoints} pts to **${name}**`;
  }
  return '';
}

const TICKET_ROUNDS = {
  vanilla: ['FT4', 'FT3', null],
  diasmp: ['FT4', 'FT3', 'FT2'],
  ogvanilla: ['FT4', 'FT2', null],
  nethpot: ['FT4', 'FT2', null],
  mace: ['FT4', 'FT2', null],
  smp: ['FT4', 'FT3', 'FT2'],
  cart: ['FT4', 'FT3', 'FT2'],
  sword: ['FT10', 'FT6', null],
  uhc: ['FT6', 'FT3', null],
  pot: ['FT10', 'FT6', null],
  creeper: ['FT6', 'FT4', 'FT3'],
  shieldlessuhc: ['FT6', 'FT4', null],
  axe: ['FT20', 'FT10', null],
  spearmace: ['FT6', 'FT3', null],
  spearelytra: ['FT6', 'FT3', null],
  diapot: ['FT4', 'FT2', null],
  crystal: ['FT4', 'FT3', null],
};

function getTicketRoundsDisplay(modeKey) {
  const rounds = TICKET_ROUNDS[modeKey.toLowerCase()];
  if (!rounds) return 'FT4';
  const [def, lt3, loss] = rounds;
  if (loss) return `${def}, below LT3: ${lt3}, if you lose a round against tester: ${loss}`;
  return `${def}, below LT3: ${lt3}`;
}

module.exports = {
  DISCORD_TOKEN,
  GUILD_ID,
  STAFF_ROLE_ID,
  EXTRA_STAFF_ROLE_IDS,
  ALLOWED_USER_IDS,
  TICKET_CATEGORY_ID,
  TIER_RESULTS_CHANNEL_ID,
  TEST_LOGS_CHANNEL_ID,
  VERIFIED_ROLE_ID,
  WEBSITE_URL,
  BOT_API_KEY,
  DB_PATH,
  COOLDOWN_SECONDS,
  LINK_CODE_LENGTH,
  LINK_CODE_EXPIRY_MINUTES,
  DATA_FILE,
  BANS_FILE,
  GAMEMODES,
  GAMEMODE_KEYS,
  TIERS,
  RANKS,
  POINTS,
  GAMEMODE_DISPLAY,
  GAMEMODE_TIER_ROLES,
  QUEUE_CHANNELS,
  QUEUE_PING_ROLES,
  GAMEMODE_TESTER_ROLES,
  getGamemodeDisplay,
  normalizeGamemode,
  getTierRoleId,
  getGamemodeColor,
  getTicketRoundsDisplay,
  getRankName,
  getRankProgress,
  RANK_THRESHOLDS,
};
