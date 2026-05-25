import os
import asyncio
import aiosqlite
from typing import Dict, Any, List, Optional

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "tierlist.db"))

_db: aiosqlite.Connection | None = None


async def init_db():
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA foreign_keys=ON")

    await _db.executescript("""
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
            discord_id INTEGER NOT NULL UNIQUE,
            minecraft_name TEXT NOT NULL,
            linked_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_linked_discord ON linked_accounts(discord_id);
        CREATE INDEX IF NOT EXISTS idx_linked_minecraft ON linked_accounts(minecraft_name);

        CREATE TABLE IF NOT EXISTS pending_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_pending_code ON pending_codes(code);
    """)
    await _db.commit()
    print(f"Database initialized: {DB_PATH}")


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None


def _row_to_dict(row) -> Dict[str, Any]:
    return dict(row) if row else {}


async def db_select(table: str, filters: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    where, params = "", []
    if filters:
        clauses = [f"{k} = ?" for k in filters]
        where = " WHERE " + " AND ".join(clauses)
        params = list(filters.values())
    async with _db.execute(f"SELECT * FROM {table}{where}", params) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def db_insert(table: str, data: Dict[str, Any]) -> bool:
    cols = ", ".join(data.keys())
    placeholders = ", ".join("?" * len(data))
    try:
        await _db.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})", list(data.values()))
        await _db.commit()
        return True
    except Exception as e:
        print(f"db_insert error: {e}")
        return False


async def db_update(table: str, data: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    set_clause = ", ".join(f"{k} = ?" for k in data)
    where_clause = " AND ".join(f"{k} = ?" for k in filters)
    params = list(data.values()) + list(filters.values())
    try:
        await _db.execute(f"UPDATE {table} SET {set_clause} WHERE {where_clause}", params)
        await _db.commit()
        return True
    except Exception as e:
        print(f"db_update error: {e}")
        return False


async def db_delete(table: str, filters: Dict[str, Any]) -> bool:
    where_clause = " AND ".join(f"{k} = ?" for k in filters)
    try:
        await _db.execute(f"DELETE FROM {table} WHERE {where_clause}", list(filters.values()))
        await _db.commit()
        return True
    except Exception as e:
        print(f"db_delete error: {e}")
        return False


async def db_upsert_test(username: str, mode: str, rank: str, tester_id: str, tester_name: str, ts: int, account_type: str = None) -> bool:
    try:
        await _db.execute("""
            INSERT INTO tests (username, mode, rank, testerId, testerName, ts, accountType)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username, mode) DO UPDATE SET
                rank = excluded.rank,
                testerId = excluded.testerId,
                testerName = excluded.testerName,
                ts = excluded.ts,
                accountType = excluded.accountType
        """, (username, mode, rank, tester_id, tester_name, ts, account_type))
        await _db.commit()
        return True
    except Exception as e:
        print(f"db_upsert_test error: {e}")
        return False


async def db_delete_test(test_id: str) -> bool:
    try:
        await _db.execute("DELETE FROM tests WHERE id = ?", (int(test_id),))
        await _db.commit()
        return True
    except Exception as e:
        print(f"db_delete_test error: {e}")
        return False


# Compat shims for code that used the Supabase helpers
async def supabase_select(table: str, filters: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    return await db_select(table, filters)

async def supabase_insert(table: str, data: Dict[str, Any]) -> bool:
    return await db_insert(table, data)

async def supabase_upsert(table: str, data: Dict[str, Any]) -> bool:
    return await db_insert(table, data)

async def supabase_update(table: str, data: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    return await db_update(table, data, filters)

async def supabase_delete(table: str, filters: Dict[str, Any]) -> bool:
    return await db_delete(table, filters)
