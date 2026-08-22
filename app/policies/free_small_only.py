from __future__ import annotations

from typing import Any

from ..novelai_enums import Action, Model


FREE_SMALL_ONLY_ALLOWED_PARAMETERS = {
    "width",
    "height",
    "scale",
    "sampler",
    "steps",
    "n_samples",
    "ucPreset",
    "qualityToggle",
    "sm",
    "sm_dyn",
    "seed",
    "negative_prompt",
    "noise_schedule",
    "cfg_rescale",
    "dynamic_thresholding",
    "controlnet_strength",
    "legacy",
    "legacy_v3_extend",
    "uncond_scale",
    "deliberate_euler_ancestral_bug",
    "prefer_brownian",
    "image_format",
    "inpaintImg2ImgStrength",
    "normalize_reference_strength_multiple",
    "skip_cfg_above_sigma",
    "stream",
    "characterPrompts",
    "v4_prompt",
    "v4_negative_prompt",
    "use_coords",
    "legacy_uc",
    "add_original_image",
    "autoSmea",
    "params_version",
}

FREE_SMALL_ONLY_FORBIDDEN_PARAMETERS = {
    "image",
    "mask",
    "strength",
    "noise",
    "extra_noise_seed",
    "reference_image",
    "reference_image_multiple",
    "reference_image_multiple_cached",
    "reference_strength_multiple",
    "reference_information_extracted_multiple",
    "director_reference_images",
    "director_reference_descriptions",
    "director_reference_strength_values",
    "director_reference_secondary_strength_values",
    "director_reference_information_extracted",
    "controlnet_condition",
    "controlnet_model",
}


class FreeSmallOnlyPolicy:
    """Conservative allow-list for requests that are definitely free on Opus."""

    def parameters_are_safe(self, parameters: dict[str, Any]) -> bool:
        unknown_keys = set(parameters) - FREE_SMALL_ONLY_ALLOWED_PARAMETERS - FREE_SMALL_ONLY_FORBIDDEN_PARAMETERS
        if unknown_keys:
            return False
        return not any(_parameter_has_value(parameters.get(key)) for key in FREE_SMALL_ONLY_FORBIDDEN_PARAMETERS)

    def parameter_violations(self, parameters: dict[str, Any]) -> list[str]:
        """逐个指出导致 parameters_are_safe=False 的参数（中文、含键名）。

        未知参数键与“带值”的禁用参数键各返回一条；空列表表示参数级检查通过。
        """
        reasons: list[str] = []
        unknown_keys = sorted(
            set(parameters) - FREE_SMALL_ONLY_ALLOWED_PARAMETERS - FREE_SMALL_ONLY_FORBIDDEN_PARAMETERS
        )
        reasons.extend(f"存在未知参数：{key}" for key in unknown_keys)
        forbidden_with_value = sorted(
            key for key in FREE_SMALL_ONLY_FORBIDDEN_PARAMETERS
            if _parameter_has_value(parameters.get(key))
        )
        reasons.extend(f"参数 {key} 不允许用于免费小图" for key in forbidden_with_value)
        return reasons

    def is_allowed(
        self,
        *,
        is_opus: bool,
        model: str,
        action: str,
        width: int,
        height: int,
        steps: int,
        n_samples: int,
        sampler_was_known: bool,
        has_image: bool,
        base_cost: int,
        reference_cost: int,
        parameters_are_safe: bool,
    ) -> bool:
        total_cost = base_cost + reference_cost
        return (
            is_opus
            and total_cost == 0
            and base_cost == 0
            and reference_cost == 0
            and self.is_known_text_to_image_model(model)
            and action == Action.GENERATE.value
            and parameters_are_safe
            and n_samples == 1
            and steps <= 28
            and width * height <= 1048576
            and sampler_was_known
            and not has_image
        )

    def violations(
        self,
        *,
        is_opus: bool,
        model: str,
        action: str,
        width: int,
        height: int,
        steps: int,
        n_samples: int,
        sampler_was_known: bool,
        has_image: bool,
        base_cost: int,
        reference_cost: int,
        parameters_are_safe: bool,
    ) -> list[str]:
        """按序返回导致 is_allowed=False 的中文失败原因（不含参数键级细节）。

        参数键级原因（未知键/禁用键）由 parameter_violations(parameters) 返回，
        调用方（GenerateCostEstimator）先拼参数级、再拼本方法的未知采样器与边界条件。
        参数与 is_allowed 完全一致；放行/拦截判定本身不变。
        """
        reasons: list[str] = []
        if not sampler_was_known:
            reasons.append("采样器未知，无法确认是否免费")
        if not is_opus:
            reasons.append("账户不是 Opus 订阅，无法确认免费小图")
        if steps > 28:
            reasons.append(f"步骤数超过免费上限（steps={steps}，上限 28）")
        if width * height > 1048576:
            reasons.append(f"图片像素超过免费上限（{width}x{height}={width * height}，上限 1048576）")
        if n_samples > 1:
            reasons.append(f"生成数量超过 1（n_samples={n_samples}）")
        if has_image:
            reasons.append("请求包含图片，不属于免费文生图")
        if not self.is_known_text_to_image_model(model):
            reasons.append(f"模型不在免费白名单内（{model}）")
        if action != "generate":
            reasons.append(f"动作不是 generate（{action}）")
        if base_cost != 0:
            reasons.append(f"基础费用不为 0（base_cost={base_cost}）")
        if reference_cost != 0:
            reasons.append(f"参考图费用不为 0（reference_cost={reference_cost}）")
        return reasons

    # 警告：不要放松此枚举白名单；只加枚举不同步 anlas_sync 计费数据，会让 free_small_only 用户绕过免费限制。
    def is_known_text_to_image_model(self, model: str) -> bool:
        try:
            parsed = Model(model)
        except ValueError:
            return False
        return parsed not in {
            Model.NAI_DIFFUSION_4_5_FULL_INPAINTING,
            Model.NAI_DIFFUSION_4_5_CURATED_INPAINTING,
            Model.NAI_DIFFUSION_4_FULL_INPAINTING,
            Model.NAI_DIFFUSION_4_CURATED_INPAINTING,
            Model.NAI_DIFFUSION_3_INPAINTING,
            Model.NAI_DIFFUSION_FURRY_3_INPAINTING,
            Model.NAI_DIFFUSION_INPAINTING,
            Model.SAFE_DIFFUSION_INPAINTING,
            Model.FURRY_DIFFUSION_INPAINTING,
        }


def _parameter_has_value(value: Any) -> bool:
    if value is None:
        return False
    if value is False:
        return False
    if isinstance(value, (str, bytes, list, dict, tuple, set)):
        return len(value) > 0
    return True
