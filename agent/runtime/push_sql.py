"""推送编译 SQL 的额外只读围栏；模型始终只能提交结构化计划。"""
from sqlglot import exp, parse
from .security import UnsafeSQL

TABLES = {'metric_push_deliveries', 'metric_devices', 'metric_device_states', 'metric_device_activity'}


def validate_push_sql(sql, params):
    trees = parse(sql, read='postgres')
    if len(trees) != 1 or not isinstance(trees[0], exp.Select) or len(sql) > 16000:
        raise UnsafeSQL('仅允许受限推送 SELECT')
    tree = trees[0]
    aliases = {c.alias for c in tree.find_all(exp.CTE)}
    if any(t.name not in TABLES | aliases or t.db or t.catalog for t in tree.find_all(exp.Table)):
        raise UnsafeSQL('仅允许已授权推送视图')
    if any(isinstance(n, (exp.Command, exp.DDL, exp.DML, exp.Union, exp.Into)) or n.comments for n in tree.walk()):
        raise UnsafeSQL('包含不允许的结构')
    allowed_functions = {'COUNT', 'SUM', 'COALESCE', 'TO_CHAR', 'CAST', 'EXTRACT'}
    for node in tree.find_all(exp.Func):
        if node.sql_name() not in allowed_functions | {'CASE', 'IF', 'TIME_TO_STR', 'TS_OR_DS_TO_TIMESTAMP', 'AND', 'OR', 'NOT'}:
            raise UnsafeSQL('包含不允许的函数')
    if len(list(tree.find_all(exp.Placeholder))) != len(params):
        raise UnsafeSQL('绑定参数数量不一致')
    limit = tree.args.get('limit')
    if not limit or limit.expression.this != '1001' or not tree.args.get('where'):
        raise UnsafeSQL('必须限制查询范围与结果行数')
