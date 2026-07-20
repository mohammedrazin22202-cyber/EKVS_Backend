from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import random
import database as db
from models import PlaceIn, PlaceUpdate, ItemIn, ItemUpdate, SuggestRequest, HistoryIn, PollCreateRequest, VoteRequest
from suggest import generate_suggestions

app = FastAPI(title="EKVS Food Decider API")

# Enable CORS for cross-origin requests from Vercel or local frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

db.init_db()


@app.on_event("startup")
def startup_pull():
    # Best-effort: grab whatever your friend added on their device, if Mongo is reachable.
    try:
        db.pull_all()
    except Exception:
        pass


@app.get("/")
def root():
    return {
        "status": "online",
        "service": "EKVS Food Decider API",
        "docs": "/docs"
    }


# ---------------------------------------------------------------------------
# Places
# ---------------------------------------------------------------------------

@app.get("/api/places")
def list_places():
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM places WHERE deleted = 0 ORDER BY name").fetchall()
        return [dict(r) for r in rows]


@app.post("/api/places")
def add_place(place: PlaceIn):
    pid = db.new_id()
    ts = db.now_iso()
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO places (id, name, area, cuisine, price_range, notes, created_at, updated_at, synced, deleted)
               VALUES (?,?,?,?,?,?,?,?,0,0)""",
            (pid, place.name, place.area, place.cuisine, place.price_range, place.notes, ts, ts),
        )
    db.try_push_single("places", "places", pid)
    return {"id": pid, "status": "created"}


@app.put("/api/places/{place_id}")
def update_place(place_id: str, place: PlaceUpdate):
    updates = {k: v for k, v in place.dict().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields to update")
    updates["updated_at"] = db.now_iso()
    updates["synced"] = 0
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with db.get_conn() as conn:
        existing = conn.execute("SELECT id FROM places WHERE id=? AND deleted=0", (place_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Place not found")
        conn.execute(f"UPDATE places SET {set_clause} WHERE id = ?", (*updates.values(), place_id))
    db.try_push_single("places", "places", place_id)
    return {"id": place_id, "status": "updated"}


@app.delete("/api/places/{place_id}")
def delete_place(place_id: str):
    ts = db.now_iso()
    with db.get_conn() as conn:
        conn.execute("UPDATE places SET deleted = 1, updated_at = ?, synced = 0 WHERE id = ?", (ts, place_id))
    db.try_push_single("places", "places", place_id)
    return {"id": place_id, "status": "deleted"}


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

@app.get("/api/places/{place_id}/items")
def list_items_for_place(place_id: str):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE place_id = ? AND deleted = 0 ORDER BY name", (place_id,)
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/items")
def list_all_items():
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT items.*, places.name as place_name FROM items
               JOIN places ON items.place_id = places.id
               WHERE items.deleted = 0 AND places.deleted = 0 ORDER BY places.name, items.name"""
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/places/{place_id}/items")
def add_item(place_id: str, item: ItemIn):
    with db.get_conn() as conn:
        place = conn.execute("SELECT id FROM places WHERE id=? AND deleted=0", (place_id,)).fetchone()
        if not place:
            raise HTTPException(404, "Place not found")
    iid = db.new_id()
    ts = db.now_iso()
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO items (id, place_id, name, price, category, tags, created_at, updated_at, synced, deleted, meal_role, paired_item_id)
               VALUES (?,?,?,?,?,?,?,?,0,0,?,?)""",
            (iid, place_id, item.name, item.price, item.category, item.tags, ts, ts, item.meal_role or "main", item.paired_item_id or ""),
        )
    db.try_push_single("items", "items", iid)
    return {"id": iid, "status": "created"}


@app.put("/api/items/{item_id}")
def update_item(item_id: str, item: ItemUpdate):
    updates = {k: v for k, v in item.dict().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields to update")
    updates["updated_at"] = db.now_iso()
    updates["synced"] = 0
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with db.get_conn() as conn:
        existing = conn.execute("SELECT id FROM items WHERE id=? AND deleted=0", (item_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Item not found")
        conn.execute(f"UPDATE items SET {set_clause} WHERE id = ?", (*updates.values(), item_id))
    db.try_push_single("items", "items", item_id)
    return {"id": item_id, "status": "updated"}


@app.delete("/api/items/{item_id}")
def delete_item(item_id: str):
    ts = db.now_iso()
    with db.get_conn() as conn:
        conn.execute("UPDATE items SET deleted = 1, updated_at = ?, synced = 0 WHERE id = ?", (ts, item_id))
    db.try_push_single("items", "items", item_id)
    return {"id": item_id, "status": "deleted"}


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

@app.post("/api/suggest")
def suggest(req: SuggestRequest):
    with db.get_conn() as conn:
        has_items = conn.execute("SELECT COUNT(*) as c FROM items WHERE deleted=0").fetchone()["c"]
    if has_items == 0:
        raise HTTPException(400, "No places/items added yet. Add some places and items first.")

    results = generate_suggestions(
        budget=req.budget,
        people=req.people,
        preference=req.preference,
        additional_info=req.additional_info,
        area=req.area,
        variety=req.variety if req.variety is not None else 1,
        who=req.who or "",
        count=req.count,
        concurrency_control=req.concurrency_control,
    )
    if not results:
        raise HTTPException(404, "Nothing fits that budget for that many people. Try raising the budget.")
    return {"suggestions": results}


@app.post("/api/suggest/upgrade")
def suggest_upgrade(req: SuggestRequest):
    max_upgrade_budget = req.budget + max(50.0, req.budget * 0.25)
    results = generate_suggestions(
        budget=max_upgrade_budget,
        people=req.people,
        preference=req.preference,
        additional_info=req.additional_info,
        area=req.area,
        variety=req.variety if req.variety is not None else 1,
        who=req.who or "",
        count=40,
        concurrency_control=req.concurrency_control,
    )
    upgrade_candidates = [c for c in results if c["expected_amount"] > req.budget]
    if not upgrade_candidates:
        raise HTTPException(404, "No upgrade suggestions found within a small top-up amount.")
    
    best_upgrade = upgrade_candidates[0]
    extra_needed = round(best_upgrade["expected_amount"] - req.budget, 2)
    return {
        "upgrade": best_upgrade,
        "current_budget": req.budget,
        "extra_needed": extra_needed,
        "new_budget": best_upgrade["expected_amount"]
    }


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

@app.get("/api/history")
def get_history(days: int = 30):
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM history WHERE eaten_on >= ? AND deleted = 0 ORDER BY eaten_on DESC", (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]


@app.delete("/api/history/{history_id}")
def delete_history_entry(history_id: str):
    with db.get_conn() as conn:
        existing = conn.execute("SELECT id FROM history WHERE id=?", (history_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "History entry not found")
        conn.execute("UPDATE history SET deleted = 1, synced = 0 WHERE id = ?", (history_id,))
    db.try_push_single("history", "history", history_id)
    return {"id": history_id, "status": "deleted"}


@app.post("/api/history")
def add_history(entry: HistoryIn):
    if entry.place_id == "custom" and entry.item_id == "custom":
        # Check weekly limit (once in the last 7 days per profile)
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        who_val = entry.who or db.DEVICE_OWNER
        with db.get_conn() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) as c FROM history WHERE place_id = 'custom' AND who = ? AND eaten_on >= ? AND deleted = 0",
                (who_val, cutoff)
            ).fetchone()["c"]
            if cnt > 0:
                raise HTTPException(400, "You can only register one custom meal per week!")
        
        place_name = entry.place_name or "Custom Restaurant"
        formatted_name = entry.item_name or "Custom Meal"
    else:
        with db.get_conn() as conn:
            place = conn.execute("SELECT name FROM places WHERE id=?", (entry.place_id,)).fetchone()
            if not place:
                raise HTTPException(404, "Place not found")
            
            # Support comma-separated item IDs (e.g. from combos)
            item_ids = entry.item_id.split(",") if entry.item_id else []
            item_names = []
            for iid in item_ids:
                item_row = conn.execute("SELECT name FROM items WHERE id=?", (iid,)).fetchone()
                if item_row:
                    item_names.append(item_row["name"])
            
            if not item_names:
                raise HTTPException(404, "Items not found")
                
            # Format combined item name, e.g. "2x Chicken Puff + 1x Falooda"
            from collections import Counter
            counts = Counter(item_names)
            formatted_name = " + ".join(f"{count}x {name}" if count > 1 else name for name, count in counts.items())
            place_name = place["name"]

    hid = db.new_id()
    ts = db.now_iso()
    eaten_on = entry.eaten_on or ts
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO history (id, place_id, place_name, item_id, item_name, people, amount, who, eaten_on, created_at, synced, budget)
               VALUES (?,?,?,?,?,?,?,?,?,?,0,?)""",
            (hid, entry.place_id, place_name, entry.item_id, formatted_name,
             entry.people, entry.amount, entry.who or db.DEVICE_OWNER, eaten_on, ts, entry.budget or 0.0),
        )
    db.try_push_single("history", "history", hid)
    return {"id": hid, "status": "created"}


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

@app.post("/api/sync/push")
def sync_push():
    return db.push_all()


@app.post("/api/sync/pull")
def sync_pull():
    return db.pull_all()


@app.get("/api/status")
def status():
    mdb = db.get_mongo_db()
    return {"mongo_connected": mdb is not None, "device_owner": db.DEVICE_OWNER}


# ---------------------------------------------------------------------------
# Group Polls
# ---------------------------------------------------------------------------

@app.post("/api/polls")
def create_poll(req: PollCreateRequest):
    mdb = db.get_mongo_db()
    if mdb is None:
        raise HTTPException(400, "Group dining polls require MongoDB cloud connection.")
        
    with db.get_conn() as conn:
        has_items = conn.execute("SELECT COUNT(*) as c FROM items WHERE deleted=0").fetchone()["c"]
    if has_items == 0:
        raise HTTPException(400, "No places/items added yet. Add some places and items first.")

    # Generate up to 3 candidate combinations
    candidates = generate_suggestions(
        budget=req.budget,
        people=req.people,
        preference=req.preference,
        additional_info=req.additional_info,
        area=req.area,
        variety=req.variety if req.variety is not None else 1,
        who="",
        count=3,
        concurrency_control=req.concurrency_control,
    )
    if not candidates:
        raise HTTPException(404, "No menu items fit this budget to generate poll candidates.")

    # Assign identifier to each candidate option
    for idx, c in enumerate(candidates):
        c["id"] = f"cand{idx}"

    room_code = str(random.randint(1000, 9999))
    poll = {
        "_id": room_code,
        "candidates": candidates,
        "votes": {c["id"]: 0 for c in candidates},
        "voted_users": [],
        "active": True,
        "winner": None
    }
    
    mdb.polls.insert_one(poll)
    return {"code": room_code, "poll": poll}


@app.get("/api/polls/{code}")
def get_poll(code: str):
    mdb = db.get_mongo_db()
    if mdb is None:
        raise HTTPException(400, "Mongo offline")
    poll = mdb.polls.find_one({"_id": code})
    if not poll:
        raise HTTPException(404, "Poll room not found")
    poll["code"] = poll.pop("_id")
    return poll


@app.post("/api/polls/{code}/vote")
def vote_poll(code: str, req: VoteRequest):
    mdb = db.get_mongo_db()
    if mdb is None:
        raise HTTPException(400, "Mongo offline")
    poll = mdb.polls.find_one({"_id": code})
    if not poll:
        raise HTTPException(404, "Poll room not found")
    if not poll.get("active", True):
        raise HTTPException(400, "This poll is already closed!")

    # Check for double voting
    who_normalized = req.who.strip().lower()
    voted_users_normalized = [u.strip().lower() for u in poll.get("voted_users", [])]
    if who_normalized in voted_users_normalized:
        raise HTTPException(400, f"Profile '{req.who}' has already cast a vote!")

    if req.candidate_id not in poll["votes"]:
        raise HTTPException(400, "Invalid candidate choice")

    result = mdb.polls.update_one(
        {"_id": code, "active": True},
        {
            "$inc": {f"votes.{req.candidate_id}": 1},
            "$push": {"voted_users": req.who}
        }
    )
    if result.matched_count == 0:
        raise HTTPException(400, "This poll is already closed!")
    return {"status": "voted"}


@app.post("/api/polls/{code}/close")
def close_poll(code: str):
    mdb = db.get_mongo_db()
    if mdb is None:
        raise HTTPException(400, "Mongo offline")
    poll = mdb.polls.find_one({"_id": code})
    if not poll:
        raise HTTPException(404, "Poll room not found")
    if not poll.get("active", True):
        return {"status": "already closed", "winner": poll.get("winner")}

    votes = poll["votes"]
    max_votes = -1
    winners = []
    for cid, val in votes.items():
        if val > max_votes:
            max_votes = val
            winners = [cid]
        elif val == max_votes:
            winners.append(cid)

    # Tie break randomly
    winning_cid = random.choice(winners)
    winner_cand = next(c for c in poll["candidates"] if c["id"] == winning_cid)

    mdb.polls.update_one(
        {"_id": code},
        {"$set": {"active": False, "winner": winner_cand}}
    )
    return {"status": "closed", "winner": winner_cand}
