# NovelAI Proxy - 需求规格文档

**版本**: 1.0
**日期**: 2026-05-25

---

## 一、项目概述

构建一个 NovelAI API 代理服务，实现多人共享使用同一个 NovelAI 付费账号，同时进行请求限制、排队管理、额度统计。

### 核心目标

1. **小图请求控制**：对免费小图生成进行频次限制，避免单个用户过度滥用
2. **大图额度管理**：对消耗 anlas 的大图生成进行额度统计和控制
3. **请求队列**：避免多用户并发请求到达上游触发 429，确保上游请求串行化

### 技术栈

- Python 3.11+
- FastAPI（代理 API + 管理后端）
- curl-cffi（直接调用 NovelAI 上游 API）
- anlas_sync（基于 NovelAI 前端规则的 anlas 消耗预估）
- SQLite（数据持久化）
- Jinja2（管理面板模板渲染）
- asyncio（请求队列）

---

## 二、功能需求

### 2.1 用户管理

| 需求 | 说明 |
|------|------|
| 管理员手动分配 API Key | 管理员通过管理面板创建用户，系统生成唯一的 Proxy API Key |
| 用户状态管理 | 支持启用/禁用用户，禁用后 API 请求被拒绝 |
| 用户等级 | 支持 `normal` 和 `vip` 两个等级，VIP 用户在队列中有更高优先级 |
| 预留扩展 | `users` 表结构和 API 架构预留用户自助注册、自助申请 Key 的扩展空间 |

### 2.2 请求限频

| 需求 | 说明 |
|------|------|
| 多条规则 | 每个用户可以绑定多条限频规则 |
| 规则周期 | 支持 `minute`、`hour`、`day`、`month` 四种周期 |
| 规则模式 | **所有规则均未超限才放行**（AND 逻辑）。例如：每分钟 ≤ 3 次 AND 每天 ≤ 100 次 AND 每月 ≤ 2000 次，全部满足才放行 |
| 计数来源 | 从 `usage_logs` 表中统计对应时间窗口内的请求记录数 |
| 超限响应 | 返回 `429 Too Many Requests`，附带 `Retry-After` 头和可读的错误信息 |
| 组内每人限频 | 用户组可配置一套「组内每人限频」模板，保存时展开写入各成员自己的限频规则，每个成员独立计数。这是写入时复制，不是运行时继承 |

### 2.3 Anlas 额度管理

| 需求 | 说明 |
|------|------|
| 消耗预估 | 使用 `anlas_sync/anlas_pricing.py` 中从 NovelAI 前端同步的公式和 `pricing_data.json`，根据模型、尺寸、步数、采样器、订阅等级与引用参数预估 anlas 消耗 |
| 预估边界 | 本地前端公式属于代理层预算控制依据，不视为 NovelAI 服务端账单真相；当模型、工具或免费额度规则变化时，应重新运行 `anlas_sync` 同步与对拍流程 |
| 免费/收费判断 | 预估消耗为 0 = 免费小图（不占用用户 anlas 额度）；> 0 = 收费请求（需要预占/扣减用户 anlas 额度） |
| 用户额度 | 每个用户有独立的 anlas 额度池 |
| 额度预占 | 收费请求入队前必须在数据库事务中预占额度，避免多个排队请求同时看到余额充足导致超发 |
| 额度确认 | 上游请求成功后将预占额度确认为已使用；请求未到达上游或明确失败时释放预占额度 |
| 实际余额同步 | 可定期请求上游 `/user/subscription` 获取账号剩余 anlas，用于发现预估漂移、管理员告警和人工校准；不自动覆盖用户侧分配额度 |
| 重置周期 | 默认按添加 Key 的时间（账号创建日期）按月重置；支持自定义重置周期（月/周/天/不重置） |
| 额度不足 | 返回 `402 Payment Required`，附带剩余额度和所需消耗信息 |
| 额度查询 | 代理 `/user/subscription` 端点返回当前用户的配额信息（伪造 NovelAI 原生格式） |

### 2.4 请求队列

| 需求 | 说明 |
|------|------|
| 串行化 | 上游请求同时最多 1 个并发，避免触发 429 `ConcurrentGenerationError` |
| 优先级 | VIP 用户请求优先于 normal 用户 |
| 排队策略 | 同优先级内 FIFO（先到先得） |
| 超限拒绝 | 队列中等待请求数达到上限（可配置）时，新请求直接返回 `503 Service Unavailable` |
| 实现方式 | `asyncio.PriorityQueue` + 后台 Worker 协程 |

### 2.5 代理 API（兼容 NovelAI 原生格式）

第一版只承诺兼容下表列出的端点。兼容性以官方 API 文档、实际抓包样例和项目自有请求模型交叉验证为准；未验证字段应透传或显式拒绝，避免静默丢弃导致用户误以为原生参数生效。

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/ai/generate-image` | POST | 文生图/图生图/重绘（核心端点） |
| `/ai/upscale` | POST | 图片放大 |
| `/ai/augment-image` | POST | 图片增强（情感/调色） |
| `/ai/encode-vibe` | POST | Vibe 编码 |
| `/ai/generate-image/suggest-tags` | GET | 标签建议（官方路径） |
| `/ai/generate-image/suggest_tags` | GET | 标签建议兼容别名 |
| `/user/subscription` | GET | 用户订阅信息（代理层返回配额数据） |

**请求格式**：兼容已验证端点的 NovelAI 原生请求/响应格式，Header 中传入：
```
Authorization: Bearer <proxy-api-key>
```

已验证客户端只需将 NovelAI API endpoint 指向代理地址即可使用；未验证客户端不承诺完全无缝兼容。

请求流程：

```
请求 → API Key 验证 → 限频检查 → anlas 消耗预估 → 数据库事务预占额度 → 入队等待 → Worker 直接调用上游 HTTP API → 结果返回 → 确认扣减或释放预占额度 → 记录日志
```

### 2.6 Web 管理面板

| 页面 | 功能 |
|------|------|
| 登录页 | 管理员用户名/密码登录（Session 认证） |
| 仪表盘 | 总用户数、今日请求量、总 anlas 消耗、活跃队列长度 |
| 用户列表 | 查看所有用户、启用/禁用、删除 |
| 用户创建/编辑 | 生成 API Key、设置用户等级、备注名 |
| 用户组 | 创建和维护用户组，查看固定组 ID、默认权限、默认额度、成员数和组共享限流 |
| 限频规则编辑 | 为每个用户增删改限频规则（周期 + 次数上限） |
| 额度编辑 | 设置用户 anlas 总额度、重置周期、手动重置已用额度 |
| 使用日志 | 按用户和时间范围查询请求记录，展示消耗明细 |

### 2.7 管理员配置

管理员账号写在项目根目录 `config.yaml` 中：

```yaml
admin:
  username: admin
  password: admin123  # 支持修改

server:
  host: 0.0.0.0
  port: 8080

queue:
  max_concurrent_upstream: 1
  max_queue_size: 50
  upstream_interval_min_seconds: 2
  upstream_interval_max_seconds: 5
  upstream_error_extra_delay_seconds: 5

# 上游 NovelAI 账号（JWT token）已不再写入 config.yaml，
# 统一在管理后台 /admin/upstreams 维护（见 2.9 上游账号管理）。
```

### 2.8 用户组和 Discord 自助注册

用户组是创建用户时复制默认配置的模板，不是实时继承关系。管理员可以在后台“用户组”页面创建用户组，页面会显示固定 SQLite 自增 `id`。启用 Discord 自助注册前，需要先创建一个启用的用户组，并把该 ID 填入本地 `config.yaml` 的 `self_service.discord.default_group_id`。

Discord 自助注册配置示例见 `config.example.yaml`。配置要点：

- `redirect_uri` 必须与 Discord Developer Portal 中登记的 OAuth2 Redirect URI 完全一致。
- `required_guild_id` 是允许注册的 Discord 服务器 ID（Guild ID），不是频道 ID 或角色 ID。
- `require_guild` 控制是否验证服务器成员身份，默认开启；关闭后任何 Discord 用户都可自助注册。
- `require_role` 控制是否额外验证身份组，默认关闭，需要 `require_guild: true` 同时开启。
- `required_role_ids` 是允许注册的身份组 ID 列表，用户拥有其中任意一个即通过。
- 开启 `require_role` 后 OAuth scope 由 `identify guilds` 变为 `identify guilds.members.read`，申请的权限范围**收窄**（只读取指定服务器的成员信息，不再读取用户的全部服务器列表），但已授权用户下次登录会重新看到一次 Discord 授权页。不需要 Bot Token，也不需要特权 Server Members Intent。
- 用户退出服务器**或**失去指定身份组后再次登录，已绑定账号会被自动停用；重新满足验证条件后再次登录会自动恢复启用，原 Proxy API Key 继续可用。关闭 `require_guild` / `require_role` 会让验证平凡通过，因此配置回退后这批账号也会在下次登录时自动恢复。
- 自动恢复只针对「因验证失败被停用」的账号。管理员手工停用的账号不会被验证通过恢复，用户无法借重新登录自行解封；管理员的操作优先于自动恢复，一旦管理员表达了对启用状态的意图，该账号即脱离自动恢复的管辖，后续再由验证失败停用才会重新纳入。升级到本版本之前就已停用的存量账号无法区分停用来源，一律按管理员停用处理，需要管理员手工启用。
- 「管理员表达了意图」按入口判定：管理 API `PATCH /admin/api/users/{id}` 只要请求里出现 `is_active` 就算，即使提交值与当前值相同（对已停用账号再提交一次停用即为确认封禁）；后台编辑表单每次保存都会原样回传当前启用状态，因此以「提交值与当前值不同」为准，只改名字等其他字段不会误伤自动恢复。
- 表单里「未勾选启用」对停用账号而言无法与「没碰这个字段」区分，因此编辑页对因验证停用的账号额外显示独立的「永久停用」开关，勾选后即使启用状态没有变化也按管理员确认封禁处理，管理员无需先启用再停用。该开关只在账号处于验证停用状态时出现。
- `default_group_id` 是本地 `user_groups.id`，服务启动时会校验该组存在且启用。
- `client_secret` 和 `session_secret` 只能写入本地 `config.yaml`，不能提交到 Git。
- 自助账号页只展示和重置本项目 Proxy API Key；代理 API 不接受 Discord token。
- 自助账号页显示「最近调用」栏：用户最后一次调用的时间与结果（成功 / 失败 / 已拒绝 / 排队中 / 运行中），失败时给出原因。回看窗口由 `self_service.account.last_call_days` 控制，默认 7 天滚动窗口，设为 0 则隐藏该栏。被代理层在打到上游前拒绝的请求（限频、额度不足等）同样计入；管理员重放产生的日志行不计入。
- 该栏的失败原因**不透传** `usage_logs.error_message` 原文。实测该列会包含上游内网 IP 与端口（`500`）、curl / OpenSSL 报错细节（异常类名码），以及形如 `u{用户ID}-{备注}` 的他人上游 ID（`no_available_upstream`）。仅 `400`、`429` 及本项目自己生成的错误码放行原文，其余一律折叠为通用文案；`401` / `402` 因描述的是公共号池健康状况，也折叠处理。`upstream_id` 与 `output_files` 两列不向用户展示。
- 系统不持久化 Discord `access_token` 或 `refresh_token`，也不应在日志中输出这些 token。

组共享限流与个人限流同时生效。请求先检查个人限流，再检查用户所属启用用户组的共享限流；任一超限都会返回 `429`。组限流按组内成员在窗口内非 `rejected` 的 `DISTINCT request_id` 统计，重试 attempt 不重复计数。

用户组还有一套独立的「组内每人限频」模板（`group_member_rate_limit_rules`），语义与组共享限流不同：保存组配置时把模板展开写入每个成员自己的 `rate_limit_rules`，因此**每个成员各自独立计数**，超限时 `limit_scope` 仍是 `user`。与「每日免费小图限制」一致，它是写入时复制的默认值，不是运行时继承：新建成员和 Discord 自助注册会继承模板；修改组模板后可选择「仅覆盖跟随组配置的成员 / 覆盖全部成员 / 仅保存组配置」；成员被手动改过后，也可以在用户编辑页勾选「保存时套用组默认值」把规则拉回组模板。

### 2.9 上游账号管理

NovelAI 上游账号（JWT token）统一存在 `novelai_upstreams` 表中，由管理后台 `/admin/upstreams` 维护，不再支持 `config.yaml` 的 `novelai:` 配置段。

- 管理员可增删改上游 key、更换 token、启用/禁用、测试连通性，并维护全局账号等级（`account_tier`，Opus 计费依据）。
- 上游 ID 由管理员自定义；`__all__` 是保留字不能作为 ID，`default` 是常用的默认 ID。ID 创建后不可变（`usage_logs`、`dashboard_hourly_stats`、白名单都按字符串引用它）。
- 调度支持多上游：请求按当前启用的上游集合路由，上游被禁用/删除后会自动从运行态与调度队列移除；命中错误码（默认 `400`、`401`、`402`、`403`）会自动禁用该上游并通知管理员。
- 测试连通性对启用和禁用（含自动禁用）账号都可用：启用账号的探测进入该上游的调度队列，禁用账号没有队列，因此走队列外直连探测。**启用账号的探测与普通请求共用同一条失败处理路径，因此探测返回的错误码命中 `upstream_auto_disable.status_codes` 时同样会自动禁用该账号并通知管理员**——这是预期行为，相当于用一次真实请求确认账号确实不可用。禁用账号的队列外直连探测不接自动禁用逻辑，既不改变启用状态，也不会因此回到调度；同一账号同时只允许一个直连探测在跑，重复触发返回 409 `upstream_test_in_progress`。
- 删除保护：仍被用户或用户组白名单引用的上游不能删除，管理端返回 409 并列出引用方；可先停用或调整白名单。
- 若启用 Discord 自助服务（`self_service.discord.enabled`），普通用户可在 `/account` 页面上传和管理自己的上游 key，开关与上限由 `self_service.upstreams` 控制（`enabled`、`max_per_user`）：
  - 上传的 key 进入**公共池**，所有用户都能用它跑图；上传者只拥有管理权。
  - 自助 key 的 ID 由服务端生成，格式为 `u{用户ID}-{备注}`；备注可空，留空时自动编号（`u12-1`、`u12-2`…，复用已删除的最小未占用编号）。
  - 用户可新增、删除、更换 token（ID 不变）、启用/禁用自己的 key；不能改 ID，不能测试连通性。
  - **归属判定只读 `owner_user_id` 列，绝不解析 ID 字符串**；访问他人或管理员 key 一律返回 404，不暴露「该 ID 是否存在」。
  - 用户被停用/软删除后，其上传的 key 保持原样继续服务，只是失去管理权。
  - 自助 key 同样受删除保护；用户停用/删除仍被白名单引用的 key 时会给管理员写一条通知。
  - 上传表单明示「仅支持 Opus 订阅账号」：`novelai_settings.account_tier` 是全局单值，非 Opus key 会让落到它上面的请求按错误价格计费。

### 2.10 暂不实现（预留扩展）

- 文字生成（`/ai/generate`、`/ai/generate-stream`）代理
- 语音生成（`/ai/generate-voice`）代理
- 多管理员支持

---

## 三、数据库设计

### users

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| api_key | TEXT UNIQUE | Proxy 层 API Key |
| name | TEXT | 用户备注名 |
| group_id | INTEGER FK→user_groups | 主用户组，可为空 |
| tier | TEXT | normal / vip |
| is_active | BOOL | 是否启用 |
| created_at | TIMESTAMP | 创建时间 |

### user_groups

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 固定自增主键，可复制到 `config.yaml` |
| name | TEXT | 用户组名称 |
| is_active | BOOL | 是否启用 |
| default_tier | TEXT | 创建用户时复制的默认等级 |
| default_free_small_only | BOOL | 创建用户时复制的免费小图策略 |
| default_allowed_endpoints | TEXT | 创建用户时复制的允许接口 |
| default_allowed_upstreams | TEXT | 创建用户时复制的允许上游 |
| default_anlas_total | INTEGER | 创建用户时复制的默认额度总额 |
| default_reset_period | TEXT | 创建用户时复制的重置周期 |
| default_reset_day | INTEGER | 创建用户时复制的重置日 |

### rate_limit_rules

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| user_id | INTEGER FK→users | 关联用户 |
| period | TEXT | minute / hour / day / month |
| max_requests | INTEGER | 周期内最大请求次数 |
| is_active | BOOL | 是否启用 |
| created_at | TIMESTAMP | 创建时间 |

### group_rate_limit_rules

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| group_id | INTEGER FK→user_groups | 关联用户组 |
| period | TEXT | minute / hour / day / month |
| max_requests | INTEGER | 组内共享周期请求上限 |
| is_active | BOOL | 是否启用 |
| created_at | TIMESTAMP | 创建时间 |

### group_member_rate_limit_rules

组内每人限频模板。保存组配置时按所选覆盖范围展开写入成员各自的 `rate_limit_rules`，运行时不参与限频判定。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| group_id | INTEGER FK→user_groups | 关联用户组 |
| period | TEXT | minute / hour / day / month |
| max_requests | INTEGER | 每个成员各自的周期请求上限 |
| is_active | BOOL | 是否启用 |
| created_at | TIMESTAMP | 创建时间 |

### discord_user_links

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| user_id | INTEGER UNIQUE FK→users | 关联本地用户 |
| discord_user_id | TEXT UNIQUE | Discord 用户稳定 ID |
| discord_username | TEXT | Discord 用户名 |
| discord_global_name | TEXT | Discord 全局显示名 |
| discord_avatar | TEXT | Discord 头像 ID |
| created_at | TIMESTAMP | 绑定创建时间 |
| last_login_at | TIMESTAMP | 最近自助登录时间 |

### user_anlas_quota

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| user_id | INTEGER FK→users | 关联用户（UNIQUE） |
| total | INTEGER | anlas 总额度 |
| used | INTEGER | 已确认使用的 anlas |
| reserved | INTEGER | 已预占但请求尚未完成的 anlas |
| reset_period | TEXT | month / week / day / never |
| reset_day | INTEGER | 重置日（月周期=1-28，周周期=1-7，日周期=0） |
| last_reset_at | TIMESTAMP | 上次重置时间 |
| created_at | TIMESTAMP | 创建时间 |

额度可用量计算：

```
available = total - used - reserved
```

### usage_logs

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| request_id | TEXT UNIQUE | 请求唯一 ID，用于关联排队、扣费和错误 |
| user_id | INTEGER FK→users | 关联用户 |
| action | TEXT | generate / img2img / infill / upscale / augment |
| model | TEXT | 使用的模型 |
| width | INTEGER | 图片宽度 |
| height | INTEGER | 图片高度 |
| steps | INTEGER | 采样步数 |
| n_samples | INTEGER | 生成数量 |
| estimated_anlas_cost | INTEGER | 预估消耗的 anlas（0 = 免费） |
| final_anlas_cost | INTEGER | 最终确认消耗的 anlas；第一版默认等于预估值 |
| queued_ms | INTEGER | 排队等待时长（毫秒） |
| status | TEXT | queued / running / success / failed / rejected / refunded |
| error_code | TEXT | 上游或代理层错误码 |
| error_message | TEXT | 错误摘要 |
| created_at | TIMESTAMP | 创建时间 |
| completed_at | TIMESTAMP | 完成时间 |

---

## 四、项目目录结构

```
novelai_proxy/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI 入口与生命周期
│   ├── proxy/routes.py         # 代理端点
│   ├── upstream.py             # curl-cffi 上游传输层
│   ├── novelai_models.py       # 自有请求模型
│   ├── novelai_enums.py        # 自有 NovelAI 枚举
│   ├── costing.py              # anlas 计费适配层
│   ├── routing_queue.py        # 多上游调度
│   ├── upstream_queue.py       # 单上游执行队列
│   ├── quota_manager.py        # Anlas 额度管理
│   ├── rate_limiter.py         # 限频检查
│   ├── database/               # SQLite 连接、schema 与迁移
│   ├── admin/                  # 管理后台与管理 API
│   └── templates/              # Jinja2 模板
├── anlas_sync/
│   ├── anlas_pricing.py        # 前端计费公式的 Python 实现
│   └── generated/pricing_data.json
├── tests/                       # 自动化测试
├── static/                      # 静态资源
├── config.example.yaml          # 可提交配置模板
├── requirements.txt
└── run.py                       # 启动入口
```

---

## 五、API 错误响应规范

| 场景 | HTTP 状态码 | 响应体 |
|------|------------|--------|
| API Key 缺失或无效 | 401 | `{"message": "API Key 无效或缺失"}` |
| 用户被禁用 | 403 | `{"message": "账号已被禁用"}` |
| 个人限频超限 | 429 | `{"message": "请求频率超限：分钟最多 3 次", "retry_after": 30, "limit_scope": "user"}`，附 `Retry-After` 头 |
| 用户组限频超限 | 429 | `{"message": "用户组请求频率超限：分钟最多 10 次", "retry_after": 30, "limit_scope": "group"}`，附 `Retry-After` 头 |
| 免费小图日限额超限 | 429 | `{"message": "免费小图每日限额已用尽：每日最多 10 次", "retry_after": 3600, "limit_scope": "user", "limit": ..., "used": ...}`，附 `Retry-After` 头 |
| Anlas 余额不足 | 402 | `{"message": "anlas 额度不足：需要 X，可用 Y", "need": X, "have": Y}` |
| 队列满载 | 503 | `{"message": "队列已满，请稍后重试"}` |

> 以上文案为当前实现的中文错误信息；限频的具体周期与次数随配置变化，`retry_after` 为秒。

---

## 六、待确认

> 以上需求已根据对话整理完毕。如有需要修改或补充的地方请指出，确认后将进入实现阶段。
