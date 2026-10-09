"""已发布指标 v1：仅从订单粒度视图编译查询，模型不能改写口径。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    name: str
    unit: str
    formula: str
    sample: str
    definition: str


VALID = "order_status NOT IN ('canceled', 'unavailable')"
PRICED = f"{VALID} AND item_amount IS NOT NULL"
METRICS = {
    "gmv": Metric("GMV", "BRL", f"CASE WHEN SUM(CASE WHEN {VALID} AND item_amount IS NULL THEN 1 ELSE 0 END) > 0 THEN NULL ELSE SUM(CASE WHEN {VALID} THEN item_amount END) END", f"SUM(CASE WHEN {PRICED} THEN 1 ELSE 0 END)", "有效订单商品金额，不含运费；缺少有效订单金额时不输出总额"),
    "orders": Metric("有效订单量", "单", f"SUM(CASE WHEN {VALID} THEN 1 ELSE 0 END)", f"SUM(CASE WHEN {VALID} THEN 1 ELSE 0 END)", "排除 canceled/unavailable，每单一行后计数"),
    "aov": Metric("客单价", "BRL/单", f"CASE WHEN SUM(CASE WHEN {VALID} AND item_amount IS NULL THEN 1 ELSE 0 END) > 0 THEN NULL ELSE SUM(CASE WHEN {VALID} THEN item_amount END) / NULLIF(SUM(CASE WHEN {VALID} THEN 1 ELSE 0 END), 0) END", f"SUM(CASE WHEN {VALID} THEN 1 ELSE 0 END)", "GMV / 同口径有效订单量"),
    "cancellation_rate": Metric("取消率", "%", "100.0 * SUM(CASE WHEN order_status = 'canceled' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0)", "COUNT(*)", "已取消订单 / 全部创建订单；不可履约订单单列"),
    "delivery_days": Metric("平均交付时长", "天", "AVG(delivery_days)", "COUNT(delivery_days)", "已交付且时间有效订单，创建至实际交付的平均天数"),
    "review_score": Metric("平均评分", "分", "AVG(review_score)", "COUNT(review_score)", "每个订单最新一条有效评价的平均分；无评价不计入分母"),
}


def compile_query(metric: str, grouped: bool, state_count: int = 0) -> str:
    m = METRICS[metric]
    group = "customer_state" if grouped else "'整体'"
    filters = " AND customer_state IN (" + ", ".join("?" for _ in range(state_count)) + ")" if state_count else ""
    sql = f"SELECT {group} AS label, {m.formula} AS value, {m.sample} AS sample_size, COUNT(*) AS total_orders, COUNT(*) - ({m.sample}) AS excluded_orders, SUM(CASE WHEN order_status = 'unavailable' THEN 1 ELSE 0 END) AS unavailable_orders FROM metric_orders WHERE purchased_at >= ? AND purchased_at < ?{filters}"
    if grouped:
        sql += " GROUP BY customer_state ORDER BY customer_state"
    return sql + " LIMIT 1000"
