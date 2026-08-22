from __future__ import annotations

from typing import Any

from anlas_sync import anlas_pricing

from ..novelai_enums import Sampler


def validate_generate_parameters(model: Any, action: Any, parameters: Any) -> list[str]:
    """校验 /ai/generate-image 入口的 sampler / noise_schedule，返回中文错误列表。

    只拦取值本身就非法的参数：枚举外的采样器、噪点表枚举外的值。不校验
    "采样器/噪点表是否与该模型匹配"——模块 32036 的 DF 家族采样器表与模块
    53856 的 Tz/Ux/PE 噪点表都是前端下拉菜单的填充数据（DF 返回的结构带
    Recommended/Other 分组和显示名），不是请求校验规则；前端真正的参数校验
    是模块 57863 的 Dk，只管尺寸与步数。实测上游会正常出图的菜单外组合包括
    v3+k_dpm_2、v3+k_dpmpp_3m_sde、v3 ddim_v3+native、v4.5+native、v5+karras，
    按菜单表拦截会把这些全部误杀。

    仅对 action == "generate" 启用；img2img / infill 以及 upscale / augment /
    encode-vibe 不适用。模型不在同步计费数据里时跳过，维持现有计费层英文拦截，
    避免重复报错。
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

    sampler = parameters.get("sampler")
    if sampler is not None and not _is_known_sampler(sampler):
        errors.append(
            f"参数 sampler 的值 {sampler!r} 不是已知采样器；"
            f"允许的采样器：{_join(sorted(s.value for s in Sampler))}"
        )

    noise_schedule = parameters.get("noise_schedule")
    if noise_schedule is not None:
        values = anlas_pricing.noise_schedule_values()
        if noise_schedule not in values:
            errors.append(
                f"参数 noise_schedule 的值 {noise_schedule!r} 不合法；"
                f"允许的值：{_join(values)}"
            )

    return errors


def _is_known_sampler(value: Any) -> bool:
    """枚举成员判断。Enum 查找对不可哈希值也只抛 ValueError，不会崩。"""
    try:
        Sampler(value)
    except ValueError:
        return False
    return True


def _join(values: Any) -> str:
    if not values:
        return "无"
    return "、".join(str(v) for v in values)
