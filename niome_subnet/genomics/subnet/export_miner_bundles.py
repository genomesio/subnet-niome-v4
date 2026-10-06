from __future__ import annotations

import logging
import pathlib
import shutil
import subprocess
from niome_subnet.genomics import pgxlib

from niome_subnet.utils.settings import (
    AWS_S3_BUCKET,
    BUNDLES_DIR,
    CASES_DIR,
    CONTRACT_FILE,
    TRUTH_FILE,
    bundle_key,
)

logger = logging.getLogger(__name__)

# Everything a miner needs to build a VALID submission, and nothing that helps
# build a CORRECT one. Adding a key to the contract does not add it here.
PUBLIC_CONTRACT_KEYS = [
    "round_id",
    "seed_commitment",
    "active_genes",
    "population",
    "drug_set",
    "drug_genes",
    "drug_populations",
    "observation_tier",
    "reference_release",
    "rules",
    "commit_reveal",
]

# Never allowed to appear in any miner-facing artifact. Mirrored by
# tests/test_redaction.py, which greps rather than trusting this list.
SECRET_KEYS = [
    "master_seed",
    "scoring",
    "stratum_design",
    "partial_credit",
    "rarity_weight",
    "difficulty_weight",
    "alt_discount",
    "honeypot_case_ids",
    "honeypot_kind",
    "is_probe",
    "probe_design",
    "population_admissibility",
    "bundle_assignments",
    "rarity_band",
    "sv_class",
    "depth_band",
    "ambiguity",
    "consistent_diplotypes",
    "diplotype_frequency",
    "recommendation_key",
    "modal_diplotypes",
    "strata_counts",
    "target_rarity_band",
    "driver_gene",
    "PHENO_W",
    "STRATUM_FLOOR",
    "HONEYPOT_FLOOR",
    "MIN_CALIBRATION_N",
]


def build_public_contract(contract: dict) -> dict:
    missing = [k for k in PUBLIC_CONTRACT_KEYS if k not in contract]
    if missing:
        raise AssertionError(f"contract is missing public keys {missing}")
    public = {k: contract[k] for k in PUBLIC_CONTRACT_KEYS}
    leaked = sorted(set(SECRET_KEYS) & set(_all_keys(public)))
    if leaked:
        raise AssertionError(
            f"public contract still carries secret keys {leaked} — a secret has "
            "been nested inside an allowlisted key.")
    return public


def _all_keys(obj, acc=None) -> set:
    acc = set() if acc is None else acc
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(k)
            _all_keys(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _all_keys(v, acc)
    return acc


def export_miner_bundles(miner_uids, s3_client) -> None:
    contract = pgxlib.read_json(CONTRACT_FILE)
    truth = pgxlib.read_json(TRUTH_FILE)
    cases_dir = pathlib.Path(CASES_DIR)
    out = pathlib.Path(BUNDLES_DIR)

    master_seed = pgxlib.require(contract, "master_seed", "contract.json")
    round_id = contract["round_id"]
    n_cases = contract["rules"]["n_cases"]

    if len(miner_uids) == 0:
        raise ValueError("metagraph carries no miners (every neuron has vtrust > 0)")
    # get_miner_uids() returns a numpy array of int64. Normalise to str once,
    # here, because downstream a uid is a path component, a sha256_hex() part, a
    # dict key and a subprocess argument -- and int64 is accepted by none of
    # them. truth.json round-trips these keys as strings anyway, which is what
    # the validation stages index bundle_assignments by.
    miner_uids = [str(uid) for uid in miner_uids]
    # Metagraph miner_uids are unique per uid, so a duplicate is not a bad round --
    # it means the roster was built wrong.
    if len(miner_uids) != len(set(miner_uids)):
        raise AssertionError("duplicate miner_uids in the metagraph miner set")

    pool = sorted(truth["cases"])
    if len(pool) < len(miner_uids) * n_cases:
        raise ValueError(f"case pool of {len(pool)} is too small for {len(miner_uids)} "
                         f"miners x {n_cases} cases. Raise --pool-multiplier in 02.")

    probes = set(pgxlib.require(truth, "honeypot_case_ids", "truth.json"))
    probe_design = pgxlib.require(contract, "probe_design", "contract.json")
    n_probe_per_bundle = probe_design["probes_per_bundle"]
    if len(probes) < len(miner_uids) * n_probe_per_bundle:
        raise ValueError(f"pool carries {len(probes)} integrity probes, too few "
                         f"for {len(miner_uids)} miners x {n_probe_per_bundle}. Raise "
                         f"probe_fraction in 01 or pool_multiplier in 02.")

    # Round-level shuffle: a KEYED SORT, not an RNG shuffle.
    #
    # This used `random.Random(seed).shuffle(...)`, whose output depends on
    # CPython's Mersenne Twister and on the exact shuffle algorithm in the
    # interpreter running it. 02_generate_population.py deliberately restricts
    # itself to rng.random() with an inverse-CDF draw for exactly that reason,
    # and this script was breaking its own project's rule.
    #
    # Bundle assignment is consensus-critical: it decides which case_ids are
    # `unknown_case_id` for a given miner. Two validators on different Python
    # implementations must derive identical bundles or honest submissions get
    # rejected. Sorting on a per-case digest depends on nothing but SHA-256 and
    # is stable across interpreters, versions and platforms.
    def shuffle_key(cid: str) -> str:
        return pgxlib.sha256_hex(master_seed, round_id, "pool_shuffle", cid)

    # Probes and ordinary cases are partitioned SEPARATELY so that every bundle
    # carries exactly `probes_per_bundle` probes. Slicing one combined pool
    # would leave the probe count per miner binomial, and a miner who happened
    # to draw fewer than MIN_HONEYPOT_N probe calls would silently be scored
    # with the integrity factor defaulted to 1.0 — the gate would be live for
    # some miners and inert for others in the same round.
    probe_pool = sorted((c for c in pool if c in probes), key=shuffle_key)
    plain_pool = sorted((c for c in pool if c not in probes), key=shuffle_key)
    n_plain_per_bundle = n_cases - n_probe_per_bundle
    if n_plain_per_bundle < 0:
        raise ValueError(f"probes_per_bundle={n_probe_per_bundle} exceeds "
                         f"n_cases={n_cases}")
    if len(plain_pool) < len(miner_uids) * n_plain_per_bundle:
        raise ValueError(f"pool carries {len(plain_pool)} non-probe cases, too few "
                         f"for {len(miner_uids)} miners x {n_plain_per_bundle}.")

    miner_seeds = {h: pgxlib.sha256_hex(master_seed, round_id, h) for h in miner_uids}
    ranked = sorted(miner_uids, key=lambda h: miner_seeds[h])

    public_contract = build_public_contract(contract)
    assignments: dict[str, list[str]] = {}

    if out.exists():
        shutil.rmtree(out)

    bundle_dir = pathlib.Path(BUNDLES_DIR)

    for rank, uid in enumerate(ranked):
        # Sorting the union by case_id puts probes in ordinary sequence inside
        # the bundle, so their position carries no signal.
        assigned = sorted(
            plain_pool[rank * n_plain_per_bundle:(rank + 1) * n_plain_per_bundle]
            + probe_pool[rank * n_probe_per_bundle:(rank + 1) * n_probe_per_bundle])
        assignments[uid] = assigned

        bundle = out / uid
        (bundle / "cases").mkdir(parents=True, exist_ok=True)
        for case_id in assigned:
            shutil.copytree(cases_dir / case_id, bundle / "cases" / case_id)

        pgxlib.write_json(bundle / "public_contract.json", public_contract)
        pgxlib.write_json(bundle / "bundle_manifest.json", {
            "round_id": round_id,
            "miner_uid": uid,
            "miner_seed": miner_seeds[uid],
            "n_cases": len(assigned),
            "case_ids": assigned,
        })

        compressed = str(out / f"bundle.tar.gz")
        subprocess.run(["tar", "-czf", compressed, "-C", str(out), uid],
                       check=True)

        s3_client.upload_file(
            Filename=compressed,
            Bucket=AWS_S3_BUCKET,
            Key=bundle_key(uid),
        )

    overlap = set()
    seen: set[str] = set()
    for ids in assignments.values():
        overlap |= seen & set(ids)
        seen |= set(ids)
    if overlap:
        raise AssertionError(f"bundles overlap on {sorted(overlap)[:5]} — "
                             "per-miner instancing is broken")

    truth["bundle_assignments"] = assignments
    pgxlib.write_json(TRUTH_FILE, truth)

    logger.info(f"[04] {out}/: {len(miner_uids)} disjoint bundles, "
          f"{len(seen)}/{len(pool)} pool cases used")
    logger.info(f"[04] public_contract.json built from a {len(PUBLIC_CONTRACT_KEYS)}-key "
          f"allowlist; {len(SECRET_KEYS)} secret key names checked")
