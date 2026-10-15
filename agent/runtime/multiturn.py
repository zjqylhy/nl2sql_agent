"""追问只修改受限业务字段；有效计划与待补充草稿分开。"""
import hashlib
import json
import os
import re
from datetime import date, timedelta
from typing import Annotated, Literal, Union

import httpx
from fastapi import HTTPException
from pydantic import Field, model_validator

from . import telemetry
from .catalog import StrictModel, catalog
from .intent import ALIASES, PERIODS, STATES, Clarification, Unsupported, Intent, period_bounds
from .model_transport import ModelTransport, ModelConfigurationError
from .plan import QueryPlan, TimeRange, Filter, Ordering, validate_filters
from .query_grammar import recognize, canonical_followup, STATE_SCOPE_GUIDANCE


class SetMetrics(StrictModel):
    op: Literal["set_metrics"]
    value: list[str] = Field(min_length=1, max_length=3)


class SetDimensions(StrictModel):
    op: Literal["set_dimensions"]
    value: list[str] = Field(max_length=2)


class SetTime(StrictModel):
    op: Literal["set_time_range"]
    value: TimeRange


class AddFilter(StrictModel):
    op: Literal["add_filter"]
    value: Filter


class ReplaceFilter(StrictModel):
    op: Literal["replace_filter"]
    id: str = Field(pattern=r"^[a-f0-9]{16}$")
    value: Filter


class RemoveFilter(StrictModel):
    op: Literal["remove_filter"]
    id: str = Field(pattern=r"^[a-f0-9]{16}$")


class ClearFilters(StrictModel):
    op: Literal["clear_filters"]
    field: Literal["state", "status", "amount"]


class SetOrder(StrictModel):
    op: Literal["set_order_by"]
    value: list[Ordering] = Field(max_length=3)


class SetTop(StrictModel):
    op: Literal["set_top_n"]
    value: int | None = Field(ge=1, le=100, strict=True)


Operation = Annotated[Union[SetMetrics, SetDimensions, SetTime, AddFilter, ReplaceFilter, RemoveFilter, ClearFilters, SetOrder, SetTop], Field(discriminator="op")]


class Delta(StrictModel):
    operations: list[Operation] = Field(max_length=8)


class Seed(StrictModel):
    metrics: list[str] = Field(default_factory=list, max_length=3)
    dimensions: list[str] = Field(default_factory=list, max_length=2)
    time_range: TimeRange | None = None
    filters: list[Filter] = Field(default_factory=list, max_length=8)
    order_by: list[Ordering] = Field(default_factory=list, max_length=3)
    top_n: int | None = Field(default=None, ge=1, le=100, strict=True)

    @model_validator(mode="after")
    def identifiers(self):
        c = catalog()
        if len(set(self.metrics)) != len(self.metrics) or any(m not in c.metrics for m in self.metrics):
            raise ValueError("草稿指标无效")
        if len(set(self.dimensions)) != len(self.dimensions) or any(d not in c.dimensions for d in self.dimensions) or {"day", "month"}.issubset(self.dimensions):
            raise ValueError("草稿维度无效")
        if any(o.field not in c.metrics and o.field not in c.dimensions for o in self.order_by):
            raise ValueError("草稿排序无效")
        return self


class Pending(StrictModel):
    version: Literal[1] = 1
    base_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    initial: Seed | None = None
    operations: list[Operation] = Field(default_factory=list, max_length=8)
    missing: list[Literal["metrics", "time_range", "states", "order_by", "dimensions", "expression"]] = Field(max_length=6)
    question: str = Field(max_length=2000)
    origin_task_id: str = ""
    last_task_id: str = ""


class QueryContext(StrictModel):
    binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    version: int = Field(ge=0, strict=True)
    plan: QueryPlan | None = None
    pending: Pending | None = None


def plan_hash(plan):
    raw = plan.model_dump(mode="json") if plan is not None else None
    return hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def filter_id(value):
    raw = value.model_dump(mode="json") if isinstance(value, Filter) else value
    raw = {**raw, "values": sorted(raw["values"])}
    if raw["field"] != "amount":
        raw["op"] = {"eq": "in", "ne": "not_in"}.get(raw["op"], raw["op"])
    return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:16]


def source_metadata(source, tenant, role):
    directory = hashlib.sha256(json.dumps(catalog().model_dump(), sort_keys=True).encode()).hexdigest()
    values = [tenant, role, source.host, source.port, source.database, source.user, source.context_revision, str(source.analysis_date), directory]
    return {"binding": hashlib.sha256(json.dumps(values).encode()).hexdigest(), "analysis_date": str(source.analysis_date), "catalog_hash": directory}


def operation_target(op, filters):
    if op.op in ("add_filter", "replace_filter"):
        return "filters:" + op.value.field
    if op.op == "clear_filters":
        return "filters:" + op.field
    if op.op == "remove_filter":
        if op.id not in filters:
            raise Clarification("筛选目标已变化，请明确要移除的条件。")
        return "filters:" + filters[op.id]["field"]
    return op.op


def apply_operations(base, operations, anchor):
    data = base.model_dump(mode="json") if base is not None else Seed().model_dump(mode="json")
    original = {filter_id(f): f for f in data["filters"]}
    seen, notes = set(), []
    for op in operations:
        target = (op.op, getattr(op, "id", None), getattr(op, "field", None))
        if op.op != "add_filter" and target in seen:
            raise Clarification("同一字段存在重复修改，请确认。")
        seen.add(target)
        if op.op.startswith("set_"):
            field = op.op[4:]
            value = op.model_dump(mode="json")["value"]
            data[field] = value
        elif op.op == "clear_filters":
            data["filters"] = [f for f in data["filters"] if f["field"] != op.field]
        elif op.op == "add_filter":
            value = op.value.model_dump(mode="json")
            if any(filter_id(f) == filter_id(value) for f in data["filters"]):
                raise Clarification("筛选条件重复，请确认。")
            data["filters"].append(value)
        else:
            if op.id not in original or not any(filter_id(f) == op.id for f in data["filters"]):
                raise Clarification("筛选目标不存在或已被修改。")
            if op.op == "replace_filter" and op.value.field != original[op.id]["field"]:
                raise Clarification("替换筛选不能改变字段。")
            data["filters"] = [op.value.model_dump(mode="json") if op.op == "replace_filter" and filter_id(f) == op.id else f for f in data["filters"] if op.op != "remove_filter" or filter_id(f) != op.id]
    if any(o.op == "set_dimensions" for o in operations):
        previous = list(data["order_by"])
        data["order_by"] = [o for o in previous if o["field"] in data["metrics"] + data["dimensions"]]
        if previous != data["order_by"]:
            notes.append("分组改变，移除了失效的维度排序")
        if not data["dimensions"] and not any(o.op == "set_top_n" for o in operations):
            if data["top_n"] is not None or data["order_by"]:
                notes.append("改为整体，移除了排名和 Top N")
            data.update(top_n=None, order_by=[])
    seed = Seed.model_validate({k:v for k,v in data.items() if k != "version"})
    try:
        validate_filters(seed.filters)
    except ValueError as exc:
        raise Clarification(str(exc)) from exc
    missing = []
    if not seed.metrics: missing.append("metrics")
    if seed.time_range is None: missing.append("time_range")
    if seed.top_n is not None and not seed.dimensions: missing.append("dimensions")
    if seed.top_n is not None and not seed.order_by or any(o.field not in seed.metrics + seed.dimensions for o in seed.order_by): missing.append("order_by")
    if missing:
        return seed, None, missing, notes
    plan = QueryPlan.model_validate(seed.model_dump())
    plan.check_anchor(anchor)
    return seed, plan, [], notes


def read_time(question, anchor):
    matches = [(key, m) for key, pattern in PERIODS.items() if (m := re.search(pattern, question))]
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", question)
    recent = re.findall(r"(?:最近|近)\s*(\d+)\s*个?月", question)
    if sum(bool(v) for v in (matches, dates, recent)) > 1 or len(matches) > 1 or len(recent) > 1:
        raise Clarification("时间范围冲突，请保留一种。")
    if dates:
        if len(dates) != 2: raise Clarification("请提供完整的开始和结束日期。")
        return TimeRange(start=dates[0], end_exclusive=dates[1]), re.sub(r"\d{4}-\d{2}-\d{2}", "", question)
    if recent:
        count = int(recent[0])
        if not 1 <= count <= 12: raise Clarification("最近月份须为 1–12。")
        month = anchor.year * 12 + anchor.month - count
        return TimeRange(start=date(month // 12, month % 12 + 1, 1), end_exclusive=anchor + timedelta(days=1)), re.sub(r"(?:最近|近)\s*\d+\s*个?月", "", question)
    if matches:
        period, match = matches[0]
        # 日期助手的占位指标不进入草稿或有效计划。
        start, end = period_bounds(Intent(metric="gmv", period=period), anchor)
        return TimeRange(start=start, end_exclusive=end), question[:match.start()] + question[match.end():]
    return None, question


def normalize_question(question):
    return re.sub(r"(?:最近|近)半年", "最近 6 个月", question)


def _extract_rule_delta(question, base, pending, anchor, overrides, reset_state=False):
    q = normalize_question(question)
    if re.search(r"品类|卖家|支付方式|同比|环比|归因|为什么|为何|报告|利润|营收|城市|或|(?i:\bOR\b)|再往前", q):
        raise Unsupported("当前多轮支持修改指标、时间、州和日/月分组及筛选；该分析尚未支持。")
    ops, missing, allowed = [], [], set()
    filters = base.filters if base else []
    working = pending.initial if pending and pending.initial is not None else Seed.model_validate(base.model_dump(exclude={"version"})) if base else Seed()
    if pending and pending.operations:
        working, _, _, _ = apply_operations(base, pending.operations, anchor)
    window, q = read_time(q, anchor)
    if window:
        ops.append(SetTime(op="set_time_range", value=window)); allowed.add("set_time_range")
    if overrides.get("period"):
        start, end = period_bounds(Intent(metric="gmv", period=overrides["period"], start=overrides.get("start"), end=overrides.get("end")), anchor)
        manual = SetTime(op="set_time_range", value=TimeRange(start=start, end_exclusive=end))
        if window and window != manual.value: raise Clarification("文字时间与手动选择冲突。")
        ops = [o for o in ops if o.op != "set_time_range"] + [manual]
        allowed.add("set_time_range")
    clear = re.search(r"(?:取消|去掉|移除|清除)(州|地区|订单状态|状态|商品金额|金额)筛选", q)
    if clear:
        field = {"州":"state", "地区":"state", "订单状态":"status", "状态":"status", "商品金额":"amount", "金额":"amount"}[clear[1]]
        ops.append(ClearFilters(op="clear_filters", field=field)); allowed.add("filters:" + field)
        q = q[:clear.start()] + q[clear.end():]
    directions = re.findall(r"从高到低|从低到高|降序|升序|最高|最低", q)
    if any(v in ("从高到低", "降序", "最高") for v in directions) and any(v in ("从低到高", "升序", "最低") for v in directions):
        raise Clarification("排序方向冲突。")
    direction = "desc" if any(v in ("从高到低", "降序", "最高") for v in directions) else "asc" if directions else None
    sorting = []
    sort_dimensions = []
    for key, pattern in {"state":"州(?:代码)?", "month":"月份", "day":"日期"}.items():
        sort = re.search(rf"按{pattern}\s*(?:升序|降序)", q)
        if sort:
            sorting.append(key); sort_dimensions.append(key); q = q[:sort.start()] + q[sort.end():]
    for key, pattern in ALIASES.items():
        sort = re.search(rf"(?:按|以)\s*(?:{pattern.replace('(?i)', '')})\s*(?:从高到低|从低到高|降序|升序|排名)", q, re.I)
        if sort:
            sorting.append(key); q = q[:sort.start()] + q[sort.end():]
    metrics = [key for key, pattern in ALIASES.items() if re.search(pattern, q)]
    if metrics:
        chosen = metrics
        swap = re.search(r"把(.+?)(?:换成|改成|改为)(.+)", q)
        if swap:
            before = [m for m,p in ALIASES.items() if re.search(p, swap[1])]
            after = [m for m,p in ALIASES.items() if re.search(p, swap[2])]
            if len(before) != 1 or not after or before[0] not in working.metrics: raise Clarification("请明确要替换的现有指标。")
            chosen = list(dict.fromkeys(m for old in working.metrics for m in (after if old == before[0] else [old])))
        if re.search("再加|增加|加上|同时看", q): chosen = list(dict.fromkeys(working.metrics + metrics))
        if re.search("去掉|移除|取消", q): chosen = [m for m in working.metrics if m not in metrics]
        ops.append(SetMetrics(op="set_metrics", value=chosen)); allowed.add("set_metrics")
        for pattern in ALIASES.values(): q = re.sub(pattern, "", q)
    elif re.search("指标|一单|均分|几天|几分", q): allowed.add("set_metrics")
    if overrides.get("metric"):
        if metrics and chosen != [overrides["metric"]]: raise Clarification("文字指标与手动选择冲突。")
        ops = [o for o in ops if o.op != "set_metrics"] + [SetMetrics(op="set_metrics", value=[overrides["metric"]])]
        allowed.add("set_metrics")
    dimensions = list(sort_dimensions)
    for key, pattern in {"state":r"按(?:照)?(?:各|巴西)?州|分州|逐州|各州|每州", "month":r"按月(?:份)?|每月|逐月", "day":r"按日(?:期)?|每天|逐日"}.items():
        if re.search(pattern, q):
            if key not in dimensions: dimensions.append(key)
            q = re.sub(pattern, "", q)
    if re.search("整体|总体|不分组", q): dimensions = []; q = re.sub("整体|总体|不分组", "", q); allowed.add("set_dimensions")
    if dimensions or "set_dimensions" in allowed:
        ops.append(SetDimensions(op="set_dimensions", value=dimensions)); allowed.add("set_dimensions")
    elif re.search("维度|各地|分别|一行|展开", q): allowed.add("set_dimensions")
    if overrides.get("group_by"):
        if "set_dimensions" in allowed and dimensions != [overrides["group_by"]]: raise Clarification("文字分组与手动选择冲突。")
        ops = [o for o in ops if o.op != "set_dimensions"] + [SetDimensions(op="set_dimensions", value=[overrides["group_by"]])]
        allowed.add("set_dimensions")

    def field_operation(value, positive_replace=True):
        candidates = [f for f in filters if f.field == value.field and (positive_replace or f.op in ("ne", "not_in"))]
        if len(candidates) > 1: raise Clarification("该字段有多个筛选，请明确要替换哪一个。")
        return ReplaceFilter(op="replace_filter", id=filter_id(candidates[0]), value=value) if candidates else AddFilter(op="add_filter", value=value)

    states = list(dict.fromkeys(re.findall(r"(?<![a-zA-Z])([A-Z]{2})(?![a-zA-Z])", q)))
    if any(s not in STATES for s in states): raise Clarification("州代码无效。")
    named = bool(re.search("圣保罗|里约热内卢|[\u4e00-\u9fff]{2,}州", q))
    state_request = bool(re.search("只看|仅看|州为|再加.*州|不含|排除", q)) or pending is not None and "states" in pending.missing
    if state_request:
        allowed.add("filters:state")
        negative = bool(re.search("不含|排除", q))
        if negative and re.search("只看|仅看|州为", q): raise Clarification("包含与排除州条件冲突。")
        if states:
            if re.search("再加", q):
                positive = [f for f in working.filters if f.field == "state" and f.op in ("eq", "in")]
                if len(positive) != 1: raise Clarification("增加州需要一个明确的现有州集合。")
                states = list(dict.fromkeys(positive[0].values + states))
            ops.append(field_operation(Filter(field="state", op="not_in" if negative else "in", values=states), not negative or reset_state))
            q = re.sub(r"(?<![a-zA-Z])[A-Z]{2}(?![a-zA-Z])", "", q)
        elif not named or re.search(r"[一二两三四五六七八九十\d]+个?州|几个州", q):
            missing.append("states"); q = re.sub(r"[一二两三四五六七八九十\d]+个?州|几个州", "", q)
        q = re.sub("只看|仅看|州为|不含|排除", "", q)
    amount = re.search(r"(?:订单)?商品金额\s*(大于等于|小于等于|不等于|大于|小于|等于|至少|至多|超过|>=|<=|!=|>|<|=)\s*(-?\d+(?:\.\d+)?)", q)
    if amount:
        operators = {"大于等于":"gte","至少":"gte",">=":"gte","小于等于":"lte","至多":"lte","<=":"lte","不等于":"ne","!=":"ne","大于":"gt","超过":"gt",">":"gt","小于":"lt","<":"lt","等于":"eq","=":"eq"}
        value = Filter(field="amount", op=operators[amount[1]], values=[amount[2]])
        ops.append(field_operation(value)); allowed.add("filters:amount")
        q = q[:amount.start()] + q[amount.end():]
    elif re.search("商品金额", q): allowed.add("filters:amount")
    status = re.search(r"订单状态(?:为|是|不含|不等于)\s*([a-z]+(?:[,、]\s*[a-z]+)*)", q)
    if status:
        value = Filter(field="status", op="not_in" if re.search("不含|不等于", status[0]) else "in", values=re.split(r"[,、]\s*", status[1]))
        ops.append(field_operation(value)); allowed.add("filters:status"); q = q[:status.start()] + q[status.end():]
    elif re.search("订单状态", q): allowed.add("filters:status")
    top = re.findall(r"(?:取)?前\s*([0-9]+|[一二三四五六七八九十])\s*(?:名|组|个州)?", q)
    if len(top) > 1: raise Clarification("Top N 数量冲突。")
    if top:
        number = int(top[0]) if top[0].isdigit() else "一二三四五六七八九十".index(top[0]) + 1
        ops.append(SetTop(op="set_top_n", value=number)); allowed.add("set_top_n")
        q = re.sub(r"(?:取)?前\s*([0-9]+|[一二三四五六七八九十])\s*(?:名|组|个州)?", "", q)
    if re.search("取消排名|取消 Top N|不取前", q, re.I):
        ops.extend([SetOrder(op="set_order_by", value=[]), SetTop(op="set_top_n", value=None)])
        allowed.update(("set_order_by", "set_top_n")); q = re.sub("取消排名|取消 Top N|不取前", "", q, flags=re.I)
    if direction:
        available_metrics = next((o.value for o in ops if o.op == "set_metrics"), working.metrics)
        field = sorting[0] if len(sorting) == 1 else available_metrics[0] if len(available_metrics) == 1 else None
        if field:
            ops.append(SetOrder(op="set_order_by", value=[Ordering(field=field, direction=direction)]))
        else: missing.append("order_by")
        allowed.add("set_order_by")
    q = re.sub("从高到低|从低到高|降序|升序|最高|最低|排名", "", q)
    # 只消耗明确的连接词；剩余业务文字必须进入模型或澄清，不能静默丢弃。
    q = re.sub(r"改成|换成|改为|替换成|再加上|再加|增加|加上|去掉|移除|取消|统计|查询|是多少|多少|看看|看|那|呢|把|的|和|及|与|按|至|到|州|订单|请|帮我|一下|给我|[\s，,。？?、：:；;！!]", "", q)
    return Delta(operations=ops), list(dict.fromkeys(missing)), allowed, q


ALL_TARGETS = {"set_metrics", "set_dimensions", "set_time_range", "set_order_by", "set_top_n",
    "filters:state", "filters:status", "filters:amount"}


def rule_delta(question, base, pending, anchor, overrides):
    parsed = recognize(question)
    telemetry.rule_decision(parsed, "followup")
    if parsed.unknown:
        # 未覆盖子句可能改变局部匹配的操作，只有手动选择可以锁定模型。
        delta, _, _, _ = _extract_rule_delta("", base, pending, anchor, overrides)
        return delta, [], set(ALL_TARGETS), "unknown_clause"
    canonical = canonical_followup(parsed)
    reset = any(c.kind == "state" and "看其他州" in c.text for c in parsed.clauses)
    delta, missing, allowed, remainder = _extract_rule_delta(canonical, base, pending, anchor, overrides, reset)
    if any(c.kind in ("state", "named_state") and re.search("圣保罗|里约", c.text) for c in parsed.clauses):
        delta = Delta(operations=[op for op in delta.operations if operation_target(op, {}) != "filters:state"])
    if not parsed.complete and not missing:
        remainder = remainder or "unresolved_clause"
    return delta, missing, allowed | parsed.targets, remainder


def merge_operations(previous, current, filters):
    changed = {operation_target(o, filters) for o in current}
    return [o for o in previous if operation_target(o, filters) not in changed] + current


def resolve_followup(request, anchor, parse_initial):
    context = request.context
    base, pending = context.plan, context.pending
    task_id = request.execution.task_id if request.execution else ""
    if pending and pending.base_hash != plan_hash(base): raise HTTPException(409, "草稿上下文已变化，请新建问数")
    if any(m not in request.allowed_metrics for m in (base.metrics if base else [])):
        raise HTTPException(403, "上下文指标权限已变化，请新建问数")
    question = request.input.question
    restart = bool(re.match(r"(?:重新开始|新查询|新的查询|另一个问题)[，,:：\s]*", question))
    if restart:
        question = re.sub(r"^(?:重新开始|新查询|新的查询|另一个问题)[，,:：\s]*", "", question)
        pending, parsing_base = None, None
    else: parsing_base = base
    initial_usage = None
    if parsing_base is None and pending is None:
        request = request.model_copy(update={"input": request.input.model_copy(update={"question": question})})
        try:
            plan, usage = parse_initial(request, anchor)
            return plan, usage, {"version": 1, "changes": difference(base, plan), "notes": []}
        except Unsupported: raise
        except Clarification as exc:
            initial_usage = getattr(exc, "usage", None)
    overrides = request.input.model_dump(exclude={"question"}, exclude_defaults=True)
    delta, missing, allowed, remainder = rule_delta(question, parsing_base, pending, anchor, overrides)
    known = delta.operations
    filters = {filter_id(f): f.model_dump(mode="json") for f in parsing_base.filters} if parsing_base else {}
    usage = initial_usage or {"parser": "rules", "tokens": 0}
    complete_expression = not remainder
    if remainder and not missing:
        if initial_usage or not os.getenv("LLM_API_KEY"):
            missing.append("expression")
        else:
            try: transport = ModelTransport.from_env()
            except ModelConfigurationError: raise HTTPException(503, "模型配置不完整") from None
            prompt = "你是受控追问解析器。仅返回 operations JSON 或 {clarification:简短问题}，禁止完整计划、SQL、租户和数据源。操作必须完整表达用户修改，未涉及字段不能修改。schema=" + json.dumps(Delta.model_json_schema(), ensure_ascii=False)
            prompt += "根据完整原文解释否定及修改范围；无法完整理解或表达时必须澄清。不得忽略不支持的关联、比较、归因或报告请求。允许的修改目标=" + json.dumps(sorted(allowed or ALL_TARGETS)) + "；分析基准日=" + str(anchor) + "；当前计划=" + json.dumps(parsing_base.model_dump(mode="json") if parsing_base else None)
            prompt += "当前草稿=" + json.dumps(pending.model_dump(mode="json") if pending else None) + "；筛选 ID=" + json.dumps(filters) + "；明确操作必须保留=" + json.dumps(delta.model_dump(mode="json"))
            prompt += "授权指标=" + json.dumps(request.allowed_metrics) + "；维度=" + json.dumps(list(catalog().dimensions)) + "；筛选枚举=" + json.dumps({k:v.model_dump(exclude={"column"}) for k,v in catalog().filters.items()}, ensure_ascii=False)
            prompt += STATE_SCOPE_GUIDANCE
            try:
                with telemetry.stage("model"), transport.client() as client:
                    telemetry.update(parser="llm", model=transport.model[:100], model_calls=1, input_tokens=None, output_tokens=None, total_tokens=None)
                    response = transport.post(client, prompt, question, transport.plan_max_tokens)
                    response.raise_for_status(); body = response.json()
                telemetry.usage(body.get("usage") or {})
                usage = {"parser": "llm", "tokens": (body.get("usage") or {}).get("total_tokens"), "model": transport.model}
                choice = body["choices"][0]
                if choice.get("finish_reason") == "length": raise ValueError("truncated delta")
                content = json.loads(choice["message"]["content"])
                if isinstance(content, dict) and set(content) == {"clarification"} and isinstance(content["clarification"], str) and content["clarification"].strip():
                    missing.append("expression")
                else:
                    proposal = Delta.model_validate(content)
                    if not proposal.operations:
                        raise ValueError("empty model delta")
                    if any(operation_target(o, filters) not in (allowed or ALL_TARGETS) for o in proposal.operations): raise Clarification("解析修改了未提出的条件，请明确追问。")
                    # 确认的操作不可被模型覆盖；枚举等价的完整核对由候选计划完成。
                    for op in known:
                        if op.model_dump(mode="json") not in [o.model_dump(mode="json") for o in proposal.operations]:
                            raise Clarification("解析结果改变了已明确修改，请确认。")
                    delta = proposal
                    complete_expression = True
            except Clarification as exc:
                exc.usage = usage; raise
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                telemetry.model_error(exc)
                raise HTTPException(503, "模型解析失败，请明确追问后重试") from None
    operations = merge_operations(pending.operations if pending else [], delta.operations, filters)
    seed_base = pending.initial if parsing_base is None and pending else parsing_base
    seed, plan, needed, notes = apply_operations(seed_base, operations, anchor)
    for field in (pending.missing if pending else []):
        if field == "states" and not any(operation_target(o, filters) == "filters:state" for o in delta.operations): missing.append(field)
        if field == "expression":
            # 未理解的原条件不能被一个无关的合法修改隐式取消。
            original_allowed = rule_delta(pending.question, parsing_base, None, anchor, {})[2]
            unresolved = original_allowed - {operation_target(o, filters) for o in pending.operations}
            current_targets = {operation_target(o, filters) for o in delta.operations}
            if not complete_expression or not unresolved or not unresolved <= current_targets:
                missing.append(field)
    missing = list(dict.fromkeys(missing + needed))
    if any(m not in request.allowed_metrics for m in seed.metrics): raise HTTPException(403, "没有全部请求指标的权限")
    if missing:
        draft = Pending(base_hash=plan_hash(base), initial=seed if parsing_base is None else None, operations=[] if parsing_base is None else operations,
                        missing=missing, question=pending.question if pending else request.input.question,
                        origin_task_id=pending.origin_task_id if pending else task_id, last_task_id=task_id)
        names = {"metrics":"指标", "time_range":"时间范围", "states":"具体州代码", "order_by":"排名指标和方向", "dimensions":"分组", "expression":"完整修改条件"}
        message = "请补充" + "、".join(names[f] for f in missing) + "。"
        return None, usage, {"version": 1, "pending": draft.model_dump(mode="json"), "message": message, "changes": [], "notes": []}
    telemetry.update(parser=usage["parser"])
    return plan, usage, {"version": 1, "changes": difference(base, plan), "notes": notes}


def difference(base, plan):
    before = base.model_dump(mode="json") if base else {}
    after = plan.model_dump(mode="json")
    return [{"field": key, "before": before.get(key), "after": value} for key,value in after.items() if key != "version" and before.get(key) != value]


