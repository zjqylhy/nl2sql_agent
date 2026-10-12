"""题集契约和复核状态独立版本化，禁止同意图跨分区。"""
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DATASET = Path(__file__).with_name("cases.json")
METRICS = {"gmv", "orders", "aov", "cancellation_rate", "delivery_days", "review_score"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Window(Strict):
    start: str
    end_exclusive: str

    @model_validator(mode="after")
    def ordered(self):
        if date.fromisoformat(self.start) >= date.fromisoformat(self.end_exclusive):
            raise ValueError("invalid expected date range")
        return self


class Filter(Strict):
    field: Literal["state", "status", "amount"]
    op: Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte"]
    values: list[str] = Field(min_length=1)


class Ordering(Strict):
    field: str
    direction: Literal["asc", "desc"]


class ExpectedPlan(Strict):
    metrics: list[str] = Field(min_length=1, max_length=3)
    dimensions: list[Literal["state", "day", "month"]] = Field(max_length=2)
    time_range: Window
    filters: list[Filter] = Field(max_length=8)
    order_by: list[Ordering] = Field(max_length=3)
    top_n: int | None = Field(ge=1, le=100)

    @model_validator(mode="after")
    def valid(self):
        if len(set(self.metrics)) != len(self.metrics) or not set(self.metrics) <= METRICS:
            raise ValueError("invalid expected metrics")
        if len(set(self.dimensions)) != len(self.dimensions) or {"day", "month"} <= set(self.dimensions):
            raise ValueError("invalid expected dimensions")
        if any(o.field not in self.metrics + self.dimensions for o in self.order_by):
            raise ValueError("invalid expected ordering")
        return self


class Case(Strict):
    id: str
    group: str
    category: Literal["common", "unfamiliar", "missing", "conflict", "unsupported", "permission"]
    split: Literal["development", "acceptance"]
    question: str
    features: list[str] = Field(min_length=1)
    input: dict[str, str] = Field(default_factory=dict)
    identity: Literal["admin", "ops"] = "admin"
    expected: ExpectedPlan | None = None
    expected_status: Literal["completed", "needs_clarification", "denied"]
    reason: str = ""
    error_codes: list[str] = Field(default_factory=list)
    message_any: list[str] = Field(default_factory=list)
    check: Literal["query", "cross_detail", "cross_events", "cross_stop", "ops_summary"] = "query"

    @model_validator(mode="after")
    def contract(self):
        if (self.category in ("common", "unfamiliar")) != (self.expected is not None):
            raise ValueError("supported cases require independent expected plans")
        if self.expected is not None and self.expected_status != "completed":
            raise ValueError("supported status mismatch")
        if self.expected is None and not self.reason:
            raise ValueError("boundary cases require an annotated reason")
        if not set(self.input) <= {"metric", "period", "group_by", "start", "end"}:
            raise ValueError("untrusted input fields")
        return self


class Dataset(Strict):
    version: Literal[1]
    annotation_status: Literal["draft", "human_reviewed"]
    reviewer: str | None
    analysis_date: Literal["2018-08-31"]
    cases: list[Case]

    @model_validator(mode="after")
    def frozen_contract(self):
        counts = Counter(c.category for c in self.cases)
        if counts != {"common": 30, "unfamiliar": 30, "missing": 8, "conflict": 6, "unsupported": 8, "permission": 8}:
            raise ValueError("dataset composition mismatch")
        if len({c.id for c in self.cases}) != 90 or len({c.question for c in self.cases}) != 90:
            raise ValueError("duplicate case IDs or expressions")
        if len({c.group for c in self.cases}) < 45:
            raise ValueError("insufficient independent intent groups")
        groups = {}
        for c in self.cases:
            groups.setdefault(c.group, set()).add(c.split)
        if any(len(splits) != 1 for splits in groups.values()):
            raise ValueError("intent group leaked across partitions")
        if self.annotation_status == "human_reviewed" and not self.reviewer:
            raise ValueError("human review requires reviewer attribution")
        return self


def load(path: Path = DATASET) -> Dataset:
    return Dataset.model_validate(json.loads(path.read_text(encoding="utf-8")))
