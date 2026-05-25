'use strict';
const {
  STAFF_ROLE_ID,
  EXTRA_STAFF_ROLE_IDS,
  ALLOWED_USER_IDS,
  POINTS,
  GAMEMODE_TESTER_ROLES,
} = require('./config');

function isStaff(member) {
  if (!member) return false;
  if (member.permissions && member.permissions.has('Administrator')) return true;
  if (ALLOWED_USER_IDS.includes(member.id)) return true;
  if (STAFF_ROLE_ID && member.roles.cache.has(STAFF_ROLE_ID)) return true;
  if (EXTRA_STAFF_ROLE_IDS.some(id => id && member.roles.cache.has(id))) return true;
  return false;
}

function isAdmin(member) {
  if (!member) return false;
  return !!(member.permissions && member.permissions.has('Administrator'));
}

function isGamemodeTester(member, gamemode) {
  if (!member) return false;
  if (isStaff(member)) return true;
  const testerRoleId = GAMEMODE_TESTER_ROLES[(gamemode || '').toLowerCase()];
  if (testerRoleId && member.roles.cache.has(testerRoleId)) return true;
  // Also check for role named "{gamemode} Tester"
  const gmDisplay = gamemode.charAt(0).toUpperCase() + gamemode.slice(1);
  return member.roles.cache.some(r =>
    r.name === `${gmDisplay} Tester` || r.name.toLowerCase() === `${gamemode.toLowerCase()} tester`
  );
}

function canJoinQueue(rank) {
  // Points 0-4: Unranked, LT5, HT5, LT4, HT4
  return (POINTS[rank] || 0) <= 4;
}

function canOpenTicket(rank) {
  // LT3+ (points >= 6)
  return (POINTS[rank] || 0) >= 6;
}

module.exports = { isStaff, isAdmin, isGamemodeTester, canJoinQueue, canOpenTicket };
