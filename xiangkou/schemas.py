from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    session_id: Optional[str] = Field(default=None, min_length=36, max_length=36)
    request_id: Optional[str] = Field(default=None, pattern=r"^[0-9a-fA-F-]{36}$")
    context_run_id: Optional[str] = Field(default=None, pattern=r"^[0-9a-fA-F-]{36}$")


class StopPlan(BaseModel):
    category: str
    keyword: Optional[str] = None
    max_price: Optional[int] = Field(default=None, ge=1, le=100000)


class SearchPlan(BaseModel):
    goal: Literal["search", "booking"] = "search"
    visit_day: Optional[str] = Field(default=None, max_length=20)
    area_unrestricted: bool = False
    budget_unrestricted: bool = False
    followup: bool = False
    relative_price: Optional[Literal["higher", "lower"]] = None
    min_price: Optional[int] = Field(default=None, ge=1, le=100000)
    price_reference: Optional[int] = Field(default=None, ge=0, le=100000)
    category: Optional[str] = None
    keyword: Optional[str] = None
    area: Optional[str] = None
    max_price: Optional[int] = Field(default=None, ge=1, le=100000)
    min_score: Optional[float] = Field(default=None, ge=0, le=5)
    wants_coupon: bool = False
    wants_notes: bool = False
    nearby: bool = False
    sort: Literal["score", "price"] = "score"
    unverified_preferences: list[str] = Field(default_factory=list)
    stops: list[StopPlan] = Field(default_factory=list, max_length=4)
    party_size: Optional[int] = Field(default=None, ge=1, le=20)
    total_budget: Optional[int] = Field(default=None, ge=1, le=1000000)
    visit_time: Optional[str] = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    excluded_shop_ids: list[int] = Field(default_factory=list)
    required_shop_ids: list[int] = Field(default_factory=list)
    fixed_first_shop_id: Optional[int] = None
    second_price_ceiling: Optional[int] = None
    fixed_shop_ids: list[Optional[int]] = Field(default_factory=list)
    stop_price_ceilings: dict[int, int] = Field(default_factory=dict)


class ShopCard(BaseModel):
    id: int
    name: str
    area: Optional[str] = None
    address: Optional[str] = None
    avg_price: Optional[int] = None
    score: Optional[float] = None
    distance_m: Optional[float] = None
    image: Optional[str] = None
    url: str
    reason: str
    coupons: list[dict[str, Any]] = Field(default_factory=list)
    coupon_face_saving_yuan: Optional[float] = None
    open_hours: Optional[str] = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class ItineraryCard(BaseModel):
    stops: list[ShopCard]
    estimated_total: int
    party_size: int
    straight_distance_m: Optional[float] = None
    leg_distances_m: list[Optional[float]] = Field(default_factory=list)
    reason: str
    visit_times: list[str] = Field(default_factory=list)


class RelaxationOption(BaseModel):
    label: str
    message: str
    reason: str


class CandidateDecision(BaseModel):
    shop_id: int
    name: str
    outcome: Literal["selected", "excluded", "alternative"]
    reasons: list[str]


class ChatResponse(BaseModel):
    missing_fields: list[str] = Field(default_factory=list)
    response_kind: Literal["task", "conversation"] = "task"
    agent_outcomes: list[dict] = Field(default_factory=list)
    answer: str
    shops: list[ShopCard] = Field(default_factory=list)
    plan: Optional[SearchPlan] = None
    itineraries: list[ItineraryCard] = Field(default_factory=list)
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    confirmed_constraints: list[str] = Field(default_factory=list)
    memory_applied: list[str] = Field(default_factory=list)
    relaxations: list[RelaxationOption] = Field(default_factory=list)
    candidate_decisions: list[CandidateDecision] = Field(default_factory=list)
    candidate_summary: list[dict[str, int]] = Field(default_factory=list)
    action_draft: Optional[dict[str, Any]] = None
    business_choices: list[dict[str, Any]] = Field(default_factory=list)
    business_state: Optional[str] = None
    recommendation_run_id: Optional[str] = None
    personal_orders: list[dict[str, Any]] = Field(default_factory=list)
    plan_changes: list[dict[str, Any]] = Field(default_factory=list)
    session_summary: Optional[str] = None
    status: Literal["ok", "needs_location", "needs_clarification", "no_results", "upstream_error", "session_conflict", "unsupported_request"]
    mode: Literal["model", "rules"]
    trace: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
