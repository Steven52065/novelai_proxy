# NovelAI 前端 anlas 计费：分析 / 下载 / 解包 / 重写全流程

本文档记录如何从 `https://novelai.net/image` 的前端代码中分析、下载、解包并离线复现
anlas 计费逻辑。目的是让**新的对话**可以只凭本文档 + `anlas_sync/` 下的脚本，
在官网更新后重新完成整个流程。

---

## 1. 结论速览（2026-08-18 分析结果）

- 前端**确实包含**完整的 anlas 计费公式，位于公共 bundle `pages/_app-<hash>.js` 中
  的 **webpack 模块 23379**（导出 `GI`/`tY`/`H_`/`Dk` 等）。
- image 页面主组件在 chunk `3811-<hash>.js` 的**模块 85341** 中，调用 23379 计算
  增强/变体/放大价格；角色引用 5 anlas/个/张、vibe 编码 2 anlas/未编码引用 也在其中。
- vibe 编码价格（`SW.getPrice`）是**纯本地逻辑**，位于 chunk `2075-<hash>.js` 的
  **模块 61094**，不依赖服务端接口。
- 最终扣费由服务端 `image.novelai.net` 决定；前端公式用于预估/展示/禁用判断，
  离线复现的边界见第 10 节。

### 计费公式摘要

**经典公式（≤1MP，采样器为 plms/ddim/k_euler/k_euler_ancestral/k_lms）：**
`(15.266497014243718 * exp((w*h/1048576) * 0.6326248927474729) - 15.225164493059737) / 28 * steps`

**SDXL / SDXL Furry / v4：**
`ceil(w*h*2.951823174884865e-6 + w*h*steps*5.753298233447344e-7) * (sm&&sm_dyn ? 1.4 : sm ? 1.2 : 1)`

**>1MP 或其它采样器：** 查表（见第 6.3 节数据结构）。

**通用：**
- `y = mask ? (inpaintImg2ImgStrength ?? 1) : image ? strength : 1`
- `单价 = max(ceil(基础价 * y), 2)`；`单价 > 140` 返回 `-3`（禁用）
- `总价 = 单价 * n_samples`
- **免费小图**：无角色引用 && w*h<=1048576 && steps<=28 && tier>=3 && 订阅有效
  → `n_samples -= 1`（第一张免费）

---

## 2. 代码位置

```
anlas_sync/
  download.py           # 自动下载最新前端 chunk -> cache/
  extract.py            # 自动提取计费数据 -> generated/pricing_data.json
  oracle.js             # Node oracle：直接执行网页原始函数（对拍基准）
  anlas_pricing.py      # Python 计费实现（读取 pricing_data.json）
  compare.py            # 随机对拍：Python 实现 vs oracle
  test_anlas_pricing.py # pytest 单测 + 对拍集成测试
  conftest.py
  cache/                # 下载的原始 JS（不入库）
  generated/
    pricing_data.json   # 提取出的计费数据（入库）
    js_snippets/        # 提取的原始模块源码存档（审计用）
analysis/
  ANALYSIS.md           # 本文档
```

---

## 3. 环境准备

- Node.js >= 18（oracle 用，已验证 v22）
- 项目 venv：`.\.venv\Scripts\python.exe`，已装 `httpx`
- 联网（下载 novelai.net 前端文件；沙箱中需申请网络权限）

```powershell
node --version
.\.venv\Scripts\python.exe -c "import httpx; print(httpx.__version__)"
```

---

## 4. 分析过程（如何从零定位计费逻辑）

新对话若需重新分析（官网改版时），按以下步骤：

### 4.1 打开页面、抓资源

用浏览器控制工具打开 `https://novelai.net/image`，查看：
- 加载的 JS 列表（`list_scripts`）：注意 `pages/_app-*.js`、`chunks/2075-*.js`、
  `chunks/3811-*.js`、`chunks/4126-*.js`
- 网络请求（`list_network_requests`）：页面加载只请求账号类接口
  （`user/data` 等），**没有价格接口** → 定价在前端代码里。

### 4.2 搜索关键词

在前端源码中搜索（`search_in_sources`）：
- `anlas` / `Anlas` / `Not enough Anlas` → 定位到购买弹窗、错误处理（chunk 4126/2075）
- `getPrice` / `price` → 找到 `SW.getPrice`（chunk 2075 模块 61094）
- 在 image 页 bundle 中搜索 `GI(` / `tY(` → 找到调用点（chunk 3811 模块 85341）

### 4.3 核心模块清单（webpack 模块 id，当前版本）

| 模块 id | 所在文件 | 作用 |
|---|---|---|
| 23379 | pages/_app-*.js | **定价模块**：GI(生图)/tY(放大)/H_(vibe附加)/Dk(校验)/eZ(免费小图) |
| 18401 | pages/_app-*.js | 模型/采样器/家族枚举：Jg(模型->家族)、l1(采样器)、oM(模型)、lh(家族) |
| 71810 | pages/_app-*.js | `ax`：订阅有效性判断 |
| 1018 | pages/_app-*.js | 常量：`dZ=140`(单图上限)、`kJ=900`、`Hi=75` |
| 85506 | pages/_app-*.js | accountType 枚举：RETAIL=0/B2B=1/SERVICE=2/SUPPORT=3/ADMIN=4 |
| 61094 | chunks/2075-*.js | `SW.getPrice`：vibe 引用编码价格（本地） |
| 85341 | chunks/3811-*.js | image 页面主组件：价格调用点、角色引用/vibe 附加费 |

> 注意：模块 id 可能随官网改版变化。重新定位方法见第 9 节。

### 4.4 关键函数语义

- **GI(params, {subscription}, model, flag)**（模块 23379）
  - 三个分支：SDXL 家族 / 经典公式 / 查表
  - 查表时 `sm&&sm_dyn` 用 nai_smea_dyn 表、仅 `sm` 用 nai_smea 表
    （`o = sm && sm_dyn`，不是单独的 sm_dyn！这是最容易抄错的地方）
  - `VI_SET = {ddim, plms, k_lms, nai_smea, nai_smea_dyn, ddim_v3}` 会强制关闭 sm/sm_dyn
- **tY(w, h, {subscription})**：放大价。≤409600px 且 tier>=3 且订阅有效 → 0；
  否则查表 [[1048576,7],[786432,5],[524288,3],[409600,2],[262144,1]]，>1MP → -3
- **H_(n)**：`max(0, n-4) * 2`
- **Dk(params, model)**：`(!es(model) || (w && h && steps<=50)) && w*h<=3145728`
  - `es` 是“已知模型集合”（从 es 函数体提取，包含 nai-diffusion-2 等 24 个模型）
- **ax(sub)**：accountType ∈ {B2B,SERVICE,SUPPORT,ADMIN} 或
  `expiresAt > now && tier > 0`
- **Jg(model)**：模型 -> 家族（stableDiffusion / stableDiffusionGroup2 /
  stableDiffusionXL / stableDiffusionXLFurry / v4），未知模型默认 stableDiffusion

---

## 5. 下载流程

```powershell
# 联网执行（沙箱需申请网络权限）
.\.venv\Scripts\python.exe anlas_sync\download.py
```

原理：
1. `GET https://novelai.net/image` 拿 HTML
2. 正则提取所有 `/_next/static/chunks/*.js` URL（37 个左右）
3. 按前缀挑选 7 个文件存入 `cache/`：
   - `webpack-*.js` → webpack.js（webpack runtime，含 chunk 文件名映射）
   - `framework-*.js` → framework.js（React）
   - `main-*.js` → main.js（入口）
   - `pages/_app-*.js` → _app.js（定价模块 23379 所在）
   - `2075-*.js` → chunk-2075.js（SW.getPrice）
   - `3811-*.js` → chunk-3811.js（image 页）
   - `4126-*.js` → chunk-4126.js
4. 写 `cache/manifest.json` 记录 URL/sha256，重复运行可判断是否更新

> 备选：可用 `curl.exe -L --fail` 手动下载（网络权限已批准）。

---

## 6. 解包 / 提取流程

```powershell
.\.venv\Scripts\python.exe anlas_sync\extract.py
```

### 6.1 模块源码提取

webpack chunk 是 `(self.webpackChunk_N_E=...).push([[chunkIds],{模块表}])` 的单行 JS。
模块定义形如 `23379:(e,t,r)=>{...}`（参数名可能是 `(e,t,i)` 等任意字母）。
提取方法：正则 `,\d+:(?:\([a-z],[a-z],[a-z]\)=>|function\([a-z],[a-z],[a-z]\)\{)`
定位模块 id，再到下一个匹配处为边界。提取结果存 `generated/js_snippets/module_<id>.js`。

### 6.2 Oracle：直接执行网页原始函数（对拍基准）

`oracle.js` 的原理（关键技巧）：
1. 在 Node 中 mock 最小浏览器环境（`self/window/document/navigator/localStorage` 等）
2. `eval` 加载 `cache/webpack.js`（webpack runtime）
3. 向 `self.webpackChunk_N_E` push 一个特殊 chunk，
   其第三个参数 `(wr) => { global.__wr = wr }` 会拿到 webpack require 对象
4. **接管 push**：只注册模块、不执行入口回调，避免 Next.js 启动
5. 依次注册 `framework.js / main.js / _app.js` 的模块
6. `__wr(23379)` 即得定价模块，`__wr(18401)` 得枚举，`__wr(71810)` 得 ax

这样 oracle 与网页运行的是**同一份函数**，是验证 Python 实现的权威基准。

### 6.3 提取的数据结构（generated/pricing_data.json）

| 字段 | 内容 |
|---|---|
| `table_c` | 768 个整数：查表桶大小（索引换算 `a[value]=2*index`） |
| `table_u/table_d/table_h/table_f` | 各 1536 个浮点：nai_smea/nai_smea_dyn/k_euler_ancestral/ddim 查表 |
| `upscale_table` | `[[1048576,7],[786432,5],[524288,3],[409600,2],[262144,1]]` |
| `max_pixels` | 3145728（Dk 分辨率上限） |
| `max_single_price` | 140（单图价格上限） |
| `classic_formula` | 经典公式系数 a/b/c |
| `sdxl_formula` | SDXL/v4 公式系数 + sm 倍率 |
| `free_small` | {max_pixels:1048576, max_steps:28, min_tier:3} |
| `vibe` | {per_encoding:2, free_count:4, extra_per:2} |
| `char_ref_per_sample` | 5 |
| `model_family` | 24 个模型 -> 家族（由 oracle 对每个模型执行 Jg 生成） |
| `vi_set` | 禁用 sm/sm_dyn 的采样器 |
| `es_set` | Dk 校验模型集合（24 个） |
| `classic_samplers` | 经典公式适用的采样器 |
| `enum_all` | 18401 的全部枚举键值（模型/采样器/家族/噪声调度等） |

### 6.4 提取技巧与坑

- JS 浮点数可能写成 `.124`（省略前导 0），解析用 `float(".124")` 没问题
- `e0=[[...]]` 提取用非贪婪 `\[\[(.*?)\]\]`，再 `(\d+),(\d+)` 取对
- `es` 函数最后一个 case 没有 `||` 结尾，正则必须提取整个函数体再 findall
- `rs`（VI_SET）必须精确匹配 `let rs=new Set(...)`，不能取“最后一个含采样器的 Set”

---

## 7. Python 重写

`anlas_pricing.py` 是手写公式 + 读取 `pricing_data.json` 数据，函数签名与 oracle 对齐：

| 函数 | 对应网页 |
|---|---|
| `price_generate(params, sub, model, free_small_disabled=False)` | GI |
| `price_upscale(width, height, sub)` | tY |
| `vibe_extra_price(count)` | H_ |
| `validate_params(params, model)` | Dk |
| `is_active_subscription(sub)` | ax |
| `model_family(model)` | Jg |
| `total_enhance_price(...)` | 模块 85341 的增强组合逻辑 |

params 键：`width, height, steps, n_samples, sampler, sm, sm_dyn,
image/mask/strength/inpaintImg2ImgStrength, characterRef`（与 oracle 一致）。
sub 键：`tier, expiresAt, accountType`。

---

## 8. 验证（对拍 + 单测）

```powershell
# 随机对拍：Python 实现 vs 网页原始函数
.\.venv\Scripts\python.exe -m anlas_sync.compare --cases 5000 --seed 20260818

# 单元测试（含 300 组对拍集成测试）
.\.venv\Scripts\python.exe -m pytest anlas_sync\test_anlas_pricing.py -q
```

`compare.py` 生成随机参数（模型/分辨率/steps/sampler/sm/sm_dyn/img2img/mask/
订阅组合），写 `generated/_compare_in.json`，调 `node oracle.js` 批量执行，
再与 Python 实现逐条 diff。差异会打印并返回非零退出码。
已验证：**2 个 seed × 20025 用例全部一致**。

> JS 中查表索引缺失时结果为 NaN，JSON 序列化为 `null`；Python 显式返回
> `float("nan")`，对拍时视为一致。

---

## 9. 官网更新后的完整同步流程（新对话执行）

```powershell
# 1) 下载最新前端 JS（联网）
.\.venv\Scripts\python.exe anlas_sync\download.py

# 2) 重新提取计费数据
.\.venv\Scripts\python.exe anlas_sync\extract.py

# 3) 随机对拍验证（全绿才算同步成功）
.\.venv\Scripts\python.exe -m anlas_sync.compare --cases 5000 --seed 20260818

# 4) 单元测试
.\.venv\Scripts\python.exe -m pytest anlas_sync\test_anlas_pricing.py -q
```

若 `extract.py` 报“网页结构可能已变化”，说明模块 id/变量名/打包方式变了，
需按第 4 节重新定位模块，更新 `extract.py`/`oracle.js` 中的模块 id 与正则。

---

## 10. 改版后如何重新定位（方法论）

1. 浏览器打开页面，`search_in_sources` 搜 `anlas`、`Not enough Anlas`、
   `getPrice`、`price`，找到新模块 id
2. 用 `list_scripts` 确认新 chunk 文件名，更新 `download.py` 的 NEEDED 前缀
3. 若定价模块 id 变化，更新 `extract.py` 的模块列表与 `oracle.js` 的 `__wr(id)`
4. 若变量名变化，按 6.4 的语义重新写提取正则（数组顺序/公式形态变了需人工确认）
5. 用 `oracle.js` 的导出能力生成新的模型家族映射与枚举
6. 重跑对拍，直到全绿

---

## 11. 注意事项与已知边界

- **服务端为准**：前端公式是预估/展示/禁用逻辑；实际扣费由
  `image.novelai.net` 决定。离线复现无法证明服务端隐藏规则
  （账号优惠/促销/热更新）。建议周期性同步 + 少量真实请求抽查。
- 已知价格样例（可做 sanity check）：
  - 512x768 / 28 步 / SD1.5 / tier0 = **5**
  - 1024x1024 / 28 步 / NAI Diffusion 3 / tier0 = **20**
  - Opus + 512x768 / 28 步 / 4 张 = **15**（免费小图，只算 3 张）
  - 放大 1024x1024 / tier0 = **7**；512x768 / Opus = **0**
- 角色引用 5 anlas/个/张；vibe 编码 2 anlas/未编码引用，>4 个后每个再 +2。
