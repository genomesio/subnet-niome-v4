from __future__ import annotations

import math

from niome_subnet.genomics import pgxlib
from niome_subnet.genomics.validation.errors import validator_owned
from niome_subnet.utils.settings import (
    CONTRACT_FILE,
    TRUTH_FILE,
    PGX_REFERENCE_DIR,
    MINER_SUBMISSION_FILE,
    INVALID_OUT_FILE,
    VALID_OUT_FILE,
)


WHOLE_SUBMISSION_REASONS = [
    "uid_binding_mismatch", "round_id_mismatch", "uid_mismatch",
    "submission_exceeds_max_cases", "unknown_case_id", "duplicate_case_id",
    "commit_mismatch",
]


def assert_private(contract: dict, truth: dict) -> None:
    """Fail loudly if pointed at redacted artifacts. Never default a secret."""
    pgxlib.require(contract, "master_seed", "contract.json")
    scoring = pgxlib.require(contract, "scoring", "contract.json")
    for key in ("partial_credit", "alt_discount", "rarity_weight",
                "difficulty_weight", "stage3", "stage4", "stage5", "PHENO_W"):
        pgxlib.require(scoring, key, "contract.json:scoring")
    for key in ("cases", "honeypot_case_ids", "bundle_assignments",
                "modal_diplotype_frequency"):
        pgxlib.require(truth, key, "truth.json")
    if not truth["bundle_assignments"]:
        raise KeyError(
            "truth.json carries no bundle_assignments. Run "
            "04_export_miner_bundles.py before scoring — without it, case "
            "membership cannot be checked and cross-bundle submissions pass.")


# ---------------------------------------------------------------------------
# Stage 1
# ---------------------------------------------------------------------------

def stage1_whole_submission(contract: dict, truth: dict, submission: dict,
                            commitment: str | None,
                            expected_uid: int | str) -> str | None:
    """Checked first, in order, before any per-call work. Any hit rejects the
    ENTIRE submission — not truncated, not partially scored."""
    # bundle_assignments is keyed by uid-as-string (see 04_export_miner_bundles:
    # a uid is a path component, a sha256_hex() part and a dict key, and truth
    # .json round-trips the key as a string). Normalise here so a submission
    # carrying the uid as a JSON number still matches; a missing field
    # stringifies to "None", which matches no assignment and rejects.
    miner_uid = str(submission.get("miner_uid"))

    # Checked BEFORE anything else, including round_id.
    #
    # `expected_uid` is the slot the submission was actually fetched from — the
    # S3 key `niome/{uid}.json`, writable only by the holder of that uid's
    # presigned PUT URL. Without this check the scored identity is whatever the
    # submission DECLARES, and the declared uid is just a number anyone can
    # write: miner A could claim uid B and be scored against B's bundle, so two
    # colluding miners could both bank one bundle's work. Every other Stage 1
    # reason below is about whether the submission is well-formed FOR a miner;
    # this one is about whether it is that miner's submission at all, so a
    # mismatch here makes the rest meaningless.
    #
    # `expected_uid` is a required parameter, not a defaulting one. An identity
    # check that silently no-ops when the caller forgets to pass it is the
    # `.get(secret, default)` failure mode this codebase refuses elsewhere —
    # see pgxlib.require().
    if miner_uid != str(expected_uid):
        return "uid_binding_mismatch"

    if submission.get("round_id") != contract["round_id"]:
        return "round_id_mismatch"

    assignments = truth["bundle_assignments"]
    if miner_uid not in assignments:
        return "uid_mismatch"

    if commitment is not None:
        recomputed = pgxlib.sha256_hex(
            pgxlib.canonical_payload(submission), miner_uid,
            submission["round_id"], submission.get("submission_nonce", ""))
        if recomputed != commitment:
            return "commit_mismatch"

    cases = submission.get("cases", [])
    if len(cases) > contract["rules"]["max_cases"]:
        return "submission_exceeds_max_cases"

    seen = set()
    issued = set(assignments[miner_uid])
    for case in cases:
        cid = case.get("case_id")
        if cid in seen:
            return "duplicate_case_id"
        seen.add(cid)
        if cid not in issued:
            return "unknown_case_id"
    return None


def stage1_gene_call(contract: dict, reference, gene: str, call: dict,
                     seen_pairs: set) -> str | None:
    rules = contract["rules"]

    if gene not in contract["active_genes"]:
        return "gene_not_active"

    if call.get("no_call"):
        if not rules["allow_no_call"]:
            return "no_call_not_permitted"
        return None

    diplotype = call.get("diplotype")
    try:
        haplotypes = pgxlib.parse_diplotype(diplotype)
    except (ValueError, TypeError):
        return "invalid_diplotype_syntax"

    known = reference.phenotype[gene].known_alleles()
    for hap in haplotypes:
        for unit in hap.split("+"):
            if pgxlib.core_allele(unit) not in known and unit not in known:
                return "unknown_allele"

    if call.get("total_copy_number") != pgxlib.implied_copy_number(diplotype):
        return "copy_number_inconsistent"

    if gene in rules["require_activity_score"] and call.get("activity_score") is None:
        return "missing_activity_score"

    if rules["require_confidence"]:
        c = call.get("confidence")
        if not isinstance(c, (int, float)) or not (0.0 <= c <= 1.0):
            return "invalid_confidence"

    vocabulary = {r["phenotype"] for r in
                  reference.phenotype[gene].lookup.values()}
    if call.get("phenotype") not in vocabulary:
        return "invalid_phenotype_term"

    alternatives = call.get("diplotype_alternatives") or []
    mass = sum(a.get("probability", 0.0) for a in alternatives)
    if mass + (call.get("confidence") or 0.0) > 1.0 + 1e-9:
        return "probability_mass_exceeded"
    for alt in alternatives:
        try:
            pgxlib.parse_diplotype(alt["diplotype"])
        except (ValueError, TypeError, KeyError):
            return "invalid_diplotype_syntax"

    return None


# ---------------------------------------------------------------------------
# Stage 2
# ---------------------------------------------------------------------------

def function_multiset(functions: dict, diplotype: str) -> tuple:
    return tuple(sorted(functions.get(pgxlib.core_allele(h), "Unknown function")
                        for h in pgxlib.parse_diplotype(diplotype)))


def core_multiset(diplotype: str) -> tuple:
    return tuple(sorted(pgxlib.core_allele(h)
                        for h in pgxlib.parse_diplotype(diplotype)))


def classify_outcome(credits: dict, functions: dict, truth_diplotype: str,
                     submitted: str, cn_informative: bool) -> tuple[str, float]:
    """Evaluated in descending credit order; first match wins."""
    t = pgxlib.normalise_diplotype(truth_diplotype)
    s = pgxlib.normalise_diplotype(submitted)

    if s == t:
        return "EXACT_MATCH", credits["EXACT_MATCH"]

    if core_multiset(s) == core_multiset(t):
        return "CORE_MATCH_SUBALLELE_OFF", credits["CORE_MATCH_SUBALLELE_OFF"]

    if function_multiset(functions, s) == function_multiset(functions, t):
        return "FUNCTION_CLASS_MATCH", credits["FUNCTION_CLASS_MATCH"]

    # CN_MATCH_ALLELE_WRONG is only meaningful when copy number can vary.
    # At the genotype_calls tier every diplotype has CN 2, so leaving this
    # class enabled would award 0.35 to EVERY wrong answer and make
    # ONE_HAPLOTYPE_CORRECT unreachable — a substantial free floor for modal
    # farming. Gated on the round declaring CN-bearing genes.
    if cn_informative and \
            pgxlib.implied_copy_number(s) == pgxlib.implied_copy_number(t):
        return "CN_MATCH_ALLELE_WRONG", credits["CN_MATCH_ALLELE_WRONG"]

    sh, th = list(pgxlib.parse_diplotype(s)), list(pgxlib.parse_diplotype(t))
    if set(sh) & set(th):
        return "ONE_HAPLOTYPE_CORRECT", credits["ONE_HAPLOTYPE_CORRECT"]

    return "NO_MATCH", credits["NO_MATCH"]


# ---------------------------------------------------------------------------

def run_stage12(expected_uid: int | str) -> None:
    """`expected_uid` is the metagraph uid whose S3 key this submission was
    downloaded from. It is what the submission's self-declared `miner_uid` is
    checked against — see stage1_whole_submission."""
    commitment = None

    # Validator-owned inputs. assert_private() belongs in here by its own
    # docstring — being pointed at the redacted public contract is a validator
    # misconfiguration, and charging the miner zero for it would hide a fault
    # that affects the whole round behind one miner's score.
    with validator_owned("validator round artifacts"):
        contract = pgxlib.read_json(CONTRACT_FILE)
        truth = pgxlib.read_json(TRUTH_FILE)
        reference = pgxlib.Reference(PGX_REFERENCE_DIR)
        assert_private(contract, truth)

        scoring = contract["scoring"]
        credits = scoring["partial_credit"]
        alt_discount = scoring["alt_discount"]
        cn_informative = bool(contract["rules"]["require_copy_number"])

    # Miner-owned. Absent, truncated or non-JSON is a zero score, not a fault.
    submission = pgxlib.read_json(MINER_SUBMISSION_FILE)

    valid: list[dict] = []
    invalid: list[dict] = []

    rejection = stage1_whole_submission(contract, truth, submission, commitment,
                                        expected_uid)
    miner_uid = str(submission.get("miner_uid"))
    # Probe membership is PER GENE CALL, not per case: a probe case draws
    # off-prior diplotypes only for the genes that can supply them in this
    # population, and any other gene in that case is an ordinary call.
    probes = set(pgxlib.require(truth, "honeypot_case_ids", "truth.json"))

    def is_probe_call(case_id: str, gene: str) -> bool:
        return bool(truth["cases"][case_id]["genes"][gene]["is_probe"])

    if rejection is not None:
        # Labelled with the uid being SCORED, not the one the submission
        # claims. On uid_binding_mismatch those differ, and writing the claimed
        # uid here would leave an artifact in miner A's slot that says it is
        # miner B's — the exact confusion the binding check exists to stop. The
        # claimed value is kept alongside for disclosure.
        scored_uid = str(expected_uid)
        issued = truth["bundle_assignments"].get(scored_uid, [])
        for case in submission.get("cases", []):
            for gene in case.get("gene_calls", {}):
                invalid.append({"case_id": case.get("case_id"), "gene": gene,
                                "reason": rejection, "scope": "submission"})
        pgxlib.write_json(VALID_OUT_FILE, {
            "round_id": submission.get("round_id"),
            "miner_uid": scored_uid,
            "claimed_miner_uid": miner_uid,
            "rejected": True, "rejection_reason": rejection,
            "issued_case_ids": issued, "calls": [],
        })
        pgxlib.write_json(INVALID_OUT_FILE, {
            "rejected": True, "rejection_reason": rejection, "calls": invalid})
        return

    issued = truth["bundle_assignments"][miner_uid]
    seen_pairs: set = set()

    for case in submission["cases"]:
        case_id = case["case_id"]
        truth_case = truth["cases"][case_id]
        for gene, call in sorted(case.get("gene_calls", {}).items()):
            if (case_id, gene) in seen_pairs:
                invalid.append({"case_id": case_id, "gene": gene,
                                "reason": "duplicate_gene_call", "scope": "call"})
                continue
            seen_pairs.add((case_id, gene))

            reason = stage1_gene_call(contract, reference, gene, call, seen_pairs)
            if reason is not None:
                invalid.append({"case_id": case_id, "gene": gene,
                                "reason": reason, "scope": "call"})
                continue

            gt = truth_case["genes"][gene]
            strata = {"rarity_band": gt["rarity_band"],
                      "sv_class": gt["sv_class"],
                      "depth_band": gt["depth_band"],
                      "ambiguity": gt["ambiguity"]}
            functions = reference.phenotype[gene].functions

            if call.get("no_call"):
                if strata["ambiguity"] == "ambiguous":
                    outcome, credit = "NO_CALL_AMBIGUOUS", credits["NO_CALL_AMBIGUOUS"]
                else:
                    outcome, credit = "NO_CALL_RESOLVABLE", credits["NO_CALL_RESOLVABLE"]
            else:
                outcome, credit = classify_outcome(
                    credits, functions, gt["diplotype"], call["diplotype"],
                    cn_informative)

                # Alternatives credit, discounted so that listing many
                # low-probability options never beats committing correctly.
                best_alt = 0.0
                for alt in call.get("diplotype_alternatives") or []:
                    _, base = classify_outcome(
                        credits, functions, gt["diplotype"], alt["diplotype"],
                        cn_informative)
                    best_alt = max(best_alt,
                                   base * alt.get("probability", 0.0) * alt_discount)
                if best_alt > credit:
                    outcome, credit = "ALTERNATIVE_MATCH", best_alt

            rw = pgxlib.rarity_weight(scoring["rarity_weight"],
                               gt["diplotype_frequency"],
                               truth["modal_diplotype_frequency"][gene])
            dw = pgxlib.difficulty_weight(scoring["difficulty_weight"], strata)

            valid.append({
                "case_id": case_id,
                "gene": gene,
                "outcome_class": outcome,
                "stage2_credit": credit,
                "rarity_weight": rw,
                "difficulty_weight": dw,
                "exact_match": outcome == "EXACT_MATCH",
                "no_call": bool(call.get("no_call")),
                "confidence": call.get("confidence"),
                "submitted_diplotype": call.get("diplotype"),
                "submitted_phenotype": call.get("phenotype"),
                "submitted_activity_score": call.get("activity_score"),
                "submitted_total_copy_number": call.get("total_copy_number"),
                "strata": strata,
                "is_probe": is_probe_call(case_id, gene),
            })

    # Cases in the bundle but absent from the submission are NOT rejections.
    # They score zero and still count in Stage 5 denominators — otherwise
    # omitting hard cases would hide them from the coverage gate.
    submitted_ids = {c["case_id"] for c in submission["cases"]}
    missing = sorted(set(issued) - submitted_ids)

    # Order-independent summation. Plain `sum` over a differently-ordered
    # list of the same floats returns a different last bit — that is what made
    # this file's output order-dependent on the first run of
    # tests/test_determinism.py, and with more than one validator it is a
    # consensus break. math.fsum is correctly rounded regardless of order, so
    # the class of bug is removed rather than papered over by sorting.
    valid.sort(key=lambda c: (c["case_id"], c["gene"]))
    # Integrity-probe calls are deliberately drawn off-population. They are
    # scored, reported and carried forward, but they do not enter stage2_raw:
    # a probe's true diplotype has no frequency in this population, so its
    # rarity_weight pins to the clip maximum and including it would both
    # inflate the score and gate it. Probes feed honeypot_integrity_factor at
    # Stage 5 and nothing else.
    raw = math.fsum(c["stage2_credit"] * c["rarity_weight"] * c["difficulty_weight"]
                    for c in valid if not c["is_probe"])

    pgxlib.write_json(VALID_OUT_FILE, {
        "round_id": submission["round_id"],
        "miner_uid": miner_uid,
        "rejected": False,
        "rejection_reason": None,
        "issued_case_ids": issued,
        "missing_case_ids": missing,
        "stage2_raw": raw,
        "calls": valid,
    })
    pgxlib.write_json(INVALID_OUT_FILE, {
        "rejected": False, "rejection_reason": None,
        "calls": sorted(invalid, key=lambda c: (str(c["case_id"]), c["gene"]))})
