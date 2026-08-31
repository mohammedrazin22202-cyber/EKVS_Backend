import os
import io
import sys
import tempfile
import pytest
from pydantic import BaseModel
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

# Ensure backend directory is in path
sys.path.insert(0, os.path.dirname(__file__))

import database as db

# Create a temporary database for testing
temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
temp_db_path = temp_db.name
temp_db.close()

db.DB_PATH = temp_db_path

from main import app

# Initialize test database tables
db.init_db()

client = TestClient(app)

@pytest.fixture(autouse=True)
def clean_db():
    # Clear tables before each test to have a clean state
    with db.get_conn() as conn:
        conn.execute("DELETE FROM items")
        conn.execute("DELETE FROM places")
        conn.execute("DELETE FROM history")
    yield

def test_root():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "online"

def test_status():
    response = client.get("/api/status")
    assert response.status_code == 200
    assert "mongo_connected" in response.json()

def test_places_crud():
    # 1. Create a place
    place_data = {
        "name": "Test Restaurant",
        "area": "Downtown",
        "cuisine": "Italian",
        "price_range": "mid",
        "notes": "Good pasta"
    }
    response = client.post("/api/places", json=place_data)
    assert response.status_code == 200
    res_json = response.json()
    assert "id" in res_json
    assert res_json["status"] == "created"
    place_id = res_json["id"]

    # 2. List places
    response = client.get("/api/places")
    assert response.status_code == 200
    places = response.json()
    assert len(places) == 1
    assert places[0]["id"] == place_id
    assert places[0]["name"] == "Test Restaurant"

    # 3. Update place
    update_data = {
        "name": "Updated Restaurant",
        "notes": "Excellent pasta"
    }
    response = client.put(f"/api/places/{place_id}", json=update_data)
    assert response.status_code == 200
    assert response.json()["status"] == "updated"

    # Verify update
    response = client.get("/api/places")
    places = response.json()
    assert places[0]["name"] == "Updated Restaurant"
    assert places[0]["notes"] == "Excellent pasta"

    # 4. Delete place
    response = client.delete(f"/api/places/{place_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "deleted"

    # List again should be empty
    response = client.get("/api/places")
    assert len(response.json()) == 0

def test_items_crud():
    # Add place first
    place_response = client.post("/api/places", json={"name": "Pizzeria"})
    place_id = place_response.json()["id"]

    # 1. Add item
    item_data = {
        "name": "Margherita",
        "price": 250.0,
        "category": "veg",
        "tags": "cheese,tomato",
        "meal_role": "main"
    }
    response = client.post(f"/api/places/{place_id}/items", json=item_data)
    assert response.status_code == 200
    res_json = response.json()
    assert "id" in res_json
    item_id = res_json["id"]

    # 2. List items for place
    response = client.get(f"/api/places/{place_id}/items")
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 1
    assert items[0]["id"] == item_id
    assert items[0]["name"] == "Margherita"

    # 3. List all items
    response = client.get("/api/items")
    assert response.status_code == 200
    assert len(response.json()) == 1

    # 4. Update item
    response = client.put(f"/api/items/{item_id}", json={"price": 280.0, "rating": 5})
    assert response.status_code == 200
    assert response.json()["status"] == "updated"

    # Verify update
    response = client.get(f"/api/places/{place_id}/items")
    assert response.json()[0]["price"] == 280.0
    assert response.json()[0]["rating"] == 5

    # 5. Delete item
    response = client.delete(f"/api/items/{item_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "deleted"

    response = client.get(f"/api/places/{place_id}/items")
    assert len(response.json()) == 0

def test_suggestions():
    # Add place and items
    place_resp = client.post("/api/places", json={"name": "Burger Joint", "area": "West"})
    place_id = place_resp.json()["id"]

    # Add a main
    client.post(f"/api/places/{place_id}/items", json={"name": "Cheeseburger", "price": 150.0, "meal_role": "main"})
    # Add a side
    client.post(f"/api/places/{place_id}/items", json={"name": "Fries", "price": 50.0, "meal_role": "side"})

    # Suggest with budget 250 for 1 person
    resp = client.post("/api/suggest", json={
        "budget": 250.0,
        "people": 1,
        "count": 3,
        "concurrency_control": False
    })
    assert resp.status_code == 200
    suggestions = resp.json()["suggestions"]
    assert len(suggestions) > 0
    # The budget fits Cheeseburger + Fries (200) or just Cheeseburger (150)
    assert suggestions[0]["expected_amount"] <= 250.0

    # Suggest upgrade
    resp = client.post("/api/suggest/upgrade", json={
        "budget": 120.0,  # low budget
        "people": 1,
        "concurrency_control": False
    })
    # Upgrade budget would allow the 150 Cheeseburger
    assert resp.status_code == 200
    assert "upgrade" in resp.json()
    assert resp.json()["new_budget"] == 150.0

def test_history():
    # Add place and item
    place_resp = client.post("/api/places", json={"name": "Cafe"})
    place_id = place_resp.json()["id"]
    item_resp = client.post(f"/api/places/{place_id}/items", json={"name": "Coffee", "price": 80.0})
    item_id = item_resp.json()["id"]

    # 1. Add history entry
    hist_resp = client.post("/api/history", json={
        "place_id": place_id,
        "item_id": item_id,
        "people": 1,
        "amount": 80.0,
        "who": "MegaTron"
    })
    assert hist_resp.status_code == 200
    hist_id = hist_resp.json()["id"]

    # 2. Get history
    get_resp = client.get("/api/history")
    assert get_resp.status_code == 200
    assert len(get_resp.json()) == 1
    assert get_resp.json()[0]["item_name"] == "Coffee"

    # 3. Delete history
    del_resp = client.delete(f"/api/history/{hist_id}")
    assert del_resp.status_code == 200

    get_resp = client.get("/api/history")
    assert len(get_resp.json()) == 0

def test_custom_meal_weekly_limit():
    # 1. Add first custom meal
    resp = client.post("/api/history", json={
        "place_id": "custom",
        "item_id": "custom",
        "place_name": "Custom Place",
        "item_name": "Custom Item",
        "people": 1,
        "amount": 100.0,
        "who": "MegaTron"
    })
    assert resp.status_code == 200

    # 2. Add second custom meal within 7 days should fail
    resp = client.post("/api/history", json={
        "place_id": "custom",
        "item_id": "custom",
        "place_name": "Custom Place 2",
        "item_name": "Custom Item 2",
        "people": 1,
        "amount": 120.0,
        "who": "MegaTron"
    })
    assert resp.status_code == 400
    assert "one custom meal per week" in resp.json()["detail"]

    # 3. Add custom meal for a different user should succeed
    resp = client.post("/api/history", json={
        "place_id": "custom",
        "item_id": "custom",
        "place_name": "Custom Place",
        "item_name": "Custom Item",
        "people": 1,
        "amount": 100.0,
        "who": "Friend"
    })
    assert resp.status_code == 200

@patch("database.get_mongo_db")
def test_group_polls(mock_get_mongo_db):
    # Mock MongoDB collections
    mock_db = MagicMock()
    mock_get_mongo_db.return_value = mock_db
    
    # Set up some database items first
    place_resp = client.post("/api/places", json={"name": "Deli"})
    place_id = place_resp.json()["id"]
    client.post(f"/api/places/{place_id}/items", json={"name": "Sandwich", "price": 100.0})

    # Create poll
    poll_data = {
        "budget": 200.0,
        "people": 1,
        "preference": "",
        "additional_info": "",
        "area": "",
        "variety": 1,
        "concurrency_control": False
    }
    
    resp = client.post("/api/polls", json=poll_data)
    assert resp.status_code == 200
    poll_code = resp.json()["code"]
    
    # Mock find_one to return the poll document
    poll_doc = resp.json()["poll"]
    poll_doc["_id"] = poll_code
    mock_db.polls.find_one.return_value = poll_doc

    # Get poll
    resp = client.get(f"/api/polls/{poll_code}")
    assert resp.status_code == 200
    assert resp.json()["code"] == poll_code

    # Vote in poll
    resp = client.post(f"/api/polls/{poll_code}/vote", json={
        "candidate_id": "cand0",
        "who": "MegaTron"
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "voted"

    # Close poll
    # Update mock to show voted user
    poll_doc["voted_users"] = ["MegaTron"]
    poll_doc["votes"]["cand0"] = 1
    resp = client.post(f"/api/polls/{poll_code}/close")
    assert resp.status_code == 200
    assert resp.json()["status"] == "closed"
    assert resp.json()["winner"]["id"] == "cand0"

def test_null_rating_suggestions():
    # Setup database with an item that has rating = NULL in SQLite
    place_resp = client.post("/api/places", json={"name": "Null Rating Place"})
    place_id = place_resp.json()["id"]
    item_resp = client.post(f"/api/places/{place_id}/items", json={"name": "Null Rating Item", "price": 100.0})
    item_id = item_resp.json()["id"]

    # Explicitly set rating to NULL in the SQLite DB
    with db.get_conn() as conn:
        conn.execute("UPDATE items SET rating = NULL WHERE id = ?", (item_id,))

    # Now generate suggestions - this would fail if there's a TypeError with None rating
    resp = client.post("/api/suggest", json={
        "budget": 200.0,
        "people": 1,
        "count": 3,
        "concurrency_control": False
    })
    
    # If the bug exists, this will return 500 / error out due to TypeError
    assert resp.status_code == 200
    suggestions = resp.json()["suggestions"]
    assert len(suggestions) > 0
    assert suggestions[0]["place_id"] == place_id

def test_multi_select_preferences():
    place_resp = client.post("/api/places", json={"name": "Veg Spicy Cafe"})
    place_id = place_resp.json()["id"]
    # Add a veg spicy main item
    client.post(f"/api/places/{place_id}/items", json={"name": "Paneer Tikka", "price": 150.0, "category": "veg", "tags": "spicy", "meal_role": "main"})
    
    # Suggest with multi preference: "veg,spicy"
    resp = client.post("/api/suggest", json={
        "budget": 200.0,
        "people": 1,
        "preference": "veg,spicy",
        "concurrency_control": False
    })
    assert resp.status_code == 200
    suggestions = resp.json()["suggestions"]
    assert len(suggestions) > 0
    assert "Paneer Tikka" in suggestions[0]["item_name"]


def test_leaderboard():
    # Clear history first
    with db.get_conn() as conn:
        conn.execute("DELETE FROM history")
        
    place_resp = client.post("/api/places", json={"name": "Diner"})
    place_id = place_resp.json()["id"]
    item_resp = client.post(f"/api/places/{place_id}/items", json={"name": "Burger", "price": 100.0})
    item_id = item_resp.json()["id"]
    
    # Add eating history for two profiles
    client.post("/api/history", json={
        "place_id": place_id,
        "item_id": item_id,
        "people": 1,
        "amount": 100.0,
        "who": "Alice",
        "budget": 120.0
    })
    
    client.post("/api/history", json={
        "place_id": place_id,
        "item_id": item_id,
        "people": 1,
        "amount": 100.0,
        "who": "Bob",
        "budget": 150.0
    })
    
    resp = client.get("/api/leaderboard")
    assert resp.status_code == 200
    leaderboard = resp.json()
    assert len(leaderboard) == 2
    
    bobs_record = next(r for r in leaderboard if r["who"] == "Bob")
    assert bobs_record["total_meals"] == 1
    assert bobs_record["total_spent"] == 100.0
    assert bobs_record["total_savings"] == 50.0


@patch("database.get_mongo_db")
def test_group_polls_chat(mock_get_mongo_db):
    mock_db = MagicMock()
    mock_get_mongo_db.return_value = mock_db
    
    poll_code = "1234"
    poll_doc = {
        "_id": poll_code,
        "active": True,
        "candidates": [],
        "votes": {},
        "chat": []
    }
    mock_db.polls.find_one.return_value = poll_doc
    
    resp = client.post(f"/api/polls/{poll_code}/chat", json={
        "who": "MegaTron",
        "message": "Hello friends!"
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"
    assert resp.json()["message"]["message"] == "Hello friends!"


class ChatMessageRequest(BaseModel):
    who: str
    message: str


def test_csv_export_import():
    # Add place and item in DB
    place_resp = client.post("/api/places", json={"name": "CSV Diner", "area": "North", "notes": "Good food"})
    place_id = place_resp.json()["id"]
    client.post(f"/api/places/{place_id}/items", json={
        "name": "Burger Combo", "price": 120.0, "category": "non-veg", "meal_role": "main", "tags": "fast-food"
    })
    
    # 1. Export CSV
    export_resp = client.get("/api/places/export")
    assert export_resp.status_code == 200
    csv_content = export_resp.text
    assert "CSV Diner" in csv_content
    assert "Burger Combo" in csv_content
    assert "120.0" in csv_content
    
    # 2. Clear Database to verify import
    with db.get_conn() as conn:
        conn.execute("DELETE FROM items")
        conn.execute("DELETE FROM places")
        
    # 3. Import CSV
    import_file = io.BytesIO(csv_content.encode("utf-8"))
    import_resp = client.post(
        "/api/places/import",
        files={"file": ("import_test.csv", import_file, "text/csv")}
    )
    assert import_resp.status_code == 200
    assert import_resp.json()["imported_items"] == 1
    
    # 4. Verify import in DB
    places_resp = client.get("/api/places")
    assert len(places_resp.json()) == 1
    assert places_resp.json()[0]["name"] == "CSV Diner"
    
    items_resp = client.get("/api/items")
    assert len(items_resp.json()) == 1
    assert items_resp.json()[0]["name"] == "Burger Combo"
    assert items_resp.json()[0]["price"] == 120.0


@patch("database.get_mongo_db")
def test_dictator_mode(mock_get_mongo_db):
    mock_db = MagicMock()
    mock_get_mongo_db.return_value = mock_db
    
    poll_code = "4321"
    poll_doc = {
        "_id": poll_code,
        "active": True,
        "candidates": [
            {"id": "cand0", "item_name": "Dish A", "place_name": "Place X", "price_per_person": 50, "expected_amount": 100, "score": 100}
        ],
        "votes": {"cand0": 0},
        "voted_users": ["Alice", "Bob"],
        "chat": [],
        "dictator": None
    }
    mock_db.polls.find_one.return_value = poll_doc
    
    # Select Dictator
    resp = client.post(f"/api/polls/{poll_code}/dictator")
    assert resp.status_code == 200
    dictator = resp.json()["dictator"]
    assert dictator in ["Alice", "Bob"]
    
    # Dictator Closes Poll with winner
    poll_doc["dictator"] = dictator
    resp = client.post(f"/api/polls/{poll_code}/dictator_close", json={
        "candidate_id": "cand0",
        "who": dictator
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "closed"
    assert resp.json()["winner"]["id"] == "cand0"
    
    # Non-dictator trying to close should fail
    non_dictator = "Bob" if dictator == "Alice" else "Alice"
    resp = client.post(f"/api/polls/{poll_code}/dictator_close", json={
        "candidate_id": "cand0",
        "who": non_dictator
    })
    assert resp.status_code == 400


@patch("database.get_mongo_db")
def test_poll_websocket(mock_get_mongo_db):
    mock_db = MagicMock()
    mock_get_mongo_db.return_value = mock_db
    
    poll_code = "9999"
    poll_doc = {
        "_id": poll_code,
        "active": True,
        "candidates": [],
        "votes": {},
        "chat": []
    }
    mock_db.polls.find_one.return_value = poll_doc
    
    # Test WebSocket connection and message receipt
    with client.websocket_connect(f"/ws/polls/{poll_code}") as websocket:
        data = websocket.receive_json()
        assert data["type"] == "poll_update"
        assert data["poll"]["code"] == poll_code


import io

# Cleanup temporary database at the end of execution
def test_cleanup():
    try:
        os.unlink(temp_db_path)
    except OSError:
        pass

