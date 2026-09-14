"""Result contract shared by the average estimator and its independent evaluator."""

from dataclasses import dataclass, field


@dataclass
class ConsumptionInterval:
    start_at: float
    end_at: float
    included: bool
    reason: str
    change_g: float = 0.0
    reason_label: str = "Counted"


@dataclass
class AverageConsumptionResult:
    """Estimator output: rate is authoritative (negative for consumption).

    Time, grams, and intervals explain the estimate; evaluators must not use
    them to reconstruct rate, since another estimator may derive it differently.
    """

    counted_seconds: float = 0.0
    change_g: float = 0.0
    intervals: list[ConsumptionInterval] = field(default_factory=list)
    rate: float | None = None
    filtered_samples: int = 0
