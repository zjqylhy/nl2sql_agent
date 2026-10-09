"""执行边界采用 AST 白名单；不依赖模型遵守提示词。"""
from sqlglot import exp, parse
from sqlglot.errors import ParseError

ALLOWED_COLUMNS = {"order_status", "item_amount", "delivery_days", "review_score", "customer_state", "purchased_at"}
ALLOWED_NODES = {
    exp.Select, exp.From, exp.Table, exp.Identifier, exp.Column, exp.Alias,
    exp.Literal, exp.Where, exp.And, exp.Or, exp.Not, exp.In, exp.Is,
    exp.Case, exp.If, exp.Sum, exp.Count, exp.Avg, exp.Nullif,
    exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Paren, exp.Null,
    exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ,
    exp.Group, exp.Order, exp.Ordered, exp.Limit, exp.Placeholder, exp.Star,
}


class UnsafeSQL(ValueError):
    pass


def validate_sql(sql: str, parameters: list) -> None:
    if len(sql) > 12000:
        raise UnsafeSQL("SQL 长度超限")
    try:
        statements = parse(sql, read="mysql")
    except ParseError as exc:
        raise UnsafeSQL("SQL 无法解析") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise UnsafeSQL("只允许单条 SELECT")
    tree = statements[0]
    tables = list(tree.find_all(exp.Table))
    if len(tables) != 1 or tables[0].name != "metric_orders" or tables[0].db or tables[0].catalog or tables[0].alias:
        raise UnsafeSQL("仅允许授权订单视图")
    for node in tree.walk():
        if type(node) not in ALLOWED_NODES:
            raise UnsafeSQL("SQL 包含未授权结构或函数")
        if isinstance(node, exp.Column) and (node.name not in ALLOWED_COLUMNS or node.table):
            raise UnsafeSQL("SQL 包含未授权字段")
        if isinstance(node, exp.Star) and not isinstance(node.parent, exp.Count):
            raise UnsafeSQL("禁止查询全部字段")
        if node.comments:
            raise UnsafeSQL("禁止 SQL 注释")
    limit = tree.args.get("limit")
    if not limit or not isinstance(limit.expression, exp.Literal) or not limit.expression.is_int or not 1 <= int(limit.expression.this) <= 1000:
        raise UnsafeSQL("必须设置有效行数上限")
    if len(list(tree.find_all(exp.Placeholder))) != len(parameters):
        raise UnsafeSQL("参数数量不一致")
    if not tree.args.get("where"):
        raise UnsafeSQL("必须限制时间区间")
