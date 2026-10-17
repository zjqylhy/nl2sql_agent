"""语义定义随镜像发布，模型只能引用 ID，不能提交物理表达式。"""
import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .security import ALLOWED_COLUMNS, postgres_sql, validate_sql


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MetricDefinition(StrictModel):
    name: str
    unit: str
    formula: str
    sample: str
    definition: str
    version: str


class DimensionDefinition(StrictModel):
    name: str
    expression: str


class FilterDefinition(StrictModel):
    name: str
    column: str
    type: Literal["enum", "decimal"]
    values: list[str]


class Dataset(StrictModel):
    view: Literal["metric_orders"]
    grain: Literal["order"]
    time_field: Literal["purchased_at"]


class Catalog(StrictModel):
    version: Literal[1]
    dataset: Dataset
    metrics: dict[str, MetricDefinition] = Field(min_length=1)
    dimensions: dict[str, DimensionDefinition]
    filters: dict[str, FilterDefinition]

    @model_validator(mode="after")
    def validate_definitions(self):
        import re
        for definitions in (self.metrics, self.dimensions, self.filters):
            if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", key) for key in definitions):
                raise ValueError("语义 ID 无效")
        for item in self.filters.values():
            if item.column not in ALLOWED_COLUMNS or (item.type == "enum" and not item.values):
                raise ValueError("筛选定义无效")
        expressions = [value for m in self.metrics.values() for value in (m.formula, m.sample)]
        expressions += [d.expression for d in self.dimensions.values()]
        for expression in expressions:
            validate_sql(postgres_sql(f"SELECT {expression} AS value FROM metric_orders WHERE purchased_at >= ? LIMIT 1"), ["2000-01-01"])
        return self


@lru_cache
def catalog() -> Catalog:
    return Catalog.model_validate(json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8")))
