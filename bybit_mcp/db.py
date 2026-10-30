import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet


def token():
    return secrets.token_urlsafe(32)


def digest(value: str):
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path: Path, key: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fernet = Fernet(key.encode())
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS records (
          kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
          expires REAL NOT NULL DEFAULT 0, PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY, at REAL NOT NULL, profile TEXT, action TEXT,
          outcome TEXT, detail TEXT);
        """)

    def put(self, kind, id, data, ttl=0):
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?)", (
                kind, id, json.dumps(data, ensure_ascii=False), time.time() + ttl if ttl else 0))

    def get(self, kind, id, consume=False):
        with self.lock, self.conn:
            row = self.conn.execute("SELECT data,expires FROM records WHERE kind=? AND id=?", (kind, id)).fetchone()
            if not row:
                return None
            if consume or (row[1] and row[1] < time.time()):
                self.conn.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, id))
            if row[1] and row[1] < time.time():
                return None
            return json.loads(row[0])

    def delete(self, kind, id):
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, id))

    def insert_once(self, kind, id, data, ttl=0):
        """Durable compare-and-insert used before sending a financial mutation."""
        with self.lock, self.conn:
            cur = self.conn.execute("INSERT OR IGNORE INTO records VALUES (?,?,?,?)", (
                kind, id, json.dumps(data, ensure_ascii=False), time.time() + ttl if ttl else 0))
            return cur.rowcount == 1

    def list(self, kind):
        with self.lock:
            rows = self.conn.execute("SELECT id,data FROM records WHERE kind=? AND (expires=0 OR expires>?)", (kind, time.time())).fetchall()
            return [{"id": r[0], **json.loads(r[1])} for r in rows]

    def encrypt(self, value):
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value):
        return self.fernet.decrypt(value.encode()).decode()

    def save_profile(self, name, environment, api_key, api_secret, scopes, usdt_only=True):
        ident = digest(environment + ":" + api_key)[:24]
        previous = self.get("profile", ident) or {}
        value = {
            "name": name[:100] or environment,
            "environment": environment,
            "api_key": self.encrypt(api_key), "api_secret": self.encrypt(api_secret),
            "key_hint": "••••" + api_key[-4:],
            "scopes": sorted(set(scopes)), "usdt_only": bool(usdt_only),
            "enabled": True, "created_at": previous.get("created_at", time.time()),
            "updated_at": time.time(),
        }
        self.put("profile", ident, value)
        return {"id": ident, **value}

    def profile(self, ident):
        value = self.get("profile", ident)
        return {"id": ident, **value} if value else None

    @staticmethod
    def public_profile(p):
        return {k: v for k, v in p.items() if k not in {"api_key", "api_secret"}}

    def audit(self, profile, action, outcome, detail=""):
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO audit(at,profile,action,outcome,detail) VALUES (?,?,?,?,?)", (
                time.time(), profile, action, outcome, detail[:500]))
            self.conn.execute("DELETE FROM audit WHERE id < (SELECT MAX(id)-10000 FROM audit)")

    def audits(self, limit=80):
        with self.lock:
            cur = self.conn.execute("SELECT id,at,profile,action,outcome,detail FROM audit ORDER BY id DESC LIMIT ?", (limit,))
            keys = [x[0] for x in cur.description]
            return [dict(zip(keys, r)) for r in cur.fetchall()]

    def cleanup(self):
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM records WHERE expires>0 AND expires<?", (time.time(),))
