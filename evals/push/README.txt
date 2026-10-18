推送与设备分析：模拟数据评审样包 v0.1

所有记录为人工生成的模拟数据。用途是评审字段、验证统计口径与开发测试，不能证明真实推送效果、厂商优劣或真实业务规模。没有混入公开研究数据或用户真实数据。

数据范围
发送区间：[2026-04-01T00:00:00+08:00, 2026-10-01T00:00:00+08:00)。
活跃事件截至：2026-10-15T00:00:00+08:00，不含该时刻。
快照时刻：2026-10-15T00:00:00+08:00。所有时间均为 ISO 8601，使用 +08:00。
600 条应用设备记录对应 400 个模拟物理设备 ID。相同设备可以注册多个应用，按 (app_id, device_id) 去重。
device_id 为直接生成的 dev_000001 一类标识，没有真实来源，无需额外脱敏；不包含 IMEI、广告 ID 或可用推送 Token。

文件与粒度
apps.jsonl：每应用一行，主键 app_id。app_name 为虚构应用名。
devices.jsonl：每应用设备一行，主键 (app_id, device_id)。manufacturer 是设备厂商，os 是操作系统；registered_at 为注册时刻；last_active_at 为快照前最后一次应用前台事件；notification_permission 和 push_status 均为快照时刻状态，不能直接用于历史状态统计。
device_state_history.jsonl：每应用设备每个状态区间一行，区间 [valid_from, valid_to)，valid_to=null 表示持续至快照。notification_permission=granted/denied/unknown。
device_channel_history.jsonl：每路由每个 Token 状态区间一行；route_id 关联应用设备和实际 channel，token_status=valid/invalid/unknown。token_ref 仅是不可用的模拟引用。设备厂商与实际通道独立：部分 Android 设备另有 FCM 路由。
device_activity.jsonl：每次应用前台事件一行，主键 activity_id，occurred_at 是实际事件时间。应用活跃与推送点击独立生成。
push_deliveries.jsonl：每条消息向一个应用设备的逻辑投递一行，主键 delivery_id。message_id 为批次内消息标识；route_id/channel 为实际选用路由。sent_at 为首次实际发送尝试，accepted_at 为厂商受理，delivered_at 为首次已收到回执的到达时间，clicked_at 为首次已收到回执的点击时间。null 表示未观察到事件；不能统一解释为失败。final_status 和 failure_reason 反映截至快照的已知结果。
push_attempts.jsonl：每次实际发送尝试一行，主键 attempt_id，attempt_index 是逻辑投递内序号；outcome=accepted/transient_failure/permanent_failure。重试不增加逻辑发送量。
push_events.jsonl：每条收到的原始回执一行，receipt_id 唯一，event_id 为业务去重键。occurred_at 是发生时间，received_at 是采集时间。允许重复回执和采集顺序颠倒；event_type=delivered/clicked/expired。以事件实际发生时间统计窗口，以 received_at 截断某个历史时点当时可见的数据。
expected_results.jsonl：四组自定义窗口与观察时长的静态预期结果，供实现比对，不是已接入项目的评测成绩。
validation.json：生成后的关系、时间和场景检查及各 JSONL 的 SHA256。初次独立评审样包另含 SQLite 统计复核；仓库中的 agent/tests/test_push.py 验证真实 PostgreSQL 视图和持久任务。
各业务文件均带 is_synthetic=true。

建议指标口径，待正式 Spec 确认
1. 发送量：按 sent_at 属于用户指定 [start,end) 的 delivery_id 去重计数。调用尝试量另计。
2. 已确认到达率：上述发送批次中，在 sent_at+观察时长之内有到达回执的逻辑投递数 / 逻辑发送量。缺回执保留未知；该值不声称是真实完整到达率。
3. 点击/发送率：同一发送批次、同一观察时长内有点击的逻辑投递数 / 逻辑发送量。
4. 点击/到达率：分子只能取具有到达回执且同时具有点击回执的交集，不能直接将所有点击除以已确认到达数。缺到达回执的点击单列，不补造到达时间。
5. 有效设备：时点上通知权限明确 granted，且至少一个路由 Token 明确 valid。
6. 无效设备：时点上通知权限明确 denied，或全部路由 Token 明确 invalid。
7. 未知设备：不满足前两类的设备；有效、无效、未知互斥且覆盖已注册设备。
8. 活跃设备：指定 [start,end) 内至少发生一次应用前台事件的 (app_id,device_id) 去重数。没有七天限制。跨应用物理设备数是另一个指标，需明确按 device_id 去重。
9. 历史设备数量按所选 as_of 时点查询状态区间；期末存量不可按日累加。未注册设备不计入该时点。
10. 查询时间、事件观察时长和设备状态时点是三个独立参数。示例 24/48/72 小时均非固定产品上限。观察未成熟时必须标注不完整。
11. 关联前先按目标粒度去重或聚合，不能用多行回执/活动直接联表后 COUNT(*)。

模拟场景
同一设备跨应用注册、注册时间不同、权限开关、Token 失效/恢复、多路由、未活跃设备、失败发送、重试、延迟/重复回执、跨天点击、点击存在但到达回执缺失、未知状态。
app_1 的 xiaomi_push 在 2026-09-01 以后被人为降低到达概率并增大时延，仅是验证两期核查逻辑的注入场景，不代表该厂商实际质量，也不能用它证明系统具有因果归因能力。
种子固定为 20261018。运行 python -m agent.tools.generate_push --output data/push，业务文件生成到 data/push/data。真实数据需单独核对回执覆盖、字段含义和使用权限。

公开来源检索，2026-10-18
https://github.com/komis1/ami2018-notifications
研究数据包含 14 用户的 176195 条 Android 通知日志，有应用包名、通知出现/移除时间；公开字段不构成服务器发送、厂商受理、设备到达、点击和设备有效性全链路。仓库标 GPL-3.0，本样包没有导入其数据或代码。
https://sites.google.com/view/mobile-phone-use-dataset
342 名参与者的手机使用数据，含前台应用与收到通知等事件，发布数据匿名化；公开介绍未提供所需完整推送全链路和通道 Token 生命周期。未下载或导入。
https://docs.mapp.com/docs/raw-data-export-file
商业产品字段参考，包含推送到达/点击时间、应用、DeviceUUID、Manufacturer 等。是导出格式参考，不是公开业务记录下载。
https://developer.apple.com/documentation/pushkit/exporting-delivery-metrics-logs
APNs 按开发者账号及应用导出的汇总指标，包含状态、日期、计数；需要自己的账号导出 Token，不能作为公共逐设备全链路数据集。
结论限于本次核查来源：未找到能直接覆盖全部字段需求且可公开下载使用的完整数据集，不表示互联网上不存在其他数据。

运行与验证
用户已确认此业务方案。规格见 docs/push-device-spec.md；生成和导入说明见 README.md；验证记录见 docs/push-device-results.md。模拟数据不能作为真实业务效果或模型准确率证据。
