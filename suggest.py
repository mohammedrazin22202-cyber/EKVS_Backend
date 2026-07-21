"""
Suggestion engine: given budget, people, preference and free-text notes,
scores every (place, item) combo and returns the top N as "prizes".
"""
import math
import random
from datetime import datetime, timedelta, timezone

from database import get_conn


def _gumbel_noise() -> float:
    """Generate a Gumbel(0, 1) noise sample for Gumbel-Top-K randomized sampling."""
    u = random.uniform(1e-10, 1.0 - 1e-10)
    return -math.log(-math.log(u))


def _recent_eaten_map(days=30, who=""):
    """Return {(place_id, item_id): days_ago} for everything eaten in the last `days` days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = {}
    
    query = "SELECT place_id, item_id, eaten_on FROM history WHERE eaten_on >= ? AND deleted = 0"
    params = [cutoff]
    if who and who.lower() != "all":
        query += " AND who = ?"
        params.append(who)
    query += " ORDER BY eaten_on DESC"

    with get_conn() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
        for r in rows:
            history_ids = r["item_id"].split(",") if r["item_id"] else []
            for h_id in history_ids:
                key = (r["place_id"], h_id)
                if key not in out:
                    try:
                        eaten_dt = datetime.fromisoformat(r["eaten_on"])
                        days_ago = (datetime.now(timezone.utc) - eaten_dt).days
                    except Exception:
                        days_ago = 0
                    out[key] = days_ago
    return out


def _recent_places(days=7, who=""):
    """Return set of place_ids eaten in the last `days` days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = set()
    
    query = "SELECT DISTINCT place_id FROM history WHERE eaten_on >= ? AND deleted = 0"
    params = [cutoff]
    if who and who.lower() != "all":
        query += " AND who = ?"
        params.append(who)

    with get_conn() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
        for r in rows:
            if r["place_id"]:
                out.add(r["place_id"])
    return out


def generate_suggestions(budget: float, people: int, preference: str = "", additional_info: str = "", area: str = "", variety: int = 1, who: str = "", count: int = 3, concurrency_control: bool = True):
    preference = (preference or "").strip().lower()
    additional_info = (additional_info or "").strip().lower()
    keywords = [w for w in additional_info.replace(",", " ").split() if len(w) > 2]

    recent = _recent_eaten_map(30, who)
    recently_eaten_places = _recent_places(7, who) if concurrency_control else set()

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT items.id as item_id, items.name as item_name, items.price as price,
                   items.category as category, items.tags as tags, items.rating as rating,
                   items.meal_role as meal_role, items.paired_item_id as paired_item_id,
                   places.id as place_id, places.name as place_name, places.area as area,
                   places.cuisine as cuisine, places.notes as place_notes
            FROM items
            JOIN places ON items.place_id = places.id
            WHERE items.deleted = 0 AND places.deleted = 0
            """
        ).fetchall()

    from collections import defaultdict, Counter
    import itertools

    place_items = defaultdict(list)
    for row in rows:
        row_area = (row["area"] or "").strip().lower()
        if area and row_area != area.strip().lower():
            continue
        place_items[row["place_id"]].append(dict(row))

    pref_lower = (preference or "").strip().lower()
    is_specific_snack_or_dessert = pref_lower in ["dessert", "sweet", "snack", "light", "street food", "drink", "beverage"]

    candidates = []
    for place_id, items in place_items.items():
        if concurrency_control and place_id in recently_eaten_places:
            continue
        
        items_by_id = {it["item_id"]: it for it in items}
        items = sorted(items, key=lambda x: x["price"])
        
        combos = []
        max_size = min(len(items), max(people + 2, 3))
        for size in range(1, max_size + 1):
            combos.extend(list(itertools.combinations_with_replacement(items, size)))

        if len(combos) > 120:
            step = max(1, len(combos) // 120)
            combos = combos[::step][:120]

        for combo in combos:
            # Option B: Bundle mandatory paired items
            final_bundle = list(combo)
            bundle_item_ids = set(it["item_id"] for it in final_bundle)
            
            for it in combo:
                paired_id = it.get("paired_item_id")
                if paired_id and paired_id in items_by_id and paired_id not in bundle_item_ids:
                    final_bundle.append(items_by_id[paired_id])
                    bundle_item_ids.add(paired_id)

            # Problem A: Standard meal must contain at least 1 Main Course
            if not is_specific_snack_or_dessert:
                has_main = any(it.get("meal_role", "main") == "main" for it in final_bundle)
                if not has_main:
                    continue

            expected_amount = sum(it["price"] for it in final_bundle)
            if expected_amount > budget:
                continue

            combo_item_ids = ",".join(it["item_id"] for it in final_bundle)

            # Format combined name
            item_names = [it["item_name"] for it in final_bundle]
            counts = Counter(item_names)
            combo_item_name = " + ".join(f"{count}x {name}" if count > 1 else name for name, count in counts.items())

            avg_rating = sum(it.get("rating", 0) for it in final_bundle) / len(final_bundle)
            categories = " ".join(set(it["category"] or "" for it in final_bundle))
            tags = " ".join(set(it["tags"] or "" for it in final_bundle))

            first_item = combo[0]
            place_name = first_item["place_name"]
            area_val = first_item["area"]
            cuisine = first_item["cuisine"]
            place_notes = first_item["place_notes"]

            base_score = 100.0
            haystack = " ".join([
                combo_item_name, categories, tags,
                place_name or "", cuisine or "", area_val or "", place_notes or "",
            ]).lower()

            # Preference match
            if pref_lower and pref_lower in haystack:
                base_score += 35

            # Keyword bonus
            for kw in keywords:
                if kw in haystack:
                    base_score += 12

            # Budget efficiency
            utilization = expected_amount / budget
            base_score += utilization * 15

            # Rating bonus (8 points per average rating star, max 40)
            base_score += avg_rating * 8

            # Recency penalties: evaluate each unique item in the combo
            max_penalty = 0
            if variety > 0:
                penalty_multiplier = 1 if variety == 1 else 3
                for it in combo:
                    key = (place_id, it["item_id"])
                    if key in recent:
                        days_ago = recent[key]
                        if days_ago <= 2:
                            max_penalty = max(max_penalty, 60 * penalty_multiplier)
                        elif days_ago <= 7:
                            max_penalty = max(max_penalty, 35 * penalty_multiplier)
                        else:
                            max_penalty = max(max_penalty, 15 * penalty_multiplier)
            base_score -= max_penalty

            # Noise scale based on Adventurous level (variety)
            # 0 (Safe): scale 12.0 - top matches favored, light randomization
            # 1 (Medium/Default): scale 35.0 - strong dynamic randomization across fitting places
            # 2 (Wild): scale 70.0 - maximum wild randomization
            if variety == 0:
                noise_scale = 12.0
            elif variety == 2:
                noise_scale = 70.0
            else:
                noise_scale = 35.0

            score = base_score + _gumbel_noise() * noise_scale

            candidates.append({
                "place_id": place_id,
                "place_name": place_name,
                "area": area_val,
                "item_id": combo_item_ids,
                "item_name": combo_item_name,
                "price_per_person": round(expected_amount / people, 2),
                "expected_amount": round(expected_amount, 2),
                "score": score,
            })

    # Sort all candidates by randomized score descending
    candidates.sort(key=lambda c: c["score"], reverse=True)

    # Diversify places: pick best randomized candidate per place first
    seen_places = set()
    diversified = []
    leftovers = []
    for c in candidates:
        if c["place_id"] not in seen_places:
            diversified.append(c)
            seen_places.add(c["place_id"])
        else:
            leftovers.append(c)

    final_candidates = diversified + leftovers

    return final_candidates[:count]

