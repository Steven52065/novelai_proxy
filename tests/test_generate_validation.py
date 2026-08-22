from __future__ import annotations

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
    assert "nai-diffusion-3" in message
    assert "k_euler" in message  # 允许列表里有采样器


def test_exception_samplers_pass_for_all_families():
    for sampler in ("plms", "k_lms", "k_dpm_2_ancestral", "k_dpm_adaptive", "nai_smea", "nai_smea_dyn"):
        for model in ("nai-diffusion", "nai-diffusion-2", "nai-diffusion-3", "nai-diffusion-4-5-full", "nai-diffusion-5-full"):
            assert validate_generate_parameters(model, "generate", _params(sampler=sampler)) == [], (model, sampler)


def test_family_sampler_ok_but_other_family_sampler_rejected():
    # k_dpm_2 在 stableDiffusion 家族，但不在 stableDiffusionXL 家族
    assert validate_generate_parameters("nai-diffusion", "generate", _params(sampler="k_dpm_2")) == []
    errors = validate_generate_parameters("nai-diffusion-3", "generate", _params(sampler="k_dpm_2"))
    assert len(errors) == 1
    assert "k_dpm_2" in errors[0]


def test_invalid_noise_schedule_value_rejected():
    errors = validate_generate_parameters("nai-diffusion-3", "generate", _params(noise_schedule="future_schedule"))

    assert len(errors) == 1
    assert "noise_schedule" in errors[0]
    assert "future_schedule" in errors[0]
    assert "native" in errors[0]


def test_noise_schedule_unsupported_model_rejected():
    errors = validate_generate_parameters("nai-diffusion", "generate", _params(noise_schedule="karras"))

    assert len(errors) == 1
    assert "noise_schedule" in errors[0]
    assert "nai-diffusion" in errors[0]


def test_noise_schedule_intersection_not_contained_rejected():
    # ddim_v3 在 SDXL 家族采样器列表里（sampler 校验通过），但 A(ddim_v3) 允许列表为空，
    # model_allowed ∩ sampler_allowed 为空 → 交集分支报错。
    errors = validate_generate_parameters(
        "nai-diffusion-3",
        "generate",
        _params(sampler="ddim_v3", noise_schedule="karras"),
    )

    assert len(errors) == 1
    assert "noise_schedule" in errors[0]
    assert "karras" in errors[0]
    assert "ddim_v3" in errors[0]
    assert "允许的值：无" in errors[0]


def test_noise_schedule_intersection_ok_when_sampler_allows():
    assert validate_generate_parameters(
        "nai-diffusion-3",
        "generate",
        _params(sampler="k_euler_ancestral", noise_schedule="karras"),
    ) == []


def test_v5_model_rejects_any_noise_schedule():
    # v5 模型前端 g 返回空列表，任何噪点表值都不允许
    errors = validate_generate_parameters("nai-diffusion-5-full", "generate", _params(noise_schedule="karras"))
    assert len(errors) == 1
    assert "noise_schedule" in errors[0]
    assert "nai-diffusion-5-full" in errors[0]


def test_noise_schedule_without_sampler_uses_model_allowed():
    assert validate_generate_parameters(
        "nai-diffusion-3",
        "generate",
        _params(sampler=None, noise_schedule="native"),
    ) == []


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
