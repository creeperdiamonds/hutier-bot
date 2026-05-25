'use strict';
const path = require('path');
const { DB_PATH } = require('./config');

let db;

function initDb() {
  const Database = require('better-sqlite3');
  db = new Database(DB_PATH);
  db.pragma('journal_mode = WAL');
  db.pragma('foreign_keys = ON');

  db.exec(`
    CREATE TABLE IF NOT EXISTS tests (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT NOT NULL,
      mode TEXT NOT NULL,
      rank TEXT NOT NULL,
      testerId TEXT,
      testerName TEXT,
      ts INTEGER NOT NULL,
      accountType TEXT,
      UNIQUE(username, mode)
    );
    CREATE INDEX IF NOT EXISTS idx_tests_username ON tests(username);
    CREATE INDEX IF NOT EXISTS idx_tests_mode ON tests(mode);
    CREATE INDEX IF NOT EXISTS idx_tests_ts ON tests(ts DESC);

    CREATE TABLE IF NOT EXISTS linked_accounts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      discord_id TEXT NOT NULL UNIQUE,
      minecraft_name TEXT NOT NULL,
      linked_at TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_linked_discord ON linked_accounts(discord_id);
    CREATE INDEX IF NOT EXISTS idx_linked_minecraft ON linked_accounts(minecraft_name);

    CREATE TABLE IF NOT EXISTS pending_codes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      discord_id TEXT NOT NULL,
      code TEXT NOT NULL,
      created_at TEXT DEFAULT (datetime('now')),
      expires_at TEXT NOT NULL,
      used INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_pending_code ON pending_codes(code);
  `);

  console.log(`[DB] Initialized: ${DB_PATH}`);
  return db;
}

function getDb() {
  if (!db) throw new Error('Database not initialized. Call initDb() first.');
  return db;
}

// --- Tests ---

function upsertTest({ username, mode, rank, testerId, testerName, ts, accountType }) {
  const stmt = getDb().prepare(`
    INSERT INTO tests (username, mode, rank, testerId, testerName, ts, accountType)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(username, mode) DO UPDATE SET
      rank = excluded.rank,
      testerId = excluded.testerId,
      testerName = excluded.testerName,
      ts = excluded.ts,
      accountType = excluded.accountType
  `);
  try {
    stmt.run(username, mode, rank, testerId || null, testerName || null, ts, accountType || null);
    return true;
  } catch (e) {
    console.error('[DB] upsertTest error:', e);
    return false;
  }
}

function getTestsByUsername(username) {
  return getDb().prepare('SELECT * FROM tests WHERE LOWER(username) = LOWER(?)').all(username);
}

function getTestByUsernameAndMode(username, mode) {
  return getDb().prepare(
    'SELECT * FROM tests WHERE LOWER(username) = LOWER(?) AND LOWER(mode) = LOWER(?)'
  ).get(username, mode);
}

function getTestsByMode(mode) {
  return getDb().prepare('SELECT * FROM tests WHERE LOWER(mode) = LOWER(?)').all(mode);
}

function getTestsByModeAndRank(mode, rank) {
  return getDb().prepare(
    'SELECT * FROM tests WHERE LOWER(mode) = LOWER(?) AND rank = ?'
  ).all(mode, rank);
}

function getAllTests() {
  return getDb().prepare('SELECT * FROM tests ORDER BY ts DESC').all();
}

function deleteTestsByUsername(username) {
  try {
    getDb().prepare('DELETE FROM tests WHERE LOWER(username) = LOWER(?)').run(username);
    return true;
  } catch (e) {
    console.error('[DB] deleteTestsByUsername error:', e);
    return false;
  }
}

function renamePlayer(oldName, newName) {
  try {
    getDb().prepare(
      'UPDATE tests SET username = ? WHERE LOWER(username) = LOWER(?)'
    ).run(newName, oldName);
    getDb().prepare(
      'UPDATE linked_accounts SET minecraft_name = ? WHERE LOWER(minecraft_name) = LOWER(?)'
    ).run(newName, oldName);
    return true;
  } catch (e) {
    console.error('[DB] renamePlayer error:', e);
    return false;
  }
}

// --- Linked Accounts ---

function getLinkedAccount(discordId) {
  return getDb().prepare(
    'SELECT * FROM linked_accounts WHERE discord_id = ?'
  ).get(String(discordId));
}

function getLinkedAccountByMinecraft(minecraftName) {
  return getDb().prepare(
    'SELECT * FROM linked_accounts WHERE LOWER(minecraft_name) = LOWER(?)'
  ).get(minecraftName);
}

function getAllLinkedAccounts() {
  return getDb().prepare('SELECT * FROM linked_accounts').all();
}

function linkAccount(discordId, minecraftName) {
  try {
    getDb().prepare(`
      INSERT INTO linked_accounts (discord_id, minecraft_name)
      VALUES (?, ?)
      ON CONFLICT(discord_id) DO UPDATE SET minecraft_name = excluded.minecraft_name
    `).run(String(discordId), minecraftName);
    return true;
  } catch (e) {
    console.error('[DB] linkAccount error:', e);
    return false;
  }
}

function unlinkAccount(discordId) {
  try {
    const info = getDb().prepare(
      'DELETE FROM linked_accounts WHERE discord_id = ?'
    ).run(String(discordId));
    return info.changes > 0;
  } catch (e) {
    console.error('[DB] unlinkAccount error:', e);
    return false;
  }
}

// --- Pending Codes ---

function createPendingCode(discordId, code, expiresAt) {
  try {
    // Delete any existing codes for this user
    getDb().prepare('DELETE FROM pending_codes WHERE discord_id = ?').run(String(discordId));
    getDb().prepare(`
      INSERT INTO pending_codes (discord_id, code, expires_at, used)
      VALUES (?, ?, ?, 0)
    `).run(String(discordId), code.toUpperCase(), expiresAt);
    return true;
  } catch (e) {
    console.error('[DB] createPendingCode error:', e);
    return false;
  }
}

function getPendingCodeByDiscord(discordId) {
  const now = new Date().toISOString();
  return getDb().prepare(`
    SELECT * FROM pending_codes
    WHERE discord_id = ? AND used = 0 AND expires_at > ?
    ORDER BY created_at DESC LIMIT 1
  `).get(String(discordId), now);
}

function verifyCode(code) {
  const now = new Date().toISOString();
  const row = getDb().prepare(`
    SELECT * FROM pending_codes
    WHERE UPPER(code) = UPPER(?) AND used = 0 AND expires_at > ?
    LIMIT 1
  `).get(code, now);
  if (!row) return null;
  getDb().prepare('UPDATE pending_codes SET used = 1 WHERE id = ?').run(row.id);
  return row.discord_id;
}

function getDb_raw() { return db; }

module.exports = {
  initDb,
  getDb: getDb_raw,
  upsertTest,
  getTestsByUsername,
  getTestByUsernameAndMode,
  getTestsByMode,
  getTestsByModeAndRank,
  getAllTests,
  deleteTestsByUsername,
  renamePlayer,
  getLinkedAccount,
  getLinkedAccountByMinecraft,
  getAllLinkedAccounts,
  linkAccount,
  unlinkAccount,
  createPendingCode,
  getPendingCodeByDiscord,
  verifyCode,
};
