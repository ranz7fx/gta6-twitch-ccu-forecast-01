"""gta6-twitch-ccu-forecast-01: launch-day Twitch viewers from comparable launches.

Model: the release-day curve of a big launch is mostly a *shape* problem, not a
level problem. Hour 1's print anchors the level; the shape (each later hour as a
ratio to hour 1) is borrowed from the past launches whose hour-1 print is closest
to this case's, in log space.

    For each hour h = 2..24, over the N_NEAR comparables nearest in log(hour-1):
        med_h = median( log(viewers_h / viewers_1) )     # typical shape
        sig_h = max(SIGMA_FLOOR, 1.4826 * MAD(log ratios)) # cross-launch spread
        q_tau = p1 * exp(med_h) * exp(z_tau * sig_h)

Why this: across the 37 usable comparable launches in the kit, log(peak/hour-1)
correlates -0.94 with log(hour-1): launches that open big stay near their hour-1
level (peak/hour-1 ~1.0-3.6), while launches that open tiny explode (100x+).
A single global shape would badly over-predict a big opener like GTA VI, and
hour-1 level alone predicts the shape far better than anything else in the case
(the pre-day hype ratio adds nothing: corr 0.01 with peak/hour-1). Conditioning
on log(hour-1) is the whole trick.

Backtest (leave-one-out over the kit's comparables, pinball loss on the log
scale, lower is better; baseline = the kit starter, flat hour-1 with a
0.15*sqrt(h-1) widening spread):
    large openers only, hour-1 >= 20k (the GTA-VI-like regime, 27 launches):
        this model  0.258   vs  baseline 0.321   (typical miss 47% vs 63%)
    all 37 launches:
        this model  0.455   vs  baseline 0.930
    The house baseline scores ~0.387 on the practice cases; this model beats
    the starter by ~20% in the regime that matters. Parameters sit on a broad
    plateau: N_NEAR 10-20 and spread multiplier 0.85-1.15 all score 0.257-0.266
    on large openers, so nothing here is tuned to a spike.

Robustness: the program reads only the case file it is given (comparables
included), uses no network, no clock, no randomness and no dict-order
dependence, so two runs are byte-identical. Any failure (missing hour-1,
too few comparables, bad data) falls back to the starter's persistence
forecast, never to a crash: a valid flat forecast always beats an invalid
case. Quantiles are non-decreasing, in (0, 1e9], rounded to 2 decimals.

Deterministic, stdlib only, ~0.1 s/case.
"""

import json
import math
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path

# The grader's -P leaves /bot off sys.path: put it back first, so a module
# shipped beside this file imports (none is needed today, but keep the hook).
sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

# The quantile levels the contest scores, in the order each row lists them.
LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
# The standard normal's quantile at each level.
Z = [-1.645, -0.674, 0.0, 0.674, 1.645]
# How many past launches, nearest in log(hour-1), shape each hour's forecast.
N_NEAR = 12
# Narrowest log-spread allowed for any hour: keeps the band honest when the
# nearest launches happen to agree too tightly.
SIGMA_FLOOR = 0.15
# Fallback spread (the starter's): log-sigma per sqrt-hour of persistence.
BASE_SIGMA = 0.15
# A comparable counts only if it has hour 1 and at least this many later hours.
MIN_DAY_HOURS = 10
# Output bounds from the rules: every quantile in (0, 1e9].
MAX_VIEWERS = 1_000_000_000.0


def round2(x: float) -> float | int:
    """x rounded to 2 decimals, half up, as JavaScript's Math.round rounds it.

    A whole number comes back as an int, so json.dumps writes 1240 where
    JavaScript writes 1240 (not 1240.0).
    """
    scaled = x * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100
    return int(value) if value.is_integer() else value


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def day_series(comp: dict) -> dict:
    """A comparable's release-day hourly viewers, keyed by hour 1..24 (UTC day)."""
    try:
        start = parse_time(comp["day_start"])
    except (KeyError, TypeError, ValueError):
        return {}
    series = {}
    for point in comp.get("hourly") or []:
        try:
            at = parse_time(point["from"])
            viewers = point["viewers"]
        except (KeyError, TypeError, ValueError):
            continue
        if viewers is None or viewers <= 0:
            continue
        hour = int((at - start).total_seconds() // 3600) + 1
        if 1 <= hour <= 24:
            series[hour] = float(viewers)
    return series


def hour1_print(case: dict) -> float:
    """Hour 1's print, with fallbacks that never fail the run."""
    for row in case.get("known") or []:
        if row.get("hour") == 1 and row.get("viewers"):
            return max(1.0, float(row["viewers"]))
    hourly = case.get("history_hourly") or []
    if hourly:
        last = hourly[-1]
        if last.get("viewers"):
            return max(1.0, float(last["viewers"]))
    history = case.get("history") or []
    if history:
        last = history[-1]
        if last.get("viewers"):
            return max(1.0, float(last["viewers"]))
    return 1.0


def nearest_references(case: dict, p1: float) -> list:
    """Past launches usable as shape references, nearest in log(hour-1) first."""
    refs = []
    for comp in case.get("comparables") or []:
        series = day_series(comp)
        if 1 not in series or series[1] <= 0:
            continue
        if sum(1 for h in series if h >= 2) < MIN_DAY_HOURS:
            continue
        refs.append((series[1], series))
    refs.sort(key=lambda r: abs(math.log(r[0]) - math.log(p1)))
    return refs[:N_NEAR]


def baseline_quantiles(p1: float, h: int) -> list:
    """The starter's persistence forecast for hour h (the fallback)."""
    return [round2(p1 * math.exp(z * BASE_SIGMA * math.sqrt(h - 1))) for z in Z]


def clamp_quantiles(values: list) -> list:
    """Force the scorer's bounds and non-decreasing order after rounding."""
    out = []
    prev = 0.01
    for value in values:
        value = min(MAX_VIEWERS, max(0.01, float(value)))
        value = max(prev, value)
        out.append(round2(value))
        prev = out[-1]
    return out


def forecast_case(case: dict) -> dict:
    p1 = hour1_print(case)
    refs = nearest_references(case, p1)
    hours = case.get("forecast") or [{"hour": h} for h in range(2, 25)]
    rows = []
    for row in hours:
        h = int(row["hour"])
        quantiles = None
        if refs:
            ratios = [
                math.log(series[h] / series[1])
                for _, series in refs
                if h in series and 1 in series and series[h] > 0
            ]
            if len(ratios) >= 3:
                med = statistics.median(ratios)
                mad = statistics.median([abs(x - med) for x in ratios])
                sigma = max(SIGMA_FLOOR, 1.4826 * mad)
                center = p1 * math.exp(med)
                quantiles = [center * math.exp(z * sigma) for z in Z]
        if quantiles is None:
            quantiles = baseline_quantiles(p1, h)
        rows.append({"hour": h, "q": clamp_quantiles(quantiles)})
    rows.sort(key=lambda r: r["hour"])
    return {"hours": rows}


def safe_forecast(case: dict) -> dict:
    """Never raise: any surprise in the case degrades to the persistence flat."""
    try:
        return forecast_case(case)
    except Exception:
        try:
            p1 = hour1_print(case)
        except Exception:
            p1 = 1.0
        rows = [
            {"hour": h, "q": clamp_quantiles(baseline_quantiles(p1, h))}
            for h in range(2, 25)
        ]
        return {"hours": rows}


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Every case file, in name order, so two runs write the same bytes.
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = safe_forecast(case)
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
