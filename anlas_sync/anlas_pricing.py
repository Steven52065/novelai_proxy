#!/usr/bin/env python
"""NovelAI anlas 计费逻辑的 Python 实现（数据来自 generated/pricing_data.json）。

数据由 extract.py 从网页前端 chunk 中自动提取，公式逐条对照网页逻辑（module 23379 等）。
函数签名与 Node oracle（oracle.js）保持一致，可用 compare.py 做随机对拍验证。
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent / "generated" / "pricing_data.json"

# accountType 枚举（module 85506）
ACCOUNT_TYPE = {"RETAIL": 0, "B2B": 1, "SERVICE": 2, "SUPPORT": 3, "ADMIN": 4}
_ACTIVE_ACCOUNT_TYPES = frozenset(
    {ACCOUNT_TYPE["B2B"], ACCOUNT_TYPE["SERVICE"], ACCOUNT_TYPE["SUPPORT"], ACCOUNT_TYPE["ADMIN"]})

_FAMILY_SD = "stableDiffusion"
_FAMILY_SD2 = "stableDiffusionGroup2"
_FAMILY_SDXL = "stableDiffusionXL"
_FAMILY_SDXL_FURRY = "stableDiffusionXLFurry"
_FAMILY_V4 = "v4"

_FAMILY_SDXL_LIKE = frozenset({_FAMILY_SDXL, _FAMILY_SDXL_FURRY, _FAMILY_V4})


def _load_data() -> dict:
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


DATA: dict = _load_data()


# ---------------------------------------------------------------- 基础判断

def model_family(model: str) -> str:
    """模型 -> 家族（对应前端 module 18401 Jg）。未知模型默认 stableDiffusion。"""
    return DATA["model_family"].get(model, _FAMILY_SD)


def is_active_subscription(sub: dict) -> bool:
    """订阅是否有效（对应前端 module 71810 ax）。"""
    return (
        sub.get("accountType", 0) in _ACTIVE_ACCOUNT_TYPES
        or (sub.get("expiresAt", 0) > time.time() and sub.get("tier", 0) > 0)
    )


def is_es_model(model: str) -> bool:
    """是否属于 Dk 校验中需要 steps<=50 限制的模型（对应前端 es()）。"""
    return model in DATA["es_set"]


def validate_params(params: dict, model: str) -> bool:
    """参数校验（对应前端 module 23379 Dk/eI）。"""
    w = params.get("width")
    h = params.get("height")
    steps = params.get("steps", 0) or 0
    return (
        (not is_es_model(model) or (bool(w) and bool(h) and steps <= 50))
        and (w or 0) * (h or 0) <= DATA["max_pixels"]
    )


# ---------------------------------------------------------------- 基础价格

def _classic(w: int, h: int, steps: int) -> float:
    """经典公式（<=1MP + 经典采样器）。"""
    cf = DATA["classic_formula"]
    return (cf["a"] * math.exp((w * h / 1048576) * cf["b"]) + cf["c"]) / 28 * steps


def _sdxl(w: int, h: int, steps: int, sm: bool, sm_dyn: bool) -> float:
    """SDXL / SDXL Furry / v4 公式。"""
    sf = DATA["sdxl_formula"]
    o = w * h
    base = math.ceil(sf["pixels"] * o + sf["per_step"] * o * steps)
    return base * (sf["sm_dyn_mult"] if (sm and sm_dyn) else sf["sm_mult"] if sm else 1)


def _lookup(w: int, h: int, steps: int, sampler: str, sm: bool, sm_dyn: bool) -> float:
    """>1MP 或其它采样器的查表公式。"""
    table_c = DATA["table_c"]
    a = {bucket: 2 * idx for idx, bucket in enumerate(table_c)}
    if sampler == "k_euler_ancestral":
        table = DATA["table_h"]
    elif sampler == "nai_smea":
        table = DATA["table_u"]
    elif sampler == "nai_smea_dyn":
        table = DATA["table_d"]
    elif sampler == "ddim":
        table = DATA["table_f"]
    else:
        table = DATA["table_h"]
    if sm and sm_dyn:
        # JS: o = sm && sm_dyn 时用 nai_smea_dyn 表；仅 sm 时用 nai_smea 表
        table = DATA["table_d"]
    elif sm:
        table = DATA["table_u"]
    key = math.floor(w / 64) * math.floor(h / 64)
    i = a.get(key)
    if i is None:
        # JS 中 a[key] 为 undefined，价格结果为 NaN；这里显式返回 NaN 保持一致
        return float("nan")
    return table[i] * steps + table[i + 1]


def price_generate(params: dict, sub: dict, model: str, free_small_disabled: bool = False) -> int:
    """生图价格（对应前端 module 23379 GI）。

    params 键: width, height, steps, n_samples, sampler, sm, sm_dyn,
               image(可选), mask(可选), strength(可选), inpaintImg2ImgStrength(可选),
               characterRef(可选)
    sub 键: tier, expiresAt, accountType
    返回 anlas 数；-3 表示超出单图价格上限（界面禁用）。
    """
    w = params["width"]
    h = params["height"]
    m = w * h
    if m < 65536:
        m = 65536
    if params.get("mask"):
        y = params.get("inpaintImg2ImgStrength")
        if y is None:
            y = 1
    elif params.get("image"):
        y = params.get("strength", 1)
    else:
        y = 1

    steps = params["steps"]
    v = params["n_samples"]
    free = DATA["free_small"]
    if (
        not params.get("characterRef")
        and w * h <= free["max_pixels"]
        and steps <= free["max_steps"]
        and sub.get("tier", 0) >= free["min_tier"]
        and is_active_subscription(sub)
        and not free_small_disabled
    ):
        v -= 1

    family = model_family(model)
    sampler = params["sampler"]
    if family in _FAMILY_SDXL_LIKE:
        wval = _sdxl(w, h, steps, bool(params.get("sm")), bool(params.get("sm_dyn")))
    elif m <= 1048576 and sampler in DATA["classic_samplers"]:
        wval = _classic(w, h, steps)
    else:
        sm = bool(params.get("sm"))
        sm_dyn = bool(params.get("sm_dyn"))
        if sampler in DATA["vi_set"]:
            sm = False
            sm_dyn = False
        wval = _lookup(w, h, steps, sampler, sm, sm_dyn)

    cost = wval * y
    if math.isnan(cost):
        # JS 中查表缺失时结果为 NaN，最终返回 NaN（对拍时 oracle 输出 null）
        return float("nan")
    i = max(math.ceil(cost), 2)
    if i > DATA["max_single_price"]:
        return -3
    return i * v


def price_upscale(width: int, height: int, sub: dict) -> int:
    """放大价格（对应前端 module 23379 tY/e1）。"""
    n = width * height
    free = DATA["free_small"]
    if n <= 409600 and sub.get("tier", 0) >= 3 and is_active_subscription(sub):
        return 0
    price = -3
    for px, p in DATA["upscale_table"]:
        if n <= px:
            price = p
    return price


def vibe_extra_price(count: int) -> int:
    """vibe 引用数 >4 时的附加费（对应前端 module 23379 H_）。"""
    v = DATA["vibe"]
    return max(0, count - v["free_count"]) * v["extra_per"]


# ---------------------------------------------------------------- 组合价格

def total_enhance_price(
    params: dict,
    sub: dict,
    model: str,
    vibe_encoding_prices: list[int] | None = None,
    char_ref_count: int = 0,
) -> tuple[int, int]:
    """增强/变体等场景的总价（对应前端 image 页 hd 逻辑）。

    返回 (总价, 附加费)。
    附加费 = sum(每个启用 vibe 引用的编码价) + H_(启用个数) + 角色引用单价*个数*n_samples
    """
    m = 0
    if vibe_encoding_prices:
        m += sum(vibe_encoding_prices)
        m += vibe_extra_price(len(vibe_encoding_prices))
    if char_ref_count:
        m += DATA["char_ref_per_sample"] * char_ref_count * params.get("n_samples", 1)
    base = price_generate(params, sub, model)
    return base + m, m


if __name__ == "__main__":
    sub0 = {"tier": 0, "expiresAt": 0, "accountType": 0}
    p = {"width": 512, "height": 768, "steps": 28, "n_samples": 1,
         "sampler": "k_euler_ancestral", "sm": False, "sm_dyn": False}
    print("示例: 512x768/28/SD1.5 =", price_generate(p, sub0, "nai-diffusion"))
    print("示例: 1024x1024/28/NAI3 =", price_generate({**p, "width": 1024, "height": 1024}, sub0, "nai-diffusion-3"))
