# AGENTS.md

## 项目概况

本项目是一个 NovelAI API 代理服务，用于在多人共享同一个 NovelAI 上游账号时，提供统一的鉴权、请求队列、频率限制、额度统计和管理后台。

主要功能：

- 代理 NovelAI 图像生成、放大、图像增强和标签建议接口。
- 使用本项目自己的用户 API Key 做访问鉴权，避免直接暴露 NovelAI API Key。
- 对用户请求做限流，避免单个用户过度使用免费小图生成能力。
- 对上游请求做队列化处理，避免多个用户同时打到 NovelAI 上游。
- 对需要消耗 anlas 的请求做额度预留、确认、释放和日志记录。
- 提供管理后台和管理 API，用于创建用户、调整额度、配置限流规则和查看日志。

核心代码位置：

- `app/main.py`：FastAPI 应用入口、生命周期初始化。
- `app/proxy/routes.py`：代理接口、鉴权后请求提交、额度与日志处理。
- `app/admin/routes.py`：管理后台页面和管理 API。
- `app/database.py`：SQLite 数据库封装与表结构初始化。
- `app/queue_manager.py`：上游请求队列。
- `app/quota_manager.py`：anlas 额度管理。
- `app/rate_limiter.py`：用户请求频率限制。
- `app/upstream.py`：NovelAI 上游 SDK 调用封装。
- `novelai-python/`：第三方 NovelAI Python SDK，作为 Git 子模块记录。

## 本地配置

本地真实配置文件是 `config.yaml`，其中包含 NovelAI API Key 和管理后台密码，已被 `.gitignore` 忽略，不能提交。

可提交的模板文件是 `config.example.yaml`。需要新建本地配置时，从模板复制并填写真实配置。

SQLite 运行数据库默认是 `novelai_proxy.db`，以及对应的 `*.db-shm`、`*.db-wal` 文件，均不提交。

## 虚拟环境

项目使用根目录下的 `.venv` 虚拟环境。

在 PowerShell 中优先使用虚拟环境里的 Python：

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe run.py
```

不要默认使用系统 Python，因为系统 Python 可能没有安装项目依赖。

如需安装依赖，使用：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## 运行方式

启动服务：

```powershell
.\.venv\Scripts\python.exe run.py
```

默认监听地址由 `config.yaml` 控制，当前模板中为：

- `host: 0.0.0.0`
- `port: 8080`

健康检查：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8080/health
```

管理后台：

```text
http://127.0.0.1:8080/admin
```

## 测试方式

运行自动化测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

现有测试主要覆盖：

- 健康检查。
- 管理 API 创建用户。
- 用户订阅和额度查询。
- 代理 API 鉴权。
- 频率限制。
- 额度不足拒绝。
- 队列与日志记录。
- 管理后台登录。

真实 NovelAI 生图测试会访问外网并使用 `config.yaml` 中的真实 NovelAI API Key。执行真实上游测试时，必须避免在输出中打印 API Key 或完整配置内容。

建议真实 smoke test 使用低成本参数，例如：

- `model: nai-diffusion-3`
- `width: 512`
- `height: 768`
- `steps: 1`
- `n_samples: 1`

测试后应确认：

- `/ai/generate-image` 返回 `201 application/zip`。
- zip 内包含非空图片文件。
- `usage_logs.status` 为 `success`。
- 成功请求后额度统计正确。
- 失败请求后 `reserved` 额度已释放。

## Git 与提交约定

提交前必须检查：

```powershell
git status --short
git diff --stat
```

确认不要提交以下本地文件：

- `config.yaml`
- `.venv/`
- `__pycache__/`
- `.pytest_cache/`
- `*.db`
- `*.db-shm`
- `*.db-wal`

提交信息必须使用中文。提交标题简洁说明本次变更，提交正文/description 也使用中文，并较详细地描述修改内容、影响范围和验证方式。例如：

```powershell
git commit -m "关闭应用退出时的数据库连接" -m "新增数据库关闭方法，并在 FastAPI 应用生命周期结束时调用，避免 Windows 下 sqlite 文件句柄残留。已通过自动化测试和真实生图 smoke test 验证。"
git commit -m "补充真实生图测试说明" -m "在项目协作说明中补充真实 NovelAI 上游测试的低成本参数、检查项和密钥输出注意事项，方便后续验证代理接口。"
```

每次提交应保持关注点单一，避免把初始化、功能修复、格式化和测试脚本变更混在同一个提交里。
