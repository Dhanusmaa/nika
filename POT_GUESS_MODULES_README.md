# POT and GUESS modules

Files:
- `pot_system.py`: POT number selection and optional live winning-range calibration.
- `guess_system.py`: historical GUESS selection and live crowd-aware selection.
- `bestpick_engine.py`: shared prediction engine used by the Nika • Joy Boy bot.

Copy all three Python files into the same folder in the destination bot.

## Install / import

No extra dependency is required for these modules beyond Python's standard library.

```python
from pot_system import pick_pot, is_pot_hit
from guess_system import pick_guess, pick_guess_from_crowd

pot_number, pot_backups, pot_info = pick_pot(
    history=[42, 90, 43, 87],
    recent_ranges=[(28, 56), (81, 99), (19, 67)],
)

guess_number, guess_backups, guess_info = pick_guess(
    history=[33, 22, 18, 55]
)

# For a live GUESS round, pass other players' first guesses:
best_number, ranked, crowd_info = pick_guess_from_crowd(
    other_guesses=[33, 44, 77, 20],
    expected_players=12,
)
```

## Data rules
- POT history must contain actual revealed POT numbers, not the bot's predictions.
- POT ranges must be completed winning ranges parsed from result posts.
- GUESS history must contain actual revealed GUESS numbers.
- For live crowd GUESS, collect each other player's first valid number (1-100) once per round.
- Keep history isolated by game; never mix POT and GUESS data.

This is a heuristic model. If the source game chooses a truly random number, no algorithm can guarantee a correct prediction. Test against a baseline before relying on it.
