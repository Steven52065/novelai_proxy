#!/usr/bin/env python
"""从下载的 chunk 中程序化提取 anlas 计费所需的数据，生成 generated/pricing_data.json。

用法:
    .\\.venv\\Scripts\\python.exe anlas_sync\\extract.py

依赖:
    - cache/_app.js       (download.py 下载)
    - cache/chunk-1052.js (GI/tY/H_ 定价模块 61225、Dk/尺寸模块 57863、
                           家族-采样器表 模块 32036)
    - cache/chunk-7416.js (SW.getPrice vibe 编码价格, 模块 25690)
    - cache/chunk-1741.js (image 页角色引用附加费)
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


def parse_float_array(body: str) -> list[float] | None:
    """解析形如 `.124,0.11,.07` 的 JS 数字数组；含非数字 token 时返回 None。"""
    out = []
    for tok in body.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(float(tok))
        except ValueError:
            return None
    return out


def parse_int_array(body: str) -> list[int]:
    out = []
    for tok in body.split(","):
        tok = tok.strip()
        if not tok:
            continue
        out.append(int(tok))
    return out



def extract_pe_opus_limit_models(mod53856: str, enum: dict[str, str]) -> list[str]:
    """从 PE(模块 53856) 提取 opusUsageLimit:!0 的模型。

    PE 的 switch 中多个 case 标签可共享同一个 return 块（例如 v5 的 4 个模型），
    因此用“连续 case 标签 + return 块”的正则整组匹配，再取 opusUsageLimit:!0
    组内的全部标签。这些模型（v5/custom）的免费小图会被
    subscription.usage.isNegative 禁用。
    """
    pe_idx = mod53856.find("function h(e){")
    if pe_idx < 0:
        raise SystemExit("错误: 未找到 PE(opusUsageLimit) 函数")
    body = mod53856[pe_idx:]
    group_re = re.compile(r'(?:case"([^"]+)":)+return\{[^{}]*opusUsageLimit:(!0|!1)')
    out: list[str] = []
    for m in group_re.finditer(body):
        if m.group(2) != "!0":
            continue
        labels = re.findall(r'case"([^"]+)"', m.group(0))
        out.extend(labels)
    model_values = set(enum.values())
    return sorted(x for x in out if x in model_values)




def extract_enum(mod: str, var: str) -> dict[str, str]:
    """提取形如 `,a=function(e){return e.key="value",...,e}({})` 的 JS 枚举。"""
    m = re.search(r'(?:var|,)\s*%s=function\(e\)\{return (.*?),e\}\(\{\}\)' % re.escape(var), mod)
    if not m:
        return {}
    return dict(re.findall(r'e\.([A-Za-z0-9_$]+)="([^"]*)"', m.group(1)))


def extract_family_samplers(
    mod32036: str,
    sampler_enum: dict[str, str],
    family_enum: dict[str, str],
) -> dict[str, list[str]]:
    """从模块 32036 提取家族-采样器表（DF）。

    表结构为 `let n=e=>{switch(e){case r.lh.<家族>:return <变量>;...}},o=[...],l=[...],s=[...],d=[...],u=[...];`，
    每个变量是一组 `{label, options:[{name, value:r.l1.<采样器>}]}`。
    返回按家族键（stableDiffusion / stableDiffusionGroup2 / stableDiffusionXL /
    stableDiffusionXLFurry / v4 / v5）扁平化去重后的采样器值列表。
    """
    start = mod32036.find("let n=e=>{")
    end = mod32036.find("function h(e)", start)
    if start < 0 or end < 0:
        raise SystemExit("错误: 未找到模块 32036 家族-采样器表")
    body = mod32036[start:end]

    sw = re.search(r"switch\(e\)\{(.*?)\}\}", body, re.S)
    if not sw:
        raise SystemExit("错误: 未找到模块 32036 家族映射 switch")
    mapping: dict[str, str] = {}
    for m in re.finditer(r"(?:case r\.lh\.([A-Za-z0-9_$]+):)+return\s*([a-z]);?", sw.group(1)):
        labels = re.findall(r"case r\.lh\.([A-Za-z0-9_$]+):", m.group(0))
        for label in labels:
            mapping[label] = m.group(2)

    var_positions = [(m.group(1), m.end() - 1)
                     for m in re.finditer(r"(?:^|[,\s])([olsdu])=(\[)", body)]
    tables: dict[str, list[str]] = {}
    for idx, (var, pos) in enumerate(var_positions):
        endpos = var_positions[idx + 1][1] if idx + 1 < len(var_positions) else body.find(";function", pos)
        if endpos < 0:
            endpos = len(body)
        values = [sampler_enum[key] for key in re.findall(r"value:r\.l1\.([A-Za-z0-9_$]+)", body[pos:endpos])]
        tables[var] = values

    out: dict[str, list[str]] = {}
    for key, var in mapping.items():
        family = family_enum.get(key)
        if not family:
            continue
        seen: list[str] = []
        for value in tables.get(var, []):
            if value not in seen:
                seen.append(value)
        out[family] = seen
    return out


def extract_noise_schedule(
    mod53856: str,
    model_names: list[str],
    sampler_enum: dict[str, str],
) -> dict:
    """从模块 53856 提取噪点表数据（p 枚举 / g 模型允许 / A 采样器允许 / PE 能力）。

    - values: 噪点表合法值（native/karras/exponential/polyexponential）。
    - model_allowed: 前端 g(values, model) 对每个已知模型的结果
      （v5 为空、v4/v4.5 去掉 native、其余为全部）。
    - model_supports: 前端 PE(model).noiseSchedule 是否支持该参数。
    - sampler_allowed: 前端 A(sampler) 对每个采样器枚举值的允许列表。
    """
    p_start = mod53856.find("var p=function(e){")
    if p_start < 0:
        raise SystemExit("错误: 未找到噪点表枚举 p")
    p_m = re.search(r"var p=function\(e\)\{return (.*?),e\}\(\{\}\)", mod53856[p_start:p_start + 500])
    if not p_m:
        raise SystemExit("错误: 未找到噪点表枚举 p 内容")
    values = [v for _, v in re.findall(r'e\.([A-Za-z0-9_$]+)="([^"]*)"', p_m.group(1))]

    g_start = mod53856.find("function g(e,t){switch(t){", p_start)
    if g_start < 0:
        raise SystemExit("错误: 未找到噪点表模型允许函数 g")
    g_m = re.search(r"function g\(e,t\)\{switch\(t\)\{(.*?)\}\}", mod53856[g_start:], re.S)
    if not g_m:
        raise SystemExit("错误: 未找到噪点表模型允许函数 g 内容")
    g_groups: list[tuple[list[str], str]] = []
    for m in re.finditer(r'(?:case"([^"]+)":)+return([^;]+);', g_m.group(1)):
        g_groups.append((re.findall(r'case"([^"]+)"', m.group(0)), m.group(2).strip()))

    def model_allowed_for(model: str) -> list[str]:
        for labels, ret in g_groups:
            if model in labels:
                if ret == "[]":
                    return []
                if "filter" in ret:
                    return [v for v in values if v != "native"]
                return list(values)
        return list(values)

    model_allowed = {model: model_allowed_for(model) for model in model_names}

    a_start = mod53856.find("function A(e){switch(e){", g_start)
    if a_start < 0:
        raise SystemExit("错误: 未找到噪点表采样器允许函数 A")
    a_m = re.search(r"function A\(e\)\{switch\(e\)\{(.*?)\}\}", mod53856[a_start:], re.S)
    if not a_m:
        raise SystemExit("错误: 未找到噪点表采样器允许函数 A 内容")
    a_groups: list[tuple[list[str], list[str]]] = []
    for m in re.finditer(r'(?:case"([^"]+)":)+return(\[[^\]]*\]);?', a_m.group(1)):
        a_groups.append((re.findall(r'case"([^"]+)"', m.group(0)), re.findall(r'"([^"]+)"', m.group(2))))

    sampler_allowed = {}
    for sampler in sampler_enum.values():
        allowed: list[str] = []
        for labels, vals in a_groups:
            if sampler in labels:
                allowed = vals
                break
        sampler_allowed[sampler] = allowed

    pe_idx = mod53856.find("function h(e){")
    if pe_idx < 0:
        raise SystemExit("错误: 未找到 PE 函数")
    supports: dict[str, bool] = {}
    group_re = re.compile(r'(?:case"([^"]+)":)+return\{[^{}]*noiseSchedule:(!0|!1)')
    for m in group_re.finditer(mod53856[pe_idx:]):
        supported = m.group(2) == "!0"
        for label in re.findall(r'case"([^"]+)"', m.group(0)):
            supports[label] = supported
    model_supports = {model: supports.get(model, False) for model in model_names}

    return {
        "values": values,
        "model_allowed": model_allowed,
        "model_supports": model_supports,
        "sampler_allowed": sampler_allowed,
    }


# ---- 主流程 -----------------------------------------------------------------

def main() -> int:
    GEN.mkdir(parents=True, exist_ok=True)
    SNIPPETS.mkdir(parents=True, exist_ok=True)

    app_src = (CACHE / "_app.js").read_text(encoding="utf-8")
    chunk1052 = (CACHE / "chunk-1052.js").read_text(encoding="utf-8")
    chunk7416 = (CACHE / "chunk-7416.js").read_text(encoding="utf-8")
    chunk1741 = (CACHE / "chunk-1741.js").read_text(encoding="utf-8")

    data: dict = {}

    # 1) 模块源码存档
    archive = [
        ("_app.js", 53856),      # 枚举/Jg/PE/VI
        ("_app.js", 62654),      # 订阅 ax
        ("_app.js", 46542),      # 常量 dZ/kJ/Hi
        ("_app.js", 41179),      # 免费小图 t1 等
        ("chunk-1052.js", 61225),  # 定价 GI/tY/H_/Lq
        ("chunk-1052.js", 57863),  # Dk/尺寸/放大表/最大像素
        ("chunk-1052.js", 32036),  # 家族-采样器表 DF/sC
        ("chunk-7416.js", 25690),  # SW.getPrice
    ]
    for fname, mid in archive:
        src = app_src if fname == "_app.js" else (chunk1052 if fname == "chunk-1052.js" else chunk7416)
        mod, kind = extract_module(src, mid)
        (SNIPPETS / f"module_{mid}.js").write_text(mod, encoding="utf-8")
        print(f"[1] 提取模块 {mid} ({fname}, {kind}, {len(mod)} 字符)")

    mod61225, _ = extract_module(chunk1052, 61225)
    mod57863, _ = extract_module(chunk1052, 57863)
    mod32036, _ = extract_module(chunk1052, 32036)
    mod53856, _ = extract_module(app_src, 53856)
    mod62654, _ = extract_module(app_src, 62654)
    mod46542, _ = extract_module(app_src, 46542)
    mod41179, _ = extract_module(app_src, 41179)
    mod25690, _ = extract_module(chunk7416, 25690)

    # 2) 查表数组: i(整数桶表) + o/l/s/d(采样器浮点表)
    #    let i=[...],o=[...],l=[...],s=[...],d=[...];var u=...
    chain_start = mod61225.find("let i=[")
    chain_end = mod61225.find(";var u=", chain_start)
    if chain_start < 0 or chain_end < 0:
        print("错误: 未找到查表 let 链", file=sys.stderr)
        return 1
    chain = mod61225[chain_start:chain_end]

    i_m = re.search(r"let i=\[([^\]]*)\]", chain)
    if not i_m:
        print("错误: 未找到 i 数组", file=sys.stderr)
        return 1
    data["table_c"] = parse_int_array(i_m.group(1))
    print(f"[2] 提取 i 数组(table_c): {len(data['table_c'])} 项")

    # JS 变量名 -> 数据键（语义与旧版一致）
    table_map = {"o": "table_u", "l": "table_d", "s": "table_h", "d": "table_f"}
    for js_name, key in table_map.items():
        m = re.search(r"," + js_name + r"=\[([^\]]*)\]", chain)
        if not m:
            print(f"错误: 未找到采样器表 {js_name}", file=sys.stderr)
            return 1
        nums = parse_float_array(m.group(1))
        if nums is None or len(nums) != 1536:
            print(f"错误: 采样器表 {js_name} 长度异常: {len(nums) if nums else 0}", file=sys.stderr)
            return 1
        data[key] = nums
        print(f"[2] 提取采样器表 {js_name} -> {key}: {len(nums)} 项")

    # 3) upscale 表 g=[[1048576,1],[1747627,2],[2446678,3],[c.xM,4]]
    g_m = re.search(r"(?:^|[;,\s])g=\[\[(.*?)\]\]", mod61225)
    if not g_m:
        print("错误: 未找到 upscale 表 g", file=sys.stderr)
        return 1
    g_body = g_m.group(1)
    px_m = re.search(r"(?:^|[;,\s])m=(\d+),g=1048576", mod57863)
    max_px = int(px_m.group(1)) if px_m else None
    upscale = []
    for a, b in re.findall(r"(\d+|c\.xM),(\d+)(?:\]|$)", g_body):
        if a == "c.xM":
            if max_px is None:
                print("错误: 未找到 max_pixels(m)", file=sys.stderr)
                return 1
            a = str(max_px)
        upscale.append([int(a), int(b)])
    data["upscale_table"] = upscale
    print(f"[3] 提取 upscale 表: {data['upscale_table']}")

    # 4) max_pixels (模块 57863: m=3145728)
    px_m = re.search(r"(?:^|[;,\s])m=(\d+),g=1048576", mod57863)
    if not px_m:
        print("错误: 未找到 max_pixels(m)", file=sys.stderr)
        return 1
    data["max_pixels"] = int(px_m.group(1))
    print(f"[4] max_pixels(m) = {data['max_pixels']}")

    # 5) 常量 (模块 46542: dZ=140 单图价格上限, kJ=900, Hi=75)
    const_m = re.search(r"let n=(\d+),i=(\d+),a=(\d+)", mod46542)
    if not const_m:
        print("错误: 未找到 46542 常量", file=sys.stderr)
        return 1
    data["max_single_price"] = int(const_m.group(2))
    print(f"[5] 常量 dZ={data['max_single_price']} kJ={const_m.group(1)} Hi={const_m.group(3)}")

    # 6) 经典公式 (模块 61225 GI)
    classic_m = re.search(
        r"\(([\d.]+)\*Math\.exp\(g\*m/1048576\*\.([\d.]+)\)\+(-?[\d.]+)\)/28",
        mod61225)
    if not classic_m:
        print("错误: 未找到经典公式", file=sys.stderr)
        return 1
    data["classic_formula"] = {
        "a": float(classic_m.group(1)),
        "b": float("." + classic_m.group(2)),
        "c": float(classic_m.group(3)),
    }
    print(f"[6] 经典公式: {data['classic_formula']}")

    # 7) SDXL/v4/v5 公式 + v5 倍率
    sdxl_m = re.search(
        r"Math\.ceil\(([\d.e+-]+)\*i\+([\d.e+-]+)\*i\*a\)\*\(n\?1\.4:r\?1\.2:1\)",
        mod61225)
    if not sdxl_m:
        print("错误: 未找到 SDXL 公式", file=sys.stderr)
        return 1
    data["sdxl_formula"] = {
        "pixels": float(sdxl_m.group(1)),
        "per_step": float(sdxl_m.group(2)),
        "sm_mult": 1.2,
        "sm_dyn_mult": 1.4,
    }
    v5_m = re.search(r"===n\.lh\.v5&&\(M\*=\s*([\d.]+)\)", mod61225)
    data["v5_multiplier"] = float(v5_m.group(1)) if v5_m else 1.0
    print(f"[7] SDXL 公式: {data['sdxl_formula']} | v5_multiplier={data['v5_multiplier']}")

    # 8) 免费小图: t1(模块 41179) 的像素/步数 + GI 的 tier 条件
    free_m = re.search(r"!e\.characterRef&&e\.width\*e\.height<=(\d+)&&e\.steps<=(\d+)", mod41179)
    if not free_m:
        print("错误: 未找到免费小图 t1 条件", file=sys.stderr)
        return 1
    tier_m = re.search(r"tier>=(\d+)", mod61225)
    data["free_small"] = {
        "max_pixels": int(free_m.group(1)),
        "max_steps": int(free_m.group(2)),
        "min_tier": int(tier_m.group(1)) if tier_m else 3,
    }
    print(f"[8] free_small: {data['free_small']}")

    # 9) opusUsageLimit 模型（免费小图的负 usage 禁用条件）
    enum_pairs = re.findall(r'e\.([A-Za-z0-9_$]+)="([^"]*)"', mod53856)
    enum: dict[str, str] = {}
    for k, v in enum_pairs:
        enum.setdefault(k, v)
    data["opus_usage_limit_models"] = extract_pe_opus_limit_models(mod53856, enum)
    print(f"[9] opus_usage_limit_models: {data['opus_usage_limit_models']}")

    # 10) vibe 编码价格 (模块 25690 SW.getPrice 未编码分支)
    gp_idx = mod25690.find("async getPrice(")
    if gp_idx < 0:
        print("错误: 未找到 SW.getPrice 函数", file=sys.stderr)
        return 1
    price_matches = list(re.finditer(r"exists:!1,price:(\d+)", mod25690[gp_idx:gp_idx + 2000]))
    if not price_matches:
        print("错误: 未找到 SW.getPrice 未编码价格", file=sys.stderr)
        return 1
    per_encoding = int(price_matches[-1].group(1))
    h_m = re.search(r"p=(\d+);function v\(e\)\{return Math\.max\(0,e-(\d+)\)", mod61225)
    if not h_m:
        print("错误: 未找到 H_ 附加费常量", file=sys.stderr)
        return 1
    data["vibe"] = {
        "per_encoding": per_encoding,
        "free_count": int(h_m.group(2)),
        "extra_per": int(h_m.group(1)),
    }
    print(f"[10] vibe 编码价格: {data['vibe']}")

    # 11) 角色引用单价 (chunk-1741: g+=5*l.length*u.n_samples)
    ref_m = re.search(r"g\+=(\d+)\*l\.length\*u\.n_samples", chunk1741)
    if not ref_m:
        print("错误: 未找到角色引用单价", file=sys.stderr)
        return 1
    data["char_ref_per_sample"] = int(ref_m.group(1))
    print(f"[11] 角色引用单价: {data['char_ref_per_sample']}")

    # 12) 模型/采样器枚举 (模块 53856)
    data["enum_all"] = dict(enum)
    print(f"[12] 枚举键值: {len(enum)} 个")

    # 13) VI_SET: 禁用 sm/sm_dyn 的采样器 (模块 53856 的 C Set)
    vi_m = re.search(r"let C=new Set\(\[([^\]]*)\]\)", mod53856)
    if not vi_m:
        print("错误: 未找到 VI_SET(C) 采样器集合", file=sys.stderr)
        return 1
    data["vi_set"] = re.findall(r'"([^"]+)"', vi_m.group(1))
    print(f"[13] VI_SET: {data['vi_set']}")

    # 14) CLASSIC_SAMPLERS: 经典解析公式适用的采样器 (GI 内联判断)
    classic_m = re.search(
        r"e\.sampler===n\.l1\.plms\|\|e\.sampler===n\.l1\.ddim\|\|e\.sampler===n\.l1\.kEuler\|\|e\.sampler===n\.l1\.kEulerAncestral\|\|e\.sampler===n\.l1\.kLms",
        mod61225)
    if not classic_m:
        print("错误: 未找到经典采样器集合", file=sys.stderr)
        return 1
    data["classic_samplers"] = ["plms", "ddim", "k_euler", "k_euler_ancestral", "k_lms"]
    print(f"[14] CLASSIC_SAMPLERS: {data['classic_samplers']}")

    # 15) Dk 校验 steps 上限 (模块 57863: !(e.steps>50))
    step_m = re.search(r"!\(e\.steps>(\d+)\)", mod57863)
    if not step_m:
        print("错误: 未找到 Dk steps 上限", file=sys.stderr)
        return 1
    data["validate_steps_limit"] = int(step_m.group(1))
    print(f"[15] Dk steps 上限: {data['validate_steps_limit']}")

    # 16) 模型家族映射（oracle 对每个模型执行 Jg）
    model_names = sorted({
        v for k, v in enum.items()
        if v.startswith(("nai-diffusion", "stable-diffusion", "safe-diffusion",
                         "waifu-diffusion", "furry-diffusion", "curated-diffusion", "custom"))
    })
    cases = [{"id": i, "fn": "Jg", "args": [m]} for i, m in enumerate(model_names)]
    inp = GEN / "_jg_cases.json"
    outp = GEN / "_jg_results.json"
    inp.write_text(json.dumps({"cases": cases}), encoding="utf-8")
    print(f"[16] 调用 oracle 生成模型家族映射 ({len(model_names)} 个模型)...")
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
    print(f"[16] 模型家族映射: {data['model_family']}")

    # 17) 家族-采样器表 + 噪点表（模块 32036 / 模块 53856）
    sampler_enum = extract_enum(mod53856, "d")   # l1 采样器枚举
    family_enum = extract_enum(mod53856, "a")    # lh 家族枚举
    if not sampler_enum or not family_enum:
        print("错误: 未找到采样器/家族枚举", file=sys.stderr)
        return 1
    data["family_samplers"] = extract_family_samplers(mod32036, sampler_enum, family_enum)
    print(f"[17] 家族-采样器表: {data['family_samplers']}")
    data["noise_schedule"] = extract_noise_schedule(mod53856, model_names, sampler_enum)
    print(f"[17] 噪点表: values={data['noise_schedule']['values']} "
          f"model_supports={sum(data['noise_schedule']['model_supports'].values())} 个模型 "
          f"sampler_allowed={sum(bool(v) for v in data['noise_schedule']['sampler_allowed'].values())} 个采样器")

    # 18) 写出 pricing_data.json
    out = GEN / "pricing_data.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n完成: {out} ({out.stat().st_size} 字节)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
