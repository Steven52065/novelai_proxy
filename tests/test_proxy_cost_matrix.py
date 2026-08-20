from __future__ import annotations

import pytest

from app.costing import (
    GenerateCostEstimator,
    ReferenceCostCalculator,
    ensure_supported_anlas_price,
)
from helpers import PAYLOAD


def _payload_with(**parameter_overrides):
    return PAYLOAD | {"parameters": PAYLOAD["parameters"] | parameter_overrides}


def _estimate(payload: dict, *, is_opus: bool = True) -> tuple[int, bool]:
    estimator = GenerateCostEstimator()
    inputs = estimator.extract_inputs(payload)
    return estimator.calculate(inputs, is_opus=is_opus)


@pytest.mark.parametrize(
    ("name", "payload", "is_opus", "expected_cost", "expected_free_small_allowed"),
    [
        ("opus_small_text2img_is_free", PAYLOAD, True, 0, True),
        ("non_opus_small_text2img_is_paid", PAYLOAD, False, 17, False),
        ("opus_too_many_steps_is_paid", _payload_with(steps=29), True, 20, False),
        ("opus_too_many_samples_is_paid", _payload_with(n_samples=2), True, 17, False),
        ("opus_too_many_pixels_is_paid", _payload_with(width=1216, height=1216), True, 24, False),
    ],
)
def test_generate_cost_matrix_for_free_small_boundaries(
    name: str,
    payload: dict,
    is_opus: bool,
    expected_cost: int,
    expected_free_small_allowed: bool,
):
    estimated_cost, free_small_allowed = _estimate(payload, is_opus=is_opus)

    assert estimated_cost == expected_cost, name
    assert free_small_allowed is expected_free_small_allowed, name


@pytest.mark.parametrize(
    ("parameters", "expected_reference_cost"),
    [
        ({"director_reference_images": ["precise"]}, 5),
        ({"director_reference_images": ["precise", "", None, "precise-2"]}, 10),
        ({"reference_image_multiple": ["v1", "v2", "v3", "v4"]}, 0),
        ({"reference_image_multiple": ["v1", "v2", "v3", "v4", "v5"]}, 2),
        ({"reference_image": "single", "reference_image_multiple": ["v2", "v3", "v4", "v5"]}, 2),
        (
            {
                "director_reference_images": ["precise-a", "precise-b"],
                "reference_image_multiple": ["v1", "v2", "v3", "v4", "v5"],
            },
            12,
        ),
    ],
)
def test_reference_cost_matrix(parameters: dict, expected_reference_cost: int):
    assert ReferenceCostCalculator().calculate(parameters) == expected_reference_cost


@pytest.mark.parametrize(
    "bad_parameters",
    [
        {"width": True},
        {"height": None},
        {"steps": 0},
        {"n_samples": False},
        {"n_samples": "not-an-int"},
    ],
)
def test_generate_cost_inputs_reject_invalid_required_numeric_fields(bad_parameters: dict):
    payload = _payload_with(**bad_parameters)

    with pytest.raises(ValueError):
        GenerateCostEstimator().extract_inputs(payload)


def test_generate_cost_keeps_unknown_official_parameters_but_disallows_free_small_only():
    payload = _payload_with(future_official_parameter={"kept": True})
    estimator = GenerateCostEstimator()

    inputs = estimator.extract_inputs(payload)
    estimated_cost, free_small_allowed = estimator.calculate(inputs, is_opus=True)

    assert inputs.free_small_only_parameters_safe is False
    assert estimated_cost == 0
    assert free_small_allowed is False
    assert payload["parameters"]["future_official_parameter"] == {"kept": True}


def test_anlas_adapter_rejects_frontend_unsupported_prices():
    import math

    with pytest.raises(ValueError):
        ensure_supported_anlas_price(float("nan"))
    with pytest.raises(ValueError):
        ensure_supported_anlas_price(-3)
    assert ensure_supported_anlas_price(17.0) == 17
