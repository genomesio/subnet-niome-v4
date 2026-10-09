from __future__ import annotations

import math

from niome_subnet.genomics import pgxlib
from niome_subnet.genomics.validation.errors import ValidatorFault, validator_owned
from niome_subnet.utils.settings import (
    CONTRACT_FILE, 
    TRUTH_FILE, 
    PGX_REFERENCE_DIR, 
    MINER_SUBMISSION_FILE, 
    STAGE3_RESULTS_FILE, 
    VALID_OUT_FILE, 
)


def build_adjacency(reference, contract) -> dict[str, dict[str, set[str]]]:
    """phenotype -> set of phenotypes sharing an action for some round drug."""
    adjacency: dict[str, dict[str, set[str]]] = {}
    for gene in contract["active_genes"]:
        by_action: dict[str, set[str]] = {}
        loader = reference.phenotype[gene]
        rec_loader = reference.recommendation[gene]
        # Map each PHENOTYPE to the action(s) it produces for this round's
        # drugs, going through the gene's own recommendation lookup key.
        pheno_to_key: dict[str, set[str]] = {}
        for row in loader.lookup.values():
            pheno_to_key.setdefault(row["phenotype"], set()).add(row["lookup_key"])

        for drug in rec_loader.drug_list():
            if drug not in contract["drug_set"]:
                continue
            for phenotype, keys in pheno_to_key.items():
                for key in keys:
                    entry = rec_loader.lookup(drug, key)
                    if entry is None:
                        continue
                    by_action.setdefault(f"{drug}|{entry['action']}",
                                         set()).add(phenotype)

        table: dict[str, set[str]] = {}
        for group in by_action.values():
            for phenotype in group:
                table.setdefault(phenotype, set()).update(group)
        for phenotype in table:
            table[phenotype].discard(phenotype)
        adjacency[gene] = table
    return adjacency


def direction_of(action: str, classes: dict) -> str:
    for name, actions in classes.items():
        if action in actions:
            return name
    return "none"


def recommendation_credit(constants: dict, classes: dict,
                          submitted: dict | None, truth: dict) -> tuple[float, str]:
    if submitted is None:
        # Omitting the recommendation entirely is NOT abstention.
        #
        # REC_ABSTAINED is defined as submitting `no_recommendation` where a
        # recommendation exists — a positive, legible act. This branch used to
        # return REC_ABSTAINED for a call carrying no recommendation at all,
        # which handed a miner that never populates the `recommendations`
        # block a free 0.4 x 0.10 of Stage 3 credit on every call for emitting
        # nothing. That is exactly the kind of floor rarity weighting and the
        # coverage gate exist to remove.
        return constants["REC_MISSING"], "REC_MISSING"

    s_action = submitted.get("action")
    s_class = submitted.get("cpic_classification")

    if s_action == "no_recommendation" and truth["action"] != "no_recommendation":
        return constants["REC_ABSTAINED"], "REC_ABSTAINED"

    if s_action == truth["action"]:
        if s_class == truth["cpic_classification"]:
            return constants["REC_EXACT"], "REC_EXACT"
        return constants["REC_ACTION_ONLY"], "REC_ACTION_ONLY"

    s_dir = direction_of(s_action, classes)
    t_dir = direction_of(truth["action"], classes)
    if s_dir == t_dir and s_dir != "none":
        return constants["REC_SAME_DIRECTION"], "REC_SAME_DIRECTION"
    if {s_dir, t_dir} == {"increase", "decrease"}:
        return constants["REC_OPPOSITE"], "REC_OPPOSITE"
    # REC_WRONG has its own constant rather than borrowing REC_OPPOSITE's.
    # Both are 0.00 today, but they are different findings — REC_OPPOSITE is
    # the patient-safety one and is logged separately — and a future change to
    # one must not silently move the other.
    return constants["REC_WRONG"], "REC_WRONG"


def run_stage3() -> None:
    with validator_owned("validator round artifacts"):
        contract = pgxlib.read_json(CONTRACT_FILE)
        truth = pgxlib.read_json(TRUTH_FILE)
        stage12 = pgxlib.read_json(VALID_OUT_FILE)
        reference = pgxlib.Reference(PGX_REFERENCE_DIR)

        scoring = pgxlib.require(contract, "scoring", "contract.json")
        constants = pgxlib.require(scoring, "stage3", "contract.json:scoring")
        classes = constants["direction_classes"]
        composition = constants["composition"]

    if stage12["rejected"]:
        pgxlib.write_json(STAGE3_RESULTS_FILE, {
            "rejected": True, "rejection_reason": stage12["rejection_reason"],
            "calls": [], "stage3_raw": 0.0})
        pgxlib.write_json("data/inconsistency_report.json", {"calls": []})
        pgxlib.write_json("data/inverted_recommendations.json", {"calls": []})
        return

    submitted_recs: dict[tuple[str, str], dict] = {}
    submission = pgxlib.read_json(MINER_SUBMISSION_FILE)
    # A ValidatorFault, not a rejection: Stage 2 derived VALID_OUT_FILE from
    # this same file moments ago, so a disagreement cannot be something the
    # miner did. It means stale state in the shared working directory.
    if str(submission.get("miner_uid")) != stage12["miner_uid"]:
        raise ValidatorFault(
            f"[stage3] {MINER_SUBMISSION_FILE} is from "
            f"{submission.get('miner_uid')!r} but {VALID_OUT_FILE} "
            f"scored {stage12['miner_uid']!r}. Different runs.")
    for case in submission["cases"]:
        for rec in case.get("recommendations", []):
            submitted_recs[(case["case_id"], rec["gene"])] = rec

    adjacency = build_adjacency(reference, contract)

    results, inconsistent, inverted = [], [], []

    for call in stage12["calls"]:
        case_id, gene = call["case_id"], call["gene"]
        gt = truth["cases"][case_id]["genes"][gene]
        loader = reference.phenotype[gene]

        if call["no_call"]:
            # Abstention is handled by Stage 2's NO_CALL_* credit and by Stage
            # 5's coverage gate. It produces no clinical answer, so Stage 3
            # scores it zero rather than excluding it.
            results.append({"case_id": case_id, "gene": gene,
                            "stage3_credit": 0.0, "phenotype_credit": 0.0,
                            "recommendation_credit": 0.0,
                            "phenotype_outcome": "NO_CALL",
                            "recommendation_outcome": "NO_CALL",
                            "internally_consistent": True,
                            "rarity_weight": call["rarity_weight"],
                            "is_probe": call.get("is_probe", False)})
            continue

        # -- component 3 first: it gates the other two -----------------------
        submitted_dip = call["submitted_diplotype"]
        derived_phenotype = loader.phenotype(submitted_dip)
        derived_score = loader.activity_score(submitted_dip)
        derived_cn = pgxlib.implied_copy_number(submitted_dip)

        reasons = []
        if call["submitted_phenotype"] != derived_phenotype:
            reasons.append(
                f"phenotype {call['submitted_phenotype']!r} does not follow "
                f"from {submitted_dip} (CPIC: {derived_phenotype!r})")
        if gene in contract["rules"]["require_activity_score"]:
            if call["submitted_activity_score"] != derived_score:
                reasons.append(
                    f"activity_score {call['submitted_activity_score']} does "
                    f"not follow from {submitted_dip} (CPIC: {derived_score})")
        if call["submitted_total_copy_number"] != derived_cn:
            reasons.append(
                f"total_copy_number {call['submitted_total_copy_number']} does "
                f"not follow from {submitted_dip} (implied: {derived_cn})")

        if reasons:
            inconsistent.append({"case_id": case_id, "gene": gene,
                                 "reasons": reasons,
                                 "submitted_diplotype": submitted_dip})
            results.append({"case_id": case_id, "gene": gene,
                            "stage3_credit": 0.0, "phenotype_credit": 0.0,
                            "recommendation_credit": 0.0,
                            "phenotype_outcome": "INTERNAL_INCONSISTENCY",
                            "recommendation_outcome": "INTERNAL_INCONSISTENCY",
                            "internally_consistent": False,
                            "rarity_weight": call["rarity_weight"],
                            "is_probe": call.get("is_probe", False)})
            continue

        # -- component 1: phenotype concordance ------------------------------
        true_phenotype = gt["phenotype"]
        submitted_phenotype = call["submitted_phenotype"]
        if submitted_phenotype == true_phenotype:
            p_credit, p_outcome = constants["PHENO_EXACT"], "PHENO_EXACT"
        elif submitted_phenotype in adjacency[gene].get(true_phenotype, set()):
            p_credit, p_outcome = constants["PHENO_ADJACENT"], "PHENO_ADJACENT"
        else:
            p_credit, p_outcome = constants["PHENO_WRONG"], "PHENO_WRONG"

        # -- component 2: prescribing concordance ----------------------------
        r_credit, r_outcome = recommendation_credit(
            constants, classes, submitted_recs.get((case_id, gene)),
            gt["recommendation"])
        if r_outcome == "REC_OPPOSITE":
            inverted.append({
                "case_id": case_id, "gene": gene,
                "submitted": submitted_recs.get((case_id, gene), {}).get("action"),
                "truth": gt["recommendation"]["action"]})

        credit = (composition["phenotype"] * p_credit
                  + composition["recommendation"] * r_credit)

        results.append({"case_id": case_id, "gene": gene,
                        "stage3_credit": credit,
                        "phenotype_credit": p_credit,
                        "recommendation_credit": r_credit,
                        "phenotype_outcome": p_outcome,
                        "recommendation_outcome": r_outcome,
                        "internally_consistent": True,
                        "rarity_weight": call["rarity_weight"],
                        "is_probe": call.get("is_probe", False)})

    # Order-independent summation — see the note in stage12_validator.py.
    results.sort(key=lambda r: (r["case_id"], r["gene"]))
    # Probe calls are excluded here for the same reason as at Stage 2: they are
    # off-population by construction and their rarity_weight is pinned at the
    # clip maximum. They are still scored and carried forward for Stage 5.
    raw = math.fsum(r["stage3_credit"] * r["rarity_weight"]
                    for r in results if not r.get("is_probe"))

    pgxlib.write_json(STAGE3_RESULTS_FILE, {
        "rejected": False,
        "round_id": stage12["round_id"],
        "miner_uid": stage12["miner_uid"],
        "stage3_raw": raw,
        "adjacency_is_round_dependent": True,
        "adjacency": {g: {p: sorted(v) for p, v in sorted(t.items())}
                      for g, t in sorted(adjacency.items())},
        "calls": results,
    })
    pgxlib.write_json("data/inconsistency_report.json", {
        "n_inconsistent": len(inconsistent),
        "calls": sorted(inconsistent, key=lambda r: (r["case_id"], r["gene"]))})
    pgxlib.write_json("data/inverted_recommendations.json", {
        "n_inverted": len(inverted),
        "calls": sorted(inverted, key=lambda r: (r["case_id"], r["gene"]))})
