"""Standalone GUESS prediction module for LOKI/Mazepot.

Requires bestpick_engine.py in the same directory.
The live crowd strategy selects a number based on guesses already posted by others.
"""
from __future__ import annotations

from collections.abc import Iterable
from bestpick_engine import pick as bestpick_pick
from bestpick_engine import guess_best_response


def pick_guess(history: Iterable[int] | None = None) -> tuple[int, list[int], dict]:
    """Return (main_number, backup_numbers, details).

    history contains revealed GUESS outcomes only; never include predictions.
    """
    clean_history = [int(x) for x in (history or []) if 1 <= int(x) <= 100]
    number, details = bestpick_pick("GUESS", history=clean_history)
    ranked = [int(x) for x in details.get("top", []) if 1 <= int(x) <= 100]
    backups = [x for x in ranked if x != number][:2]
    return number, backups, details


def pick_guess_from_crowd(other_guesses: Iterable[int],
                          expected_players: int | None = None,
                          top_n: int = 3) -> tuple[int, list[tuple[int, float, float]], dict]:
    """Choose the best GUESS number using other players' posted numbers.

    Use each player's first valid guess once per round.
    Returns (best_number, ranked_candidates, details).
    """
    clean = [int(x) for x in other_guesses if 1 <= int(x) <= 100]
    ranked, details = guess_best_response(
        clean, players=expected_players, top_n=top_n
    )
    if not ranked:
        raise ValueError("No candidate numbers were produced")
    return int(ranked[0][0]), ranked, details
