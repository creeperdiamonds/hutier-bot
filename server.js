'use strict';
const express = require('express');
const db = require('./database');
const { POINTS, getGamemodeDisplay } = require('./config');

async function startServer(bot) {
  const app = express();
  app.use(express.json());

  // GET /health
  app.get('/health', (_req, res) => {
    res.send('ok');
  });

  // GET /api/link/verify?code=X&minecraft=Y
  app.get('/api/link/verify', async (req, res) => {
    try {
      const { code, minecraft } = req.query;
      if (!code || !minecraft) {
        return res.status(400).json({ success: false, error: 'Missing code or minecraft parameter' });
      }

      const discordId = db.verifyCode(code.toUpperCase());
      if (!discordId) {
        return res.status(400).json({ success: false, error: 'Invalid or expired code' });
      }

      db.linkAccount(discordId, minecraft);

      // Send DM
      try {
        const user = await bot.users.fetch(discordId);
        if (user) {
          const { EmbedBuilder } = require('discord.js');
          const embed = new EmbedBuilder()
            .setTitle('✅ Account Linked!')
            .setDescription(
              `Your Discord account has been linked to your Minecraft account!\n\n` +
              `**Minecraft name:** \`${minecraft}\``
            )
            .setColor(0x2ecc71);
          await user.send({ embeds: [embed] }).catch(() => {});
        }
      } catch (e) {
        console.warn('[Server] Could not send DM:', e.message);
      }

      res.json({ success: true, discord_id: discordId, minecraft });
    } catch (e) {
      console.error('[Server] /api/link/verify error:', e);
      res.status(500).json({ success: false, error: e.message });
    }
  });

  // GET /api/v1/tier/:username
  app.get('/api/v1/tier/:username', (req, res) => {
    const { username } = req.params;
    if (!username || username.length > 64) {
      return res.status(400).json({ error: 'Invalid username' });
    }

    const tests = db.getTestsByUsername(username);
    if (!tests.length) {
      return res.status(404).json({ error: 'Player not found' });
    }

    const tiers = {};
    let highestRank = 'Unranked';
    let highestGamemode = null;
    let highestPoints = 0;

    for (const t of tests) {
      tiers[t.mode] = t.rank;
      const pts = POINTS[t.rank] || 0;
      if (pts > highestPoints) {
        highestPoints = pts;
        highestRank = t.rank;
        highestGamemode = t.mode;
      }
    }

    res.json({
      username: tests[0].username,
      tiers,
      highest: highestRank,
      highest_gamemode: highestGamemode,
    });
  });

  // GET /api/v1/tier/:username/:gamemode
  app.get('/api/v1/tier/:username/:gamemode', (req, res) => {
    const { username, gamemode } = req.params;
    if (!username || username.length > 64) {
      return res.status(400).json({ error: 'Invalid username' });
    }

    const modeDisplay = getGamemodeDisplay(gamemode);
    const test = db.getTestByUsernameAndMode(username, modeDisplay) ||
      db.getTestByUsernameAndMode(username, gamemode);

    res.json({
      username,
      gamemode: modeDisplay || gamemode,
      rank: test ? test.rank : 'Unranked',
    });
  });

  // GET /api/v1/leaderboard/:gamemode
  app.get('/api/v1/leaderboard/:gamemode', (req, res) => {
    const { gamemode } = req.params;
    const modeDisplay = getGamemodeDisplay(gamemode) || gamemode;
    const tests = db.getTestsByMode(modeDisplay);

    if (!tests.length) {
      return res.status(404).json({ error: 'No data for this gamemode' });
    }

    const players = tests
      .sort((a, b) => (POINTS[b.rank] || 0) - (POINTS[a.rank] || 0))
      .map(t => ({ username: t.username, rank: t.rank }));

    res.json({ gamemode: modeDisplay, players });
  });

  // Find available port
  const basePort = parseInt(process.env.PORT || '8080', 10);
  for (let port = basePort; port < basePort + 10; port++) {
    try {
      await new Promise((resolve, reject) => {
        const server = app.listen(port, '0.0.0.0', () => {
          console.log(`[Server] HTTP server running on port ${port}`);
          resolve();
        });
        server.on('error', reject);
      });
      return;
    } catch {
      // Try next port
    }
  }
  console.warn(`[Server] Could not start on ports ${basePort}–${basePort + 9}`);
}

module.exports = { startServer };
