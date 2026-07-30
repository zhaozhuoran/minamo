"""HSM Heat Calculation Engine.

Defines the pluggable BaseHeatCalculator and the default ExponentialDecayHeatCalculator,
which dynamically calculates object heat by applying exponential time-decay
to individual historical access logs and the object's age.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import List, Tuple


class BaseHeatCalculator(ABC):
    @abstractmethod
    def calculate_heat(
        self,
        last_modified: datetime,
        access_logs: List[Tuple[str, datetime]],
        now: datetime,
    ) -> float:
        """Calculate and return the dynamic heat score of an object."""
        pass


class ExponentialDecayHeatCalculator(BaseHeatCalculator):
    def __init__(
        self,
        base_score: float = 100.0,
        read_weight: float = 10.0,
        write_weight: float = 5.0,
        decay_rate_per_hour: float = 0.01,  # e.g., 1% decay per hour
    ) -> None:
        self.base_score = base_score
        self.read_weight = read_weight
        self.write_weight = write_weight
        self.decay_rate_per_hour = decay_rate_per_hour

    def calculate_heat(
        self,
        last_modified: datetime,
        access_logs: List[Tuple[str, datetime]],
        now: datetime,
    ) -> float:
        # Age of object since last modification
        age_hours = max(0.0, (now - last_modified).total_seconds() / 3600.0)
        # Base heat from recency: base_score * e^(-decay_rate * age)
        heat = self.base_score * math.exp(-self.decay_rate_per_hour * age_hours)

        # Heat from access history
        for event_type, timestamp in access_logs:
            event_age_hours = max(0.0, (now - timestamp).total_seconds() / 3600.0)
            weight = self.read_weight if event_type == "READ" else self.write_weight
            heat += weight * math.exp(-self.decay_rate_per_hour * event_age_hours)

        return round(heat, 4)
