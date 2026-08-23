from __future__ import annotations

from typing import Any

from anlas_sync import anlas_pricing

from ..novelai_enums import Sampler


DIMENSION_STEP = 64

# v4 / v5 家族按真实上游实测结果收窄采样器（512x512 / 10 步 / 参考请求体，
# v4.5 full 与 curated 结果一致）：接受家族表 6 个 + 下面 3 个遗留采样器，
# 其余枚举成员（plms / ddim / k_dpm_adaptive / k_dpm_fast / k_dpmpp_3m_sde /
# ddim_v3 / nai_smea / nai_smea_dyn）上游全部 500 拒绝。
# 其余家族（stableDiffusion / SDXL 等）上游对菜单外采样器更宽容（实测
# v3 + k_dpm_2 / ddim / k_dpmpp_3m_sde 等可正常出图），因此只做枚举校验。
STRICT_SAMPLER_FAMILIES = frozenset({"v4", "v5"})
V4_V5_EXTRA_SAMPLERS = frozenset({"k_dpm_2", "k_dpm_2_ancestral", "k_lms"})


def validate_generate_parameters(model: Any, action: Any, parameters: Any) -> list[str]:
    """校验 /ai/generate-image 入口的分辨率 / sampler / noise_schedule，返回中文错误列表。

    校验规则：宽高必须是 64 的整数倍；采样器必须在 Sampler 枚举内，且对
    v4 / v5 家族还要在 family_samplers 表 + V4_V5_EXTRA_SAMPLERS 内
    （实测 4.5 full + ddim 会被上游拒绝，而 k_dpm_2 / k_lms 虽不在表内
    但上游接受）；噪点表只校验取值合法性。
    其余家族（stableDiffusion / SDXL 等）不按菜单表收窄采样器——模块 32036 的
    DF 家族采样器表是前端下拉菜单填充数据，不是请求校验规则，实测 v3 + k_dpm_2 /
    ddim / k_dpmpp_3m_sde 等菜单外组合上游可正常出图；噪点表与模型/采样器的匹配
    关系也不校验（实测 v4.5+native、v5+karras 可正常出图）。

    仅对 action == "generate" 启用；img2img / infill 以及 upscale / augment /
    encode-vibe 不适用。模型不在同步计费数据里时跳过，维持现有计费层拦截并返回中文提示，
    避免重复报错。
    """
    # 仅文生图 generate 启用本校验；img2img / infill 不适用。
    if action != "generate":
        return []
    # 未知模型维持现有计费层拦截（返回中文提示），这里不重复报错。
    if not isinstance(model, str) or model not in anlas_pricing.DATA["model_family"]:
        return []
    if not isinstance(parameters, dict):
        return []

    errors: list[str] = []

    # 上游要求宽高都是 64 的整数倍，否则直接拒绝生成（例如 786x786）。
    for key in ("width", "height"):
        size = _dimension(parameters.get(key))
        if size is not None and size > 0 and size % DIMENSION_STEP != 0:
            lower = size // DIMENSION_STEP * DIMENSION_STEP
            # 计费层要求宽高 >= 64，所以不把 0 当作可选值给用户。
            suggestion = (
                f"{lower} 或 {lower + DIMENSION_STEP}"
                if lower >= DIMENSION_STEP
                else f"{DIMENSION_STEP}"
            )
            errors.append(
                f"参数 {key} 的值 {parameters[key]!r} 不合法："
                f"图片宽高必须是 {DIMENSION_STEP} 的整数倍，"
                f"{size} 除以 {DIMENSION_STEP} 不是整数；"
                f"最接近的合法值是 {suggestion}"
            )

    sampler = parameters.get("sampler")
    if sampler is not None:
        # 允许集合按模型算一次、两条报错共用：拼错采样器时也只推荐该模型真能用的，
        # 否则 v4/v5 会出现"提示里写着 ddim，改成 ddim 又被拒"的前后矛盾。
        allowed = _allowed_samplers(model)
        if not _is_known_sampler(sampler):
            errors.append(
                f"参数 sampler 的值 {sampler!r} 不是已知采样器；"
                f"允许的采样器：{_join(sorted(allowed))}"
            )
        elif sampler not in allowed:
            errors.append(
                f"参数 sampler 的值 {sampler!r} 对模型 {model} 不受支持；"
                f"允许的采样器：{_join(sorted(allowed))}"
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


def _dimension(value: Any) -> int | None:
    """按计费层 _required_int 的方式取整数；取不出就返回 None，交给计费层统一报错。

    非数字、bool、缺失都返回 None——这些情况计费层会给出 400 无效的请求，
    这里不重复报错。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _allowed_samplers(model: str) -> set[str]:
    """该模型实际可用的采样器集合。

    v4 / v5 家族按真实上游实测收窄为 family_samplers + V4_V5_EXTRA_SAMPLERS
    （其余枚举采样器如 4.5 full + ddim 上游会 500 拒绝）；其余家族维持整个
    Sampler 枚举——菜单表不是校验规则，实测 v3 + k_dpm_2 等菜单外组合可正常出图。
    """
    if anlas_pricing.model_family(model) in STRICT_SAMPLER_FAMILIES:
        return set(anlas_pricing.samplers_for_model(model)) | V4_V5_EXTRA_SAMPLERS
    return {s.value for s in Sampler}


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
