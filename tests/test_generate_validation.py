from __future__ import annotations

import pytest

from anlas_sync import anlas_pricing
from app.novelai_enums import Sampler
from app.policies.generate_validation import validate_generate_parameters


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


def test_every_enum_sampler_passes_for_every_known_model():
    """采样器只做枚举成员判断，不按模型家族收窄。

    前端模块 32036 的家族采样器表是下拉菜单数据，不是校验规则：实测上游对
    v3+k_dpm_2、v3+k_dpmpp_3m_sde 等菜单外组合都会正常出图。
    """
    for model in anlas_pricing.DATA["model_family"]:
        for sampler in (s.value for s in Sampler):
            assert validate_generate_parameters(model, "generate", _params(sampler=sampler)) == [], (
                model,
                sampler,
            )


def test_invalid_noise_schedule_value_rejected():
    errors = validate_generate_parameters("nai-diffusion-3", "generate", _params(noise_schedule="future_schedule"))

    assert len(errors) == 1
    assert "noise_schedule" in errors[0]
    assert "future_schedule" in errors[0]
    assert "native" in errors[0]


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
