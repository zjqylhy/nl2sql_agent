# 技术方案 v1.0

已确认。Go 负责 HTTP、鉴权、权限与元数据；Python 负责模型意图、时间解析、SQL AST 安全检查与分析数据库只读取数。浏览器不直接调用 Python。Go 从当前账号构造可信权限上下文，Python 从服务端租户/角色配置选择受限 MySQL 账号，模型不能选 DSN 或租户。

Go 使用 PostgreSQL 持久化账号、会话、任务、事件及 outbox，任务变更采用条件更新。Cookie 随机会话令牌仅存哈希；CSRF 要求写操作 Origin 同源；线上强制 secure cookie。登录失败统一提示，限速；权限变化后会话请求重新加载账号。管理员 CLI 用独立管理连接 bootstrap；应用启动执行数据库迁移属于部署管理职责。

M1 任务按一用户一活跃任务调度，outbox 行领取使用数据库租约。Python API 超时短于租约；Go 在停止时取消调用，在结果落库前再次校验状态和执行版本。崩溃时过期任务有限恢复，超出预算终止。历史事件带序号，轮询 PostgreSQL 后通过 SSE 发出，Last-Event-ID 支持续传；关闭浏览器不会停止任务。

所有指标经过订单粒度聚合视图。只读查询仅针对 metric_orders，数据库账号不得读取基表；地区限制在 SQL SECURITY DEFINER 视图内表达。SQL AST 校验仅允许 SELECT、单授权表、批准列和函数，不允许 CTE、子查询、UNION、写操作、系统表、用户变量、任意函数。SQL 参数独立绑定，LIMIT 上限 1000、执行超时 10s。

M1 支持显式指标/时间输入，也支持配置真实 LLM 的自然语言意图解析。未配置模型时，只运行明确标记的受限规则解析（单指标、地区、日/周/月及显式区间）；不使用伪造模型结果。模型输出不含自由 SQL，受 schema 与语义校验约束。数据不可用时明确失败，不退回模拟数据。

部署用 Compose 包含 PostgreSQL、MySQL、agent、Go API、前端。Redis/Celery、对象存储、LangGraph checkpoint 随 M2/M3 接入。数据库和 agent 不映射公网端口，反向代理提供同源路由和 TLS。生产密码与 API 密钥通过环境/secret 注入，仓库只保存样例。备份分别覆盖平台 PostgreSQL、MySQL 和报告对象。
