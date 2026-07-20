from typing import Optional, List
from pydantic import BaseModel, Field


class PlaceIn(BaseModel):
    name: str
    area: Optional[str] = ""
    cuisine: Optional[str] = ""
    price_range: Optional[str] = ""   # e.g. "budget", "mid", "splurge" - just a label
    notes: Optional[str] = ""


class PlaceUpdate(BaseModel):
    name: Optional[str] = None
    area: Optional[str] = None
    cuisine: Optional[str] = None
    price_range: Optional[str] = None
    notes: Optional[str] = None


class ItemIn(BaseModel):
    name: str
    price: float
    category: Optional[str] = ""      # e.g. "veg", "non-veg", "dessert", "drink"
    tags: Optional[str] = ""          # free-text comma separated, e.g. "spicy,rice,south indian"


class ItemUpdate(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    category: Optional[str] = None
    tags: Optional[str] = None
    rating: Optional[int] = None


class SuggestRequest(BaseModel):
    budget: float = Field(..., gt=0, description="Total budget for the group in your currency")
    people: int = Field(..., gt=0)
    preference: Optional[str] = ""     # e.g. "veg", "non-veg", "spicy", "dessert"
    additional_info: Optional[str] = ""  # free text, e.g. "feeling like something light, rainy day"
    area: Optional[str] = ""
    variety: Optional[int] = 1          # 0 = Safe, 1 = Regular, 2 = Wild
    who: Optional[str] = ""
    count: int = 3
    concurrency_control: bool = True


class HistoryIn(BaseModel):
    place_id: str
    item_id: str
    people: int
    amount: float
    who: Optional[str] = ""
    eaten_on: Optional[str] = None   # ISO date, defaults to now
    budget: Optional[float] = 0.0
    place_name: Optional[str] = ""
    item_name: Optional[str] = ""


class PollCreateRequest(BaseModel):
    budget: float
    people: int
    preference: Optional[str] = ""
    additional_info: Optional[str] = ""
    area: Optional[str] = ""
    variety: Optional[int] = 1
    concurrency_control: bool = True


class VoteRequest(BaseModel):
    candidate_id: str
    who: str
