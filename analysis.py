"""Cycle detection and hysteresis characterization from power samples.

Power draw is roughly bimodal: a low mode (indoor fan only, compressor off) and a
high mode (compressor running). We split them with an Otsu threshold rather than a
guessed constant, then read the indoor temperature at each transition. On an
inverter split the compressor modulates instead of stopping, so the two modes may
not separate. bimodality() decides whether there is a real on/off cycle to measure.
"""
from statistics import mean


def otsu_threshold(values: list[float], bins: int = 64) -> float:
    # Threshold that best splits two populations (maximizes between-class variance).
    # Otsu always returns one, even on a unimodal set, so this is not a bimodality
    # test. bimodality() decides whether the two classes are real physical regimes.
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return hi

    hist = [0] * bins
    for v in values:
        idx = min(int((v - lo) / (hi - lo) * bins), bins - 1)
        hist[idx] += 1

    total = len(values)
    sum_all = sum((lo + (i + 0.5) * (hi - lo) / bins) * h for i, h in enumerate(hist))

    best_var, best_thresh = -1.0, hi
    w_bg, sum_bg = 0, 0.0
    for i, h in enumerate(hist):
        w_bg += h
        if w_bg == 0:
            continue
        w_fg = total - w_bg
        if w_fg == 0:
            break
        centre = lo + (i + 0.5) * (hi - lo) / bins
        sum_bg += centre * h
        mean_bg = sum_bg / w_bg
        mean_fg = (sum_all - sum_bg) / w_fg
        between = w_bg * w_fg * (mean_bg - mean_fg) ** 2 / total**2
        if between > best_var:
            best_var, best_thresh = between, lo + (i + 1) * (hi - lo) / bins

    return best_thresh


def bimodality(values: list[float], threshold: float) -> dict:
    # Physical discriminant, not statistical: a compressor that stops leaves only
    # the indoor fan, so the high mode is several times the low mode. An inverter
    # that modulates stays the same order of magnitude (ratio near 1), and there is
    # no on/off cycle to measure whatever Otsu says.
    low = [v for v in values if v < threshold]
    high = [v for v in values if v >= threshold]
    if not low or not high:
        return {"ratio": None, "valley": None, "bimodal": False,
                "mean_low_w": None, "mean_high_w": None}

    mean_low, mean_high = mean(low), mean(high)
    ratio = mean_high / mean_low if mean_low > 1e-6 else float("inf")

    # Valley: fraction of samples in the middle zone between the two modes. Measured
    # from the means, not the Otsu threshold (which hugs the majority mode). Two
    # clean regimes leave the valley nearly empty.
    gap = mean_high - mean_low
    lo_edge, hi_edge = mean_low + 0.15 * gap, mean_high - 0.15 * gap
    valley = sum(1 for v in values if lo_edge < v < hi_edge) / len(values)

    return {
        "ratio": round(ratio, 2) if ratio != float("inf") else None,
        "valley": round(valley, 3),
        "bimodal": ratio >= 2.0 and valley <= 0.10,
        "mean_low_w": round(mean_low, 1),
        "mean_high_w": round(mean_high, 1),
    }


# In fan-only or dry mode the compressor is not driven by the thermostat toward a
# setpoint, so those samples have no cycle. Mixing them with cooling creates a fake
# bimodality (fan ~13W vs compressor ~120W) that Otsu reads as an on/off cycle.
THERMOSTATIC_MODES = {"COOL", "HEAT", "AUTO"}


def _pct(xs: list[float], p: float):
    # Linear percentile. xs must be sorted. None if empty.
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def response_curve(usable: list[dict], bin_width: float = 0.25) -> list[dict]:
    # Typical power vs distance-from-setpoint, aggregated into bins. This is what
    # makes the hysteresis readable over any span: instead of the raw trajectory
    # (spaghetti after a few hours) we bin by error (indoor - target) and summarize
    # each bin (median + 25-75 band). We also split by whether the room is cooling
    # or warming: if the two branches separate, that is the hysteresis; otherwise
    # it is just a response curve (a modulating inverter).
    # Direction is estimated over ~5 min (robust to the sensor's 0.5C quantization).
    LOOKBACK = 5
    bins: dict[float, dict] = {}
    for i, s in enumerate(usable):
        err = s["indoor"] - s["target"]
        b = round(err / bin_width) * bin_width
        rec = bins.setdefault(b, {"all": [], "cool": [], "warm": []})
        rec["all"].append(s["watts"])
        if i >= LOOKBACK:
            ref = usable[i - LOOKBACK]
            if s["ts"] - ref["ts"] <= 900:  # skip across a data gap
                d = s["indoor"] - ref["indoor"]
                if d < 0:
                    rec["cool"].append(s["watts"])
                elif d > 0:
                    rec["warm"].append(s["watts"])

    out = []
    for b in sorted(bins):
        a = sorted(bins[b]["all"])
        cool = sorted(bins[b]["cool"])
        warm = sorted(bins[b]["warm"])
        out.append({
            "error": round(b, 3),
            "n": len(a),
            "p25": round(_pct(a, 0.25), 1),
            "median": round(_pct(a, 0.50), 1),
            "p75": round(_pct(a, 0.75), 1),
            "cool": round(_pct(cool, 0.50), 1) if len(cool) >= 3 else None,
            "warm": round(_pct(warm, 0.50), 1) if len(warm) >= 3 else None,
        })
    return out


def thermal_rates(samples: list[dict]) -> dict:
    # Room warming rate when the AC is off, and cooling rate when actively cooling,
    # in C/h. Measured over SUSTAINED runs (>= 20 min), not sample to sample: the
    # indoor sensor is quantized to 0.5C, so a single step over 60s reads as a
    # nonsense ~30C/h. Used by autopilot to anticipate.
    from statistics import median

    def run_rates(keep) -> list[tuple]:
        # (rate C/h, first sample of the run), for each sustained run.
        out, run = [], []

        def flush():
            if len(run) >= 2:
                dt = (run[-1]["ts"] - run[0]["ts"]) / 3600
                dT = run[-1]["indoor"] - run[0]["indoor"]
                if dt >= 0.33 and abs(dT) >= 0.5:  # >= 20 min and a real move
                    out.append((dT / dt, run[0]))

        for s in samples:
            if s["indoor"] is None or not keep(s):
                flush(); run.clear(); continue
            if run and s["ts"] - run[-1]["ts"] > 300:  # gap breaks the run
                flush(); run.clear()
            run.append(s)
        flush()
        return out

    # Warming: any off run that warmed. Cooling: only runs that pulled the room down
    # by at least 1C (the capacity to cool, not the near-flat holding runs), and
    # kept per fan speed, since a quieter/lower fan cools more slowly.
    warm = [r for r, _ in run_rates(lambda s: not s["power"]) if r > 0]
    cool = [(r, s) for r, s in run_rates(
        lambda s: s["power"] and s.get("mode") == "COOL" and (s["watts"] or 0) > 60)
        if r <= -1.0]
    by_fan: dict = {}
    for r, s in cool:
        by_fan.setdefault(s.get("fan"), []).append(r)
    return {
        "warm_rate": round(median(warm), 2) if len(warm) >= 2 else None,   # +C/h
        "cool_rate": round(median([r for r, _ in cool]), 2) if len(cool) >= 2 else None,
        "cool_by_fan": {k: round(median(v), 2) for k, v in by_fan.items() if len(v) >= 2},
        "n_warm": len(warm), "n_cool": len(cool),
    }


def analyse(samples: list[dict]) -> dict:
    # Anything not computable stays None, never 0.
    usable = [
        s for s in samples
        if s["watts"] is not None and s["indoor"] is not None and s["power"]
        and s.get("mode") in THERMOSTATIC_MODES
    ]
    if len(usable) < 10:
        return {
            "ok": False,
            "reason": f"not enough thermostatic samples (COOL/HEAT/AUTO): "
                      f"{len(usable)}, need at least 10",
            "n_samples": len(usable),
        }

    watts = [s["watts"] for s in usable]
    threshold = otsu_threshold(watts)
    modes = bimodality(watts, threshold)

    for s in usable:
        s["compressor"] = s["watts"] >= threshold

    # A transition is dated at the sample that crosses the threshold.
    starts, stops = [], []
    for prev, cur in zip(usable, usable[1:]):
        if not prev["compressor"] and cur["compressor"]:
            starts.append(cur)
        elif prev["compressor"] and not cur["compressor"]:
            stops.append(cur)

    duty = sum(1 for s in usable if s["compressor"]) / len(usable)

    # Without two distinct regimes the threshold splits nothing, so duty cycle and
    # the slopes derived from it would be noise dressed as measurement. Kept silent.
    trustworthy = modes["bimodal"]

    # iSense/Follow-Me: the AC regulates on the remote's sensor, but `indoor` is the
    # unit's. The thresholds would then be measured against the wrong sensor.
    follow_me = sum(1 for s in usable if s.get("follow_me")) / len(usable)

    modes_seen = sorted({s["mode"] for s in usable})

    result = {
        "ok": True,
        "n_samples": len(usable),
        "modes_included": modes_seen,
        "power_threshold_w": round(threshold, 1),
        **modes,
        "follow_me_fraction": round(follow_me, 3),
        "mean_power_w": round(mean(watts), 1),
        "duty_cycle": round(duty, 3) if trustworthy else None,
        "n_starts": len(starts) if trustworthy else None,
        "n_stops": len(stops) if trustworthy else None,
        "t_on": None,
        "t_off": None,
        "deadband": None,
        "mean_cycle_min": None,
        "cool_rate_c_per_h": None,
        "warm_rate_c_per_h": None,
        "curve": response_curve(usable),  # readable at any span
    }
    if not trustworthy:
        return result

    # Transition temperatures, as error from setpoint (the setpoint may have changed
    # during the window; the error stays comparable).
    if starts:
        result["t_on"] = round(mean(s["indoor"] for s in starts), 2)
        result["t_on_error"] = round(mean(s["indoor"] - s["target"] for s in starts), 2)
    if stops:
        result["t_off"] = round(mean(s["indoor"] for s in stops), 2)
        result["t_off_error"] = round(mean(s["indoor"] - s["target"] for s in stops), 2)
    if starts and stops:
        result["deadband"] = round(result["t_on"] - result["t_off"], 2)

    if len(starts) >= 2:
        gaps = [b["ts"] - a["ts"] for a, b in zip(starts, starts[1:])]
        result["mean_cycle_min"] = round(mean(gaps) / 60, 1)

    # Cooling rate (compressor on) and warming rate (compressor off), in C/h. Only
    # consecutive pairs in the same state, to avoid measuring across a transition.
    cool, warm = [], []
    for prev, cur in zip(usable, usable[1:]):
        dt_h = (cur["ts"] - prev["ts"]) / 3600
        if dt_h <= 0 or dt_h > 0.25:  # data gap, skip
            continue
        if prev["compressor"] != cur["compressor"]:
            continue
        rate = (cur["indoor"] - prev["indoor"]) / dt_h
        (cool if cur["compressor"] else warm).append(rate)
    if cool:
        result["cool_rate_c_per_h"] = round(mean(cool), 2)
    if warm:
        result["warm_rate_c_per_h"] = round(mean(warm), 2)

    return result
