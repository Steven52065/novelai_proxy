from __future__ import annotations

import pytest

from anlas_sync import anlas_pricing
from app.novelai_enums import Sampler
from app.policies.generate_validation import (
    STRICT_SAMPLER_FAMILIES,
    V4_V5_EXTRA_SAMPLERS,
    validate_generate_parameters,
)


def _params(**overrides) -> dict:
    params = {
        "width": 832,
        "height": 1216,
        "scale": 5.0,
        "sampler": "k_euler_ancestral",
        "steps": 23,
        "n_samples": 1,
        "sm": False,
        "sm_dyn": False,
    }
    params.update(overrides)
    return params


def test_valid_sampler_passes():
    assert validate_generate_parameters("nai-diffusion-3", "generate", _params()) == []


def test_valid_noise_schedule_passes():
    assert validate_generate_parameters(
        "nai-diffusion-3",
        "generate",
        _params(noise_schedule="karras"),
    ) == []


def test_unknown_sampler_rejected_with_chinese_message():
    errors = validate_generate_parameters("nai-diffusion-3", "generate", _params(sampler="future_sampler"))

    assert len(errors) == 1
    message = errors[0]
    assert "sampler" in message
    assert "future_sampler" in message
    assert "k_euler" in message  # 允许列表里有采样器


@pytest.mark.parametrize("bad_sampler", [["k_euler"], {"a": 1}, 123, 4.5])
def test_non_string_sampler_rejected_instead_of_crashing(bad_sampler):
    """回归：非字符串 sampler 以前在集合成员判断里抛 TypeError，导致 500。"""
    errors = validate_generate_parameters("nai-diffusion-3", "generate", _params(sampler=bad_sampler))

    assert len(errors) == 1
    assert "sampler" in errors[0]


def test_every_enum_sampler_passes_for_non_strict_families():
    """非 v4/v5 家族只做枚举判断，不按家族收窄（实测 v3 菜单外组合可正常出图）。"""
    for model in anlas_pricing.DATA["model_family"]:
        if anlas_pricing.model_family(model) in STRICT_SAMPLER_FAMILIES:
            continue
        for sampler in (s.value for s in Sampler):
            assert validate_generate_parameters(model, "generate", _params(sampler=sampler)) == [], (
                model,
                sampler,
            )


def test_v4_v5_families_accept_family_table_plus_extra_samplers():
    """v4/v5 家族按真实上游实测收窄：family_samplers + V4_V5_EXTRA_SAMPLERS 放行，其余 400。"""
    for model in anlas_pricing.DATA["model_family"]:
        family = anlas_pricing.model_family(model)
        if family not in STRICT_SAMPLER_FAMILIES:
            continue
        allowed = set(anlas_pricing.samplers_for_model(model)) | V4_V5_EXTRA_SAMPLERS
        for sampler in (s.value for s in Sampler):
            errors = validate_generate_parameters(model, "generate", _params(sampler=sampler))
            if sampler in allowed:
                assert errors == [], (model, sampler)
            else:
                assert len(errors) == 1, (model, sampler)
                assert sampler in errors[0]
                assert model in errors[0]
                assert "不受支持" in errors[0]


def test_v4_5_full_rejects_ddim_before_upstream():
    """回归：4.5 full + ddim 不在 v4 家族表，必须 400，不再转发上游。"""
    errors = validate_generate_parameters(
        "nai-diffusion-4-5-full",
        "generate",
        _params(sampler="ddim"),
    )
    assert len(errors) == 1
    message = errors[0]
    assert "ddim" in message
    assert "nai-diffusion-4-5-full" in message
    assert "k_euler_ancestral" in message  # 允许列表里有家族采样器


def test_v4_5_full_accepts_real_upstream_extra_samplers():
    """实测上游接受但不在 v4 家族表内的遗留采样器必须放行。"""
    for sampler in sorted(V4_V5_EXTRA_SAMPLERS):
        assert validate_generate_parameters(
            "nai-diffusion-4-5-full",
            "generate",
            _params(sampler=sampler),
        ) == [], sampler


def test_invalid_noise_schedule_value_rejected():
    errors = validate_generate_parameters("nai-diffusion-3", "generate", _params(noise_schedule="future_schedule"))

    assert len(errors) == 1
    assert "noise_schedule" in errors[0]
    assert "future_schedule" in errors[0]
    assert "native" in errors[0]


@pytest.mark.parametrize(
    ("width", "height"),
    [(64, 64), (512, 768), (832, 1216), (1024, 1024), (1216, 832), (640, 640)],
)
def test_dimensions_that_are_multiples_of_64_pass(width, height):
    assert validate_generate_parameters(
        "nai-diffusion-3", "generate", _params(width=width, height=height)
    ) == []


@pytest.mark.parametrize(
    ("key", "value", "lower", "upper"),
    [
        ("width", 786, 768, 832),
        ("height", 1000, 960, 1024),
        ("width", 513, 512, 576),
        ("height", 1215, 1152, 1216),
    ],
)
def test_dimension_not_multiple_of_64_rejected_with_chinese_message(key, value, lower, upper):
    errors = validate_generate_parameters("nai-diffusion-3", "generate", _params(**{key: value}))

    assert len(errors) == 1
    message = errors[0]
    assert key in message
    assert str(value) in message
    assert "64 的整数倍" in message
    assert f"{lower} 或 {upper}" in message


def test_both_dimensions_invalid_reported_separately():
    """失败2.txt 的真实场景：786x786 两个维度都不合法。"""
    errors = validate_generate_parameters("nai-diffusion-3", "generate", _params(width=786, height=786))

    assert len(errors) == 2
    assert "width" in errors[0]
    assert "height" in errors[1]


def test_dimension_below_64_does_not_suggest_zero():
    """计费层要求宽高 >= 64，建议值里不能出现 0。"""
    errors = validate_generate_parameters("nai-diffusion-3", "generate", _params(width=63))

    assert len(errors) == 1
    assert "最接近的合法值是 64" in errors[0]
    assert "0 或" not in errors[0]


@pytest.mark.parametrize("value", [None, True, ["786"], "abc", {}])
def test_non_numeric_dimension_left_to_costing_layer(value):
    """取不出整数的宽高交给计费层的 400 无效的请求，这里不重复报错。"""
    assert validate_generate_parameters("nai-diffusion-3", "generate", _params(width=value)) == []


@pytest.mark.parametrize("value", [0, -64])
def test_non_positive_dimension_left_to_costing_layer(value):
    """0 与负数由计费层的 minimum=64 拦下，这里不报 64 倍数错误。"""
    assert validate_generate_parameters("nai-diffusion-3", "generate", _params(width=value)) == []


def test_dimension_and_sampler_errors_reported_together():
    errors = validate_generate_parameters(
        "nai-diffusion-3",
        "generate",
        _params(width=786, sampler="future_sampler"),
    )

    assert len(errors) == 2
    assert "width" in errors[0]
    assert "sampler" in errors[1]


@pytest.mark.parametrize(
    ("model", "sampler", "noise_schedule"),
    [
        # 以下组合均已对真实上游验证：返回 200 与正常图片 zip。
        ("nai-diffusion-5-full", "k_euler", "karras"),          # 模块 53856 PE 说 v5 不支持噪点表
        ("nai-diffusion-5-full", "k_euler", "native"),
        ("nai-diffusion-4-5-full", "k_euler", "native"),        # Tz 对 v4 剔除了 native
        ("nai-diffusion-3", "ddim_v3", "native"),               # Ux(ddim_v3) 为空
        ("nai-diffusion-3", "plms", "karras"),
    ],
)
def test_model_and_sampler_combinations_are_not_validated(model, sampler, noise_schedule):
    """噪点表只校验取值合法性，不校验与模型/采样器是否匹配。"""
    assert validate_generate_parameters(
        model,
        "generate",
        _params(sampler=sampler, noise_schedule=noise_schedule),
    ) == []


def test_noise_schedule_without_sampler_passes():
    assert validate_generate_parameters(
        "nai-diffusion-3",
        "generate",
        _params(sampler=None, noise_schedule="native"),
    ) == []


def test_sampler_and_noise_schedule_errors_reported_together():
    errors = validate_generate_parameters(
        "nai-diffusion-3",
        "generate",
        _params(sampler="future_sampler", noise_schedule="future_schedule"),
    )

    assert len(errors) == 2
    assert "sampler" in errors[0]
    assert "noise_schedule" in errors[1]


def test_action_not_generate_skips_validation():
    assert validate_generate_parameters(
        "nai-diffusion-3",
        "img2img",
        _params(sampler="future_sampler", noise_schedule="future_schedule"),
    ) == []
    assert validate_generate_parameters(
        "nai-diffusion-3",
        "infill",
        _params(sampler="future_sampler"),
    ) == []


def test_unknown_model_skips_validation():
    assert validate_generate_parameters(
        "future-official-model",
        "generate",
        _params(sampler="future_sampler", noise_schedule="future_schedule"),
    ) == []


def test_non_dict_parameters_skipped():
    assert validate_generate_parameters("nai-diffusion-3", "generate", None) == []
    assert validate_generate_parameters("nai-diffusion-3", "generate", "not-a-dict") == []
