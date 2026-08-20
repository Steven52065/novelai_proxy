from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from novelai_python.sdk.ai._cost import CostCalculator

from .novelai_enums import Sampler
from .policies.free_small_only import FreeSmallOnlyPolicy


IMAGE_ANLAS_PER_PRECISE_REFERENCE = 5
IMAGE_ANLAS_PER_VIBE_ENCODING = 2
FREE_VIBE_REFERENCES_PER_GENERATION = 4
IMAGE_ANLAS_PER_EXTRA_VIBE_REFERENCE = 2


@dataclass(frozen=True)
class GenerateCostInputs:
    model: str
    action: str
    width: int
    height: int
    steps: int
    n_samples: int
    sampler: Sampler | None
    sampler_was_known: bool
    sm: bool
    sm_dyn: bool
    image: bool
    strength: float | None
    reference_cost: int
    free_small_only_parameters_safe: bool


class ReferenceCostCalculator:
    def calculate(self, parameters: dict[str, Any]) -> int:
        precise_references = self._reference_count(parameters, "director_reference_images")

        vibe_references = self._reference_count(parameters, "reference_image")
        vibe_references += self._reference_count(parameters, "reference_image_multiple")
        extra_vibes = max(vibe_references - FREE_VIBE_REFERENCES_PER_GENERATION, 0)
        return (
            precise_references * IMAGE_ANLAS_PER_PRECISE_REFERENCE
            + extra_vibes * IMAGE_ANLAS_PER_EXTRA_VIBE_REFERENCE
        )

    def _reference_count(self, parameters: dict[str, Any], key: str) -> int:
        value = parameters.get(key)
        if isinstance(value, list):
            return sum(1 for item in value if item)
        if value:
            return 1
        return 0


class GenerateCostEstimator:
    def __init__(
        self,
        *,
        reference_cost_calculator: ReferenceCostCalculator | None = None,
        free_small_only_policy: FreeSmallOnlyPolicy | None = None,
    ):
        self.reference_cost_calculator = reference_cost_calculator or ReferenceCostCalculator()
        self.free_small_only_policy = free_small_only_policy or FreeSmallOnlyPolicy()

    def extract_inputs(self, payload: dict[str, Any]) -> GenerateCostInputs:
        model = payload.get("model")
        action = payload.get("action", "generate")
        parameters = payload.get("parameters")
        if not isinstance(model, str) or not model:
            raise ValueError("model is required")
        if not isinstance(action, str) or not action:
            raise ValueError("action is required")
        if not isinstance(parameters, dict):
            raise ValueError("parameters is required")

        sampler_value = parameters.get("sampler")
        sampler, sampler_was_known = _optional_sampler(sampler_value)
        return GenerateCostInputs(
            model=model,
            action=action,
            width=_required_int(parameters, "width", minimum=64),
            height=_required_int(parameters, "height", minimum=64),
            steps=_required_int(parameters, "steps", minimum=1),
            n_samples=_required_int(parameters, "n_samples", minimum=1),
            sampler=sampler,
            sampler_was_known=sampler_was_known,
            sm=_optional_bool(parameters.get("sm")),
            sm_dyn=_optional_bool(parameters.get("sm_dyn")),
            image=bool(parameters.get("image")),
            strength=_optional_float(parameters.get("strength")),
            reference_cost=self.reference_cost_calculator.calculate(parameters),
            # Unknown NovelAI fields are still proxied, but they cannot be treated as definitely free.
            free_small_only_parameters_safe=self.free_small_only_policy.parameters_are_safe(parameters),
        )

    def calculate(self, params: GenerateCostInputs, *, is_opus: bool) -> tuple[int, bool]:
        base_cost = int(
            CostCalculator.calculate(
                width=params.width,
                height=params.height,
                steps=params.steps,
                model=params.model,
                image=params.image,
                n_samples=params.n_samples,
                account_tier=3 if is_opus else 1,
                strength=params.strength,
                # The SDK cost model validates against its own Enum class.
                # Pass the wire value while the calculator is still present;
                # the anlas_sync migration removes this compatibility edge.
                sampler=params.sampler.value if params.sampler is not None else None,
                is_sm_enabled=params.sm,
                is_sm_dynamic=params.sm_dyn,
                is_account_active=True,
            )
        )
        total_cost = base_cost + params.reference_cost
        is_certainly_free = self.free_small_only_policy.is_allowed(
            is_opus=is_opus,
            model=params.model,
            action=params.action,
            width=params.width,
            height=params.height,
            steps=params.steps,
            n_samples=params.n_samples,
            sampler_was_known=params.sampler_was_known,
            has_image=params.image,
            base_cost=base_cost,
            reference_cost=params.reference_cost,
            parameters_are_safe=params.free_small_only_parameters_safe,
        )
        return total_cost, is_certainly_free


def _required_int(parameters: dict[str, Any], key: str, *, minimum: int) -> int:
    value = parameters.get(key)
    if value is None or isinstance(value, bool):
        raise ValueError(f"parameters.{key} is required")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"parameters.{key} must be an integer") from exc
    if number < minimum:
        raise ValueError(f"parameters.{key} must be >= {minimum}")
    return number


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _optional_sampler(value: Any) -> tuple[Sampler | None, bool]:
    if value is None:
        return None, False
    try:
        return Sampler(value), True
    except ValueError:
        return None, False
