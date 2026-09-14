import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "opsbot.db"

def connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    return sqlite3.connect(DB_PATH)

def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS verified_users (
            discord_user_id INTEGER PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            verified_at TEXT NOT NULL,
            last_sync_at TEXT
        );
        CREATE TABLE IF NOT EXISTS verification_codes (
            discord_user_id INTEGER PRIMARY KEY,
            email TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            attempts INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            last_sent_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            discord_user_id INTEGER,
            email TEXT,
            action TEXT NOT NULL,
            details TEXT
        );
        """)

def now():
    return datetime.now(timezone.utc).isoformat()

def audit(user_id, email, action, details=""):
    with connect() as con:
        con.execute(
            "INSERT INTO audit_log(timestamp,discord_user_id,email,action,details) VALUES(?,?,?,?,?)",
            (now(), user_id, email, action, details)
        )

def get_verified_user(user_id):
    with connect() as con:
        row = con.execute(
            "SELECT discord_user_id,email,verified_at,last_sync_at FROM verified_users WHERE discord_user_id=?",
            (user_id,)
        ).fetchone()
    return row

def get_verified_by_email(email):
    with connect() as con:
        return con.execute(
            "SELECT discord_user_id,email FROM verified_users WHERE lower(email)=lower(?)",
            (email,)
        ).fetchone()

def save_verified_user(user_id, email):
    with connect() as con:
        con.execute("""
        INSERT INTO verified_users(discord_user_id,email,verified_at,last_sync_at)
        VALUES(?,?,?,NULL)
        ON CONFLICT(discord_user_id) DO UPDATE SET email=excluded.email, verified_at=excluded.verified_at
        """, (user_id, email, now()))

def list_verified_users():
    with connect() as con:
        return con.execute("SELECT discord_user_id,email FROM verified_users").fetchall()

def touch_sync(user_id):
    with connect() as con:
        con.execute("UPDATE verified_users SET last_sync_at=? WHERE discord_user_id=?", (now(), user_id))

def save_code(user_id, email, code_hash, expires_at):
    ts = now()
    with connect() as con:
        con.execute("""
        INSERT INTO verification_codes(discord_user_id,email,code_hash,expires_at,attempts,created_at,last_sent_at)
        VALUES(?,?,?,?,0,?,?)
        ON CONFLICT(discord_user_id) DO UPDATE SET
          email=excluded.email, code_hash=excluded.code_hash, expires_at=excluded.expires_at,
          attempts=0, created_at=excluded.created_at, last_sent_at=excluded.last_sent_at
        """, (user_id,email,code_hash,expires_at,ts,ts))

def get_code(user_id):
    with connect() as con:
        return con.execute("""
        SELECT email,code_hash,expires_at,attempts,last_sent_at
        FROM verification_codes WHERE discord_user_id=?
        """, (user_id,)).fetchone()

def increment_attempts(user_id):
    with connect() as con:
        con.execute("UPDATE verification_codes SET attempts=attempts+1 WHERE discord_user_id=?", (user_id,))

def delete_code(user_id):
    with connect() as con:
        con.execute("DELETE FROM verification_codes WHERE discord_user_id=?", (user_id,))
