from __future__ import annotations

from app.costing import GenerateCostEstimator, ReferenceCostCalculator
from app.policies.free_small_only import FreeSmallOnlyPolicy


FREE_SMALL_PAYLOAD = {
    "input": "1girl",
    "model": "nai-diffusion-3",
    "action": "generate",
    "parameters": {
        "width": 832,
        "height": 1216,
        "scale": 5.0,
        "sampler": "k_euler_ancestral",
        "steps": 23,
        "n_samples": 1,
        "ucPreset": 0,
        "qualityToggle": False,
        "sm": False,
        "sm_dyn": False,
    },
}


def _estimate(payload: dict, *, is_opus: bool = True) -> tuple[int, bool]:
    estimator = GenerateCostEstimator()
    inputs = estimator.extract_inputs(payload)
    return estimator.calculate(inputs, is_opus=is_opus)


def test_free_small_only_allows_definitely_free_small_generation():
    estimated_cost, is_free_small_allowed = _estimate(FREE_SMALL_PAYLOAD)

    assert estimated_cost == 0
    assert is_free_small_allowed is True


def test_free_small_only_rejects_large_generation():
    payload = FREE_SMALL_PAYLOAD | {
        "parameters": FREE_SMALL_PAYLOAD["parameters"] | {
            "width": 1216,
            "height": 1216,
        }
    }

    estimated_cost, is_free_small_allowed = _estimate(payload)

    assert estimated_cost > 0
    assert is_free_small_allowed is False


def test_free_small_only_rejects_img2img_and_inpaint():
    img2img_payload = FREE_SMALL_PAYLOAD | {
        "parameters": FREE_SMALL_PAYLOAD["parameters"] | {
            "image": "base64-image",
            "strength": 0.5,
        }
    }
    inpaint_payload = FREE_SMALL_PAYLOAD | {
        "model": "nai-diffusion-3-inpainting",
        "parameters": FREE_SMALL_PAYLOAD["parameters"] | {
            "mask": "base64-mask",
        },
    }

    assert _estimate(img2img_payload)[1] is False
    assert _estimate(inpaint_payload)[1] is False


def test_reference_cost_calculator_charges_precise_and_extra_vibe_references():
    cost = ReferenceCostCalculator().calculate(
        {
            "director_reference_images": ["precise-a", "", "precise-b"],
            "reference_image_multiple": ["v1", "v2", "v3", "v4", "v5"],
        }
    )

    assert cost == 12


def test_free_small_only_rejects_reference_requests_even_when_transport_fields_are_known():
    payload = FREE_SMALL_PAYLOAD | {
        "parameters": FREE_SMALL_PAYLOAD["parameters"] | {
            "reference_image_multiple": ["v1"],
            "reference_strength_multiple": [0.5],
            "reference_information_extracted_multiple": [1],
        }
    }

    estimated_cost, is_free_small_allowed = _estimate(payload)

    assert estimated_cost == 0
    assert is_free_small_allowed is False


def test_free_small_only_rejects_unknown_sampler_even_if_cost_is_zero():
    payload = FREE_SMALL_PAYLOAD | {
        "parameters": FREE_SMALL_PAYLOAD["parameters"] | {
            "sampler": "future_sampler",
        }
    }

    estimated_cost, is_free_small_allowed = _estimate(payload)

    assert estimated_cost == 0
    assert is_free_small_allowed is False


def test_free_small_only_rejects_unknown_parameters_without_rejecting_cost_inputs():
    payload = FREE_SMALL_PAYLOAD | {
        "parameters": FREE_SMALL_PAYLOAD["parameters"] | {
            "future_official_parameter": {"kept": True},
        }
    }
    estimator = GenerateCostEstimator()

    inputs = estimator.extract_inputs(payload)
    estimated_cost, is_free_small_allowed = estimator.calculate(inputs, is_opus=True)

    assert inputs.free_small_only_parameters_safe is False
    assert estimated_cost == 0
    assert is_free_small_allowed is False
    assert payload["parameters"]["future_official_parameter"] == {"kept": True}


def test_free_small_only_parameter_policy_allows_empty_forbidden_transport_fields():
    policy = FreeSmallOnlyPolicy()

    assert policy.parameters_are_safe(FREE_SMALL_PAYLOAD["parameters"] | {"reference_image_multiple_cached": []}) is True
    assert policy.parameters_are_safe(FREE_SMALL_PAYLOAD["parameters"] | {"reference_image_multiple_cached": ["cache-id"]}) is False

