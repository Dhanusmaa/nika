"""Best Pick v4 — crowd-aware POT/GUESS engine adapted for Mazepot.

POT scores numbers by historical win-window probability and expected pool share.
GUESS estimates crowd preferences from the supplied seed rounds and simulates
expected prize/top-three chance. Revealed result history is used for bias tests.
This is a heuristic, not a guarantee for random outcomes.
"""
import math
import os
import random
from collections import Counter

def _env_int(name, default=0):
    value = os.getenv(name, "").strip()
    try:
        return int(value) if value else default
    except ValueError:
        return default

def _env_float(name, default):
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return float(default)

SEED_POT_ROUNDS = [
(28,18,38,3),(49,36,62,2),(89,77,100,6),(78,65,91,4),(84,71,97,6),(79,71,87,2),
(53,42,64,4),(10,4,16,3),(56,44,68,5),(15,1,31,6),(44,31,57,1),(48,38,58,6),
(94,76,100,7),(93,75,100,3),(2,1,8,0),(23,4,42,5),(34,22,46,2),(94,88,100,1),
(21,1,41,4),(50,32,68,2),(10,1,19,1),(73,66,80,1),(72,54,90,2),(64,54,74,1),
(75,53,97,4),(51,27,75,7),(69,45,93,4),(54,48,60,3),(63,45,81,9),(93,87,99,0),
(59,37,81,12),(9,2,16,2),(15,1,37,6),(45,33,57,4),(19,10,28,4),(6,1,31,0),
(33,14,52,5),(95,80,100,2),(70,65,75,1),(63,47,79,5),(86,77,95,1),(11,1,23,1),
(70,48,92,8),(13,2,24,5),(77,72,82,0),(95,77,100,2),(21,14,28,4),(88,64,100,2),
(59,52,66,2),(24,9,39,2),(83,62,100,6),(53,31,75,5),(47,26,68,7),(65,54,76,6),(35,14,56,7)
]
SEED_GUESS_ROUNDS = [
(33,32,44,50),(22,34,6,44),(18,14,11,25),(55,53,59,50),(98,97,95,77),
(23,22,9,39),(89,94,84,77),(11,15,15,20),(90,90,89,91),(13,14,9,7),
(54,54,49,45),(84,88,89,77),(69,76,77,89),(33,36,23,44),(62,66,73,77),
(31,33,21,42),(17,18,6,36),(29,31,18,9),(58,72,78,27),(23,26,31,9),
(92,86,63,62),(38,35,44,50),(93,85,77,67),(54,47,44,67),(19,23,31,33),
(52,56,57,44),(16,20,9,31),(28,36,36,19),(9,27,44,48),(11,7,30,33),
(23,23,23,21),(31,30,33,26),(59,56,54,54),(40,39,44,34),(96,94,79,67),
(35,33,33,38),(1,22,22,28),(37,44,54,20),(3,9,20,23),(71,67,77,77),
(95,89,77,77),(56,54,54,53),(36,34,29,44),(22,21,24,33),(29,32,33,34),
(5,4,8,22),(31,32,33,44),(83,77,67,53),(76,89,89,54),(87,88,89,84),
(89,91,95,80),(60,63,47,46),(20,22,29,37),(78,77,79,80),(73,76,77,78)
]

BP_MODE = os.getenv("BP_MODE", "smart").strip().lower()
BP_SHARPNESS_POT = max(0.5, _env_float("BP_SHARPNESS", 3.0))
BP_SHARPNESS_GUESS = max(0.5, _env_float("BP_SHARPNESS", 4.0))
BP_MIN_RATIO_POT = min(1.0, max(0.1, _env_float("BP_MIN_RATIO", 0.93)))
BP_MIN_RATIO_GUESS = min(1.0, max(0.1, _env_float("BP_MIN_RATIO", 0.92)))
BP_AVOID = {int(x) for x in os.getenv("BP_AVOID", "").replace(" ", "").split(",")
            if x.isdigit() and 1 <= int(x) <= 100}
BP_GUESS_PLAYERS = _env_int("BP_GUESS_PLAYERS", 0)
BP_POT_POOL = _env_float("BP_POT_POOL", 555.0)
BP_HISTORY_LIMIT = max(55, _env_int("BP_HISTORY_LIMIT", 400))
BP_RECENT_BLOCK = max(0, _env_int("BP_RECENT_BLOCK", 4))
BP_BOOTSTRAP = max(0, _env_int("BP_BOOTSTRAP", 24))
BP_STAB_MIN = 0.6
BP_CHI_CRITICAL = 21.67
BP_CHI5_CRITICAL = 13.28
_RECENT = {"POT": [], "GUESS": []}

def _bp_result_distribution(history):
    f = [0.01] * 100
    n = len(history)
    chi, lam = 0.0, 0.0
    if n >= 100:
        bins = [0] * 10
        for v in history:
            bins[min(9, (int(v) - 1) // 10)] += 1
        expected = n / 10.0
        chi = sum((o - expected) ** 2 / expected for o in bins)
        zone = [bins[2*i] + bins[2*i+1] for i in range(5)]
        chi5 = sum((o - n / 5.0) ** 2 / (n / 5.0) for o in zone)
        if chi >= BP_CHI_CRITICAL:
            lam = min(0.5, n / 400.0)
            f = [(1-lam)*0.01 + lam*(bins[i//10]/n)/10.0 for i in range(100)]
        elif chi5 >= BP_CHI5_CRITICAL:
            chi, lam = chi5, min(0.5, n / 400.0)
            f = [(1-lam)*0.01 + lam*(zone[i//20]/n)/20.0 for i in range(100)]
    return f, lam, chi

def _pot_halfwidth(actual, low, high):
    if low > 1 and high < 100:
        return max(actual-low, high-actual)
    if low <= 1 and high < 100:
        return high-actual
    if high >= 100 and low > 1:
        return actual-low
    return None

def _pot_scores(result_prob, rounds):
    widths = []
    made_sum = 0.0
    size_sum = 0.0
    for actual, low, high, made in rounds:
        width = _pot_halfwidth(int(actual), int(low), int(high))
        if width is not None and width > 0:
            widths.append(width)
        if made is not None and int(low) <= int(high):
            made_sum += max(0.0, float(made))
            size_sum += min(100, int(high)) - max(1, int(low)) + 1
    widths = widths or [14]
    density = min(0.4, max(0.03, made_sum / size_sum)) if size_sum > 0 else 0.135
    width_counts = Counter(widths)
    total = float(sum(width_counts.values())) or 1.0
    win, ev = [0.0]*100, [0.0]*100
    for width, count in width_counts.items():
        share_by_result = [0.0] * 101
        for actual in range(1, 101):
            low, high = max(1, actual-width), min(100, actual+width)
            size = high-low+1
            lam = max(1e-9, density*(size-1))
            share_by_result[actual] = BP_POT_POOL * (1-math.exp(-lam)) / lam
        for p in range(1, 101):
            low, high = max(1, p-width), min(100, p+width)
            probability = sum(result_prob[a-1] for a in range(low, high+1))
            win[p-1] += count * probability / total
            ev[p-1] += count * sum(
                result_prob[a-1] * share_by_result[a] for a in range(low, high+1)
            ) / total
    return win, ev, widths, density

def _softmax(z):
    m = max(z)
    expv = [math.exp(v-m) for v in z]
    total = sum(expv) or 1.0
    return [v/total for v in expv]

def _fit_guess_crowd(rounds, players, iters=260, reg=0.3):
    obs, cnt = [], [0.0]*100
    for actual, guesses in rounds:
        if len(guesses) != 3:
            continue
        d3 = max(abs(actual-x) for x in guesses)
        obs.append((max(1, actual-d3+1), min(100, actual+d3-1)))
        for guess in guesses:
            cnt[guess-1] += 1.0
    if not obs:
        return [0.01]*100
    z = [0.0]*100
    m1, m2 = [0.0]*100, [0.0]*100
    total_w = 3.0*len(obs)
    others = max(0, players-3)
    for it in range(1, iters+1):
        g = _softmax(z)
        cumulative = [0.0]
        for value in g:
            cumulative.append(cumulative[-1]+value)
        diff = [0.0]*102
        sum_ratio = 0.0
        for low, high in obs:
            if high < low:
                continue
            mass = min(0.999, cumulative[high]-cumulative[low-1])
            inv = 1.0/(1.0-mass)
            diff[low-1] += inv
            diff[high] -= inv
            sum_ratio += mass*inv
        common = total_w - others*sum_ratio
        zbar = sum(z)/100.0
        running = 0.0
        for k in range(100):
            running += diff[k]
            grad = cnt[k] - g[k]*others*running - g[k]*common - reg*(z[k]-zbar)
            m1[k] = 0.9*m1[k]+0.1*grad
            m2[k] = 0.999*m2[k]+0.001*grad*grad
            mhat = m1[k]/(1-0.9**it)
            vhat = m2[k]/(1-0.999**it)
            z[k] += 0.06*mhat/(math.sqrt(vhat)+1e-8)
    return _softmax(z)

def _simulate_guess(crowd, players, result_prob, sims=3000, seed=12345):
    rnd = random.Random(seed)
    nums = list(range(1, 101))
    cumulative, total = [], 0.0
    for value in crowd:
        total += value
        cumulative.append(total)
    ev, chance = [0.0]*100, [0.0]*100
    # Simulate competitor guesses and retain the 3rd-place distance per trial.
    trials = []
    for _ in range(max(500, sims)):
        actual = rnd.choices(nums, weights=result_prob, k=1)[0]
        distances = sorted(abs(x-actual) for x in rnd.choices(nums, cum_weights=cumulative, k=max(3, players)))
        trials.append((actual, distances[0], distances[1], distances[2]))
    for p in range(1, 101):
        value = hits = 0
        for actual, d1, d2, d3 in trials:
            d = abs(p-actual)
            if d < d3:
                hits += 1
                value += 150 if d < d1 else (100 if d < d2 else 50)
        ev[p-1] = value/len(trials)
        chance[p-1] = hits/len(trials)
    mean_third = sum(row[3] for row in trials)/len(trials)
    return ev, chance, mean_third

def _smooth(values):
    return [sum(values[max(0,i-1):min(len(values),i+2)]) / len(values[max(0,i-1):min(len(values),i+2)])
            for i in range(len(values))]

def _guess_scores(history, result_prob):
    rounds = [(r[0], [r[1], r[2], r[3]]) for r in SEED_GUESS_ROUNDS]
    players = max(10, min(16, BP_GUESS_PLAYERS or 12))
    crowd = _fit_guess_crowd(rounds, players)
    ev, chance, _third = _simulate_guess(crowd, players, result_prob, sims=2500)
    scores = _smooth(ev)
    info = (f"crowd fitted on {len(rounds)} seed rounds · ~{players} players · "
            f"expected prize simulation; seed crowd profile")
    return scores, chance, info

def _draw_weights(scores, sharpness, min_ratio):
    best = max(scores) or 1.0
    weights = [((max(0.0, v)/best)**sharpness if v/best >= min_ratio else 0.0) for v in scores]
    if sum(w > 0 for w in weights) < 8:
        for i in sorted(range(100), key=lambda j: -scores[j])[:12]:
            weights[i] = max(weights[i], (max(0.0, scores[i])/best)**sharpness)
    for number in BP_AVOID:
        weights[number-1] = 0.0
    return weights

GUESS_PRIZES = (150, 100, 50)  # 1st / 2nd / 3rd closest

def guess_best_response(others, players=None, top_n=3):
    """Live GUESS strategy: best number GIVEN the numbers other players already posted.

    The winning number is uniform random (no pattern to predict), but the players'
    guesses are public in the chat. Win = being one of the 3 closest, so a number
    sitting in a *gap* between other guesses owns a wide slice of the 1-100 line,
    while a number next to a crowded cluster (77, 44, 33...) owns almost nothing.

    others  : list of numbers already posted by other players (first number per user)
    players : expected TOTAL players incl. late ones (default BP_GUESS_PLAYERS or 12);
              the not-yet-posted ones are modelled as random from a crowd prior.
    Ties are treated as losses for us (conservative). Returns (ranked, info) where
    ranked = [(number, expected_prize, win_chance), ...] best first.
    """
    others = [int(x) for x in others if 1 <= int(x) <= 100]
    total = max(len(others), int(players or BP_GUESS_PLAYERS or 12))
    unknown = total - len(others)  # players who have not posted yet
    # Crowd prior for late players: mostly uniform + a bit of the observed clusters.
    emp = [0.0] * 100
    for g in others:
        for d in (-2, -1, 0, 1, 2):
            if 1 <= g + d <= 100:
                emp[g + d - 1] += (3 - abs(d)) / 9.0
    emp_total = sum(emp)
    crowd = [0.7 * 0.01 + (0.3 * emp[i] / emp_total if emp_total else 0.3 * 0.01) for i in range(100)]
    prefix = [0.0]
    for v in crowd:
        prefix.append(prefix[-1] + v)
    prizes = GUESS_PRIZES[:top_n]
    rows = []
    for p in range(1, 101):
        ev = win = 0.0
        for a in range(1, 101):
            d = abs(p - a)
            ahead = sum(1 for g in others if abs(g - a) <= d)
            if ahead >= top_n:
                continue
            lo, hi = max(1, a - d), min(100, a + d)
            q = prefix[hi] - prefix[lo - 1]
            for j in range(0, top_n - ahead):
                pmf = math.comb(unknown, j) * q ** j * (1 - q) ** (unknown - j) if unknown >= j else 0.0
                ev += 0.01 * pmf * prizes[ahead + j]
                win += 0.01 * pmf
        rows.append((p, ev, win))
    rows.sort(key=lambda r: -r[1])
    info = {"others": len(others), "unknown": unknown, "best_ev": round(rows[0][1], 2),
            "best_win": round(rows[0][2], 3)}
    return rows, info


def pick(game, history=None):
    """Return (number, details). history must contain revealed outcomes only."""
    game = str(game).upper()
    if game not in ("POT", "GUESS"):
        raise ValueError("game must be POT or GUESS")
    clean = [int(v) for v in (history or []) if str(v).lstrip("-").isdigit() and 1 <= int(v) <= 100][-BP_HISTORY_LIMIT:]
    result_prob, lam, chi = _bp_result_distribution(clean)
    bias_note = (f"significant result tilt (chi2={chi:.1f})" if lam > 0
                 else (f"results look random (chi2={chi:.1f})" if chi else "bias test needs 100+ results"))
    if game == "POT":
        win, scores, widths, density = _pot_scores(result_prob, SEED_POT_ROUNDS)
        info = (f"POT win-window/expected-share model ({len(widths)} seed windows; "
                f"mean half-width≈{sum(widths)/len(widths):.1f}; density≈{density:.2f})")
        sharpness, min_ratio = BP_SHARPNESS_POT, BP_MIN_RATIO_POT
        score_for_detail = scores
    else:
        scores, chance, info = _guess_scores(clean, result_prob)
        sharpness, min_ratio = BP_SHARPNESS_GUESS, BP_MIN_RATIO_GUESS
        score_for_detail = scores
    weights = _draw_weights(scores, sharpness, min_ratio)
    recent = _RECENT[game][-BP_RECENT_BLOCK:] if BP_RECENT_BLOCK else []
    for number in recent:
        weights[number-1] = 0.0
    if not any(weights):
        weights = [0.0 if i+1 in BP_AVOID else 1.0 for i in range(100)]
    number = random.SystemRandom().choices(range(1, 101), weights=weights, k=1)[0]
    _RECENT[game].append(number)
    del _RECENT[game][:-8]
    top = sorted(range(1,101), key=lambda i: -score_for_detail[i-1])[:5]
    details = {
        "mode": "smart", "samples": len(clean), "info": info, "bias": bias_note,
        "top": top, "score": round(score_for_detail[number-1], 5),
        "win_chance": round((win[number-1] if game == "POT" else chance[number-1]), 4),
    }
    return number, details

if __name__ == "__main__":
    for game_name in ("POT", "GUESS"):
        number, detail = pick(game_name)
        print(game_name, "->", number, "|", detail)
