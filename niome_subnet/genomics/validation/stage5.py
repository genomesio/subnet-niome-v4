from __future__ import annotations

import math

from niome_subnet.genomics import pgxlib
from niome_subnet.genomics.validation.errors import validator_owned
from niome_subnet.utils.settings import (
    CONTRACT_FILE,
    FINAL_REWARD_FILE,
    STAGE3_RESULTS_FILE,
    TRUTH_FILE,
    VALID_OUT_FILE,
)

STRATUM_AXES = ["rarity_band", "sv_class", "depth_band"]


def stratum_key(strata: dict, wildcards: frozenset = frozenset()) -> tuple:
    return tuple("*" if axis in wildcards else strata[axis] for axis in STRATUM_AXES)


def merge_small_strata(members: dict[tuple, list], min_n: int,
                       merge_order: list[str]) -> tuple[dict[tuple, list], list]:
    """Deterministic merge: a DEFICIENT stratum is merged into its neighbours
    along one axis at a time, in the declared order.

    The merge is LOCAL. Only a neighbourhood containing a deficient stratum
    collapses; a neighbourhood whose members are all at or above min_n keeps
    its full key. A neighbourhood is the set of strata that agree on every axis
    except the one being relaxed, so "merge into the nearest neighbour along
    the depth axis" pools exactly the depth bands of one (rarity, sv) cell and
    leaves every other cell alone.

    The previous implementation (stage5_validator_legacy.py) relaxed an axis
    GLOBALLY as soon as any single stratum was deficient, which erased that
    axis for the whole submission. At v2 that collapsed 12 strata to 4 and left
    stratum_coverage_factor a geometric mean over rarity bands alone, with
    depth band and structural-variant class contributing nothing at all. That
    is not the documented behaviour and it silently removed two thirds of the
    coverage gate.

    Determinism: neighbourhoods are visited in sorted key order and pooled rows
    are concatenated in sorted member-key order, so the result depends only on
    the set of strata, never on dict insertion order. Ties are impossible
    because the axis order is fixed and every stratum belongs to exactly one
    neighbourhood per pass.

    Strata still below min_n after every axis has been relaxed are kept as they
    are and reported in the log. Clipping an under-powered stratum is safer
    than pooling unrelated difficulty classes to manufacture a denominator.
    """
    log = []
    current = dict(members)

    for axis in merge_order:
        if len(current) <= 1 or all(len(v) >= min_n for v in current.values()):
            break
        idx = STRATUM_AXES.index(axis)

        neighbourhoods: dict[tuple, list[tuple]] = {}
        for key in sorted(current):
            relaxed = tuple("*" if i == idx else k for i, k in enumerate(key))
            neighbourhoods.setdefault(relaxed, []).append(key)

        rebuilt: dict[tuple, list] = {}
        n_collapsed = 0
        for relaxed, group in sorted(neighbourhoods.items()):
            deficient = any(len(current[k]) < min_n for k in group)
            if deficient and len(group) > 1:
                rows: list = []
                for k in sorted(group):
                    rows.extend(current[k])
                rebuilt[relaxed] = rows
                n_collapsed += len(group)
            else:
                # Either healthy, or a lone stratum that relaxing this axis
                # cannot enlarge. Leave it for the next axis in the order.
                for k in group:
                    rebuilt[k] = current[k]

        log.append({"relaxed_axis": axis,
                    "strata_before": len(current),
                    "strata_after": len(rebuilt),
                    "strata_collapsed": n_collapsed})
        current = rebuilt

    residual = sorted("|".join(k) for k, v in current.items() if len(v) < min_n)
    if residual:
        log.append({"residual_below_min_n": residual,
                    "note": "every axis in merge_order relaxed; these strata "
                            "stay as they are and are clipped like any other"})

    return current, log


def run_stage5():
    with validator_owned("validator round artifacts"):
        contract = pgxlib.read_json(CONTRACT_FILE)
        truth = pgxlib.read_json(TRUTH_FILE)
        stage12 = pgxlib.read_json(VALID_OUT_FILE)
        stage3 = pgxlib.read_json(STAGE3_RESULTS_FILE)
        reward = pgxlib.read_json(FINAL_REWARD_FILE)

        scoring = pgxlib.require(contract, "scoring", "contract.json")
        constants = pgxlib.require(scoring, "stage5", "contract.json:scoring")
        pheno_w = pgxlib.require(scoring, "PHENO_W", "contract.json:scoring")
        # Probe membership is per GENE CALL — see stage12_validator. The
        # case-level list is kept for disclosure and redaction only.
        honeypots = set(pgxlib.require(truth, "honeypot_case_ids", "truth.json"))

    if stage12["rejected"]:
        return {
            "breakdown": {
                "rejected": True, 
                "rejection_reason": stage12["rejection_reason"],
                "raw_score": 0.0, 
                "calibration_factor": 0.0, 
                "stratum_coverage_factor": 0.0, 
                "honeypot_integrity_factor": 0.0,
                "final_reward": 0.0, 
                "achievable_max": 0.0, 
                "raw_score_fraction": 0.0, "score_fraction": 0.0, 
            },
            "final_score": 0.0, 
        }

    miner_uid = stage12["miner_uid"]
    issued = truth["bundle_assignments"][miner_uid]
    genes = contract["active_genes"]

    scored = {(c["case_id"], c["gene"]): c for c in stage12["calls"]}
    s3 = {(c["case_id"], c["gene"]): c for c in stage3["calls"]}

    # Denominator is every (case, gene) the miner was ISSUED. Anything not
    # scored — omitted, or rejected at Stage 1 — enters its stratum as 0.
    members: dict[tuple, list] = {}
    per_call = []
    for case_id in issued:
        for gene in genes:
            gt = truth["cases"][case_id]["genes"][gene]
            strata = {"rarity_band": gt["rarity_band"],
                      "sv_class": gt["sv_class"],
                      "depth_band": gt["depth_band"]}
            call = scored.get((case_id, gene))
            if call is None:
                norm = 0.0
                status = "missing_or_rejected"
            else:
                credit3 = s3.get((case_id, gene), {}).get("stage3_credit", 0.0)
                norm = (call["stage2_credit"] + pheno_w * credit3) / (1.0 + pheno_w)
                status = "scored"
            row = {"case_id": case_id, "gene": gene, "norm_score": norm,
                   "status": status, "strata": strata,
                   "is_honeypot": bool(gt["is_probe"])}
            per_call.append(row)
            # Integrity probes are off-population by construction and do not
            # belong to the round's difficulty design, so they are kept out of
            # the coverage strata. They enter honeypot_integrity_factor below
            # and nothing else.
            if not row["is_honeypot"]:
                members.setdefault(stratum_key(strata), []).append(row)

    merged, merge_log = merge_small_strata(
        members, constants["MIN_STRATUM_N"], constants["merge_order"])

    floor = constants["STRATUM_FLOOR"]
    stratum_means = {}
    log_terms = []
    for key, rows in sorted(merged.items()):
        # Order-independent summation — see stage12_validator.py.
        mean = math.fsum(r["norm_score"] for r in sorted(
            rows, key=lambda r: (r["case_id"], r["gene"]))) / len(rows)
        clipped = min(max(mean, floor), 1.0)
        stratum_means["|".join(key)] = {"n": len(rows), "mean": mean,
                                        "clipped": clipped}
        log_terms.append(math.log(clipped))
    coverage = math.exp(math.fsum(log_terms) / len(merged)) if merged else floor

    # -- honeypot integrity -------------------------------------------------
    honeypot_rows = [r for r in per_call if r["is_honeypot"]]
    ordinary_rows = [r for r in per_call if not r["is_honeypot"]]
    honeypot_warning = None
    matched_mean = baseline_mean = None
    if len(honeypot_rows) < constants["MIN_HONEYPOT_N"]:
        honeypot_factor = 1.0
        honeypot_warning = (
            f"only {len(honeypot_rows)} integrity-probe calls < "
            f"MIN_HONEYPOT_N={constants['MIN_HONEYPOT_N']}; factor defaults to "
            "1.0, so this submission is scored with NO integrity gate. Check "
            "that 02 generated probes and that 04 assigned probes_per_bundle "
            "of them to this miner.")
        h_mean = s_mean = None
    elif not ordinary_rows:
        # No non-probe issued calls at all: there is no baseline to compare
        # against and the ratio is undefined. Refuse rather than divide.
        honeypot_factor = 1.0
        honeypot_warning = (
            "the miner was issued NO non-probe calls, so the integrity "
            "denominator is undefined and the factor defaults to 1.0. This "
            "submission is scored with NO integrity gate. A bundle of probes "
            "only is a bundling bug in 04.")
        h_mean = math.fsum(r["norm_score"] for r in honeypot_rows) / len(honeypot_rows)
        s_mean = None
    else:
        # Denominator = the miner's own score on ORDINARY calls. Two candidates:
        #
        #   matched  — ordinary calls sharing a probe's (rarity, sv, depth)
        #              stratum. Difficulty-faithful, but small: probes always
        #              band `rare` with `sv_class = absent`, so this set is
        #              typically ~5% of the bundle (measured 8-13 calls at
        #              n_cases = 50).
        #   baseline — every ordinary issued call.
        #
        # The denominator is the miner's OWN work on a set the miner can
        # identify without any secret: rarity_band follows from the population
        # frequency table shipped in the bundle, depth_band from the visible
        # no-call fraction, sv_class from the record. A miner restricted to the
        # generator's support can therefore raise this ratio by deliberately
        # failing the matched calls until the denominator collapses. Flooring
        # the denominator at `baseline` removes that lever: moving the ratio
        # now requires degrading the WHOLE submission, which costs more through
        # raw_score and stratum_coverage_factor than the gate is worth.
        #
        # `max` and not a plain switch to `baseline`: where the miner genuinely
        # scores higher in the probes' strata than overall, the matched figure
        # is the fairer comparison and is kept. The floor only ever binds
        # downward, which is the direction sabotage pushes.
        matched_strata = {stratum_key(r["strata"]) for r in honeypot_rows}
        matched = [r for r in ordinary_rows
                   if stratum_key(r["strata"]) in matched_strata]
        h_mean = math.fsum(r["norm_score"] for r in honeypot_rows) / len(honeypot_rows)
        matched_mean = (math.fsum(r["norm_score"] for r in matched) / len(matched)
                        if matched else 0.0)
        baseline_mean = (math.fsum(r["norm_score"] for r in ordinary_rows)
                         / len(ordinary_rows))
        s_mean = max(matched_mean, baseline_mean)
        honeypot_factor = min(max(h_mean / max(s_mean, 1e-9),
                                  constants["HONEYPOT_FLOOR"]), 1.0)
        if not matched:
            honeypot_warning = (
                "no ordinary call shares a stratum with any integrity probe, "
                "so the difficulty-matched comparison is empty and the factor "
                f"rests on the {len(ordinary_rows)}-call whole-submission "
                "baseline alone. The gate still applies; the difficulty "
                "matching does not.")

    # -- achievable ceiling -------------------------------------------------
    # What this round's issued, non-probe calls would be worth if every one of
    # them scored EXACT_MATCH with full Stage 3 credit, at the weights this
    # round actually drew. `final_score` is a SUM: it scales with n_cases and
    # with the tier's difficulty ceiling, so it is not comparable across rounds.
    # `score_fraction` is.
    scoring_rw = pgxlib.require(scoring, "rarity_weight", "contract.json:scoring")
    scoring_dw = pgxlib.require(scoring, "difficulty_weight", "contract.json:scoring")
    achievable_terms = []
    for case_id in issued:
        for gene in genes:
            gt = truth["cases"][case_id]["genes"][gene]
            if gt["is_probe"]:
                continue
            rw = pgxlib.rarity_weight(scoring_rw, gt["diplotype_frequency"],
                                      truth["modal_diplotype_frequency"][gene])
            dw = pgxlib.difficulty_weight(scoring_dw, {
                "sv_class": gt["sv_class"], "depth_band": gt["depth_band"],
                "ambiguity": gt["ambiguity"]})
            achievable_terms.append(rw * dw)
            achievable_terms.append(pheno_w * rw)
    achievable_max = math.fsum(sorted(achievable_terms))

    final_score = reward["final_reward"] * coverage * honeypot_factor
    score_fraction = (final_score / achievable_max) if achievable_max > 0 else 0.0
    # Before the multiplicative gates: how much of the round's available credit
    # the miner actually earned on accuracy alone. `score_fraction` is that same
    # figure after calibration, coverage and integrity are applied.
    raw_score_fraction = ((reward["raw_score"] / achievable_max)
                          if achievable_max > 0 else 0.0)

    pgxlib.write_json("stage5_summary.json", {
        "round_id": stage12["round_id"],
        "miner_uid": miner_uid,
        "n_issued_calls": len(per_call),
        "n_scored": sum(r["status"] == "scored" for r in per_call),
        "n_missing_or_rejected": sum(r["status"] != "scored" for r in per_call),
        "stratum_means": stratum_means,
        "merge_log": merge_log,
        "stratum_coverage_factor": coverage,
        "honeypot": {
            "kind": truth.get("honeypot_kind", "none"),
            "n_honeypot_calls": len(honeypot_rows),
            "n_ordinary_calls": len(ordinary_rows),
            "honeypot_mean": h_mean,
            "matched_synthetic_mean": matched_mean,
            "baseline_mean": baseline_mean,
            "denominator": s_mean,
            "factor": honeypot_factor,
            "warning": honeypot_warning,
        },
    })

    return {
        "breakdown": {
            "rejected": False,
            "round_id": stage12["round_id"],
            "miner_uid": miner_uid,
            "raw_score": reward["raw_score"],
            "calibration_factor": reward["calibration_factor"],
            "final_reward": reward["final_reward"],
            "stratum_coverage_factor": coverage,
            "honeypot_integrity_factor": honeypot_factor,
            "achievable_max": achievable_max,
            "raw_score_fraction": raw_score_fraction,
            "score_fraction": score_fraction,
        }, 
        "final_score": final_score, 
    }
