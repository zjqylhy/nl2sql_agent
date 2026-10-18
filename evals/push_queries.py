"""公开开发题单：固定问题与预期条件，真实模型运行前冻结。"""


def expected(metrics, dimensions=(), *, start='2026-07-01', end='2026-08-01', hours=48, filters=(), as_of=None):
    return dict(metrics=list(metrics), dimensions=list(dimensions),
                time_range={'start':start+'T00:00:00+08:00','end_exclusive':end+'T00:00:00+08:00'},
                observation_hours=hours, filters=list(filters), as_of=as_of)


def case(name, question, plan=None, dialogue=()):
    return dict(id=name, question=question, dialogue=list(dialogue),
                status='completed' if plan else 'needs_clarification', plan=plan)


CASES = [
    case('daily_vendor', '统计2026年7月1日至8月1日（结束不含），按发送日期和设备厂商分组的发送量、到达率、点击/发送率，发送后观察48小时。',
         expected(['sent','delivery_rate','click_rate'],['day','manufacturer'])),
    case('app_channel', '2026年8月1日至9月1日，结束不含，只看app_1，按实际推送通道统计发送量、已确认到达量，观察24小时。',
         expected(['sent','delivered'],['channel'],start='2026-08-01',end='2026-09-01',hours=24,filters=[{'field':'app_id','op':'in','values':['app_1']} ])),
    case('exclude_vendor', '2026年7月1日至8月1日（结束不含），排除小米设备厂商，按厂商统计发送量和到达率，观察48小时。',
         expected(['sent','delivery_rate'],['manufacturer'],filters=[{'field':'manufacturer','op':'not_in','values':['xiaomi']} ])),
    case('vendor_and_app', '2026年7月1日至8月1日（结束不含），仅app_2的华为设备，按日查看发送量、点击/发送率，观察48小时。',
         expected(['sent','click_rate'],['day'],filters=[{'field':'app_id','op':'in','values':['app_2']},{'field':'manufacturer','op':'in','values':['huawei']} ])),
    case('two_channels', '统计2026年7月1日至8月1日（结束不含）apns与fcm通道的发送量、到达率，按通道分组，观察72小时。',
         expected(['sent','delivery_rate'],['channel'],hours=72,filters=[{'field':'channel','op':'in','values':['apns','fcm']} ])),
    case('click_denominator', '2026年7月1日至8月1日，结束不含，按应用统计点击/到达率：分子为观察窗内同时已到达且点击，分母为已确认到达，观察48小时。',
         expected(['click_to_delivery_rate'],['app_id'])),
    case('failed_reason', '2026年7月1日至8月1日（结束不含），按已知失败原因统计已确认失败量，观察48小时。',
         expected(['failed'],['failure_reason'])),
    case('device_status', '统计2026年7月1日至8月1日（结束不含）的设备查询，状态时点明确取2026年8月1日00:00+08:00，按设备厂商看有效、无效、状态未知设备数量。',
         expected(['valid_devices','invalid_devices','unknown_devices'],['manufacturer'],hours=24,as_of='2026-08-01T00:00:00+08:00')),
    case('active_devices', '按应用统计2026年9月1日至10月1日（结束不含）的活跃设备数量，设备状态时点为2026年10月1日00:00+08:00。',
         expected(['active_devices'],['app_id'],start='2026-09-01',end='2026-10-01',hours=24,as_of='2026-10-01T00:00:00+08:00')),
    case('active_valid', '统计app_1在2026年9月1日至10月1日（结束不含）的活跃有效设备数量和设备活跃率，按厂商分组，设备状态时点取2026年10月1日00:00+08:00。',
         expected(['active_valid_devices','active_rate'],['manufacturer'],start='2026-09-01',end='2026-10-01',hours=24,as_of='2026-10-01T00:00:00+08:00',filters=[{'field':'app_id','op':'in','values':['app_1']} ])),
    case('delivery_latency', '2026年7月1日至8月1日（结束不含），按通道统计平均到达时延，观察48小时。',
         expected(['delivery_seconds'],['channel'])),
    case('month_app', '2026年6月1日至9月1日（结束不含），按发送月份和应用统计发送量、点击/发送率，观察48小时。',
         expected(['sent','click_rate'],['month','app_id'],start='2026-06-01',end='2026-09-01')),
    case('arbitrary_range', '2026年8月3日至8月20日，结束不含，按厂商查询发送量与到达率，观察48小时。',
         expected(['sent','delivery_rate'],['manufacturer'],start='2026-08-03',end='2026-08-20')),
    case('missing_observation', '2026年7月1日至8月1日（结束不含），按厂商统计到达率，我尚未确定回执观察时长，请先向我确认。'),
    case('immature_window', '发送时间为2026年8月15日至10月15日（结束不含），各厂商的发送量、到达率和点击/发送率，按发送日期分组，观察48小时。'),
    case('mixed_grain', '2026年7月1日至8月1日（结束不含），发送量和有效设备数量放在同一次查询中，按厂商统计，观察48小时。'),
    case('unsupported_causality', '2026年7月1日至8月1日（结束不含），观察48小时，请直接证明华为到达率变化是由手机系统更新造成的。'),
    case('unknown_manufacturer', '2026年7月1日至8月1日（结束不含），只查询荣耀厂商设备的发送量，观察48小时。'),
    case('clarification_48', '按日期分析各厂商最近两个月的发送量、到达率、点击/发送率变化', dialogue=[
        {'question':'请指定回执观察时长并明确发送区间','answer':'48小时，发送区间2026年8月15日至10月15日，结束不含'}]),
    case('clarification_dates', '按日期分析各厂商最近两个月的发送量、到达率、点击/发送率变化',
         expected(['sent','delivery_rate','click_rate'],['day','manufacturer'],start='2026-08-01',end='2026-10-01'), dialogue=[
             {'question':'请指定回执观察时长','answer':'48小时'},
             {'question':'数据截止10月15日，48小时观察要求发送区间结束不晚于10月13日，请确认起止时间','answer':'2026年8月1日至10月1日，结束不含，仍观察48小时'}]),
]
