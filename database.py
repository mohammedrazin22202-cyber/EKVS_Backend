"""
Storage layer for the Food Decider app.

Design:
- SQLite (food_decider.db) is the fast local cache every request reads from,
  and it also works fully offline.
- MongoDB Atlas is the shared source of truth so you and your friend see
  each other's places/items/history across devices.
- Every write goes to SQLite immediately. The app then tries to push it to
  Mongo right away. If Mongo is unreachable (no internet, bad URI, etc.),
  the change is marked "unsynced" in SQLite and gets pushed automatically
  next time /api/sync/push runs (or next successful write).
- /api/sync/pull downloads everything from Mongo and merges it into the
  local SQLite cache, so you can pick up your friend's additions.
"""
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "food_decider.db")
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "food_decider")
DEVICE_OWNER = os.getenv("DEVICE_OWNER", "me")

_mongo_client = None
_mongo_ok = False


def get_mongo_db():
    """Lazily connect to Mongo. Returns None if not configured/unreachable."""
    global _mongo_client, _mongo_ok
    if not MONGO_URI or MONGO_URI.startswith("mongodb+srv://<username>"):
        return None
    if _mongo_client is None:
        try:
            from pymongo import MongoClient
            _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=4000)
            _mongo_client.admin.command("ping")
            _mongo_ok = True
        except Exception:
            _mongo_client = None
            _mongo_ok = False
            return None
    return _mongo_client[MONGO_DB_NAME] if _mongo_client is not None else None


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def new_id():
    return uuid.uuid4().hex[:12]


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS places (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                area TEXT,
                cuisine TEXT,
                price_range TEXT,
                notes TEXT,
                created_at TEXT,
                updated_at TEXT,
                synced INTEGER DEFAULT 0,
                deleted INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                place_id TEXT NOT NULL,
                name TEXT NOT NULL,
                price REAL NOT NULL,
                category TEXT,
                tags TEXT,
                created_at TEXT,
                updated_at TEXT,
                synced INTEGER DEFAULT 0,
                deleted INTEGER DEFAULT 0,
                rating INTEGER DEFAULT 0,
                FOREIGN KEY (place_id) REFERENCES places(id)
            );

            CREATE TABLE IF NOT EXISTS history (
                id TEXT PRIMARY KEY,
                place_id TEXT,
                place_name TEXT,
                item_id TEXT,
                item_name TEXT,
                people INTEGER,
                amount REAL,
                who TEXT,
                eaten_on TEXT,
                created_at TEXT,
                synced INTEGER DEFAULT 0,
                deleted INTEGER DEFAULT 0,
                budget REAL DEFAULT 0
            );
            """
        )
        # Migrate existing database if needed
        try:
            conn.execute("ALTER TABLE history ADD COLUMN deleted INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            conn.execute("ALTER TABLE items ADD COLUMN rating INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            conn.execute("ALTER TABLE history ADD COLUMN budget REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists


# ---------------------------------------------------------------------------
# Generic push/pull sync helpers
# ---------------------------------------------------------------------------

def push_unsynced(collection_name: str, table: str):
    """Push any row with synced=0 in `table` up to Mongo collection `collection_name`."""
    mdb = get_mongo_db()
    if mdb is None:
        return {"pushed": 0, "mongo_available": False}
    pushed = 0
    with get_conn() as conn:
        rows = conn.execute(f"SELECT * FROM {table} WHERE synced = 0").fetchall()
        for row in rows:
            doc = dict(row)
            doc["_id"] = doc["id"]
            if "deleted" in doc and doc["deleted"] == 1 and table == "history":
                # For history, if it is deleted, hard-delete it from Mongo so the cluster stays clean
                mdb[collection_name].delete_one({"_id": doc["id"]})
                conn.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
            else:
                mdb[collection_name].replace_one({"_id": doc["id"]}, doc, upsert=True)
                conn.execute(f"UPDATE {table} SET synced = 1 WHERE id = ?", (row["id"],))
            pushed += 1
    return {"pushed": pushed, "mongo_available": True}


def push_all():
    results = {}
    results["places"] = push_unsynced("places", "places")
    results["items"] = push_unsynced("items", "items")
    results["history"] = push_unsynced("history", "history")
    return results


def pull_all():
    """Pull everything from Mongo and merge into local SQLite (Mongo wins on conflict by updated_at)."""
    mdb = get_mongo_db()
    if mdb is None:
        return {"pulled": 0, "mongo_available": False}
    pulled = 0
    with get_conn() as conn:
        for place in mdb.places.find({}):
            place["id"] = place.pop("_id")
            existing = conn.execute("SELECT updated_at FROM places WHERE id=?", (place["id"],)).fetchone()
            if not existing or (place.get("updated_at", "") >= existing["updated_at"]):
                conn.execute(
                    """INSERT INTO places (id, name, area, cuisine, price_range, notes,
                       created_at, updated_at, synced, deleted)
                       VALUES (?,?,?,?,?,?,?,?,1,?)
                       ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name, area=excluded.area, cuisine=excluded.cuisine,
                       price_range=excluded.price_range, notes=excluded.notes,
                       updated_at=excluded.updated_at, synced=1, deleted=excluded.deleted""",
                    (place["id"], place.get("name"), place.get("area"), place.get("cuisine"),
                     place.get("price_range"), place.get("notes"), place.get("created_at"),
                     place.get("updated_at"), place.get("deleted", 0)),
                )
                pulled += 1

        for item in mdb.items.find({}):
            item["id"] = item.pop("_id")
            existing = conn.execute("SELECT updated_at FROM items WHERE id=?", (item["id"],)).fetchone()
            if not existing or (item.get("updated_at", "") >= existing["updated_at"]):
                conn.execute(
                    """INSERT INTO items (id, place_id, name, price, category, tags,
                       created_at, updated_at, synced, deleted, rating)
                       VALUES (?,?,?,?,?,?,?,?,1,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                       place_id=excluded.place_id, name=excluded.name, price=excluded.price,
                       category=excluded.category, tags=excluded.tags,
                       updated_at=excluded.updated_at, synced=1, deleted=excluded.deleted, rating=excluded.rating""",
                    (item["id"], item.get("place_id"), item.get("name"), item.get("price"),
                     item.get("category"), item.get("tags"), item.get("created_at"),
                     item.get("updated_at"), item.get("deleted", 0), item.get("rating", 0)),
                )
                pulled += 1

        for h in mdb.history.find({}):
            h["id"] = h.pop("_id")
            existing = conn.execute("SELECT id, deleted FROM history WHERE id=?", (h["id"],)).fetchone()
            if not existing:
                conn.execute(
                    """INSERT INTO history (id, place_id, place_name, item_id, item_name,
                       people, amount, who, eaten_on, created_at, synced, deleted, budget)
                       VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (h["id"], h.get("place_id"), h.get("place_name"), h.get("item_id"),
                     h.get("item_name"), h.get("people"), h.get("amount"), h.get("who"),
                     h.get("eaten_on"), h.get("created_at"), h.get("deleted", 0), h.get("budget", 0.0)),
                )
                pulled += 1
            else:
                # If local exists but Mongo says it's deleted, update local
                if h.get("deleted", 0) and not existing["deleted"]:
                    conn.execute("UPDATE history SET deleted = 1, synced = 1 WHERE id = ?", (h["id"],))
                # Update budget locally if changed
                conn.execute("UPDATE history SET budget = ? WHERE id = ?", (h.get("budget", 0.0), h["id"]))
    return {"pulled": pulled, "mongo_available": True}


def try_push_single(collection_name: str, table: str, row_id: str):
    """Best-effort immediate push of a single row right after writing it locally."""
    mdb = get_mongo_db()
    if mdb is None:
        return False
    with get_conn() as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if not row:
            # If the row is deleted locally and it is a history entry, make sure it is hard-deleted in Mongo too
            if table == "history":
                try:
                    mdb[collection_name].delete_one({"_id": row_id})
                    return True
                except Exception:
                    return False
            return False
        doc = dict(row)
        doc["_id"] = doc["id"]
        try:
            if "deleted" in doc and doc["deleted"] == 1 and table == "history":
                mdb[collection_name].delete_one({"_id": doc["id"]})
                conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
            else:
                mdb[collection_name].replace_one({"_id": doc["id"]}, doc, upsert=True)
                conn.execute(f"UPDATE {table} SET synced = 1 WHERE id = ?", (row_id,))
            return True
        except Exception:
            return False
