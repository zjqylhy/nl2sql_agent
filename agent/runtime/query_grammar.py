"""从当前位置匹配完整子句；未知文字不能删掉后重新解释为肯定条件。"""
import re
from dataclasses import dataclass, field

from .intent import ALIASES, STATES


METRIC = r"(?:有效)?(?:" + "|".join(p.replace("(?i)", "") for p in ALIASES.values()) + ")"
METRICS = rf"{METRIC}(?:\s*(?:和|及|与|、)\s*{METRIC})*"
STATE = r"(?:[A-Za-z]{2}(?![A-Za-z])|里约热内卢州?|圣保罗州?)\s*州?"
STATE_LIST = rf"{STATE}(?:\s*(?:和|及|与|、|,|，)\s*{STATE})*"
BARE_STATE = r"(?:(?:" + "|".join(sorted(STATES)) + r")(?![A-Za-z])\s*州?|里约热内卢州?|圣保罗州?)"
DIRECTION = r"从高到低|从低到高|降序|升序|最高|最低|排名"
DIMENSION = r"下单月份|下单日期|月份|日期|州代码|地区|州|月|日"
OPERATORS = {"大于等于": "gte", "至少": "gte", ">=": "gte", "小于等于": "lte", "至多": "lte", "<=": "lte",
    "不等于": "ne", "!=": "ne", "大于": "gt", "超过": "gt", ">": "gt", "小于": "lt", "<": "lt", "等于": "eq", "=": "eq"}
STATUS = {"已创建": "created", "已批准": "approved", "已开票": "invoiced", "处理中": "processing", "已发货": "shipped",
    "已交付": "delivered", "已送达": "delivered", "不可履约": "unavailable", "已取消": "canceled"}

# 首轮和追问共享范围定义，避免模型把扩大范围误解为排除原来的州。
STATE_SCOPE_GUIDANCE = (
    "州筛选语义：不看某州、排除某州表示 not_in；不要只看某州、别仅限某州不表示排除该州。"
    "明确说其他州也算上、剩余州都算上或查看全部州，表示取消州限制并保留该州。"
    "首轮只说不要只看某州但没有目标州范围时必须澄清，不能猜全部或排除；"
    "只看 SP 但不要排除 RJ 是范围冲突，必须澄清。"
)


@dataclass(frozen=True)
class Clause:
    kind: str
    text: str
    start: int
    end: int


@dataclass
class Recognition:
    clauses: list[Clause] = field(default_factory=list)
    unknown: list[tuple[int, int]] = field(default_factory=list)

    @property
    def complete(self):
        return not self.unknown and not any(c.kind in ("unknown_metric", "unknown_group") or
            c.kind in ("state", "named_state") and re.search("圣保罗|里约", c.text) for c in self.clauses)

    @property
    def targets(self):
        mapping = {"metric": "set_metrics", "unknown_metric": "set_metrics", "metric_change": "set_metrics",
            "group": "set_dimensions", "unknown_group": "set_dimensions", "overall": "set_dimensions",
            "time": "set_time_range", "state": "filters:state", "named_state": "filters:state", "state_missing": "filters:state",
            "amount": "filters:amount", "status": "filters:status", "top": "set_top_n", "sort": "set_order_by", "direction": "set_order_by"}
        targets = {mapping[c.kind] for c in self.clauses if c.kind in mapping}
        for c in self.clauses:
            if c.kind == "clear": targets.add("filters:" + ("amount" if "金额" in c.text else "status" if "状态" in c.text else "state"))
            if c.kind == "clear_rank": targets.update(("set_order_by", "set_top_n"))
        return targets


# 完整否定和修改优先于其中的州、指标及分组，匹配位置不能向后搜索。
PATTERNS = [(kind, re.compile(pattern, re.I)) for kind, pattern in [
    ("clear", rf"不要(?:只看|仅看)\s*{STATE_LIST}\s*[,，]?\s*(?:查看|查询|看)?(?:全部|所有)州"),
    ("clear", r"(?:查看|查询|看)(?:全部|所有)州|(?:取消|去掉|移除|清除)(?:州|地区|订单状态|状态|商品金额|金额)筛选"),
    ("clear_rank", r"取消排名|取消\s*Top\s*N|不取前"),
    ("metric_change", rf"把\s*{METRICS}\s*(?:换成|改成|改为)\s*{METRICS}|(?:再加上|再加|增加|加上|同时看|去掉|移除|取消)\s*{METRICS}"),
    ("time", r"(?:从\s*)?\d{4}-\d{2}-\d{2}\s*(?:至|到|起至|起到|[,，])\s*\d{4}-\d{2}-\d{2}(?:\s*止)?"),
    ("time", r"\d{4}年\d{1,2}月|(?:最近|近)\s*(?:\d+\s*个?月|半年)|上个(?:星期|礼拜)|这个(?:星期|礼拜)|上个月|这个月|本月|上月|本周|这周|上周|今天|今日|昨天|昨日"),
    ("sort", rf"(?:按|以)\s*{METRIC}\s*(?:{DIRECTION})|(?:按|以)\s*(?:{DIMENSION})\s*(?:升序|降序)"),
    ("group", r"按(?:照)?(?:下单)?月(?:份)?(?:和州)?|每月|逐月|按(?:照)?(?:下单)?日(?:期)?|每天|逐日|按(?:照)?(?:巴西)?(?:各)?(?:州|地区)(?:分组)?|各州|各地区|分地区|分州|逐州|每州"),
    ("overall", r"不分组|整体|总体|合计"),
    ("amount", r"(?:订单)?商品金额\s*(?:大于等于|小于等于|不等于|大于|小于|超过|至少|至多|等于|>=|<=|!=|>|<|=)\s*-?\d+(?:\.\d+)?"),
    ("status", r"订单状态\s*(?:不等于|不包含|不含|排除|!=|不是|为|是|=)\s*[a-z]+(?:\s*[,、]\s*[a-z]+)*"),
    ("status", r"(?:只看|仅看|限定|筛选)?(?:已创建|已批准|已开票|处理中|已发货|已交付|已送达|不可履约|已取消)(?:的)?(?:订单)?"),
    ("state", rf"(?:不包含|不含|排除|除了|不看)\s*{STATE_LIST}(?:\s*(?:之外|以外))?(?:\s*[,，]\s*看其他州)?"),
    ("state", rf"(?:只看|仅看|限定|筛选|过滤|地区为|州为|位于|来自|再加)\s*{STATE_LIST}"),
    ("state_missing", r"(?:只看|仅看)\s*(?:[一二两三四五六七八九十\d]+个?州|几个州)|(?:只看|仅看)(?=\s*[,，。？?；;！!]|\s*$)"),
    ("top", r"(?:取)?前\s*(?:\d+|[一二三四五六七八九十])\s*(?:名|组|个州)?"),
    ("direction", DIRECTION), ("metric", METRICS),
    ("named_state", rf"{BARE_STATE}(?:\s*(?:和|及|与|、|,|，)\s*{BARE_STATE})*"),
    ("unknown_metric", r"销售额"),
    ("unknown_group", r"按(?:未知维度|未知分类)(?:分组|统计)?"),
    ("unsupported", r"按(?:支付方式(?:和州)?|渠道|城市|品类|卖家|客户|门店)(?:分组|统计)?|同比|环比|归因|为什么|为何|报告|利润|营收|再往前|或者|或|\bOR\b"),
    ("ambiguous_amount", r"(?:付款|消费|订单|运费|价格)金额?(?:\s*(?:大于|小于|超过)\s*\d+(?:\.\d+)?)?|只看金额超过\s*\d+(?:\.\d+)?"),
    ("invalid_time", r"(?:从\s*)?\d{4}-\d{2}-\d{2}(?:\s*起)?"),
    ("prefix", rf"(?:改成|换成|改为|替换成)(?=\s*(?:按|整体|总体|不分组|本月|上月|最近|{METRIC}))"),
    ("neutral", r"帮我|给我|请问|查一下|查看|查询|请|看看|指定时间|指标|统计|计算|分组|趋势|是多少|有多少|多少|怎么样|如何|订单|的|呢|那"),
    ("join", r"和|及|与|、"),
]]


def recognize(question: str) -> Recognition:
    result, cursor = Recognition(), 0
    while cursor < len(question):
        separator = re.match(r"[\s，,。；;？?！!：:（）()]+", question[cursor:])
        if separator:
            cursor += separator.end()
            continue
        for kind, pattern in PATTERNS:
            match = pattern.match(question, cursor)
            if match:
                text = match.group()
                if kind in ("state", "named_state", "clear"):
                    text = re.sub(r"(?<![A-Za-z])[A-Za-z]{2}(?![A-Za-z])", lambda m: m.group().upper(), text)
                result.clauses.append(Clause(kind, text, cursor, match.end()))
                cursor = match.end()
                break
        else:
            # 未知子句内部不再寻找关键词，标点后的新子句才重新尝试。
            boundary = re.search(r"[，,。；;？?！!]", question[cursor:])
            after = cursor + boundary.start() if boundary else len(question)
            result.unknown.append((cursor, after))
            cursor = after
    for index, clause in enumerate(result.clauses):
        previous = result.clauses[index - 1] if index else None
        following = result.clauses[index + 1] if index + 1 < len(result.clauses) else None
        if clause.kind == "join" and (following is None or following.kind in ("neutral", "join")):
            result.unknown.append((clause.start, clause.end))
        # “分组”和“趋势”只有在已指定对应维度时才能作后缀。
        if clause.text in ("分组", "趋势") and (previous is None or previous.kind != "group" or
            clause.text == "趋势" and not re.search("月|日|天", previous.text)):
            result.unknown.append((clause.start, clause.end))
    return result


def state_values(text):
    values = list(dict.fromkeys(re.findall(r"(?<![A-Za-z])[A-Z]{2}(?![A-Za-z])", text)))
    return values, bool(re.search("圣保罗|里约", text)), any(v not in STATES for v in values)


def state_negative(text):
    return bool(re.match(r"不包含|不含|排除|除了|不看", text))


def canonical_followup(recognition):
    parts = []
    for clause in recognition.clauses:
        text = clause.text
        if clause.kind == "state":
            text = re.sub(r"^(?:不包含|不看)", "不含", text)
            text = re.sub(r"[,，]\s*看其他州$", "", text)
        elif clause.kind == "clear" and re.search("全部|所有", text): text = "取消州筛选"
        elif clause.kind == "group" and "和州" in text: text = text.replace("和州", " 按州")
        parts.append(text)
    return " ".join(parts)
