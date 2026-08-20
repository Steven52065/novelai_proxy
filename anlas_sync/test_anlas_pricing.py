"""anlas 计费 Python 实现的单元测试。

运行:
    .\\.venv\\Scripts\\python.exe -m pytest anlas_sync\\test_anlas_pricing.py -v
"""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import pytest

from anlas_sync import anlas_pricing as ap

ROOT = Path(__file__).resolve().parent
DATA = ap.DATA

SUB_TIER0 = {"tier": 0, "expiresAt": 0, "accountType": 0}
SUB_OPUS = {"tier": 3, "expiresAt": 9999999999, "accountType": 0}
SUB_B2B = {"tier": 0, "expiresAt": 0, "accountType": 1}
SUB_EXPIRED = {"tier": 3, "expiresAt": 1, "accountType": 0}


def base_params(**kw) -> dict:
    p = {
        "width": 512, "height": 768, "steps": 28, "n_samples": 1,
        "sampler": "k_euler_ancestral", "sm": False, "sm_dyn": False,
    }
    p.update(kw)
    return p


# ---------------------------------------------------------------- 已知价格

class TestKnownPrices:
    def test_classic_512x768_28(self):
        assert ap.price_generate(base_params(), SUB_TIER0, "nai-diffusion") == 5

    def test_sdxl_1024x1024_28(self):
        assert ap.price_generate(base_params(width=1024, height=1024), SUB_TIER0, "nai-diffusion-3") == 20

    def test_free_small_opus(self):
        # Opus + 小图 + <=28 步: 4 张只算 3 张
        assert ap.price_generate(base_params(n_samples=4), SUB_OPUS, "nai-diffusion") == 15

    def test_free_small_not_opus(self):
        assert ap.price_generate(base_params(n_samples=4), SUB_TIER0, "nai-diffusion") == 20

    def test_free_small_disabled_flag(self):
        assert ap.price_generate(base_params(n_samples=4), SUB_OPUS, "nai-diffusion", True) == 20

    def test_free_small_charref_disables(self):
        p = base_params(n_samples=4, characterRef=True)
        assert ap.price_generate(p, SUB_OPUS, "nai-diffusion") == 20

    def test_price_cap(self):
        # 超上限返回 -3
        assert ap.price_generate(base_params(width=3072, height=3072, steps=100), SUB_TIER0, "nai-diffusion-3") == -3


class TestUpscale:
    def test_opus_small_free(self):
        assert ap.price_upscale(512, 768, SUB_OPUS) == 0

    def test_tier0_1mp(self):
        assert ap.price_upscale(1024, 1024, SUB_TIER0) == 7

    def test_over_1mp_disabled(self):
        assert ap.price_upscale(1536, 1024, SUB_TIER0) == -3

    def test_table(self):
        # 512x512=262144 -> 表中最小 bucket 262144 -> 1
        assert ap.price_upscale(512, 512, SUB_TIER0) == 1


class TestVibe:
    def test_extra_price(self):
        assert ap.vibe_extra_price(0) == 0
        assert ap.vibe_extra_price(3) == 0
        assert ap.vibe_extra_price(4) == 0
        assert ap.vibe_extra_price(5) == 2
        assert ap.vibe_extra_price(6) == 4


class TestSubscription:
    def test_active(self):
        assert ap.is_active_subscription(SUB_OPUS)
        assert ap.is_active_subscription(SUB_B2B)

    def test_inactive(self):
        assert not ap.is_active_subscription(SUB_TIER0)
        assert not ap.is_active_subscription(SUB_EXPIRED)


class TestModelFamily:
    def test_known(self):
        assert ap.model_family("nai-diffusion") == "stableDiffusion"
        assert ap.model_family("nai-diffusion-2") == "stableDiffusionGroup2"
        assert ap.model_family("nai-diffusion-3") == "stableDiffusionXL"
        assert ap.model_family("nai-diffusion-furry-3") == "stableDiffusionXLFurry"
        assert ap.model_family("nai-diffusion-4-5-curated") == "v4"

    def test_unknown_defaults_sd(self):
        assert ap.model_family("some-future-model") == "stableDiffusion"


class TestValidate:
    def test_ok(self):
        assert ap.validate_params(base_params(), "nai-diffusion")

    def test_too_many_pixels(self):
        assert not ap.validate_params(base_params(width=2048, height=2048), "nai-diffusion")

    def test_steps_limit_for_es_models(self):
        assert not ap.validate_params(base_params(steps=51), "nai-diffusion-4-5-full")
        # nai-diffusion-2 也在 es_set 中（与网页一致）
        assert not ap.validate_params(base_params(steps=51), "nai-diffusion-2")
        # 未知模型不在 es_set，无 steps 限制
        assert ap.validate_params(base_params(steps=51), "some-future-model")


class TestDataIntegrity:
    def test_arrays(self):
        assert len(DATA["table_c"]) == 768
        assert len(DATA["table_u"]) == 1536
        assert len(DATA["table_d"]) == 1536
        assert len(DATA["table_h"]) == 1536
        assert len(DATA["table_f"]) == 1536

    def test_constants(self):
        assert DATA["max_single_price"] == 140
        assert DATA["max_pixels"] == 3145728
        assert DATA["char_ref_per_sample"] == 5
        assert DATA["vibe"]["per_encoding"] == 2
        assert DATA["vibe"]["free_count"] == 4
        assert DATA["vibe"]["extra_per"] == 2

    def test_model_family_covers_extracted_models(self):
        for model in DATA["model_family"]:
            assert ap.model_family(model) == DATA["model_family"][model]


# ---------------------------------------------------------------- 对拍集成测试

def _node_available() -> bool:
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=10)
        return True
    except Exception:  # noqa: BLE001
        return False


HAS_ORACLE = _node_available() and (ROOT / "cache" / "_app.js").exists()


@pytest.mark.skipif(not HAS_ORACLE, reason="需要 node 与 cache/ 中的网页 chunk")
def test_oracle_compare():
    import sys
    rc = subprocess.run(
        [sys.executable, "-m", "anlas_sync.compare", "--cases", "300", "--seed", "7"],
        capture_output=True, text=True, timeout=180, cwd=ROOT.parent)
    assert rc.returncode == 0, rc.stdout + rc.stderr
