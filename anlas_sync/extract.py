#!/usr/bin/env python
"""从下载的 chunk 中程序化提取 anlas 计费所需的数据，生成 generated/pricing_data.json。

用法:
    .\\.venv\\Scripts\\python.exe anlas_sync\\extract.py

依赖:
    - cache/_app.js      (download.py 下载)
    - cache/chunk-2075.js (vibe 编码价格提取)
    - cache/chunk-3811.js (角色引用单价提取)
    - node + oracle.js    (生成模型家族映射 MODEL_FAMILY)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
GEN = ROOT / "generated"
SNIPPETS = GEN / "js_snippets"

# ---- JS 模块源码提取 -------------------------------------------------------

_MODULE_BOUNDARY = re.compile(
    r",\d+:(?:\([a-z],[a-z],[a-z]\)=>|function\([a-z],[a-z],[a-z]\)\{)")


_MODULE_DEF = re.compile(r",(\d+):(?:\([a-z],[a-z],[a-z]\)=>|function\([a-z],[a-z],[a-z]\)\{)")


def extract_module(src: str, module_id: int) -> str:
    """从 minified webpack chunk 中按模块 id 提取源码（含前导逗号）。"""
    m = _MODULE_DEF.search(src)
    while m:
        if int(m.group(1)) == module_id:
            body_start = m.start() + 1
            brace = src.find("{", m.start())
            nxt = _MODULE_DEF.search(src, brace + 1)
            if not nxt:
                raise ValueError(f"模块 {module_id} 边界未找到")
            return src[body_start:body_start + nxt.start()], "arrow"
        m = _MODULE_DEF.search(src, m.end())
    raise ValueError(f"模块 {module_id} 未找到，网页结构可能已变化")


_NUM_TOKEN = re.compile(r"^(?:-?(?:\d+\.?\d*|\.\d+)(?:e-?\d+)?)$")


def parse_float_array(body: str) -> list[float] | None:
    """解析形如 `.124,0.11,.07` 的 JS 数字数组；含非数字 token 时返回 None。"""
    out = []
    for tok in body.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if not _NUM_TOKEN.match(tok):
            return None
        out.append(float(tok))
    return out


def parse_int_array(body: str) -> list[int]:
    out = []
    for tok in body.split(","):
        tok = tok.strip()
        if not tok:
            continue
        out.append(int(tok))
    return out


# ---- 主流程 -----------------------------------------------------------------

def main() -> int:
    GEN.mkdir(parents=True, exist_ok=True)
    SNIPPETS.mkdir(parents=True, exist_ok=True)

    app_src = (CACHE / "_app.js").read_text(encoding="utf-8")
    chunk2075 = (CACHE / "chunk-2075.js").read_text(encoding="utf-8")
    chunk3811 = (CACHE / "chunk-3811.js").read_text(encoding="utf-8")

    data: dict = {}

    # 1) 模块源码存档
    for mid in (23379, 18401, 71810, 1018):
        mod, kind = extract_module(app_src, mid)
        (SNIPPETS / f"module_{mid}.js").write_text(mod, encoding="utf-8")
        print(f"[1] 提取模块 {mid} ({kind}, {len(mod)} 字符)")

    mod23379, _ = extract_module(app_src, 23379)

    # 2) >1MP 查表数组: c(整数桶表) + u/d/h/f(采样器浮点表)
    c_m = re.search(r"let c=\[([^\]]*)\]", mod23379)
    if not c_m:
        print("错误: 未找到 c 数组", file=sys.stderr)
        return 1
    data["table_c"] = parse_int_array(c_m.group(1))
    print(f"[2] 提取 c 数组: {len(data['table_c'])} 项")

    big_tables = {}
    for mm in re.finditer(r"[,;]([a-zA-Z_$][\w$]*)=\[([^\]]*)\]", mod23379):
        name, body = mm.group(1), mm.group(2)
        if not re.fullmatch(r"[\s\d.,e+-]+", body):
            continue
        nums = parse_float_array(body)
        if nums is not None and len(nums) >= 1000:
            big_tables[name] = nums
    if sorted(big_tables) != ["d", "f", "h", "u"]:
        print(f"错误: 采样器数组名异常: {sorted(big_tables)}", file=sys.stderr)
        return 1
    for name in ("u", "d", "h", "f"):
        data[f"table_{name}"] = big_tables[name]
        print(f"[2] 提取采样器表 {name}: {len(big_tables[name])} 项")

    # 3) upscale 表 e0 与常量
    e0_m = re.search(r"e0=\[\[(.*?)\]\]", mod23379)
    if not e0_m:
        print("错误: 未找到 e0 表", file=sys.stderr)
        return 1
    data["upscale_table"] = [
        [int(a), int(b)]
        for a, b in re.findall(r"(\d+),(\d+)", e0_m.group(1))
    ]
    print(f"[3] 提取 upscale 表: {data['upscale_table']}")

    ep_m = re.search(r"let ep=(\d+)", mod23379)
    if not ep_m:
        print("错误: 未找到 ep 常量", file=sys.stderr)
        return 1
    data["max_pixels"] = int(ep_m.group(1))
    print(f"[3] max_pixels(ep) = {data['max_pixels']}")

    # 4) 模块 1018 常量: dZ=140(单图价格上限), kJ=900, Hi=75
    mod1018, _ = extract_module(app_src, 1018)
    const_m = re.search(r"let n=(\d+),i=(\d+),o=(\d+)", mod1018)
    if not const_m:
        print("错误: 未找到 1018 常量", file=sys.stderr)
        return 1
    data["max_single_price"] = int(const_m.group(2))  # i = dZ
    data["kJ"] = int(const_m.group(1))
    data["Hi"] = int(const_m.group(3))
    print(f"[4] dZ={data['max_single_price']} kJ={data['kJ']} Hi={data['Hi']}")

    # 5) 公式系数（GI 的 i 函数与 o 函数）
    i_fn = re.search(
        r"function i\(e,t,r\)\{return\(([\d.e+-]+)\*Math\.exp\(e\*t/1048576\*([\d.e+-]+)\)\+([\d.e+-]+)\)/28\*r\}",
        mod23379)
    if not i_fn:
        print("错误: 未找到经典公式系数", file=sys.stderr)
        return 1
    data["classic_formula"] = {
        "a": float(i_fn.group(1)), "b": float(i_fn.group(2)), "c": float(i_fn.group(3)),
    }
    print(f"[5] 经典公式系数: {data['classic_formula']}")

    o_fn = re.search(
        r"function o\(e,t,r,n,i\)\{let o=e\*t;return Math\.ceil\(([\d.e+-]+)\*o\+([\d.e+-]+)\*o\*r\)\*\(i\?1\.4:n\?1\.2:1\)\}",
        mod23379)
    if not o_fn:
        print("错误: 未找到 SDXL/v4 公式系数", file=sys.stderr)
        return 1
    data["sdxl_formula"] = {
        "pixels": float(o_fn.group(1)), "per_step": float(o_fn.group(2)),
        "sm_mult": 1.2, "sm_dyn_mult": 1.4,
    }
    print(f"[5] SDXL/v4 公式系数: {data['sdxl_formula']}")

    # 6) 免费小图规则常量（eZ + GI 内条件）
    eZ_m = re.search(r"function eZ\(e\)\{return!e\.characterRef&&e\.width\*e\.height<=(\d+)&&e\.steps<=(\d+)\}", mod23379)
    if not eZ_m:
        print("错误: 未找到 eZ 免费规则", file=sys.stderr)
        return 1
    data["free_small"] = {
        "max_pixels": int(eZ_m.group(1)),
        "max_steps": int(eZ_m.group(2)),
        "min_tier": 3,
    }
    print(f"[6] 免费小图规则: {data['free_small']}")

    # 7) vibe 编码价格（chunk-2075 模块 61094 SW.getPrice 内的 price:2）
    mod61094, _ = extract_module(chunk2075, 61094)
    gp_idx = mod61094.find("async getPrice(")
    if gp_idx < 0:
        print("错误: 未找到 SW.getPrice 函数", file=sys.stderr)
        return 1
    # 未编码分支位于 getPrice 函数体内，取最后一个 {exists:!1,price:N}
    price_matches = list(re.finditer(r"exists:!1,price:(\d+)", mod61094[gp_idx:gp_idx + 2000]))
    if not price_matches:
        print("错误: 未找到 SW.getPrice 未编码价格", file=sys.stderr)
        return 1
    per_encoding = int(price_matches[-1].group(1))
    data["vibe"] = {
        "per_encoding": per_encoding,
        "free_count": 4,
        "extra_per": 2,   # H_ = max(0, n-4)*2
    }
    print(f"[7] vibe 编码价格: {data['vibe']}")

    # 8) 角色引用单价（chunk-3811 hd: m += 5*l.length*p.n_samples）
    ref_m = re.search(r"m\+=(\d+)\*l\.length\*p\.n_samples", chunk3811)
    if not ref_m:
        print("错误: 未找到角色引用单价", file=sys.stderr)
        return 1
    data["char_ref_per_sample"] = int(ref_m.group(1))
    print(f"[8] 角色引用单价: {data['char_ref_per_sample']}")

    # 9) 模型枚举与采样器枚举（模块 18401）
    mod18401, _ = extract_module(app_src, 18401)
    enum_pairs = re.findall(r'e\.([A-Za-z0-9_$]+)="([^"]*)"', mod18401)
    enum: dict[str, dict[str, str]] = {}
    for k, v in enum_pairs:
        # 按值分组会混淆；这里直接按出现顺序收集全部，再由 oracle 生成家族映射
        enum.setdefault("all", {})[k] = v
    data["enum_all"] = enum["all"]
    print(f"[9] 枚举键值: {len(enum['all'])} 个")

    # 采样器值集合（用于识别 Set 中的采样器）
    SAMPLER_VALUES = {
        "plms", "ddim", "k_euler", "k_euler_ancestral", "k_dpm_2", "k_dpm_2_ancestral",
        "k_lms", "k_dpmpp_2s_ancestral", "k_dpmpp_sde", "k_dpmpp_2m", "k_dpm_adaptive",
        "k_dpm_fast", "k_dpmpp_2m_sde", "k_dpmpp_3m_sde", "ddim_v3", "nai_smea", "nai_smea_dyn",
    }

    # 9b) VI_SET: 禁用 sm/sm_dyn 的采样器 (module 18401 的 rs Set)
    vi_m = re.search(r"let rs=new Set\(\[([^\]]*)\]\)", mod18401)
    if not vi_m:
        print("错误: 未找到 VI_SET(rs) 采样器集合", file=sys.stderr)
        return 1
    data["vi_set"] = re.findall(r'"([^"]+)"', vi_m.group(1))
    print(f"[9b] VI_SET: {data['vi_set']}")


    # 9c) ES_SET: Dk 校验中用到的模型 (module 23379 es 函数)
    es_idx = mod23379.find("function es(")
    if es_idx < 0:
        print("错误: 未找到 es 函数", file=sys.stderr)
        return 1
    es_close = mod23379.find("}", es_idx)
    if es_close < 0:
        print("错误: es 函数边界未找到", file=sys.stderr)
        return 1
    es_body = mod23379[es_idx:es_close + 1]
    es_keys = re.findall(r"l\.oM\.([A-Za-z0-9_$]+)", es_body)
    es_values = [enum["all"][k] for k in es_keys if k in enum["all"]]
    data["es_set"] = es_values
    print(f"[9c] ES_SET: {len(es_values)} 个模型")

    # 9d) CLASSIC_SAMPLERS: 经典解析公式适用的采样器 (GI 内联判断)
    classic_m = re.search(
        r"e\.sampler===l\.l1\.plms\|\|e\.sampler===l\.l1\.ddim\|\|e\.sampler===l\.l1\.kEuler\|\|e\.sampler===l\.l1\.kEulerAncestral\|\|e\.sampler===l\.l1\.kLms",
        mod23379)
    if not classic_m:
        print("错误: 未找到经典采样器集合", file=sys.stderr)
        return 1
    data["classic_samplers"] = ["plms", "ddim", "k_euler", "k_euler_ancestral", "k_lms"]
    print(f"[9d] CLASSIC_SAMPLERS: {data['classic_samplers']}")

    model_names = sorted({
        v for k, v in enum["all"].items()
        if v.startswith(("nai-diffusion", "stable-diffusion", "safe-diffusion",
                         "waifu-diffusion", "furry-diffusion", "curated-diffusion", "custom"))
    })
    cases = [{"id": i, "fn": "Jg", "args": [m]} for i, m in enumerate(model_names)]
    inp = GEN / "_jg_cases.json"
    outp = GEN / "_jg_results.json"
    inp.write_text(json.dumps({"cases": cases}), encoding="utf-8")
    print(f"[10] 调用 oracle 生成模型家族映射 ({len(model_names)} 个模型)...")
    subprocess.run(["node", str(ROOT / "oracle.js"), str(inp), str(outp)],
                   check=True, cwd=str(ROOT))
    jg = json.loads(outp.read_text(encoding="utf-8"))
    if jg.get("errors"):
        print(f"错误: oracle Jg 失败: {jg['errors']}", file=sys.stderr)
        return 1
    data["model_family"] = {model_names[i]: jg["results"][str(i)]
                            for i in range(len(model_names))}
    inp.unlink(missing_ok=True)
    outp.unlink(missing_ok=True)
    print(f"[10] 模型家族映射: {data['model_family']}")

    # 11) 写出 pricing_data.json
    out = GEN / "pricing_data.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n完成: {out} ({out.stat().st_size} 字节)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
