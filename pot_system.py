"""Standalone POT prediction module for LOKI/Mazepot.

Requires bestpick_engine.py in the same directory.
This is a heuristic picker, not a guarantee for random outcomes.
"""
from __future__ import annotations

from collections.abc import Iterable
from bestpick_engine import pick as bestpick_pick


def pick_pot(history: Iterable[int] | None = None,
             recent_ranges: Iterable[tuple[int, int]] | None = None,
             min_live_rounds: int = 30,
             recent_limit: int = 200) -> tuple[int, list[int], dict]:
    """Return (main_number, backup_numbers, details).

    history: revealed POT outcomes only (1..100), oldest to newest.
    recent_ranges: completed POT winning ranges as (low, high), oldest to newest.
    Once there are min_live_rounds valid ranges, candidates are ranked by
    exponentially weighted live range coverage, blended with Best Pick ranking.
    """
    clean_history = [int(x) for x in (history or []) if 1 <= int(x) <= 100]
    number, details = bestpick_pick("POT", history=clean_history)
    ranges = []
    for item in recent_ranges or []:
        try:
            low, high = sorted((int(item[0]), int(item[1])))
        except (TypeError, ValueError, IndexError):
            continue
        if 1 <= low <= high <= 100:
            ranges.append((low, high))
    ranges = ranges[-max(1, int(recent_limit)):]

    ranked = [int(x) for x in details.get("top", []) if 1 <= int(x) <= 100]
    if len(ranges) >= int(min_live_rounds):
        recent = ranges[-max(1, min(200, int(recent_limit))):]
        scores = [0.0] * 101
        total_weight = 0.0
        for index, (low, high) in enumerate(recent):
            weight = 0.985 ** (len(recent) - index - 1)
            total_weight += weight
            for candidate in range(low, high + 1):
                scores[candidate] += weight
        coverage = {n: scores[n] / total_weight for n in range(1, 101)}
        model_order = ranked + [n for n in range(1, 101) if n not in ranked]
        model_rank = {n: i for i, n in enumerate(model_order)}
        number = max(range(1, 101), key=lambda n: (coverage[n], -model_rank.get(n, 999)))
        ranked = sorted(range(1, 101),
                        key=lambda n: (coverage[n], -model_rank.get(n, 999)),
                        reverse=True)
        details = dict(details)
        details["live_range_rounds"] = len(ranges)
        details["live_range_coverage"] = round(coverage[number], 4)
        details["calibration"] = "live-range"
    else:
        details = dict(details)
        details["live_range_rounds"] = len(ranges)
        details["calibration"] = f"waiting for {min_live_rounds} valid ranges"

    backups = []
    for candidate in ranked:
        if candidate != number and candidate not in backups:
            backups.append(candidate)
        if len(backups) == 2:
            break
    return number, backups, details


def is_pot_hit(prediction: int, result_range: tuple[int, int] | list[int]) -> bool:
    """True when the submitted number is inside the completed winning range."""
    low, high = sorted((int(result_range[0]), int(result_range[1])))
    return low <= int(prediction) <= high
