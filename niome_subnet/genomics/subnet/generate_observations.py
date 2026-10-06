from __future__ import annotations

import logging
import pathlib
from niome_subnet.genomics import pgxlib

from niome_subnet.utils.settings import CASES_DIR, CONTRACT_FILE, PGX_REFERENCE_DIR, TRUTH_FILE

logger = logging.getLogger(__name__)

def generate_observations() -> None:
    contract = pgxlib.read_json(CONTRACT_FILE)
    truth = pgxlib.read_json(TRUTH_FILE)
    reference = pgxlib.Reference(PGX_REFERENCE_DIR)
    out = pathlib.Path(CASES_DIR)

    if contract["observation_tier"] != "genotype_calls":
        raise ValueError(f"observation_tier {contract['observation_tier']!r} is "
                         "not implemented in this PoC; only genotype_calls is.")
    if truth["round_id"] != contract["round_id"]:
        raise ValueError("truth.json and contract.json are from different rounds")

    genes = contract["active_genes"]
    build = reference.release["assembly"]
    n_positions = 0

    for case_id, case in sorted(truth["cases"].items()):
        case_dir = out / case_id
        blocks = {}
        for gene in genes:
            blocks[gene] = {
                "chromosome": reference.definitions[gene]["chromosome"],
                "calls": case["genes"][gene]["calls"],
            }
            n_positions += len(blocks[gene]["calls"])

        vcf = pgxlib.render_vcf(case_id, blocks, build)
        (case_dir / "observations").mkdir(parents=True, exist_ok=True)
        (case_dir / "observations" / "calls.vcf").write_text(vcf, encoding="utf-8")

        pgxlib.write_json(case_dir / "clinical_context.json",
                          case["clinical_context"])

        # The manifest is MINER-FACING. It must carry nothing that reveals the
        # answer: no diplotype, no phenotype, no stratum membership, no
        # no-call count that would let a miner infer difficulty.
        pgxlib.write_json(case_dir / "manifest.json", {
            "case_id": case_id,
            "round_id": contract["round_id"],
            "observation_tier": contract["observation_tier"],
            "genes": genes,
            "loci": {g: {"chromosome": reference.definitions[g]["chromosome"],
                         "n_defining_positions":
                             len(reference.definitions[g]["variants"])}
                     for g in genes},
            "files": {"observations": "observations/calls.vcf",
                      "clinical_context": "clinical_context.json"},
        })

    logger.info(f"[03] {out}/: {len(truth['cases'])} cases, "
          f"{n_positions} genotype records, tier=genotype_calls")
