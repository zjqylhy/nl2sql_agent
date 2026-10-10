"""已发布指标 v1：仅从订单粒度视图编译查询，模型不能改写口径。"""
from dataclasses import dataclass
from .catalog import catalog


@dataclass(frozen=True)
class Metric:
    name: str
    unit: str
    formula: str
    sample: str
    definition: str


VALID = "order_status NOT IN ('canceled', 'unavailable')"
PRICED = f"{VALID} AND item_amount IS NOT NULL"
METRICS = {key: Metric(**m.model_dump(exclude={"version"})) for key, m in catalog().metrics.items()}


def compile_query(metric: str, grouped: bool, state_count: int = 0) -> str:
    m = METRICS[metric]
    group = "customer_state" if grouped else "'整体'"
    filters = " AND customer_state IN (" + ", ".join("?" for _ in range(state_count)) + ")" if state_count else ""
    sql = f"SELECT {group} AS label, {m.formula} AS value, {m.sample} AS sample_size, COUNT(*) AS total_orders, COUNT(*) - ({m.sample}) AS excluded_orders, SUM(CASE WHEN order_status = 'unavailable' THEN 1 ELSE 0 END) AS unavailable_orders FROM metric_orders WHERE purchased_at >= ? AND purchased_at < ?{filters}"
    if grouped:
        sql += " GROUP BY customer_state ORDER BY customer_state"
    return sql + " LIMIT 1000"
