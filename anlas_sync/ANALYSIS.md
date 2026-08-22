# NovelAI 前端 anlas 计费：分析 / 下载 / 解包 / 重写全流程

本文档记录如何从 `https://novelai.net/image` 的前端代码中分析、下载、解包并离线复现
anlas 计费逻辑。目的是让**新的对话**可以只凭本文档 + `anlas_sync/` 下的脚本，
在官网更新后重新完成整个流程。

---

## 1. 结论速览（2026-08-22 分析结果）

- 前端**确实包含**完整的 anlas 计费公式。当前版本定价逻辑不在 `_app.js`，
  而是位于 chunk `1052-<hash>.js` 的 **webpack 模块 61225**（导出 `GI`/`tY`/`H_`/`Lq`），
  配套的尺寸/校验模块是同一 chunk 的 **模块 57863**（导出 `Dk`/`xM` 等）。
- 枚举/家族/Jg/订阅 ax/常量/免费小图 t1 仍在 `pages/_app-<hash>.js`
  （模块 53856 / 62654 / 46542 / 41179）。
- image 页面主组件在 chunk `1741-<hash>.js`，调用 61225 计算增强/变体/放大价格；
  角色引用 5 anlas/个/张、vibe 附加费也在其中。
- vibe 编码价格（`SW.getPrice`）是**纯本地逻辑**，位于 chunk `7416-<hash>.js` 的
  **模块 25690**，不依赖服务端接口。
- 最终扣费由服务端 `image.novelai.net` 决定；前端公式用于预估/展示/禁用判断，
  离线复现的边界见第 11 节。

### 计费公式摘要（2026-08-22 版）

**经典公式（≤1MP，采样器为 plms/ddim/k_euler/k_euler_ancestral/k_lms）：**
`(15.266497014243718 * exp((w*h/1048576) * 0.6326248927474729) - 15.225164493059737) / 28 * steps`

**SDXL / SDXL Furry / v4 / v5：**
`ceil(w*h*2.951823174884865e-6 + w*h*steps*5.753298233447344e-7) * (sm&&sm_dyn ? 1.4 : sm ? 1.2 : 1)`
**v5 家族再乘 1.5**（`M *= 1.5`，模块 61225）。`custom` 现在也属于 v5 家族。

**>1MP 或其它采样器：** 查表（见第 6.3 节数据结构）。

**通用：**
- `y = mask ? (inpaintImg2ImgStrength ?? 1) : image ? strength : 1`
- `单价 = max(ceil(基础价 * y), 2)`；`单价 > 140` 返回 `-3`（禁用）
- `总价 = 单价 * n_samples`
- **免费小图**：无角色引用 && w*h<=1048576 && steps<=28 && tier>=3 && 订阅有效
  → `n_samples -= 1`（第一张免费）。**新增条件**：模型在 `opus_usage_limit_models`
  （v5/custom）且 `subscription.usage.isNegative` 为真时，禁用免费小图。

**放大（tY，2026-08-22 大改）：** 直接按 `upscale_table` 分桶
`[[1048576,1],[1747627,2],[2446678,3],[3145728,4]]`，**取消了 Opus 免费档**；
`w*h==0` 返回 `-3`。

**参数校验（Dk，2026-08-22 简化）：** 所有模型统一要求 `w/h 存在 && steps<=50 &&
w*h<=3145728`，不再有 es_set 特例集合。

---

## 2. 代码位置

```
anlas_sync/
  download.py           # 自动下载最新前端 chunk -> cache/（curl_cffi 模拟 Chrome）
  extract.py            # 自动提取计费数据 -> generated/pricing_data.json
  oracle.js             # Node oracle：直接执行网页原始函数（对拍基准）
  anlas_pricing.py      # Python 计费实现（读取 pricing_data.json）
  compare.py            # 随机对拍：Python 实现 vs oracle
  test_anlas_pricing.py # pytest 单测 + 对拍集成测试
  cache/                # 下载的原始 JS（不入库）
  generated/
    pricing_data.json   # 提取出的计费数据（入库）
    js_snippets/        # 提取的原始模块源码存档（审计用）
  ANALYSIS.md           # 本文档
```

---

## 3. 环境准备

- Node.js >= 18（oracle 用，已验证 v22）
- 项目 venv：`.\.venv\Scripts\python.exe`，已装 `curl_cffi`
- 联网（下载 novelai.net 前端文件；沙箱中需申请网络权限）

```powershell
node --version
.\.venv\Scripts\python.exe -c "import curl_cffi; print(curl_cffi.__version__)"
```

> 注意：novelai.net 的 CDN 会拦截默认 TLS 指纹（httpx 直接报 SSL EOF），
> `download.py` 必须用 `curl_cffi` + `impersonate="chrome136"` 才能下载。

---

## 4. 分析过程（如何从零定位计费逻辑）

新对话若需重新分析（官网改版时），按以下步骤：

### 4.1 打开页面、抓资源

用浏览器控制工具打开 `https://novelai.net/image`，查看：
- 加载的 JS 列表（`list_scripts`）：注意 `pages/_app-*.js`、`chunks/1052-*.js`、
  `chunks/1741-*.js`、`chunks/7416-*.js`
- 网络请求（`list_network_requests`）：页面加载只请求账号类接口
  （`user/data` 等），**没有价格接口** → 定价在前端代码里。

### 4.2 搜索关键词

在前端源码中搜索（`search_in_sources`）：
- `anlas` / `Anlas` / `Not enough Anlas` → 定位到购买弹窗、错误处理
- `getPrice` / `price` → 找到 `SW.getPrice`（chunk 7416 模块 25690）
- 特征常量 `15.266497014243718` 或 `0.6326248927474729` → 直接定位定价模块
  （当前在 chunk 1052 模块 61225）

### 4.3 核心模块清单（webpack 模块 id，当前版本）

| 模块 id | 所在文件 | 作用 |
|---|---|---|
| 61225 | chunks/1052-*.js | **定价模块**：GI(生图)/tY(放大)/H_(vibe附加)/Lq(vibe单价) |
| 57863 | chunks/1052-*.js | 尺寸/校验：Dk(参数校验)、xM(3145728)、放大/增强尺寸工具 |
| 53856 | pages/_app-*.js | 枚举：Jg(模型->家族)、l1(采样器)、oM(模型)、lh(家族)、PE(能力含 opusUsageLimit)、VI(禁用 sm 的采样器) |
| 62654 | pages/_app-*.js | `ax`：订阅有效性判断 |
| 46542 | pages/_app-*.js | 常量：`dZ=140`(单图上限)、`kJ=900`、`Hi=75`、`gb="nai-diffusion-5-curated"` |
| 41179 | pages/_app-*.js | `t1`：免费小图参数判断（!characterRef && <=1MP && <=28 步） |
| 25690 | chunks/7416-*.js | `SW.getPrice`：vibe 引用编码价格（本地） |
| 1741 | chunks/1741-*.js | image 页面主组件：价格调用点、角色引用/vibe 附加费 |

> 注意：模块 id 可能随官网改版变化。重新定位方法见第 10 节。

### 4.4 关键函数语义（2026-08-22 版）

- **GI(params, {subscription}, model, flag)**（模块 61225）
  - 三个分支：SDXL/v4/v5 家族公式 / 经典公式 / 查表
  - **v5 家族**（含 `custom`）走 SDXL 公式后再 `M *= 1.5`
  - 查表时 `sm&&sm_dyn` 用 nai_smea_dyn 表、仅 `sm` 用 nai_smea 表
    （`o = sm && sm_dyn`，不是单独的 sm_dyn！这是最容易抄错的地方）
  - `VI_SET = {ddim, plms, k_lms, nai_smea, nai_smea_dyn, ddim_v3}` 会强制关闭 sm/sm_dyn
  - 免费小图：`t1(params) && tier>=3 && ax(sub) && !(opusUsageLimit && usage.isNegative) && !flag`
- **tY(w, h, {subscription})**：放大价。`w*h==0 → -3`；否则查表
  `[[1048576,1],[1747627,2],[2446678,3],[3145728,4]]`，>3MP → -3。**无 Opus 免费档**。
- **H_(n)**：`max(0, n-4) * 2`
- **Dk(params, model)**：`!!w && !!h && !(steps>50) && !(w*h>3145728)`（所有模型统一）
- **ax(sub)**：accountType ∈ {B2B,SERVICE,SUPPORT,ADMIN} 或
  `expiresAt > now && tier > 0`
- **Jg(model)**：模型 -> 家族（stableDiffusion / stableDiffusionGroup2 /
  stableDiffusionXL / stableDiffusionXLFurry / v4 / v5），未知模型默认 stableDiffusion；
  `custom` 与 `nai-diffusion-5-*` 归 v5
- **PE(model).opusUsageLimit**：v5 与 custom 为 true，用于免费小图的负 usage 禁用条件

---

## 5. 下载流程

```powershell
# 联网执行（沙箱需申请网络权限）
.\.venv\Scripts\python.exe anlas_sync\download.py
```

原理：
1. `GET https://novelai.net/image` 拿 HTML（curl_cffi impersonate chrome136）
2. 正则提取所有 `/_next/static/chunks/*.js` URL（43 个左右）
3. 按前缀挑选 7 个文件存入 `cache/`：
   - `webpack-*.js` → webpack.js（webpack runtime，含 chunk 文件名映射）
   - `framework-*.js` → framework.js（React）
   - `main-*.js` → main.js（入口）
   - `pages/_app-*.js` → _app.js（枚举/订阅/常量/免费小图模块）
   - `1052-*.js` → chunk-1052.js（定价模块 61225、尺寸模块 57863）
   - `7416-*.js` → chunk-7416.js（SW.getPrice 模块 25690）
   - `1741-*.js` → chunk-1741.js（image 页角色引用附加费）
4. 写 `cache/manifest.json` 记录 URL/sha256，重复运行可判断是否更新

> 备选：可用 `curl.exe -L --fail` 手动下载（网络权限已批准）。

---

## 6. 解包 / 提取流程

```powershell
.\.venv\Scripts\python.exe anlas_sync\extract.py
```

### 6.1 模块源码提取

webpack chunk 是 `(self.webpackChunk_N_E=...).push([[chunkIds],{模块表}])` 的单行 JS。
模块定义形如 `61225:(e,t,r)=>{...}`（参数名可能是 `(e,t,i)` 等任意字母）。
提取方法：正则 `,\d+:(?:\([a-z],[a-z],[a-z]\)=>|function\([a-z],[a-z],[a-z]\)\{)`
定位模块 id，再到下一个匹配处为边界。提取结果存 `generated/js_snippets/module_<id>.js`。

### 6.2 Oracle：直接执行网页原始函数（对拍基准）

`oracle.js` 的原理（关键技巧）：
1. 在 Node 中 mock 最小浏览器环境（`self/window/document/navigator/localStorage` 等）
2. `eval` 加载 `cache/webpack.js`（webpack runtime）
3. 向 `self.webpackChunk_N_E` push 一个特殊 chunk，
   其第三个参数 `(wr) => { global.__wr = wr }` 会拿到 webpack require 对象
4. **接管 push**：只注册模块、不执行入口回调，避免 Next.js 启动
5. 依次注册 `framework.js / main.js / _app.js / chunk-1052.js` 的模块
6. `__wr(61225)` 即得定价模块，`__wr(53856)` 得枚举/Jg，
   `__wr(62654)` 得 ax，`__wr(57863)` 得 Dk

这样 oracle 与网页运行的是**同一份函数**，是验证 Python 实现的权威基准。

### 6.3 提取的数据结构（generated/pricing_data.json）

| 字段 | 内容 |
|---|---|
| `table_c` | 768 个整数：查表桶大小（索引换算 `a[value]=2*index`） |
| `table_u/table_d/table_h/table_f` | 各 1536 个浮点：nai_smea/nai_smea_dyn/k_euler_ancestral/ddim 查表 |
| `upscale_table` | `[[1048576,1],[1747627,2],[2446678,3],[3145728,4]]`（末项由 `c.xM` 解析） |
| `max_pixels` | 3145728（Dk 分辨率上限） |
| `max_single_price` | 140（单图价格上限） |
| `classic_formula` | 经典公式系数 a/b/c |
| `sdxl_formula` | SDXL/v4/v5 公式系数 + sm 倍率 |
| `v5_multiplier` | 1.5（v5 家族在 SDXL 公式后整体乘的倍率） |
| `free_small` | {max_pixels:1048576, max_steps:28, min_tier:3} |
| `opus_usage_limit_models` | 使用 Opus 用量额度的模型（custom + 4 个 v5） |
| `validate_steps_limit` | 50（Dk 对所有模型的 steps 上限） |
| `vibe` | {per_encoding:2, free_count:4, extra_per:2} |
| `char_ref_per_sample` | 5 |
| `model_family` | 29 个模型 -> 家族（由 oracle 对每个模型执行 Jg 生成） |
| `vi_set` | 禁用 sm/sm_dyn 的采样器 |
| `classic_samplers` | 经典公式适用的采样器 |
| `enum_all` | 53856 的全部枚举键值（模型/采样器/家族/噪声调度等） |

> 已移除：旧版 `es_set`（Dk 已对所有模型统一 steps<=50）。

### 6.4 提取技巧与坑

- JS 浮点数可能写成 `.124`（省略前导 0），解析用 `float(".124")` 没问题
- 定价模块 61225 中 `let i=[...],o=[...],l=[...],s=[...],d=[...]` 是**同一个 let 链**，
  提取时先截 `let i=[` 到 `;var u=` 的片段再逐个取数组
- `upscale_table` 是 `g=[[1048576,1],[1747627,2],[2446678,3],[c.xM,4]]`：
  `\[\[(.*?)\]\]` 捕获组以 `c.xM,4` 结尾（末项没有闭合 `]`），解析用
  `(\d+|c\.xM),(\d+)(?:\]|$)`；`c.xM` 从模块 57863 的 `m=3145728` 解析
- PE 的 `opusUsageLimit:!0` 在**多标签共享 return** 的 case 组里（v5 4 个模型一个 return），
  必须用 `(?:case"([^"]+)":)+return\{...opusUsageLimit:(!0|!1)` 整组匹配再收集全部标签
- `SW.getPrice` 未编码价格取函数体内**最后一个** `exists:!1,price:N`（前面还有 0 的兜底）
- `VI_SET`（模块 53856）必须精确匹配 `let C=new Set(...)`，不能取“最后一个含采样器的 Set”

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
| `total_enhance_price(...)` | 模块 1741 的增强组合逻辑 |

params 键：`width, height, steps, n_samples, sampler, sm, sm_dyn,
image/mask/strength/inpaintImg2ImgStrength, characterRef`（与 oracle 一致）。
sub 键：`tier, expiresAt, accountType, usage`（`usage.isNegative` 影响 v5/custom 免费小图）。

---

## 8. 验证（对拍 + 单测）

```powershell
# 随机对拍：Python 实现 vs 网页原始函数（不同 seed 各 20025 组）
.\.venv\Scripts\python.exe -m anlas_sync.compare --cases 5000 --seed 20260818
.\.venv\Scripts\python.exe -m anlas_sync.compare --cases 5000 --seed 20260822
.\.venv\Scripts\python.exe -m anlas_sync.compare --cases 5000 --seed 12345

# 单元测试（含 300 组对拍集成测试）
.\.venv\Scripts\python.exe -m pytest anlas_sync\test_anlas_pricing.py -q
```

`compare.py` 生成随机参数（模型/分辨率/steps/sampler/sm/sm_dyn/img2img/mask/
订阅组合，订阅偶尔带 `usage.isNegative`），写 `generated/_compare_in.json`，
调 `node oracle.js` 批量执行，再与 Python 实现逐条 diff。差异会打印并返回非零退出码。
2026-08-22 已用 **3 个 seed × 20025 用例全部一致** 验证。

> JS 中查表索引缺失时结果为 NaN，JSON 序列化为 `null`；Python 显式返回
> `float("nan")`，对拍时视为一致。
> 注意：`compare.py` 并发运行多个 seed 会互相覆盖 `_compare_in/out.json`，需串行执行。

---

## 9. 官网更新后的完整同步流程（新对话执行）

```powershell
# 1) 下载最新前端 JS（联网；curl_cffi 模拟 Chrome，勿改回 httpx）
.\.venv\Scripts\python.exe anlas_sync\download.py

# 2) 重新提取计费数据
.\.venv\Scripts\python.exe anlas_sync\extract.py

# 3) 随机对拍验证（全绿才算同步成功，串行跑多个 seed）
.\.venv\Scripts\python.exe -m anlas_sync.compare --cases 5000 --seed 20260818

# 4) 单元测试
.\.venv\Scripts\python.exe -m pytest anlas_sync\test_anlas_pricing.py -q

# 5) 全量回归
.\.venv\Scripts\python.exe -m pytest -q
```

若 `extract.py` 报“网页结构可能已变化”，说明模块 id/变量名/打包方式变了，
需按第 10 节重新定位模块，更新 `extract.py`/`oracle.js` 中的模块 id 与正则。

---

## 10. 改版后如何重新定位（方法论）

1. 浏览器打开页面，`search_in_sources` 搜 `anlas`、`Not enough Anlas`、
   `getPrice`、`price`，或特征常量 `15.266497014243718`，找到新模块 id
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
- 已知价格样例（2026-08-22 版，可做 sanity check）：
  - 512x768 / 28 步 / SD1.5 / tier0 = **5**
  - 1024x1024 / 28 步 / NAI Diffusion 3 / tier0 = **20**
  - 1024x1024 / 28 步 / NAI Diffusion v5 / tier0 = **30**（v5 倍率 1.5）
  - Opus + 512x768 / 28 步 / 4 张 = **15**（免费小图，只算 3 张）
  - 放大 1024x1024 / tier0 = **1**；512x768 / Opus = **1**（免费档已取消）
  - 放大 1536x1024 = **2**；2048x1536 = **4**
- 角色引用 5 anlas/个/张；vibe 编码 2 anlas/未编码引用，>4 个后每个再 +2。
