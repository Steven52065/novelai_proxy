#!/usr/bin/env python
"""随机参数对拍：Python 实现 vs Node oracle（网页真实逻辑）。

用法:
    .\\.venv\\Scripts\\python.exe -m anlas_sync.compare [--cases N] [--seed S]

对拍覆盖: GI(生图价格), tY(放大), H_(vibe附加), Dk(参数校验), ax(订阅判断),
Tz(噪点表模型允许), Ux(噪点表采样器允许), PEn(PE.noiseSchedule), DF(家族-采样器表)
任何不一致会打印差异并以非零退出码结束。
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

from anlas_sync import anlas_pricing as ap

DATA = ap.DATA
MODELS = sorted(DATA["model_family"])
SAMPLERS = sorted(set(DATA["enum_all"].values()) & {
    "plms", "ddim", "k_euler", "k_euler_ancestral", "k_dpm_2", "k_dpm_2_ancestral",
    "k_lms", "k_dpmpp_2s_ancestral", "k_dpmpp_sde", "k_dpmpp_2m", "k_dpm_adaptive",
    "k_dpm_fast", "k_dpmpp_2m_sde", "k_dpmpp_3m_sde", "ddim_v3", "nai_smea", "nai_smea_dyn",
})
SIZES = [64, 128, 256, 512, 640, 768, 832, 1024, 1152, 1216, 1344, 1536, 1920, 2048, 2176, 2304, 3072]
TIERS = [0, 1, 2, 3]
ACCOUNT_TYPES = [0, 1, 2, 3, 4]


def random_sub(rng: random.Random) -> dict:
    sub = {
        "tier": rng.choice(TIERS),
        "expiresAt": rng.choice([0, 9999999999]),
        "accountType": rng.choice(ACCOUNT_TYPES),
    }
    # v5/custom 的 opusUsageLimit 免费小图禁用条件
    if rng.random() < 0.3:
        sub["usage"] = {"isNegative": rng.random() < 0.5}
    return sub


def random_params(rng: random.Random, model: str) -> dict:
    w = rng.choice(SIZES) + rng.choice([-2, -1, 0, 0, 1, 2])
    h = rng.choice(SIZES) + rng.choice([-2, -1, 0, 0, 1, 2])
    steps = rng.choice([1, 3, 5, 9, 14, 27, 28, 29, 50, 51, rng.randint(1, 100)])
    params = {
        "width": max(1, w), "height": max(1, h), "steps": steps,
        "n_samples": rng.choice([1, 1, 2, 3, 4]),
        "sampler": rng.choice(list(SAMPLERS)),
        "sm": rng.random() < 0.5, "sm_dyn": rng.random() < 0.3,
        "characterRef": rng.random() < 0.3,
    }
    mode = rng.random()
    if mode < 0.15:
        params["image"] = True
        params["strength"] = round(rng.uniform(0.1, 1.0), 3)
    elif mode < 0.25:
        params["mask"] = True
        params["inpaintImg2ImgStrength"] = round(rng.uniform(0.1, 1.0), 3)
    return params


def make_cases(rng: random.Random, count: int) -> list[dict]:
    cases = []
    for i in range(count):
        model = rng.choice(MODELS)
        sub = random_sub(rng)
        p = random_params(rng, model)
        cases.append({"id": i * 4 + 0, "fn": "GI",
                      "args": [p, {"subscription": sub}, model, rng.random() < 0.3]})
        cases.append({"id": i * 4 + 1, "fn": "Dk", "args": [p, model]})
        cases.append({"id": i * 4 + 2, "fn": "tY",
                      "args": [p["width"], p["height"], {"subscription": sub}]})
        cases.append({"id": i * 4 + 3, "fn": "ax", "args": [sub]})
    # H_ 用例
    for i in range(25):
        cases.append({"id": 100000 + i, "fn": "H_", "args": [i]})

    # 噪点表/家族-采样器用例
    noise_values = list(DATA["noise_schedule"]["values"])
    for i, model in enumerate(MODELS):
        cases.append({"id": 200000 + i * 2, "fn": "Tz", "args": [noise_values, model]})
        cases.append({"id": 200000 + i * 2 + 1, "fn": "PEn", "args": [model]})
    for i, sampler in enumerate(SAMPLERS):
        cases.append({"id": 300000 + i, "fn": "Ux", "args": [sampler]})
    for i, family in enumerate(sorted(DATA["family_samplers"])):
        cases.append({"id": 400000 + i, "fn": "DF", "args": [family]})
    return cases


def py_run(cases: list[dict]) -> dict[int, object]:
    out = {}
    for c in cases:
        fn = c["fn"]
        args = c["args"]
        try:
            if fn == "GI":
                out[c["id"]] = ap.price_generate(args[0], args[1]["subscription"], args[2], args[3])
            elif fn == "Dk":
                out[c["id"]] = ap.validate_params(args[0], args[1])
            elif fn == "tY":
                out[c["id"]] = ap.price_upscale(args[0], args[1], args[2]["subscription"])
            elif fn == "ax":
                out[c["id"]] = ap.is_active_subscription(args[0])
            elif fn == "H_":
                out[c["id"]] = ap.vibe_extra_price(args[0])
            elif fn == "Tz":
                out[c["id"]] = list(ap.noise_schedule_for_model(args[1]))
            elif fn == "PEn":
                out[c["id"]] = ap.model_supports_noise_schedule(args[0])
            elif fn == "Ux":
                out[c["id"]] = list(ap.noise_schedule_for_sampler(args[0]))
            elif fn == "DF":
                out[c["id"]] = list(DATA["family_samplers"].get(args[0], []))
            else:
                out[c["id"]] = None
        except Exception as e:  # noqa: BLE001
            out[c["id"]] = f"PY-ERROR: {e}"
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=5000, help="生图类随机用例数")
    parser.add_argument("--seed", type=int, default=20260818)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    cases = make_cases(rng, args.cases)
    print(f"生成 {len(cases)} 个用例 (seed={args.seed})")

    inp = ROOT / "generated" / "_compare_in.json"
    outp = ROOT / "generated" / "_compare_out.json"
    inp.write_text(json.dumps({"cases": cases}), encoding="utf-8")
    print("调用 Node oracle ...")
    subprocess.run(["node", str(ROOT / "oracle.js"), str(inp), str(outp)],
                   check=True, cwd=str(ROOT))
    oracle = json.loads(outp.read_text(encoding="utf-8"))
    results = oracle["results"]
    errors = oracle.get("errors", {})

    print("计算 Python 实现 ...")
    py = py_run(cases)

    mismatches = []
    for c in cases:
        cid = str(c["id"])
        if cid in errors:
            mismatches.append((c, "oracle-error", errors[cid], py.get(c["id"])))
            continue
        o = results.get(cid)
        p = py.get(c["id"])
        if (o is None and isinstance(p, float) and p != p):  # JS NaN -> JSON null
            continue
        if o != p:
            mismatches.append((c, o, p, None))

    if mismatches:
        print(f"\n不一致 {len(mismatches)} 个:")
        for c, o, p, extra in mismatches[:30]:
            print(f"  id={c['id']} fn={c['fn']} args={json.dumps(c['args'], ensure_ascii=False)[:200]}")
            print(f"    oracle={o!r} python={p!r} {extra or ''}")
        inp.unlink(missing_ok=True)
        outp.unlink(missing_ok=True)
        return 1

    inp.unlink(missing_ok=True)
    outp.unlink(missing_ok=True)
    print(f"\n全部 {len(cases)} 个用例一致 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
