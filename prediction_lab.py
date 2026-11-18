"""Offline walk-forward audit for the 31 POT + 32 GUESS rounds supplied by the owner.

This module intentionally does NOT alter live predictions. It keeps the supplied
rounds as an evaluation set and reports leakage-safe baselines. Small samples
cannot establish a 70% future win rate; GUESS prize outcomes also depend on other
players' guesses, which were not included in the supplied history.
Run: python prediction_lab.py
"""

from statistics import median, mean

# Newest first as shown by /potstats; convert to oldest-first below.
POT_NEWEST_FIRST = [
    (20, 53, 69), (20, 2, 50), (66, 78, 100), (31, 1, 35),
    (69, 43, 67), (16, 6, 34), (71, 13, 37), (22, 79, 100),
    (79, 3, 41), (87, 20, 40), (67, 1, 37), (41, 31, 81),
    (82, 64, 84), (51, 1, 26), (27, 62, 86), (70, 1, 35),
    (68, 1, 36), (38, 1, 19), (20, 61, 89), (83, 56, 100),
    (35, 42, 56), (79, 3, 23), (77, 54, 100), (86, 46, 58),
    (55, 55, 100), (80, 39, 59), (21, 1, 13), (40, 1, 22),
    (79, 1, 9), (58, 11, 53), (64, 19, 61),
]

# (sent, actual, prize), newest first as shown by /guessstats.
GUESS_NEWEST_FIRST = [
    (16, 66, 0), (19, 40, 0), (20, 20, 0), (14, 83, 0),
    (21, 46, 0), (25, 16, 0), (24, 9, 0), (26, 84, 0),
    (28, 61, 0), (13, 10, 0), (29, 24, 0), (14, 71, 0),
    (22, 71, 0), (24, 47, 0), (13, 7, 0), (16, 92, 0),
    (17, 31, 150), (25, 22, 0), (29, 19, 0), (18, 5, 100),
    (16, 83, 0), (17, 57, 6), (21, 60, 0), (25, 73, 0),
    (18, 59, 0), (20, 64, 0), (17, 36, 0), (22, 11, 50),
    (25, 92, 0), (29, 38, 50), (12, 96, 0), (14, 87, 0),
]


def pot_walk_forward(min_train=5):
    """At each round, select the number covered by the largest share of prior
    winning ranges. Only earlier rounds are used. Ties are resolved toward 50.
    """
    rows = list(reversed(POT_NEWEST_FIRST))
    errors, hits, evaluated = [], 0, 0
    for i in range(min_train, len(rows)):
        prior = rows[:i]
        coverage = {
            n: sum(low <= n <= high for _sent, low, high in prior) / len(prior)
            for n in range(1, 101)
        }
        pick = max(range(1, 101), key=lambda n: (coverage[n], -abs(n - 50)))
        _sent, low, high = rows[i]
        hits += int(low <= pick <= high)
        evaluated += 1
        errors.append((pick, low, high))
    return evaluated, hits, errors


def guess_walk_forward(min_train=5):
    """Compare outcome-only baselines against the historical sent number.
    The metrics are exact hits, within-5/within-10 and absolute error; they are
    NOT the game prize win condition.
    """
    rows = list(reversed(GUESS_NEWEST_FIRST))
    methods = {
        "historical sent": lambda prior: prior[-1][0],
        "fixed 50": lambda prior: 50,
        "expanding median": lambda prior: round(median(r[1] for r in prior)),
        "expanding mean": lambda prior: round(mean(r[1] for r in prior)),
    }
    output = {}
    for name, choose in methods.items():
        errors = []
        for i in range(min_train, len(rows)):
            prior = rows[:i]
            actual = rows[i][1]
            prediction = choose(prior)
            errors.append(abs(prediction - actual))
        n = len(errors)
        output[name] = {
            "n": n,
            "mae": round(mean(errors), 2) if errors else None,
            "exact": sum(e == 0 for e in errors),
            "within_5": sum(e <= 5 for e in errors),
            "within_10": sum(e <= 10 for e in errors),
        }
    return output


def main():
    pot_n, pot_wins, _ = pot_walk_forward()
    print("POT supplied rounds:", len(POT_NEWEST_FIRST))
    print("POT owner-reported baseline: 8/31 = 25.8%")
    print(f"POT walk-forward range-coverage baseline: {pot_wins}/{pot_n} = "
          f"{(100 * pot_wins / pot_n):.1f}%" if pot_n else "POT: insufficient data")

    guess_wins = sum(prize > 0 for _sent, _actual, prize in GUESS_NEWEST_FIRST)
    total_prize = sum(prize for _sent, _actual, prize in GUESS_NEWEST_FIRST)
    print("\\nGUESS supplied rounds:", len(GUESS_NEWEST_FIRST))
    print(f"GUESS owner-reported prize wins: {guess_wins}/32 = "
          f"{100 * guess_wins / 32:.1f}%; total recorded prize={total_prize}")
    print("GUESS outcome-only walk-forward baselines (not prize-win predictions):")
    for name, metrics in guess_walk_forward().items():
        print(f"  {name}: {metrics}")
    print("\\nLIMITATIONS:")
    print("- POT rows contain sent number + winning range, not the revealed POT result.")
    print("- GUESS rows omit other players' guesses, so crowd/prize rank cannot be replayed.")
    print("- Do not claim 70%: validate on future unseen rounds before enabling a strategy.")


if __name__ == "__main__":
    main()
