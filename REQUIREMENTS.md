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
- novelai-python SDK（上游 API 调用、消耗计算）
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
| 消耗预估 | 使用 novelai-python SDK 中的 `CostCalculator.calculate()` 方法，根据模型、尺寸、步数、采样器等参数预估每个请求的 anlas 消耗 |
| 预估边界 | `CostCalculator` 属于代理层预算控制依据，不视为 NovelAI 官方账单真相；当 NovelAI 模型、采样器、工具或免费额度规则变化时，预估结果可能产生偏差 |
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

第一版只承诺兼容下表列出的端点。兼容性以官方 API 文档、实际抓包样例和 `novelai-python` 当前请求模型三者交叉验证为准；未验证字段应透传或显式拒绝，避免静默丢弃导致用户误以为原生参数生效。

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/ai/generate-image` | POST | 文生图/图生图/重绘（核心端点） |
| `/ai/upscale` | POST | 图片放大 |
| `/ai/augment-image` | POST | 图片增强（情感/调色） |
| `/ai/generate-image/suggest_tags` | GET | 标签建议 |
| `/user/subscription` | GET | 用户订阅信息（代理层返回配额数据） |

**请求格式**：兼容已验证端点的 NovelAI 原生请求/响应格式，Header 中传入：
```
Authorization: Bearer <proxy-api-key>
```

已验证客户端只需将 SDK 的 endpoint 指向代理地址即可使用；未验证客户端不承诺完全无缝兼容。

请求流程：

```
请求 → API Key 验证 → 限频检查 → anlas 消耗预估 → 数据库事务预占额度 → 入队等待 → Worker 调用上游 SDK → 结果返回 → 确认扣减或释放预占额度 → 记录日志
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

novelai:
  api_key: ""  # 上游 NovelAI 账号的 JWT token
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
- 自动恢复只针对「因验证失败被停用」的账号。管理员在后台手工停用的账号不会被验证通过恢复，用户无法借重新登录自行解封；管理员真的改动了启用状态（启用↔停用）后，该账号即脱离自动恢复的管辖，后续再由验证失败停用才会重新纳入。仅编辑其他字段不影响自动恢复——后台编辑表单每次保存都会原样回传当前启用状态，只有提交值与当前值不同才算手工改动。升级到本版本之前就已停用的存量账号无法区分停用来源，一律按管理员停用处理，需要管理员手工启用。
- `default_group_id` 是本地 `user_groups.id`，服务启动时会校验该组存在且启用。
- `client_secret` 和 `session_secret` 只能写入本地 `config.yaml`，不能提交到 Git。
- 自助账号页只展示和重置本项目 Proxy API Key；代理 API 不接受 Discord token。
- 系统不持久化 Discord `access_token` 或 `refresh_token`，也不应在日志中输出这些 token。

组共享限流与个人限流同时生效。请求先检查个人限流，再检查用户所属启用用户组的共享限流；任一超限都会返回 `429`。组限流按组内成员在窗口内非 `rejected` 的 `DISTINCT request_id` 统计，重试 attempt 不重复计数。

用户组还有一套独立的「组内每人限频」模板（`group_member_rate_limit_rules`），语义与组共享限流不同：保存组配置时把模板展开写入每个成员自己的 `rate_limit_rules`，因此**每个成员各自独立计数**，超限时 `limit_scope` 仍是 `user`。与「每日免费小图限制」一致，它是写入时复制的默认值，不是运行时继承：新建成员和 Discord 自助注册会继承模板；修改组模板后可选择「仅覆盖跟随组配置的成员 / 覆盖全部成员 / 仅保存组配置」；成员被手动改过后，也可以在用户编辑页勾选「保存时套用组默认值」把规则拉回组模板。

### 2.9 暂不实现（预留扩展）

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
│   ├── main.py              # FastAPI 入口 + 启动逻辑
│   ├── config.py            # config.yaml 加载
│   ├── database.py          # SQLite 初始化 & 连接管理
│   │
│   ├── models/              # 数据模型（aiosqlite 或 SQLAlchemy）
│   │   ├── __init__.py
│   │   ├── user.py
│   │   ├── rate_limit.py
│   │   ├── anlas_quota.py
│   │   └── usage_log.py
│   │
│   ├── auth.py              # API Key 验证（Depends 注入）
│   ├── rate_limiter.py      # 限频检查逻辑
│   ├── quota_manager.py     # Anlas 额度管理（计算、扣减、重置）
│   ├── queue_manager.py     # 请求队列 + Worker
│   │
│   ├── proxy/               # 代理路由（兼容 NovelAI 原生格式）
│   │   ├── __init__.py
│   │   ├── generate_image.py
│   │   ├── upscale.py
│   │   ├── augment_image.py
│   │   └── subscription.py
│   │
│   ├── admin/               # 管理面板路由
│   │   ├── __init__.py
│   │   ├── auth.py
│   │   ├── dashboard.py
│   │   ├── users.py
│   │   └── logs.py
│   │
│   └── templates/           # Jinja2 模板
│       ├── base.html
│       ├── login.html
│       ├── dashboard.html
│       ├── users.html
│       ├── user_edit.html
│       └── logs.html
│
├── static/                  # 静态文件（CSS）
│   └── style.css
│
├── config.yaml              # 配置文件
├── requirements.txt
├── novelai-python/          # 上游 SDK（已有）
└── run.py                   # 启动入口
```

---

## 五、API 错误响应规范

| 场景 | HTTP 状态码 | 响应体 |
|------|------------|--------|
| API Key 缺失或无效 | 401 | `{"message": "Invalid or missing API Key"}` |
| 用户被禁用 | 403 | `{"message": "Account disabled"}` |
| 限频超限 | 429 | `{"message": "Rate limit exceeded: ...", "retry_after": 30, "limit_scope": "user"}` |
| 用户组限频超限 | 429 | `{"message": "Group rate limit exceeded: ...", "retry_after": 30, "limit_scope": "group"}` |
| Anlas 余额不足 | 402 | `{"message": "Insufficient anlas: need X, have Y"}` |
| 队列满载 | 503 | `{"message": "Queue full, please retry later"}` |

---

## 六、待确认

> 以上需求已根据对话整理完毕。如有需要修改或补充的地方请指出，确认后将进入实现阶段。
