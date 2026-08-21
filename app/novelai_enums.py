from enum import Enum


class Sampler(Enum):
    PLMS = "plms"
    DDIM = "ddim"
    K_EULER = "k_euler"
    K_EULER_ANCESTRAL = "k_euler_ancestral"
    K_DPM_2 = "k_dpm_2"
    K_DPM_2_ANCESTRAL = "k_dpm_2_ancestral"
    K_LMS = "k_lms"
    K_DPMPP_2S_ANCESTRAL = "k_dpmpp_2s_ancestral"
    K_DPMPP_SDE = "k_dpmpp_sde"
    K_DPMPP_2M = "k_dpmpp_2m"
    K_DPM_ADAPTIVE = "k_dpm_adaptive"
    K_DPM_FAST = "k_dpm_fast"
    K_DPMPP_2M_SDE = "k_dpmpp_2m_sde"
    K_DPMPP_3M_SDE = "k_dpmpp_3m_sde"
    DDIM_V3 = "ddim_v3"
    NAI_SMEA = "nai_smea"
    NAI_SMEA_DYN = "nai_smea_dyn"


class Action(Enum):
    GENERATE = "generate"
    """Generate Image"""
    IMG2IMG = "img2img"
    """Image to Image"""
    INFILL = "infill"
    """Inpainting"""


# 警告：此枚举是 free_small_only 策略的硬性白名单，新增模型前必须先同步 anlas_sync 计费数据并人工确认免费规则，否则不要加入。
class Model(Enum):
    """
    Nai Diffusion series
    """

    NAI_DIFFUSION_4_5_FULL = "nai-diffusion-4-5-full"
    NAI_DIFFUSION_4_5_FULL_INPAINTING = "nai-diffusion-4-5-full-inpainting"

    NAI_DIFFUSION_4_5_CURATED = "nai-diffusion-4-5-curated"
    NAI_DIFFUSION_4_5_CURATED_INPAINTING = "nai-diffusion-4-5-curated-inpainting"

    NAI_DIFFUSION_4_CURATED_PREVIEW = "nai-diffusion-4-curated-preview"
    NAI_DIFFUSION_4_FULL = "nai-diffusion-4-full"
    NAI_DIFFUSION_4_FULL_INPAINTING = "nai-diffusion-4-full-inpainting"
    NAI_DIFFUSION_4_CURATED_INPAINTING = "nai-diffusion-4-curated-inpainting"

    NAI_DIFFUSION_3 = "nai-diffusion-3"
    NAI_DIFFUSION_3_INPAINTING = "nai-diffusion-3-inpainting"
    NAI_DIFFUSION_FURRY_3 = "nai-diffusion-furry-3"
    NAI_DIFFUSION_FURRY_3_INPAINTING = "nai-diffusion-furry-3-inpainting"
    NAI_DIFFUSION = "nai-diffusion"
    NAI_DIFFUSION_2 = "nai-diffusion-2"
    NAI_DIFFUSION_INPAINTING = "nai-diffusion-inpainting"

    SAFE_DIFFUSION = "safe-diffusion"
    SAFE_DIFFUSION_INPAINTING = "safe-diffusion-inpainting"

    NAI_DIFFUSION_FURRY = "nai-diffusion-furry"

    FURRY_DIFFUSION_INPAINTING = "furry-diffusion-inpainting"

    CUSTOM = "custom"
    STABLE_DIFFUSION = "stable-diffusion"
    WAIFU_DIFFUSION = "waifu-diffusion"
    CURATED_DIFFUSION_TEST = "curated-diffusion-test"
    NAI_DIFFUSION_XL = "nai-diffusion-xl"
    DALLE_MINI = "dalle-mini"
