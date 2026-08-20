# NovelAI API Reference Notes for Proxy

整理日期：2026-05-25

本文件只整理当前 `REQUIREMENTS.md` 可能需要的 API 信息，作为实现代理层时的工程参考。字段以官方 Swagger/OpenAPI 为主，并补充实际抓包、历史 SDK 行为与当前项目自有实现的兼容性注意事项。项目运行时不再依赖 `novelai-python`。

## 来源

- Primary NovelAI API Swagger UI: `https://api.novelai.net/docs/`
- Primary API OpenAPI 来源：`https://api.novelai.net/docs/swagger-ui-init.js` 内嵌 `swaggerDoc`
- Image Generation API Swagger UI: `https://image.novelai.net/docs/index.html`
- Image Generation API Swagger JSON: `https://image.novelai.net/docs/doc.json`
- NovelAI Terms of Service: `https://novelai.net/terms`

## 总体说明

官方文档说明 NovelAI API 当前分为三类：

- Primary NovelAI API: `https://api.novelai.net/docs/`
- Image Generation API: `https://image.novelai.net/docs/index.html`
- Text Generation API: `https://text.novelai.net/docs/index.html`

当前代理需求主要涉及 Primary API 的用户订阅信息，以及 Image Generation API 的图像生成、Director Tools、标签建议等端点。

官方文档还说明，第三方集成应请求用户的 Persistent API token，并在 `Authorization` header 中使用。代理项目如果使用单一上游账号，应把上游 token 只保存在服务端配置中，不暴露给代理用户。

图像和文本生成端点的官方描述都包含限制：所有生成请求必须由人类动作发起，不能自动化生成造成过量负载。代理层的限频、队列和额度控制应服务于这个约束。

## 认证

### Primary API

来源：`https://api.novelai.net/docs/swagger-ui-init.js`

OpenAPI security scheme:

```yaml
bearer:
  type: http
  scheme: bearer
  bearerFormat: JWT
```

相关端点通常要求：

```http
Authorization: Bearer <access-token-or-api-token>
```

### Image Generation API

来源：`https://image.novelai.net/docs/doc.json`

Swagger 2 security definition:

```json
{
  "ApiKeyAuth": {
    "type": "apiKey",
    "name": "Authorization",
    "in": "header"
  }
}
```

实现代理时建议：

- 对下游用户接受 `Authorization: Bearer <proxy-api-key>`。
- 对上游 NovelAI 使用服务端配置的真实 token。
- 转发上游时不要把下游 proxy key 传给 NovelAI。

## 通用错误结构

### Primary API `ApiError`

```json
{
  "statusCode": "number",
  "message": "string"
}
```

常见响应：

- `400`: validation error
- `401`: access token incorrect
- `402`: active subscription required, 在部分 AI 端点出现
- `409`: conflict error
- `500`: unknown/internal error

### Image API `utils.JsonError`

```json
{
  "statusCode": "integer",
  "message": "string",
  "details": "any"
}
```

常见响应：

- `401`: unauthorized
- `500`: internal server error

注意：Image API 官方 Swagger 对 `/ai/generate-image` 只列出 `201/401/500`，但实际服务还会出现 `400/402/409/429`。当前自有传输层保留了这些状态码的兼容处理。

## Primary API

### GET `/user/subscription`

用途：获取当前账号订阅、过期时间和权益。代理项目可用它同步上游账号剩余 anlas，并可伪造同格式响应给下游用户。

认证：Bearer token。

响应：

- `200`: `SubscriptionResponse`
- `401`: `ApiError`
- `500`: `ApiError`

`SubscriptionResponse`:

```json
{
  "tier": "number",
  "active": "boolean",
  "expiresAt": "number",
  "perks": "SubscriptionTierPerks",
  "paymentProcessorData": "object",
  "trainingStepsLeft": "SubscriptionAvailableTrainingSteps",
  "isGracePeriod": "boolean"
}
```

`SubscriptionAvailableTrainingSteps`:

```json
{
  "fixedTrainingStepsLeft": "number",
  "purchasedTrainingSteps": "number"
}
```

代理层 anlas 可用量按当前兼容口径计算为：

```text
upstream_anlas_left = fixedTrainingStepsLeft + purchasedTrainingSteps
```

`SubscriptionTierPerks`:

```json
{
  "maxPriorityActions": "number",
  "startPriority": "number",
  "contextTokens": "number",
  "unlimitedMaxPriority": "boolean",
  "moduleTrainingSteps": "number"
}
```

注意事项：

- 历史 `novelai-python` 的 `SubscriptionResp` 额外包含 `accountType`，但当前 Primary API schema 中未在 `SubscriptionResponse` 找到该字段。代理伪造 `/user/subscription` 时为兼容既有客户端可继续保留该字段。
- 当前 Primary API schema 的 `perks` 未列出部分旧客户端使用的 `imageGeneration`、`unlimitedImageGeneration`、`voiceGeneration` 等字段。若目标客户端依赖这些字段，代理需要按兼容格式补齐。

## Image Generation API

基础文档：`https://image.novelai.net/docs/doc.json`

### POST `/ai/generate-image`

用途：使用 NovelAI diffusion models 生成图片。

认证：`Authorization` header。

请求体：`image.ImageGenerationRequest`

```json
{
  "action": "string",
  "input": "string",
  "model": "string",
  "parameters": "image.RequestParameters",
  "url": "string"
}
```

响应：

- `201`: `application/zip`，包含生成图片。
- `401`: `utils.JsonError`
- `500`: `utils.JsonError`

官方描述要求生成请求由人类动作发起，不能自动化生成造成过量负载。

代理实现注意：

- `action` 常见值：`generate`、`img2img`、`infill`。Primary API 旧 schema 明确列出这些值；Image API schema 中仅声明为 string。
- 成本预估所需字段主要在 `parameters` 中：`width`、`height`、`steps`、`n_samples`、`sampler`、`strength`、`sm`、`sm_dyn`、`image`、`model`。
- 成功响应是 ZIP。当前传输层直接保留完整上游 ZIP，不再经第三方 SDK 解包和重打包；代理仍接受五种已观察到的二进制 `Content-Type`。

### `image.RequestParameters`

官方 schema 中的字段：

```text
add_original_image: boolean
cfg_rescale: number
color_correct: boolean
controlnet_condition: string
controlnet_model: string
controlnet_strength: number
deliberate_euler_ancestral_bug: boolean
director_reference_descriptions: image.V4ConditionInput[]
director_reference_images: string[]
director_reference_information_extracted: number[]
director_reference_secondary_strength_values: number[]
director_reference_strength_values: number[]
dynamic_thresholding: boolean
extra_noise_seed: integer
height: integer
image: string
image_format: "png" | "webp"
img2img: image.Img2ImgParams
legacy: boolean
legacy_v3_extend: boolean
mask: string
n_samples: integer
negative_prompt: string
noise: number
noise_schedule: string
params_version: integer
prefer_brownian: boolean
prompt: string
qualityToggle: boolean
reference_image: string
reference_image_multiple: string[]
reference_information_extracted: number
reference_information_extracted_multiple: number[]
reference_strength: number
reference_strength_multiple: number[]
sampler: string
scale: number
seed: integer
skip_cfg_above_sigma: number
sm: boolean
sm_dyn: boolean
steps: number
stream: "msgpack" | "sse"
strength: number
ucPreset: integer
v4_negative_prompt: image.V4ConditionInput
v4_prompt: image.V4ConditionInput
width: integer
```

`image.Img2ImgParams`:

```json
{
  "color_correct": "boolean",
  "extra_noise_seed": "integer",
  "noise": "number",
  "strength": "number"
}
```

`image.ImageFormat`:

```text
png | webp
```

`image.StreamingType`:

```text
msgpack | sse
```

V4 prompt structures:

```json
{
  "v4_prompt": {
    "caption": {
      "base_caption": "string",
      "char_captions": [
        {
          "char_caption": "string",
          "centers": [
            {"x": "number", "y": "number"}
          ]
        }
      ]
    },
    "legacy_uc": "boolean",
    "use_coords": "boolean",
    "use_order": "boolean"
  }
}
```

代理实现建议：

- 第一版不要手写字段白名单丢弃未知字段。对官方 schema 已知但本地模型未显式读取的字段，应优先透传或显式返回不支持。
- 对成本预估只读取必要字段，其他字段保留原样交给上游。
- `steps` 在 Image API schema 中是 `number`；当前计费适配层要求它为整数或可安全转整数。

### POST `/ai/generate-image-stream`

用途：图像生成流式接口。

请求体同 `/ai/generate-image`。

响应：

- `200`: `text/event-stream`
- `401`: `utils.JsonError`
- `500`: `utils.JsonError`

当前 `REQUIREMENTS.md` 未要求实现。若后续支持，需要重新设计队列任务的 streaming 返回方式。

### GET `/ai/generate-image/suggest-tags`

用途：根据未完成 tag 查询标签建议。

认证：`Authorization` header。

查询参数：

```text
model: string, required, e.g. nai-diffusion-3
prompt: string, required
lang: string, optional, defaults to en, available: en, jp
```

响应：

- `200`: `image.TagSuggestionResponse`
- `401`: `utils.JsonError`
- `500`: `utils.JsonError`

`image.TagSuggestionResponse`:

```json
{
  "tags": {
    "tag": "string",
    "count": "integer",
    "confidence": "number"
  }
}
```

注意事项：

- 官方路径是 `/ai/generate-image/suggest-tags`，不是 `/ai/generate-image/suggest_tags`。
- 历史客户端和示例中出现过 `suggest_tags` 下划线路径，而官方路径使用 `suggest-tags` 连字符。
- 代理实现建议同时支持两个路径：官方 `suggest-tags` 为主，`suggest_tags` 作为兼容别名。

### POST `/ai/augment-image`

用途：Director Tools 图片增强。

认证：`Authorization` header。

请求体：`image.AugmentImageRequest`

```json
{
  "defry": "integer",
  "height": "integer",
  "image": "string",
  "prompt": "string",
  "req_type": "string",
  "width": "integer"
}
```

响应：

- `201`: `application/zip`
- `401`: `utils.JsonError`
- `500`: `utils.JsonError`

代理实现注意：

- augment 成本使用 `anlas_sync.price_generate` 按 `nai-diffusion-3`、28 steps、单样本、图生图强度 1 预估；`bg-removal` 使用基础价 `×3+5`。该结果仍属于代理预算预估，不是服务端账单证明。
- `req_type` 的合法值未在 Image API schema 中枚举。项目自有 `ReqType` 保留已验证的 7 项，并通过请求模型做本地校验。

### POST `/ai/encode-vibe`

用途：为 Vibe Transfer 提取图片 vibe 信息。

当前需求文档未要求独立代理该端点，但 Vibe Transfer 工作流可能依赖它。

请求体：`image.EncodeVibeRequest`

```json
{
  "crop_to_mask": "boolean",
  "focus_seed": "integer",
  "image": "string",
  "info_extract_seed": "integer",
  "information_extracted": "number, 0..1",
  "mask": "string",
  "model": "string"
}
```

响应：

- `201`: `application/binary`
- `401`: `utils.JsonError`
- `500`: `utils.JsonError`

代理实现注意：

- 如果下游客户端使用 Vibe Transfer 并自行调用 encode-vibe，则代理需要支持该端点或明确不支持。
- 如果只代理 `/ai/generate-image`，而客户端已经在请求中提交 `reference_*_multiple` 字段，则可以暂不实现 `/ai/encode-vibe`。

## Primary API 中的旧图像端点

Primary API `https://api.novelai.net/docs/` 仍能看到旧的 `/ai/generate-image`、`/ai/upscale`、`/ai/generate-image/suggest-tags` 等端点说明。官方 Image API 文档说明生成 API 已拆分到 `image.novelai.net`；当前自有传输层使用以下地址：

- 图像生成：`https://image.novelai.net/ai/generate-image`
- Director Tools：`https://image.novelai.net/ai/augment-image`
- 标签建议：`https://image.novelai.net/ai/generate-image/suggest-tags`
- 订阅信息：`https://api.novelai.net/user/subscription`
- 放大：`https://api.novelai.net/ai/upscale`；Primary API schema 中存在 `/ai/upscale`

### POST `/ai/upscale`

来源：Primary API schema。

请求体：`AiUpscaleImageRequest`

```json
{
  "image": "string",
  "width": "number",
  "height": "number",
  "scale": "number, enum 2|4, default 4"
}
```

响应：

- `201`: ZIP attachment
- `400`: `ApiError`
- `401`: `ApiError`
- `402`: active subscription required
- `409`: conflict
- `500`: `ApiError`

代理实现注意：

- 当前自有 `UpscaleRequest` 保留 `image/width/height/scale` 字段、图片尺寸探测和显式尺寸回退规则。
- 放大价格使用 `anlas_sync.price_upscale(width, height, sub)` 的前端查表口径；官方 schema 本身未提供成本公式，因此仍需用真实账号 smoke test 监测漂移。

## 当前自有实现的一致性检查

已确认一致：

- `app/novelai_endpoints.py` 的 generate、augment、suggest-tags、upscale 地址分别与官方 Image/Primary API 对应。
- `app/upstream.py` 使用 Bearer token、`x-correlation-id`、`x-initiated-at` 和 `chrome136` 指纹直接发起请求。
- `app/novelai_models.py` 保留 upscale/augment 的字段名、枚举和序列化结构。
- 图像生成请求核心结构 `input/model/action/parameters/url` 与官方 Image API schema 一致。
- 图像生成、放大和增强的成功响应均以完整 ZIP 返回。

需要注意的差异：

- 历史示例暴露的是 `/ai/generate-image/suggest_tags`，官方路径是 `/ai/generate-image/suggest-tags`；代理同时保留二者。
- 部分旧客户端的订阅响应字段比当前 Primary API schema 多，特别是 `accountType` 和一些旧 perks 字段。代理伪造响应时需要按目标客户端补齐。
- `anlas_sync` 的成本计算不是官方 schema 的一部分，只能作为代理层预估，并应定期与 NovelAI 前端和真实扣费对拍。
- Image API schema 对部分字段只给出 `string/number`，没有枚举或范围；代理仅对已确认字段做自有校验，其余生成参数保持透传，避免静默丢失。

## 建议映射到代理需求

第一版推荐支持：

```text
GET  /health
POST /ai/generate-image
GET  /ai/generate-image/suggest-tags
GET  /ai/generate-image/suggest_tags    # 兼容别名
GET  /user/subscription
```

第二阶段支持：

```text
POST /ai/upscale
POST /ai/augment-image
POST /ai/encode-vibe
```

额度统计字段来源：

```text
model      <- request.model
action     <- request.action
width      <- request.parameters.width
height     <- request.parameters.height
steps      <- request.parameters.steps
n_samples  <- request.parameters.n_samples
sampler    <- request.parameters.sampler
strength   <- request.parameters.strength
image mode <- request.parameters.image 是否存在
sm         <- request.parameters.sm
sm_dyn     <- request.parameters.sm_dyn
```

响应转发原则：

- 成功生成图片：保持 ZIP 响应。
- 成功标签建议：保持 JSON 响应。
- 上游错误：尽量保留 `statusCode/message/details`，同时映射到代理定义的错误规范。
- 队列满、限频、用户禁用、代理额度不足属于代理层错误，不应伪装为上游错误。
