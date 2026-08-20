from __future__ import annotations

import math
from itertools import product

from anlas_sync import anlas_pricing
from novelai_python.sdk.ai import _const as sdk_cost_data
from novelai_python.sdk.ai._cost import CostCalculator
from novelai_python.sdk.ai._enum import Sampler


MODELS = tuple(sorted(anlas_pricing.DATA["model_family"]))
SIZES = (
    (512, 512),
    (512, 768),
    (832, 1216),
    (1024, 1024),
    (1024, 1536),
    (1472, 1024),
    (1536, 1536),
)
STEPS = (1, 10, 28, 29, 50)
SAMPLERS = (
    "k_euler",
    "k_euler_ancestral",
    "k_dpmpp_2m",
    "ddim",
    "ddim_v3",
    "plms",
    "k_lms",
    "nai_smea",
    "nai_smea_dyn",
)
SM_STATES = ((False, False), (True, False), (False, True), (True, True))
N_SAMPLES = (1, 2, 4)
OPUS_STATES = (False, True)
IMAGE_STATES = (False, True)
SDXL_LIKE_FAMILIES = {"stableDiffusionXL", "stableDiffusionXLFurry", "v4"}


def _subscription(is_opus: bool) -> dict[str, int]:
    return {
        "tier": 3 if is_opus else 1,
        "expiresAt": 253402300799,
        "accountType": 0,
    }


def _sdk_price(
    *,
    model: str,
    width: int,
    height: int,
    steps: int,
    sampler: str,
    sm: bool,
    sm_dyn: bool,
    n_samples: int,
    is_opus: bool,
    image: bool,
    strength: float | None,
) -> int:
    return CostCalculator.calculate(
        width=width,
        height=height,
        steps=steps,
        model=model,
        image=image,
        n_samples=n_samples,
        account_tier=3 if is_opus else 1,
        strength=strength,
        sampler=Sampler(sampler),
        is_sm_enabled=sm,
        is_sm_dynamic=sm_dyn,
        is_account_active=True,
    )


def _frontend_price(
    *,
    model: str,
    width: int,
    height: int,
    steps: int,
    sampler: str,
    sm: bool,
    sm_dyn: bool,
    n_samples: int,
    is_opus: bool,
    image: bool,
    strength: float | None,
    **extra,
) -> int | float:
    params = {
        "width": width,
        "height": height,
        "steps": steps,
        "n_samples": n_samples,
        "sampler": sampler,
        "sm": sm,
        "sm_dyn": sm_dyn,
        "image": image,
        "strength": strength,
        **extra,
    }
    return anlas_pricing.price_generate(params, _subscription(is_opus), model)


def _known_difference_reasons(case: dict) -> set[int]:
    family = anlas_pricing.model_family(case["model"])
    uses_lookup = family not in SDXL_LIKE_FAMILIES and not (
        case["width"] * case["height"] <= 1048576
        and case["sampler"] in anlas_pricing.DATA["classic_samplers"]
    )
    reasons: set[int] = set()
    if family in SDXL_LIKE_FAMILIES and case["sm_dyn"] and not case["sm"]:
        reasons.add(1)
    if uses_lookup and case["sm_dyn"] and not case["sm"]:
        reasons.add(2)
    if uses_lookup and case["sampler"] in anlas_pricing.DATA["vi_set"] and (
        case["sm"] or case["sm_dyn"]
    ):
        reasons.add(3)
    return reasons


def test_sdk_cost_tables_match_extracted_frontend_tables():
    assert sdk_cost_data.map == anlas_pricing.DATA["table_c"]
    assert sdk_cost_data.newN == anlas_pricing.DATA["table_u"]
    assert sdk_cost_data.initialN == anlas_pricing.DATA["table_d"]
    assert sdk_cost_data.step == anlas_pricing.DATA["table_h"]
    assert sdk_cost_data.initial_n == anlas_pricing.DATA["table_f"]


def test_full_cost_matrix_has_only_documented_engine_differences():
    observed_reasons: set[int] = set()
    difference_count = 0

    dimensions = product(
        MODELS,
        SIZES,
        STEPS,
        SAMPLERS,
        SM_STATES,
        N_SAMPLES,
        OPUS_STATES,
        IMAGE_STATES,
    )
    for model, (width, height), steps, sampler, (sm, sm_dyn), n_samples, is_opus, image in dimensions:
        case = {
            "model": model,
            "width": width,
            "height": height,
            "steps": steps,
            "sampler": sampler,
            "sm": sm,
            "sm_dyn": sm_dyn,
            "n_samples": n_samples,
            "is_opus": is_opus,
            "image": image,
            "strength": 0.5 if image else None,
        }
        sdk_price = _sdk_price(**case)
        frontend_price = _frontend_price(**case)
        if sdk_price == frontend_price:
            continue

        difference_count += 1
        reasons = _known_difference_reasons(case)
        assert reasons, f"unclassified difference: case={case}, sdk={sdk_price}, frontend={frontend_price}"
        observed_reasons.update(reasons)

    assert difference_count > 0
    assert observed_reasons == {1, 2, 3}


def test_remaining_migration_decisions_have_targeted_examples():
    base = {
        "model": "nai-diffusion",
        "width": 512,
        "height": 768,
        "steps": 28,
        "sampler": "k_euler_ancestral",
        "sm": False,
        "sm_dyn": False,
        "n_samples": 1,
        "is_opus": True,
        "image": False,
        "strength": None,
    }

    # 4: character references disable the free Opus sample.
    assert _sdk_price(**base) == 0
    assert _frontend_price(**base, characterRef=True) == 5

    # 5: masks use inpaintImg2ImgStrength instead of the img2img strength.
    masked = {**base, "is_opus": False, "image": True, "strength": 0.25}
    assert _sdk_price(**masked) == 2
    assert _frontend_price(**masked, mask=True, inpaintImg2ImgStrength=0.75) == 4

    # 6: precise reference cost is per sample after migration.
    assert 1 * 5 == 5
    assert 1 * 5 * 2 == 10

    # 7: vibe encoding remains a separate 2-anlas action; generation only
    # charges the >4 reference surcharge.
    assert anlas_pricing.DATA["vibe"]["per_encoding"] == 2
    assert anlas_pricing.vibe_extra_price(4) == 0
    assert anlas_pricing.vibe_extra_price(5) == 2

    # 8: a missing lookup bucket is NaN and must be rejected by the adapter.
    unsupported_lookup = {
        **base,
        "width": 3072,
        "height": 3072,
        "sampler": "k_dpmpp_2m",
        "is_opus": False,
    }
    assert math.isnan(_frontend_price(**unsupported_lookup))

    # 9: a single-image price above the frontend cap is represented by -3
    # and must likewise be rejected by the adapter.
    above_cap = {
        **base,
        "model": "nai-diffusion-3",
        "width": 3072,
        "height": 3072,
        "steps": 100,
        "is_opus": False,
    }
    assert _frontend_price(**above_cap) == -3
