'use strict';

// Auto-migrator: runs once on startup, looks for a channel named "tier-sync" (or similar),
// finds a bot message containing the old bot's JSON tier database, imports it, then deletes the channel.

const SYNC_CHANNEL_NAMES = ['tier-sync', 'sync-tier', 'tiersync', 'sync_tier', 'tier_sync'];

// Old bot may use different display names — map them to new bot's mode names
const MODE_MAP = {
  'netherite potion': 'Nethpot',
  'potion': 'Pot',
};

function normalizeMode(mode) {
  return MODE_MAP[mode.toLowerCase()] || mode;
}

async function fetchJsonFromMessage(msg) {
  // Try message content first (handles raw JSON or ```json ... ``` code blocks)
  let content = msg.content && msg.content.trim();
  if (content) {
    // Strip code block fences if present
    const fenceMatch = content.match(/^```(?:json)?\s*([\s\S]*?)```$/);
    if (fenceMatch) content = fenceMatch[1].trim();
    if (content.startsWith('{')) {
      try { return JSON.parse(content); } catch {}
    }
  }

  // Try attachments (in case the JSON was sent as a file)
  for (const att of msg.attachments.values()) {
    try {
      const res = await fetch(att.url);
      const text = await res.text();
      if (text.trim().startsWith('{')) return JSON.parse(text);
    } catch {}
  }

  return null;
}

async function runMigration(guild, db) {
  // Look for a sync channel
  const syncChannel = guild.channels.cache.find(ch =>
    ch.isTextBased() && SYNC_CHANNEL_NAMES.includes(ch.name.toLowerCase())
  );

  if (!syncChannel) return; // Nothing to do

  console.log(`[Migrate] Found sync channel: #${syncChannel.name}`);

  // Fetch recent messages and find one from a bot with JSON
  let data = null;
  try {
    const messages = await syncChannel.messages.fetch({ limit: 20 });
    for (const msg of messages.values()) {
      if (!msg.author.bot) continue;
      data = await fetchJsonFromMessage(msg);
      if (data) break;
    }
  } catch (e) {
    console.error('[Migrate] Failed to fetch messages:', e.message);
    return;
  }

  if (!data || typeof data !== 'object') {
    console.log('[Migrate] No valid JSON tier data found in sync channel. Skipping.');
    return;
  }

  // Import data
  let imported = 0;
  let skipped = 0;

  for (const [, playerData] of Object.entries(data)) {
    const ign = playerData.ign;
    const tiers = playerData.tiers;
    if (!ign || !tiers || typeof tiers !== 'object') continue;

    for (const [rawMode, rank] of Object.entries(tiers)) {
      const mode = normalizeMode(rawMode);
      try {
        const ok = db.upsertTest({
          username: ign,
          mode,
          rank,
          testerId: null,
          testerName: 'Migration',
          ts: Math.floor(Date.now() / 1000),
        });
        if (ok) imported++;
        else skipped++;
      } catch (e) {
        console.error(`[Migrate] Error inserting ${ign}/${mode}/${rank}:`, e.message);
        skipped++;
      }
    }
  }

  console.log(`[Migrate] Done — imported ${imported} tier entries, skipped ${skipped}.`);

  // Delete the sync channel — its job is complete
  try {
    await syncChannel.delete('Tier data migrated into database successfully.');
    console.log('[Migrate] Sync channel deleted. Migration complete.');
  } catch (e) {
    console.error('[Migrate] Could not delete sync channel (Missing Permissions?):', e.message);
  }
}

module.exports = { runMigration };
