"""按已确认口径编写案例；同一业务问题的改写保持同一分区。"""


def plan(metrics, dimensions=(), start="2018-08-01", end="2018-09-01", filters=(), order=(), top=None):
    return {"metrics": list(metrics), "dimensions": list(dimensions), "time_range": {"start": start, "end_exclusive": end},
            "filters": list(filters), "order_by": list(order), "top_n": top}


def condition(field, op, *values):
    return {"field": field, "op": op, "values": list(values)}


def cases():
    result = []
    families = [
        ("本月 GMV 和有效订单量和客单价按州统计", plan(("gmv", "orders", "aov"), ("state",))),
        ("本月取消率和平均交付时长和平均评分按州统计", plan(("cancellation_rate", "delivery_days", "review_score"), ("state",))),
        ("最近 3 个月 GMV 和订单量按月统计", plan(("gmv", "orders"), ("month",), start="2018-06-01")),
        ("本月 GMV 按州排名，取前 5 名", plan(("gmv",), ("state",), order=({"field": "gmv", "direction": "desc"},), top=5)),
        ("本月商品金额大于 100 的有效订单量按日统计", plan(("orders",), ("day",), filters=(condition("amount", "gt", "100"),))),
        ("本月只看已取消订单的有效订单量", plan(("orders",), filters=(condition("status", "eq", "canceled"),))),
        ("本月 GMV 不含 SP 州", plan(("gmv",), filters=(condition("state", "not_in", "SP"),))),
        ("2016-01-01 至 2016-02-01 有效订单量", plan(("orders",), start="2016-01-01", end="2016-02-01")),
        ("上月 GMV 和有效订单量", plan(("gmv", "orders"), start="2018-07-01", end="2018-08-01")),
        ("最近 2 个月 GMV 和客单价和平均评分按月和州统计", plan(("gmv", "aov", "review_score"), ("month", "state"), start="2018-07-01")),
    ]
    prefixes = ("", "请查询", "帮我统计", "请问", "查看")
    for group, (question, expected) in enumerate(families):
        for index, prefix in enumerate(prefixes):
            result.append({"id": f"rules-{group:02}-{index}", "group": f"rules-{group}", "category": "rules", "split": "development" if group < 5 else "acceptance", "question": prefix + question, "expected": expected, "critical": True})
    paraphrases = [
        ("本月 GMV，只看圣保罗州", plan(("gmv",), filters=(condition("state", "in", "SP"),))),
        ("本月 GMV 和订单量，只看里约热内卢州", plan(("gmv", "orders"), filters=(condition("state", "in", "RJ"),))),
        ("上月客单价，只看圣保罗州", plan(("aov",), start="2018-07-01", end="2018-08-01", filters=(condition("state", "in", "SP"),))),
        ("最近 3 个月 GMV 按月统计，只看圣保罗州", plan(("gmv",), ("month",), start="2018-06-01", filters=(condition("state", "in", "SP"),))),
        ("本月平均评分，只看里约热内卢州", plan(("review_score",), filters=(condition("state", "in", "RJ"),))),
    ]
    for group, (question, expected) in enumerate(paraphrases):
        for index, prefix in enumerate(prefixes):
            result.append({"id": f"paraphrase-{group:02}-{index}", "group": f"paraphrase-{group}", "category": "paraphrase", "split": "development" if group < 2 else "acceptance", "question": prefix + question, "expected": expected, "critical": index == 0})
    ambiguities = ["GMV", "本月", "本月上月 GMV", "本月 GMV 只看", "本月 GMV 只看 ZZ 州", "2018-02-30 至 2018-03-01 GMV", "2018-08-31 至 2018-08-01 GMV", "本月 GMV 商品金额大于 -1", "本月 GMV 商品金额大于 1.001", "本月 GMV 按日和按月统计"]
    unsupported = ["本月 GMV 按品类统计", "本月 GMV 按支付方式统计", "本月 GMV 同比增长多少", "本月 GMV 为什么下降", "本月 GMV 按卖家统计"]
    for category, questions, repeats in (("clarification", ambiguities, 2), ("unsupported", unsupported, 3)):
        for group, question in enumerate(questions):
            for index in range(repeats):
                result.append({"id": f"{category}-{group:02}-{index}", "group": f"{category}-{group}", "category": category, "split": "development" if group < len(questions) // 2 else "acceptance", "question": prefixes[index] + question, "critical": True})
    # 把一个超范围问题组移入开发集，使两分区各 60 条，不拆分同义组。
    for case in result:
        if case["group"] == "unsupported-2":
            case["split"] = "development"
    for index in range(10):
        group = index // 2
        result.append({"id": f"permission-{index:02}", "group": f"permission-{group}", "category": "permission", "split": "development" if group < 3 else "acceptance", "question": ("本月 GMV", "查看本月 GMV")[index % 2], "critical": True, "check": ("manual_metric", "cross_detail", "cross_events", "cross_stop", "ops_summary")[group]})
    return result
