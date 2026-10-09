from __future__ import annotations

import math
import pathlib

from niome_subnet.genomics import pgxlib
from niome_subnet.genomics.validation.errors import ValidatorFault, validator_owned
from niome_subnet.utils.settings import (
    CONTRACT_FILE,
    FINAL_REWARD_FILE,
    STAGE3_RESULTS_FILE,
    VALID_OUT_FILE,
)

N_RELIABILITY_BINS = 10


def murphy_decomposition(pairs: list[tuple[float, int]],
                         n_bins: int) -> tuple[dict, list[dict]]:
    """Brier = reliability - resolution + uncertainty. Reported, not scored."""
    n = len(pairs)
    base = sum(o for _, o in pairs) / n
    bins: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
    for c, o in pairs:
        bins[min(int(c * n_bins), n_bins - 1)].append((c, o))

    reliability = resolution = 0.0
    table = []
    for i, members in enumerate(bins):
        if not members:
            table.append({"bin": i, "range": [i / n_bins, (i + 1) / n_bins],
                          "n": 0, "mean_confidence": None,
                          "observed_rate": None})
            continue
        k = len(members)
        mean_c = sum(c for c, _ in members) / k
        obs = sum(o for _, o in members) / k
        reliability += k / n * (mean_c - obs) ** 2
        resolution += k / n * (obs - base) ** 2
        table.append({"bin": i, "range": [i / n_bins, (i + 1) / n_bins],
                      "n": k, "mean_confidence": mean_c, "observed_rate": obs})

    return ({"reliability": reliability, "resolution": resolution,
             "uncertainty": base * (1 - base)}, table)


def run_stage4() -> None:
    with validator_owned("validator round artifacts"):
        contract = pgxlib.read_json(CONTRACT_FILE)
        stage12 = pgxlib.read_json(VALID_OUT_FILE)

        scoring = pgxlib.require(contract, "scoring", "contract.json")
        constants = pgxlib.require(scoring, "stage4", "contract.json:scoring")
        pheno_w = pgxlib.require(scoring, "PHENO_W", "contract.json:scoring")
        floor = constants["FLOOR"]

    if stage12["rejected"]:
        pgxlib.write_json(FINAL_REWARD_FILE, {
            "rejected": True, "rejection_reason": stage12["rejection_reason"],
            "raw_score": 0.0, "calibration_factor": 0.0, "final_reward": 0.0,
            "n_calibration_calls": 0, "brier_score": None,
            "empirical_exact_match_rate": None, "brier_skill_score": None,
            "reliability_bins": []})
        pgxlib.write_json("data/calibration_diagnostics.json", {"rejected": True})
        return

    # Fail loud on a missing, foreign or partial Stage 3.
    #
    # The identity check matters because Stages 3-5 write several artifacts to
    # hardcoded names in the working directory. Scoring two miners in one
    # directory would otherwise let miner A's Stage 2 be combined with miner
    # B's Stage 3 without complaint.
    #
    # Every raise below is a ValidatorFault: each one describes the state of
    # the validator's own working directory, which no submission can influence.
    # Charging the miner zero for it would bury a round-wide fault in one
    # miner's score.
    stage3_by_key: dict[tuple[str, str], dict] = {}
    stage3_path = pathlib.Path(STAGE3_RESULTS_FILE)
    if not stage3_path.exists():
        raise ValidatorFault(
            f"[stage4] {STAGE3_RESULTS_FILE} does not exist. Run "
            f"stage3_validator.py first. Refusing to score Stage 2 alone "
            f"and report the result as a round score — pass "
            f"--allow-missing-stage3 if that is genuinely what you want.")
    else:
        stage3 = pgxlib.read_json(stage3_path)
        if stage3.get("rejected"):
            raise ValidatorFault(
                f"[stage4] {STAGE3_RESULTS_FILE} is marked rejected but "
                f"{VALID_OUT_FILE} is not. These are outputs of different runs.")
        for field in ("round_id", "miner_uid"):
            if stage3.get(field) != stage12.get(field):
                raise ValidatorFault(
                    f"[stage4] {field} mismatch: {VALID_OUT_FILE} has "
                    f"{stage12.get(field)!r}, {STAGE3_RESULTS_FILE} has "
                    f"{stage3.get(field)!r}. These are outputs of different "
                    f"runs and must not be combined.")
        stage3_by_key = {(r["case_id"], r["gene"]): r for r in stage3["calls"]}
        stage12_keys = {(c["case_id"], c["gene"]) for c in stage12["calls"]}
        if set(stage3_by_key) != stage12_keys:
            missing = sorted(stage12_keys - set(stage3_by_key))[:5]
            extra = sorted(set(stage3_by_key) - stage12_keys)[:5]
            raise ValidatorFault(
                f"[stage4] Stage 3 does not cover the same calls as Stage 2. "
                f"missing={missing} extra={extra}. A partial Stage 3 would "
                f"score the uncovered calls at zero phenotype credit without "
                f"saying so.")

    # -- raw score ---------------------------------------------------------
    # Order-independent summation — see the note in stage12_validator.py.
    terms = []
    for call in sorted((c for c in stage12["calls"] if not c.get("is_probe")),
                       key=lambda c: (c["case_id"], c["gene"])):
        terms.append(call["stage2_credit"] * call["rarity_weight"]
                     * call["difficulty_weight"])
        s3 = stage3_by_key.get((call["case_id"], call["gene"]))
        if s3:
            # difficulty_weight applies to the Stage 2 term only: Stage 3 is a
            # table lookup whose difficulty does not scale with depth or
            # structural complexity, so weighting it by those would
            # double-count signal already captured upstream. rarity_weight
            # applies to both, because getting the CLINICAL answer right on a
            # rare genotype does matter more.
            terms.append(pheno_w * s3["stage3_credit"] * call["rarity_weight"])
    raw = math.fsum(terms)

    # -- calibration -------------------------------------------------------
    pairs = [(float(c["confidence"]), int(c["exact_match"]))
             for c in stage12["calls"] if not c.get("is_probe")
             if not c["no_call"] and isinstance(c["confidence"], (int, float))]

    n = len(pairs)
    warnings = []

    if n == 0:
        factor, brier, base, bss = floor, None, None, None
        warnings.append("no scored calls with a confidence; failing to the floor")
    else:
        brier = math.fsum((c - o) ** 2 for c, o in pairs) / n
        base = math.fsum(o for _, o in pairs) / n
        bs_ref = base * (1.0 - base)
        bss = 1.0 - brier / max(bs_ref, 1e-9)
        factor = constants["intercept"] + constants["slope"] * bss

        # Guards fail to the FLOOR, not to neutral. A guard that resolves an
        # unevaluable case to 1.0 lets a maliciously sparse submission claim a
        # maximum factor by being too small to evaluate — the failure mode this
        # floor exists to close. The only way to reach N < 30 in a 200-case round is to
        # no_call more than ~92% of calls, which is not an honest sparse
        # submission. A gate must not rely on a different gate to be safe.
        if n < constants["MIN_CALIBRATION_N"]:
            factor = floor
            warnings.append(
                f"N={n} < MIN_CALIBRATION_N={constants['MIN_CALIBRATION_N']}; "
                "failing to the floor, not to neutral")
        elif base == 1.0 and brier == 0.0:
            factor = 1.0
        elif base == 0.0:
            factor = floor
            warnings.append("empirical exact-match rate is 0; failing to the floor")
        elif math.isnan(factor):
            factor = floor
            warnings.append("calibration factor was NaN; failing to the floor")

        factor = min(max(factor, floor), 1.0)

    decomposition, reliability_table = (
        murphy_decomposition(pairs, N_RELIABILITY_BINS) if pairs else ({}, []))

    pgxlib.write_json("data/calibration_diagnostics.json", {
        "round_id": stage12["round_id"],
        "miner_uid": stage12["miner_uid"],
        "n_scored": n,
        "brier_score": brier,
        "empirical_exact_match_rate": base,
        "brier_skill_score": bss,
        "murphy_decomposition": decomposition,
        "reliability_table": reliability_table,
        "warnings": warnings,
    })

    pgxlib.write_json(FINAL_REWARD_FILE, {
        "rejected": False,
        "round_id": stage12["round_id"],
        "miner_uid": stage12["miner_uid"],
        "raw_score": raw,
        "calibration_factor": factor,
        "final_reward": raw * factor,
        "n_calibration_calls": n,
        "brier_score": brier,
        "empirical_exact_match_rate": base,
        "brier_skill_score": bss,
        "reliability_bins": reliability_table,
        "warnings": warnings,
    })
