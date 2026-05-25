'use strict';
const fs = require('fs');
const { DATA_FILE, BANS_FILE, COOLDOWN_SECONDS } = require('./config');

// --- data.json ---

function loadData() {
  if (!fs.existsSync(DATA_FILE)) {
    return { ticket_state: {}, cooldowns: {}, queue_panel_message: null, queue_message_ids: [] };
  }
  try {
    return JSON.parse(fs.readFileSync(DATA_FILE, 'utf8'));
  } catch {
    return { ticket_state: {}, cooldowns: {}, queue_panel_message: null, queue_message_ids: [] };
  }
}

function saveData(data) {
  fs.writeFileSync(DATA_FILE, JSON.stringify(data, null, 2), 'utf8');
}

function getOpenTicketChannelId(userId, modeKey) {
  const data = loadData();
  return (data.ticket_state[String(userId)] || {})[modeKey] || null;
}

function setOpenTicketChannelId(userId, modeKey, channelId) {
  const data = loadData();
  if (!data.ticket_state) data.ticket_state = {};
  if (!data.ticket_state[String(userId)]) data.ticket_state[String(userId)] = {};
  if (channelId == null) {
    delete data.ticket_state[String(userId)][modeKey];
  } else {
    data.ticket_state[String(userId)][modeKey] = channelId;
  }
  saveData(data);
}

function getLastClosed(userId, modeKey) {
  const data = loadData();
  return parseFloat((data.cooldowns[String(userId)] || {})[modeKey] || 0);
}

function setLastClosed(userId, modeKey, ts) {
  const data = loadData();
  if (!data.cooldowns) data.cooldowns = {};
  if (!data.cooldowns[String(userId)]) data.cooldowns[String(userId)] = {};
  data.cooldowns[String(userId)][modeKey] = ts;
  saveData(data);
}

function cooldownLeft(userId, modeKey) {
  const last = getLastClosed(userId, modeKey);
  if (!last) return 0;
  const left = Math.floor((last + COOLDOWN_SECONDS) - Date.now() / 1000);
  return Math.max(0, left);
}

function getUserCooldowns(userId) {
  const data = loadData();
  return data.cooldowns[String(userId)] || {};
}

function persistQueueMessageIds(queueMessageIds) {
  try {
    const data = loadData();
    // Store as array of [msgId, gamemode]
    data.queue_message_ids = Object.entries(queueMessageIds);
    saveData(data);
  } catch (e) {
    console.error('[Storage] persistQueueMessageIds error:', e);
  }
}

function loadQueueMessageIds() {
  const data = loadData();
  const ids = {};
  const arr = data.queue_message_ids || [];
  for (const [msgId, gm] of arr) {
    ids[msgId] = gm;
  }
  return ids;
}

function getQueuePanelMessage() {
  const data = loadData();
  const qpm = data.queue_panel_message;
  if (!qpm || !Array.isArray(qpm) || qpm.length < 2) return null;
  return { channelId: String(qpm[0]), messageId: String(qpm[1]) };
}

function setQueuePanelMessage(channelId, messageId) {
  const data = loadData();
  data.queue_panel_message = channelId ? [channelId, messageId] : null;
  saveData(data);
}

// --- bans.json ---

function loadBans() {
  if (!fs.existsSync(BANS_FILE)) return {};
  try {
    return JSON.parse(fs.readFileSync(BANS_FILE, 'utf8'));
  } catch {
    return {};
  }
}

function saveBans(data) {
  fs.writeFileSync(BANS_FILE, JSON.stringify(data, null, 2), 'utf8');
}

function isPlayerBanned(username) {
  const data = loadBans();
  const ban = data[username.toLowerCase()];
  if (!ban) return false;
  const expiresAt = ban.expires_at || 0;
  if (expiresAt > 0 && Date.now() / 1000 > expiresAt) {
    delete data[username.toLowerCase()];
    saveBans(data);
    return false;
  }
  return true;
}

function getBanInfo(username) {
  const data = loadBans();
  const ban = data[username.toLowerCase()];
  if (!ban) return null;
  const expiresAt = ban.expires_at || 0;
  if (expiresAt > 0 && Date.now() / 1000 > expiresAt) {
    delete data[username.toLowerCase()];
    saveBans(data);
    return null;
  }
  return ban;
}

function banPlayer(username, days, reason = '') {
  const data = loadBans();
  const expiresAt = days === 0 ? 0 : Math.floor(Date.now() / 1000) + days * 24 * 60 * 60;
  data[username.toLowerCase()] = {
    username,
    reason,
    banned_at: Math.floor(Date.now() / 1000),
    expires_at: expiresAt,
    permanent: days === 0,
  };
  saveBans(data);
}

function unbanPlayer(username) {
  const data = loadBans();
  if (data[username.toLowerCase()]) {
    delete data[username.toLowerCase()];
    saveBans(data);
    return true;
  }
  return false;
}

module.exports = {
  loadData,
  saveData,
  getOpenTicketChannelId,
  setOpenTicketChannelId,
  getLastClosed,
  setLastClosed,
  cooldownLeft,
  getUserCooldowns,
  persistQueueMessageIds,
  loadQueueMessageIds,
  getQueuePanelMessage,
  setQueuePanelMessage,
  isPlayerBanned,
  getBanInfo,
  banPlayer,
  unbanPlayer,
};
