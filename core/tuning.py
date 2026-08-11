"""Shared statistics for the weight tuners (0DTE forecast + swing).

Both tuners map graded per-indicator accuracy to a vote weight. Raw accuracy
off small samples is mostly noise — at n=20 a coin-flip indicator routinely
measures 65% — so the mapping here applies two dampers:

  1. Shrinkage: accuracy is pulled toward 50% as if TUNED_WEIGHT_SHRINKAGE
     coin-flip pseudo-samples were mixed in (empirical-Bayes with a Beta
     prior centered on 0.5). A 60% read needs a big sample before the tuner
     believes most of it.
  2. Step cap: a weight moves at most TUNED_WEIGHT_MAX_STEP per retune from
     its previous value, so one odd day can't swing the live ensemble.

Pure math, no I/O — callers pass the previous weights in.
"""

from __future__ import annotations

from config import TUNED_WEIGHT_FLOOR, TUNED_WEIGHT_MAX_STEP, TUNED_WEIGHT_SHRINKAGE


def shrunk_accuracy(correct: int, n: int, prior: float = TUNED_WEIGHT_SHRINKAGE) -> float:
    """Accuracy pulled toward 0.5 by `prior` pseudo-samples of coin flip."""
    if n <= 0:
        return 0.5
    return (correct + prior * 0.5) / (n + prior)


def accuracy_to_weight(correct: int, n: int,
                       prior: float = TUNED_WEIGHT_SHRINKAGE,
                       floor: float = TUNED_WEIGHT_FLOOR) -> float:
    """The shared accuracy -> weight mapping, on the shrunk accuracy:
    weight = (acc - 0.50) * 20, floored. (55% -> 1.0, 60% -> 2.0.)"""
    return round(max((shrunk_accuracy(correct, n, prior) - 0.50) * 20, floor), 1)


def step_limited(new: float, prev: float,
                 max_step: float = TUNED_WEIGHT_MAX_STEP,
                 floor: float = TUNED_WEIGHT_FLOOR) -> float:
    """Clamp a suggested weight to within max_step of its previous value."""
    lo, hi = prev - max_step, prev + max_step
    return round(max(min(new, hi), lo, floor), 1)
