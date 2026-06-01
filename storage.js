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

function getQueueChannels() {
  const data = loadData();
  return data.queue_channels || {};
}

function setQueueChannels(channelMap) {
  const data = loadData();
  data.queue_channels = { ...(data.queue_channels || {}), ...channelMap };
  saveData(data);
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

// Queue waitlist (ordered array of player user IDs)
function getQueue(gamemode) {
  const data = loadData();
  return (data.queues || {})[gamemode] || [];
}
function addToQueue(gamemode, userId) {
  const data = loadData();
  if (!data.queues) data.queues = {};
  if (!data.queues[gamemode]) data.queues[gamemode] = [];
  if (!data.queues[gamemode].includes(String(userId))) data.queues[gamemode].push(String(userId));
  saveData(data);
}
function removeFromQueue(gamemode, userId) {
  const data = loadData();
  if (!data.queues?.[gamemode]) return;
  data.queues[gamemode] = data.queues[gamemode].filter(id => id !== String(userId));
  saveData(data);
}
function clearQueue(gamemode) {
  const data = loadData();
  if (!data.queues) data.queues = {};
  data.queues[gamemode] = [];
  saveData(data);
}
function getQueuePosition(gamemode, userId) {
  return getQueue(gamemode).indexOf(String(userId));
}

// Active testers per gamemode (list of tester IDs on duty)
function getActiveTesters(gamemode) {
  const data = loadData();
  return (data.active_testers || {})[gamemode] || [];
}
function addActiveTester(gamemode, testerId) {
  const data = loadData();
  if (!data.active_testers) data.active_testers = {};
  if (!data.active_testers[gamemode]) data.active_testers[gamemode] = [];
  if (!data.active_testers[gamemode].includes(String(testerId))) data.active_testers[gamemode].push(String(testerId));
  saveData(data);
}
function removeActiveTester(gamemode, testerId) {
  const data = loadData();
  if (!data.active_testers?.[gamemode]) return;
  data.active_testers[gamemode] = data.active_testers[gamemode].filter(id => id !== String(testerId));
  saveData(data);
}
function clearActiveTesters(gamemode) {
  const data = loadData();
  if (!data.active_testers) data.active_testers = {};
  data.active_testers[gamemode] = [];
  saveData(data);
}

// Active session (one per gamemode at a time)
function getActiveSession(gamemode) {
  const data = loadData();
  return (data.active_sessions || {})[gamemode] || null;
}
function setActiveSession(gamemode, testerId, playerId, channelId) {
  const data = loadData();
  if (!data.active_sessions) data.active_sessions = {};
  data.active_sessions[gamemode] = { tester_id: String(testerId), player_id: String(playerId), channel_id: String(channelId) };
  saveData(data);
}
function clearActiveSession(gamemode) {
  const data = loadData();
  if (!data.active_sessions) return;
  delete data.active_sessions[gamemode];
  saveData(data);
}
function findActiveSessionByChannel(channelId) {
  const data = loadData();
  for (const [gm, session] of Object.entries(data.active_sessions || {})) {
    if (session.channel_id === String(channelId)) return { gamemode: gm, ...session };
  }
  return null;
}

// Queue region
function getQueueRegion(gamemode) {
  const data = loadData();
  return (data.queue_regions || {})[gamemode] || null;
}
function setQueueRegion(gamemode, region) {
  const data = loadData();
  if (!data.queue_regions) data.queue_regions = {};
  data.queue_regions[gamemode] = region;
  saveData(data);
}
function clearQueueRegion(gamemode) {
  const data = loadData();
  if (data.queue_regions) delete data.queue_regions[gamemode];
  saveData(data);
}

// Closed message (the edited-in-place "closed" embed message)
function getClosedMessage(gamemode) {
  const data = loadData();
  return (data.closed_messages || {})[gamemode] || null;
}
function saveClosedMessage(gamemode, channelId, messageId) {
  const data = loadData();
  if (!data.closed_messages) data.closed_messages = {};
  data.closed_messages[gamemode] = { channel_id: String(channelId), message_id: String(messageId) };
  saveData(data);
}
function clearClosedMessage(gamemode) {
  const data = loadData();
  if (data.closed_messages) delete data.closed_messages[gamemode];
  saveData(data);
}

// Last session timestamp
function getLastSession(gamemode) {
  const data = loadData();
  const ts = (data.last_sessions || {})[gamemode];
  if (!ts) return null;
  try { return new Date(ts).toUTCString().replace(' GMT', ' UTC'); } catch { return null; }
}
function saveLastSession(gamemode) {
  const data = loadData();
  if (!data.last_sessions) data.last_sessions = {};
  data.last_sessions[gamemode] = new Date().toISOString();
  saveData(data);
}

// Queue open message (where the live embed is)
function getQueueOpenMessage(gamemode) {
  const data = loadData();
  return (data.queue_open_messages || {})[gamemode] || null;
}
function saveQueueOpenMessage(gamemode, channelId, messageId) {
  const data = loadData();
  if (!data.queue_open_messages) data.queue_open_messages = {};
  data.queue_open_messages[gamemode] = { channel_id: String(channelId), message_id: String(messageId) };
  saveData(data);
}
function clearQueueOpenMessage(gamemode) {
  const data = loadData();
  if (data.queue_open_messages) delete data.queue_open_messages[gamemode];
  saveData(data);
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
  getQueueChannels,
  setQueueChannels,
  getQueuePanelMessage,
  setQueuePanelMessage,
  isPlayerBanned,
  getBanInfo,
  banPlayer,
  unbanPlayer,
  getQueue,
  addToQueue,
  removeFromQueue,
  clearQueue,
  getQueuePosition,
  getActiveTesters,
  addActiveTester,
  removeActiveTester,
  clearActiveTesters,
  getActiveSession,
  setActiveSession,
  clearActiveSession,
  findActiveSessionByChannel,
  getQueueRegion,
  setQueueRegion,
  clearQueueRegion,
  getClosedMessage,
  saveClosedMessage,
  clearClosedMessage,
  getLastSession,
  saveLastSession,
  getQueueOpenMessage,
  saveQueueOpenMessage,
  clearQueueOpenMessage,
};
