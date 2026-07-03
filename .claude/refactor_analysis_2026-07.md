# NovelAI Proxy 后端重构计划(2026-07)

pytest 基线(2026-07-03): `314 passed in 41.70s`。

## Context

本项目是多人共享 NovelAI API 的代理服务(FastAPI + SQLite 单连接 + asyncio 队列,单进程个人规模部署)。功能已稳定,本轮目标是整治"急需改进"的后端代码。

项目已经历两轮重构:第一轮拆出 service/costing/policies/usage_logs 仓库;第二轮(`.claude/refactor_analysis_2026-06.md`)的全部建议已于 6 月中旬落地(队列拆分、RequestAccounting、RetryPolicy、database 拆包、deps.py、领域异常、allowlists 等)。本轮分析基于最新代码(约 77 个后端源文件)做了三个区域的全量审查,关键结论均已核实到行号。

**用户已确认**:① 本轮实施 P0 + P1 全面整治;② anlas 额度月/周重置时区统一为 UTC+8;③ 完整分析先写入 `.claude/refactor_analysis_2026-07.md` 存档(延续惯例)。

**总体判断**:主链路分层(routes → service → routing_queue → upstream_queue → RequestAccounting)清楚,6 月重构成果保持良好。当前风险集中在:运行模型(同步 SQLite + VACUUM/RLock 互锁可冻结全服务)、数据完整性(迁移非原子、崩溃后 reserved 永久泄漏)、安全(管理会话 cookie 确定性且无法吊销)、以及 6 月拆分后新累积的债务(shim/死代码、三处超长函数、worker 泄漏、装配重复、测试无 conftest)。

---

## 第 0 步:存档分析文档

把本轮完整分析(含 P2 遗留清单)写入 `.claude/refactor_analysis_2026-07.md`,并记录当前 pytest 基线通过数。

---

## P0 批次(1–11):稳定性 / 数据完整性 / 安全

### 批次 1|启动时回收崩溃残留的额度预留(D)

- 现状:`user_anlas_quota.reserved` 与 `free_small_daily_usage.reserved` 持久化;进程崩溃后在途请求的预留永不释放,`reset_period="never"` 用户永久泄漏。队列无持久化(重启后在途请求为零),单进程,启动全量清零安全。
- 改动:`QuotaManager.reclaim_orphan_reserved()` 与 `FreeSmallDailyLimitManager.reclaim_orphan_reserved()`(`UPDATE ... SET reserved = 0 WHERE reserved != 0`,事务内,返回行数);`app/main.py` lifespan 在两 manager 构造后、`proxy_queue` 构造前调用;行数 > 0 记 warning。方法注释注明"仅单进程部署安全"。
- 测试:预写 reserved → TestClient 启动 → 断言清零且 used/total 不变。

### 批次 2|anlas 额度重置时区统一为 UTC+8(G,用户已确认)

- 现状:`app/quota_manager.py:167-183` `_next_reset_at` 按 UTC 零点计算月/周重置边界,`normalize_reset_day` 用 UTC 的 now 取默认日;而 `app/timezones.py` 声明 UTC+8 为项目统一时区(免费小图窗口、仪表盘统计已遵循)。
- 改动:两函数改以 `DISPLAY_TIMEZONE` 计算日界(转 UTC+8 求边界,再转回 UTC 比较)。已有用户下次重置时刻提前 8 小时,无数据丢失。
- 测试:`tests/test_quota_manager.py` 补边界用例(UTC 16:00 = UTC+8 零点跨日)。

### 批次 3|VACUUM 改用独立连接,消除 RLock 互锁(A-1)

- 现状:`app/database/connection.py:65-77` `vacuum()` 持有全局 RLock;`app/database_maintenance.py:33` 在 to_thread 中执行它时,事件循环上任何 db 调用都会在 `RLock.acquire` 上硬阻塞(不可超时、不可中断)→ 整个服务冻结,时长等于 VACUUM 耗时。
- 改动:`vacuum()` 重写为每次新开独立 `sqlite3.connect(self.path)`(创建/使用/关闭都在同一线程内),设 `busy_timeout=60000`,执行 `wal_checkpoint(TRUNCATE)` → `VACUUM` → `wal_checkpoint(TRUNCATE)`,finally 关闭;**不碰 `self._lock` 与 `self.conn`**。`database_maintenance.py` 无需改动。
- 代价说明:VACUUM 独占期间主连接写操作可能在 5 秒 busy_timeout 后报 `database is locked`——凌晨执行 + 个人规模可接受,远好于现状的无限冻结。
- 测试:后台线程 vacuum 期间主连接可继续读写的双线程冒烟;现有 `test_database_maintenance.py`、`test_admin_database.py` 回归。

### 批次 4|日志清理改分批小事务并移出事件循环(A-2)

- 现状:`app/admin/database.py:247-259` `_cleanup_logs` 单个大事务 DELETE,每删一行触发 8 条仪表盘触发器 SQL(`app/dashboard_stats/triggers.py:40-44`),大批量清理长时间持锁并卡死事件循环。
- 改动:改循环 `DELETE FROM usage_logs WHERE id IN (SELECT id FROM usage_logs WHERE <条件> LIMIT 500)`,每批独立 `db.transaction()`,rowcount=0 退出;整体包 `asyncio.to_thread`。批间释放 RLock 让点写插队。
- 行为差异:不再单事务原子——清理幂等可重跑,提交说明写明。
- 测试:插入 >500 条跨批删除后全量删净、`dashboard_hourly_stats` 同步递减(现有 `test_admin_dashboard_hourly_stats.py` 打底)。

### 批次 5|管理后台重查询移出事件循环(A-3)

- 改动:`app/admin/database.py` `_vacuum_database`(:367,手动 VACUUM 目前直接在事件循环上跑)、`_database_stats`(:146-221 全表 SUM)、`_clear_payloads`(:271-364)、`_archive_payloads`;`app/admin/logs.py:130-194` CSV 导出(10000 行)——统一 `asyncio.to_thread` 包装。
- 同时在 `connection.py` 以注释固化同步/异步判定标准:O(1) 点查点写留在事件循环(auth、配额 reserve/confirm/release、usage_logs 状态写、rate_limiter——均有索引支撑);队列 done-callback 内结算必须保持同步(无法 await,且恰为点写);与表大小成正比的操作必须 to_thread 且分批短事务;不引入 aiosqlite(54 个同步 TestClient 测试 + 个人规模下收益为负)。
- 顺带:`app/upstream_queue.py:277` worker 内 `archive_zip_images` 同步写盘改 `asyncio.to_thread`(此处在 async 函数内,可直接 await)。

### 批次 6|usage_logs 列与索引定义收敛为单一来源(B-1,纯重构)

- 现状:列定义 3 份拷贝(`schema.py:93-128` 建表、`schema.py:228-255` 补列清单、`migrations.py:60-151` 重建迁移),索引在 schema.py 与 migrations.py 重复。
- 改动:`schema.py` 定义 `USAGE_LOGS_COLUMN_DEFS`(全列有序单点)、`usage_logs_create_table_sql(table_name, *, if_not_exists)`(保留 `UNIQUE(request_id, attempt_number)` 写法——`migrations.py:52-55` 靠文本匹配它)、`USAGE_LOGS_INDEX_SQL` 全集;补列清单与建表语句改为生成。
- 测试:全量测试都走 init_schema 建库,天然回归。

### 批次 7|usage_logs 重建迁移改为原子流程(B-2)

- 现状:`migrations.py:58` `executescript`(连接 `isolation_level=None` 自动提交、脚本无 BEGIN/COMMIT),DROP→RENAME 之间崩溃丢表;且 `PRAGMA foreign_keys=ON` 下 `DROP TABLE usage_logs` 触发隐式 DELETE,级联清空 `usage_log_payload_archive_refs`(schema.py:159 ON DELETE CASCADE)。
- 改动:按 SQLite 官方 12 步流程重写——`PRAGMA foreign_keys=OFF`(必须在事务外)→ `BEGIN IMMEDIATE` → 建新表(用批次 6 的生成函数)→ INSERT SELECT 回填 → DROP 旧表(FK OFF 不再级联)→ RENAME → 重建索引 → `PRAGMA foreign_key_check` → COMMIT;finally 恢复 FK=ON。触发器由 `initialize_schema` 在迁移后无条件重建,现有顺序天然正确。
- 测试(目前无迁移专测,需补):构造旧版 schema + refs 表带数据 → 跑迁移 → 断言 refs 保留、新约束生效、可重复执行;monkeypatch 中途抛错 → 旧表原样。

### 批次 8|管理后台会话改为带过期的签名令牌(C)

- 现状:`app/admin/auth.py:74-99` cookie 值 = `username:HMAC(password, username)`,确定性、无过期、登出不失效、无法吊销、未设 secure;`session_middleware.py` 每成功响应续期 30 天放大风险。
- 改动:把 `app/self_service/session.py`(已有带过期校验的 HMAC 令牌实现)平移为 `app/signed_tokens.py`(self_service/routes.py:37 是唯一引用点,改 import 后删旧文件);签名密钥从管理密码域分离派生(`HMAC(admin.password, "novelai-proxy-admin-session-v1")`),**改密码即吊销全部会话**(个人项目的吊销机制,注释写明取舍);令牌载荷含 exp(30 天)+ sub=username;现有滑动续期中间件在新实现下自动变为"重签新 exp",零改动;`set_cookie` 增加 `secure=request.url.scheme == "https"`;旧格式 cookie 校验失败自然视为未登录 → 重定向登录页,零迁移代码(提交说明"升级后需重新登录后台")。
- 测试:过期令牌拒绝、篡改签名拒绝、旧格式拒绝、成功响应后 exp 前移;现有 `test_admin_negative_auth.py`、`test_admin_dashboard_websocket.py` 回归。

### 批次 9|收敛 ProxyQueue 构造并公开队列状态接口(E-1,纯重构)

- 现状:`RoutingProxyQueue.__init__`(routing_queue.py:99-118)与 `sync_targets`(:159-175)的 ProxyQueue 17-kwargs 构造整段重复;`:181` 跨对象写私有 `_on_api_error`;`:244` 读私有 `_running_item`。
- 改动:抽 `RoutingProxyQueue._create_upstream_queue(target)` 工厂(字段全在 self,不改 `__init__` 签名——十余处测试直接 kwargs 构造);`ProxyQueue._on_api_error` 改公开属性 `on_api_error`;加只读 property `running_item`。

### 批次 10|移除上游时正确关停其队列 worker(E-2)

- 现状:`sync_targets`(routing_queue.py:150-187)移除 target 只改路 pending,`self._queues` 里的 ProxyQueue 与其 worker 协程永不清理 → 每次删除上游泄漏一个常驻协程,且 `qsize()` 统计遗漏残留项。
- 改动:移除分支改为——先更新 `_targets/_target_order`(现状已如此)→ `_reroute_pending_from_disabled_upstream` → `pop` 队列 → `asyncio.create_task(queue.stop(drain=True))`(drain 只等在途 item 自然跑完,不杀请求;结算走既有 done-callback,重试用新 target 序);stop task 存入集合防泄漏,`RoutingProxyQueue.stop()` 追加 gather 保证应用关闭不遗留协程;`_adaptive_scores` 保留(重加同 id 继承历史分,与现行为一致)。
- 行为差异:重加同 id 走新建分支(原来复用旧对象)——`test_queue_disable_reroute.py:311/611` 是现成回归项。
- 测试:移除后队列字典不含该 id 且 worker task done;移除时在途 item 正常完成结算;移除→重加→新请求正常。

### 批次 11|上游 client_provider 收敛到运行时管理器(F)

- 现状:`main.py:81-83` 与 `app/upstreams.py:309-311` 逐字重复的 client_provider lambda(运行时回读 app.state 三槽位);`upstream_runtime.sync()` 在 lifespan 被调两次(main.py:75、135,第二次纯冗余)。
- 改动:`UpstreamRuntimeManager` 新增 `client_provider_for(upstream_id)` 与 `queue_targets()`;`_sync_queue_targets` 与 main.py 都改用它;删除第二次 `sync()`。**兼容层 `app.state.upstream`/`default_upstream_id`/`upstream_clients` 本轮一字不动**(58 处测试直接注入 `app.state.upstream` 依赖"调用期回读"语义,用注释锁死,防止未来误改为构造期快照)。
- 测试:16 个文件的注入用例即最强回归网;补一条"替换 state.upstream 后 provider 返回新对象"锁语义。

---

## P1 批次(12–20):可维护性热点

### 批次 12|修复 `_selected_user` 漏过滤已删除用户(真实小 bug,先行)

`app/admin/logs.py:398` 查询缺 `deleted_at IS NULL`,与其余 19 处 users 查询不一致。单行修复 + 用例。

### 批次 13|删除兼容 shim 与死代码(含 AGENTS.md 同步)

- 删 `app/queue_manager.py`(44 行纯 import 门面):5 个业务模块(deps.py:13、proxy/service.py:19、proxy/routes.py:34、main.py:38、admin/dashboard.py:22、admin/logs.py:19)+ 7 个测试改回真实模块导入。
- 删 `app/admin/routes.py` shim(仅 main.py:19 使用)。
- 死代码:ProxyQueue 未用的 `quota_manager` 参数(upstream_queue.py:26,所有调用方都在传)、`QuotaManager.reset_all_due`、`upstream.py` 未用的 `from_config`/`generate_image_zip`、`valid_admin_session` 别名、`auth.py:58` 恒真的 compare_digest、deps.py 四个无使用点 provider(get_usage_logs/get_rate_limiter/get_upstream_clients/get_dashboard_events)、`dashboard_stats/__init__.py` 死导入、`admin/dashboard.py:38-54` 测试载荷中未读取的 sm/sm_dyn、tests/test_upstream.py(与 test_upstream_sdk.py 重复)。
- **同步更新 AGENTS.md"核心代码位置"**(现仍指向 app/database.py、app/queue_manager.py、app/admin/routes.py 等已拆分/将删除的路径)。

### 批次 14|错误处理统一

- APIError→HTTP 状态码映射(`int(exc.code) if ... else 502`)4 处复制(proxy/routes.py:282、proxy/service.py:217、admin/logs.py:270、admin/dashboard.py:432)收敛为单一工具函数;suggest-tags 未知异常 503(routes.py:290)与主链路 502(service.py:236)统一。
- `app/upstreams.py` 仓库层混用 HTTPException(409)/ValueError/KeyError(:113/:128/:157-160)改为领域异常(复用 domain_errors.py,main.py 已有全局 handler);`"UNIQUE" in str(exc)` 字符串匹配(:112)改捕 `sqlite3.IntegrityError`。
- `proxy/routes.py:406` 抛 HTTPException 被 :69 `except Exception` 吞掉重写的路径理顺;routes.py:77-84/:140-144 把 `get_settings()` DB 故障误归 400 的分支分离。

### 批次 15|proxy/service.py 超长函数表驱动 + free-small 释放归一

- `submit_binary`(service.py:95-244,约 150 行):七个 except 分支表驱动,消除 QueueFull 处理块逐字复制两遍(:170-181 与 :185-196)。
- `_check_before_queue`(:246-373,约 128 行):五个"insert_rejected + logger + 构造结果"同构块抽统一拒绝构造器。
- `_release_free_small_daily_reservation`(service.py:399-405)与 `request_accounting.py:189-197` 双份释放逻辑归一到 RequestAccounting。
- 顺带:`_insert_queued`/`_insert_rejected`(service.py:407-447)同构收敛;`_submit_zip_task` 改为复用 `_submit_binary_task`(proxy/routes.py:201-259)。

### 批次 16|upstream_queue._run 拆解

`ProxyQueue._run`(upstream_queue.py:147-312,约 166 行):提取"取消检查"(两段逐字重复,:156-171 与 :198-213)、"错误结算"、"成功结算/归档/图床调度"子步骤。现有队列测试(test_queue_*)密集,重构安全网充足。

### 批次 17|admin 子包重复收敛

- `admin/dashboard.py:93-200` `test_upstream` 六连 except 表驱动(与 admin/logs.py:239-275 重放错误映射共享批次 14 的工具)。
- zip→data-url 两份实现(dashboard.py:396-429 vs logs.py:402-426,含各自 content-type 映射)合一。
- `_hour_bucket_rows`/`_date_bucket_rows`(dashboard.py:641-740,两段 50 行 CTE 仅桶表达式不同)参数化合一。
- `insert_queued`/`insert_rejected`(usage_logs.py:77-151,仅 status 一字之差)合一。
- users/groups 两个 `_parse_optional_*int` 行为分叉(users.py:681 非法输入 500,groups.py:455 返回 None)统一为 400。
- `upstreams.py:33` `now_iso()` 删除,改用 `database/clock.utc_now_iso`。

### 批次 18|users/groups.py 字段映射收敛

同一组 7 个逻辑字段的 5 个手工同步 dict 函数(groups.py:386-512)收敛为单一字段规格表;`sync_group_members`(:241-298)与 `update_group_with_propagation`(:323-383)两套传播机制合一。此文件测试覆盖多(test_admin_users.py 1210 行 + groups 相关),回归网可靠。

### 批次 19|管理端鉴权依赖归一(依赖批次 8)

现状三种依赖分配无规律且强度倒挂(改上游密钥收会话 cookie、查用户列表仅 Basic;users/groups API 各有一个例外路由)。批次 8 令牌加固后,统一规则:全部 admin JSON API 用 `require_admin_or_session`,页面用 `require_admin_page_session`,router 级统一挂载(消除 logs.py:130 重复挂载、upstreams.py 路由级/router 级不一致)。

### 批次 20|测试基建 conftest.py + 杂项收尾

- 新增 `tests/conftest.py`:app fixture(收敛 147 处"setenv + import app"仪式)、admin auth fixture(279 处硬编码 `("admin", "admin123")`)、`_create_user`(6 份实现)、`_wait_until`(3 份)。存量测试增量迁移(先收敛 helpers 调用点,不强求一次改完 54 个文件)。
- 杂项:用户列表 N+1(admin/users.py:691-694 逐用户开事务)改批量;config `log_level`/`logging.level` 双轨且 `WARNING` 被静默丢弃(config.py:115 vs 184)修正;`logging_utils.py` 中 `mark_request_total_duration` 越层写库与 `archive_zip_images` 归位到合适模块。

---

## P2:仅记录到分析文档,本轮不做

模板内联 JS 规模(前端 plan.md 领域)、`users.api_key` 残留列移除迁移、suggest-tags 固定打第一个上游、HTML 错误页、requirements 版本锁定、`usage_logs` 身兼三职(审计/限流计数/统计源)的语义耦合、rate_limiter `retry_after` 返回整窗时长、self_service/routes.py 基础设施下沉、每启动全量巡检式迁移统一为 user_version 脚本等。

---

## 验证

- **每批次固定流程**(项目约定):`git status --short` → `git diff --stat` → `.\.venv\Scripts\python.exe -m pytest`;中文提交信息、单一关注点。第 0 步先记录 pytest 基线通过数。
- 批次专项测试已在各批次列出(迁移原子性、令牌矩阵、跨批删除、启动回收、worker 生命周期为必补项)。
- P0 完成后与全部完成后各做一次人工冒烟:`.\.venv\Scripts\python.exe run.py` → `/health`、后台登录/登出、仪表盘 WS 推送、日志页筛选/导出、上游管理页;数据库管理页手动 VACUUM 期间并发访问其他页面验证不再冻结。
- 涉及真实上游的行为(生图链路)不在本轮改动语义,如需可按 AGENTS.md 低成本参数做一次真实 smoke test 收尾。
