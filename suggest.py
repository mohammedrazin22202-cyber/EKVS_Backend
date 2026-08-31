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


def generate_suggestions(budget: float, people: int, preference: str = "", additional_info: str = "", area: str = "", variety: int = 1, who: str = "", count: int = 3, concurrency_control: bool = True, dislikes: str = ""):
    pref_list = [p.strip().lower() for p in (preference or "").split(",") if p.strip()]
    additional_info = (additional_info or "").strip().lower()
    keywords = [w for w in additional_info.replace(",", " ").split() if len(w) > 2]
    dislikes_list = [d.strip().lower() for d in (dislikes or "").split(",") if d.strip()]

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

    is_specific_snack_or_dessert = any(p in ["dessert", "sweet", "snack", "light", "street food", "drink", "beverage"] for p in pref_list)

    candidates = []
    for place_id, items in place_items.items():
        if concurrency_control and place_id in recently_eaten_places:
            continue
        
        items_by_id = {it["item_id"]: it for it in items}
        items = sorted(items, key=lambda x: x["price"])
        
        combos = []
        max_size = min(len(items), max(people + 2, 3))
        
        # Count total estimated combinations to determine step size
        total_combos = 0
        for size in range(1, max_size + 1):
            total_combos += math.comb(len(items) + size - 1, size)
            
        step = max(1, total_combos // 120)
        
        for size in range(1, max_size + 1):
            if step == 1:
                combos.extend(itertools.combinations_with_replacement(items, size))
            else:
                idx = 0
                for combo in itertools.combinations_with_replacement(items, size):
                    if idx % step == 0:
                        combos.append(combo)
                        if len(combos) >= 120:
                            break
                    idx += 1
            if len(combos) >= 120:
                combos = combos[:120]
                break

        for combo in combos:
            # Option B: Bundle mandatory paired items
            final_bundle = list(combo)
            bundle_item_ids = set(it["item_id"] for it in final_bundle)
            
            for it in combo:
                paired_id = it.get("paired_item_id")
                if paired_id and paired_id in items_by_id and paired_id not in bundle_item_ids:
                    final_bundle.append(items_by_id[paired_id])
                    bundle_item_ids.add(paired_id)
            # Exclusions filter
            has_dislike = False
            for it in final_bundle:
                haystack_item = " ".join([
                    it["item_name"] or "",
                    it["category"] or "",
                    it["tags"] or ""
                ]).lower()
                for dl in dislikes_list:
                    if dl in haystack_item:
                        has_dislike = True
                        break
                if has_dislike:
                    break
            if has_dislike:
                continue

            # Problem A: Standard meal must contain at least 1 Main Course
            if not is_specific_snack_or_dessert:
                has_main = any((it.get("meal_role") or "main") == "main" for it in final_bundle)
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

            avg_rating = sum(it.get("rating") or 0 for it in final_bundle) / len(final_bundle)
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

            # Balanced diet / Combo variety bonus
            has_main = any((it.get("meal_role") or "main") == "main" for it in final_bundle)
            has_side = any(it.get("meal_role") == "side" for it in final_bundle)
            has_bev = any(it.get("meal_role") == "beverage" for it in final_bundle)
            
            diversity_bonus = 0
            if has_main and has_side:
                diversity_bonus += 15
            if has_main and has_bev:
                diversity_bonus += 15
            if has_main and has_side and has_bev:
                diversity_bonus += 15 # combo bonus
            base_score += diversity_bonus

            # Penalize duplicate main courses if eating alone
            if people == 1:
                mains = [it["item_id"] for it in final_bundle if (it.get("meal_role") or "main") == "main"]
                if len(mains) > len(set(mains)):
                    base_score -= 25

            # Preference match
            for pref in pref_list:
                if pref in haystack:
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

    # Group candidates by place_id
    from collections import defaultdict
    by_place = defaultdict(list)
    for c in candidates:
        by_place[c["place_id"]].append(c)

    # Sort candidates of each place by score in descending order
    for pid in by_place:
        by_place[pid].sort(key=lambda x: x["score"], reverse=True)

    # Sort places by their best randomized combo score
    sorted_place_ids = sorted(by_place.keys(), key=lambda pid: by_place[pid][0]["score"], reverse=True)

    # Interleave up to 5 top randomized combo candidates per place
    final_candidates = []
    # Round 0: Best combo for every place
    for pid in sorted_place_ids:
        if by_place[pid]:
            final_candidates.append(by_place[pid][0])

    # Rounds 1..4: Alternative meal combos per place
    for round_num in range(1, 5):
        for pid in sorted_place_ids:
            if len(by_place[pid]) > round_num:
                final_candidates.append(by_place[pid][round_num])

    return final_candidates[:count]


