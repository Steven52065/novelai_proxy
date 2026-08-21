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
