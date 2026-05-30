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

### 2.8 暂不实现（预留扩展）

- 用户自助注册 / 自助申请 Key
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
| tier | TEXT | normal / vip |
| is_active | BOOL | 是否启用 |
| created_at | TIMESTAMP | 创建时间 |

### rate_limit_rules

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| user_id | INTEGER FK→users | 关联用户 |
| period | TEXT | minute / hour / day / month |
| max_requests | INTEGER | 周期内最大请求次数 |
| is_active | BOOL | 是否启用 |
| created_at | TIMESTAMP | 创建时间 |

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
| 限频超限 | 429 | `{"message": "Rate limit exceeded: ...", "retry_after": 30}` |
| Anlas 余额不足 | 402 | `{"message": "Insufficient anlas: need X, have Y"}` |
| 队列满载 | 503 | `{"message": "Queue full, please retry later"}` |

---

## 六、待确认

> 以上需求已根据对话整理完毕。如有需要修改或补充的地方请指出，确认后将进入实现阶段。
