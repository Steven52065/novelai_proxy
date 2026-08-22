from __future__ import annotations

from typing import Any

from anlas_sync import anlas_pricing


# module 32036 家族-采样器表未列出、但枚举/计费层支持的遗留采样器，对所有家族放行。
# 警告：这是显式硬编码的例外；新增枚举成员不会自动加入，需人工确认后再补充。
KNOWN_SAMPLER_EXCEPTIONS = frozenset({
    "plms",
    "k_lms",
    "k_dpm_2_ancestral",
    "k_dpm_adaptive",
    "nai_smea",
    "nai_smea_dyn",
})


def validate_generate_parameters(model: Any, action: Any, parameters: Any) -> list[str]:
    """校验 /ai/generate-image 入口的 sampler / noise_schedule，返回中文错误列表。

    仅对 action == "generate" 的文生图启用；img2img / infill 以及
    upscale / augment / encode-vibe 不适用，直接跳过（相关调用方也不要为它们调用）。
    模型不在同步计费数据里时跳过，维持现有计费层英文拦截，避免重复报错。
    """
    # 仅文生图 generate 启用本校验；img2img / infill 不适用。
    if action != "generate":
        return []
    # 未知模型维持现有计费层英文拦截（行为不变），这里不重复报错。
    if not isinstance(model, str) or model not in anlas_pricing.DATA["model_family"]:
        return []
    if not isinstance(parameters, dict):
        return []

    errors: list[str] = []

    allowed_samplers = set(anlas_pricing.samplers_for_model(model)) | KNOWN_SAMPLER_EXCEPTIONS
    sampler = parameters.get("sampler")
    if sampler is not None and sampler not in allowed_samplers:
        errors.append(
            f"参数 sampler 的值 {sampler!r} 对模型 {model} 不受支持；"
            f"允许的采样器：{_join(sorted(allowed_samplers))}"
        )

    noise_schedule = parameters.get("noise_schedule")
    if noise_schedule is not None:
        values = anlas_pricing.noise_schedule_values()
        if noise_schedule not in values:
            errors.append(
                f"参数 noise_schedule 的值 {noise_schedule!r} 对模型 {model} 不合法；"
                f"允许的值：{_join(values)}"
            )
        elif not anlas_pricing.model_supports_noise_schedule(model):
            errors.append(
                f"参数 noise_schedule 的值 {noise_schedule!r} 对模型 {model} 不受支持；"
                f"允许的值：无"
            )
        else:
            model_allowed = set(anlas_pricing.noise_schedule_for_model(model))
            if sampler is not None:
                sampler_allowed = set(anlas_pricing.noise_schedule_for_sampler(sampler))
                allowed = model_allowed & sampler_allowed
            else:
                allowed = model_allowed
            if noise_schedule not in allowed:
                context = f"对模型 {model} 与采样器 {sampler!r}" if sampler is not None else f"对模型 {model}"
                errors.append(
                    f"参数 noise_schedule 的值 {noise_schedule!r} {context} 不受支持；"
                    f"允许的值：{_join(sorted(allowed))}"
                )

    return errors


def _join(values: Any) -> str:
    if not values:
        return "无"
    return "、".join(str(v) for v in values)
