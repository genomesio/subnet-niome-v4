from __future__ import annotations

import hashlib
import logging
import pathlib
import re
import subprocess
from niome_subnet.genomics import pgxlib

from niome_subnet.utils.settings import CPIC_ALLELE_REQUENCY_FILE, PGX_REFERENCE_DIR

logger = logging.getLogger(__name__)

GENES = ["CYP2C19", "TPMT", "CYP2C9", "SLCO1B1", "CYP3A5"]

PHARMCAT_REPO = "https://github.com/PharmGKB/PharmCAT.git"
PHARMCAT_RES = "src/main/resources/org/pharmgkb/pharmcat"

# The reference build is PINNED. These two constants move together, and the
# build refuses to emit a bundle if the commit's CPIC tables and the dated
# frequency snapshot disagree about the release. See ensure_pharmcat().
PHARMCAT_COMMIT = "66ea91d3bfe7f46e2116fdf7e55af37434df3164"
EXPECTED_CPIC_DATA_VERSION = "2026-07-13-11-40"

# Round drug set for the PoC: exactly one drug per gene, each exercising a
# different recommendation lookup-key shape.
#   clopidogrel  CYP2C19  keyed on phenotype term
#   azathioprine TPMT     keyed on phenotype term, JOINT with NUDT15
#   celecoxib    CYP2C9   keyed on ACTIVITY SCORE STRING
#   simvastatin  SLCO1B1  keyed on FUNCTION term
DRUGS = {
    "CYP2C19": "clopidogrel",
    "TPMT": "azathioprine",
    "CYP2C9": "celecoxib",
    "SLCO1B1": "simvastatin",
    "CYP3A5": "tacrolimus",
}

# CPIC publishes several recommendations per phenotype for some drugs, one per
# clinical sub-population. The round must declare which it scores against or
# the truth recommendation is ambiguous. Declared here, republished in the
# contract, and visible to miners.
DRUG_POPULATIONS = {
    "clopidogrel": "CVI ACS PCI",
    "azathioprine": "general",
    "celecoxib": None,        # single population
    "simvastatin": "general",
    "tacrolimus": "general",
}

# TPMT has no single-gene-resolvable CPIC drug: every thiopurine
# recommendation is keyed on TPMT x NUDT15 jointly. The round pins NUDT15 and
# publishes the assumption.
FIXED_GENE_ASSUMPTIONS = {"NUDT15": "Normal Metabolizer"}


# ---------------------------------------------------------------------------
# ACTION MAPPING — NIOME-AUTHORED. NOT A CPIC FIELD.
# ---------------------------------------------------------------------------
#
# The seven-term `action` vocabulary this subnet scores against does not
# exist in CPIC. CPIC publishes a recommendation strength classification
# (Strong/Moderate/Optional/No recommendation), three structured booleans
# (dosingInformation, alternateDrugAvailable, otherPrescribingGuidance) and
# prose. The enum has to be derived from those.
#
# This mapping is therefore an authored artifact and must be treated as one —
# a curated engineering derivation, not a published table. It is NOT guideline text
# and must never be presented as CPIC-derived. It is version-pinned, it is
# unit-tested, and every rule below was written against the actual recommendation
# prose for this round's drug set rather than invented.
#
# Design: classify on the LEADING directive of the recommendation text, then
# require CPIC's own booleans to corroborate. Leading-phrase-first matters —
# celecoxib at activity score 0.5 carries alternateDrugAvailable=True but its
# primary directive is a dose reduction, and a flags-first rule mislabels it.

ACTION_MAPPING_VERSION = "1.1.0"

LEADING_PHRASE_RULES = [
    # (regex against the first sentence, action)
    (r"^\s*no recommendation", "no_recommendation"),
    (r"^\s*avoid\b", "avoid"),
    (r"^\s*(consider|choose|use|prescribe|select)\s+(an?\s+)?alternative", "alternative_therapy"),
    (r"^\s*initiate therapy with (reduced|decreased|lowest|\d+\s*[-–]?\s*\d*\s*%)", "reduced_dose"),
    (r"^\s*(prescribe|limit dose to)\s*≤", "reduced_dose"),
    (r"^\s*consider a\s+\d+%?\s*reduction", "reduced_dose"),
    (r"^\s*initiate therapy with (the\s+)?(recommended|standard)", "standard_dose"),
    (r"^\s*(if considering .{0,60})?use .{0,30}at standard dose", "standard_dose"),
    (r"^\s*prescribe desired starting dose", "standard_dose"),
    (r"^\s*(consider a\s+)?(higher|increased) (starting )?dose", "increased_dose"),
    # CPIC writes the CYP3A5/tacrolimus guidance in the VERB form — "increase
    # starting dose 1.5 to 2 times..." — not the adjective form above. This is
    # the only place in the current gene set where a recommendation raises the
    # dose, because CYP3A5 is an expression gene: an expresser clears tacrolimus
    # faster and needs MORE, while a *3/*3 non-expresser takes the standard dose.
    (r"^\s*increase\s+(the\s+)?(starting\s+)?dose", "increased_dose"),
    (r"^\s*(utili[sz]e|use) therapeutic drug monitoring", "monitor"),
    (r"^\s*monitor\b", "monitor"),
]

# CPIC's own booleans must agree with the phrase classification. A mismatch
# means either CPIC changed its prose or a rule above is wrong; either way the
# build fails rather than emitting a quietly wrong action.
#
# WHICH DISTRIBUTION THESE BOOLEANS COME FROM MATTERS. They are read from
# PharmCAT's `prescribing_guidance.json`, where they are populated. The same
# fields in CPIC's REST API (`/v1/recommendation.alternatedrugavailable`,
# `.dosinginformation`) are false for every row in the table — verified
# 2026-08-21: `?alternatedrugavailable=is.true` and `?dosinginformation=is.true`
# both return []. A build re-pointed at the API would therefore fail
# corroboration on every avoid / alternative_therapy / reduced_dose
# classification, and the error would look like a bug in the rules above rather
# than an unpopulated upstream column. Do not "fix" that by relaxing the check.
ACTION_COROBORATION = {
    "no_recommendation": lambda c, d, a, o: c == "No recommendation",
    "avoid":             lambda c, d, a, o: a is True,
    "alternative_therapy": lambda c, d, a, o: a is True,
    "reduced_dose":      lambda c, d, a, o: d is True,
    "standard_dose":     lambda c, d, a, o: True,
    "increased_dose":    lambda c, d, a, o: d is True,
    "monitor":           lambda c, d, a, o: True,
}


def strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def first_sentence(text: str) -> str:
    m = re.split(r"(?<=[.!?])\s", text, maxsplit=1)
    return m[0] if m else text


def classify_action(text: str, classification: str, dosing: bool,
                    alternate: bool, other: bool) -> str:
    lead = first_sentence(strip_html(text)).lower()
    for pattern, action in LEADING_PHRASE_RULES:
        if re.search(pattern, lead):
            if not ACTION_COROBORATION[action](classification, dosing, alternate, other):
                raise AssertionError(
                    f"action mapping disagreement: prose classified as {action!r} "
                    f"but CPIC flags are classification={classification!r} "
                    f"dosing={dosing} alternate={alternate} other={other}. "
                    f"Text: {lead[:160]!r}. Fix action_mapping rules rather than "
                    "relaxing the corroboration check."
                )
            return action
    raise AssertionError(
        f"no action rule matched recommendation text: {lead[:200]!r}. "
        "Add an explicit rule; do not fall through to a default."
    )


# ---------------------------------------------------------------------------
# Source acquisition
# ---------------------------------------------------------------------------

def ensure_pharmcat(src: pathlib.Path | None, cache: pathlib.Path,
                    commit: str | None) -> pathlib.Path:
    """Fetch PharmCAT at an EXACT commit.

    This used to be `git clone --depth 1` of the default branch, recording
    whatever HEAD it happened to land on. That is not a pin: the same command
    run six months apart returns different CPIC tables, and because
    --frequencies is pinned to a dated snapshot file the build would then join
    NEW allele definitions against an OLD frequency table. Silently. That is
    the TPMT *3A namespace failure — a name join that drops *3A/*3B/*3C and
    leaves TPMT ~95% *1 with no non-normal metabolizers — and nothing about it
    crashes.

    GitHub permits fetching a bare SHA, so a shallow fetch of the pinned commit
    costs the same as the shallow clone did.
    """
    if src is not None:
        if not (src / PHARMCAT_RES).is_dir():
            raise ValueError(
                f"{src} does not look like a PharmCAT checkout")
        return src

    if (cache / PHARMCAT_RES).is_dir():
        have = git_commit(cache)
        if commit and have != commit:
            raise ValueError(
                f"[00] cached checkout at {cache} is at {have}, not the pinned "
                f"{commit}. Delete the cache, or pass --pharmcat-commit {have} "
                f"to build against what is there deliberately.")
        return cache

    cache.parent.mkdir(parents=True, exist_ok=True)
    if commit:
        logger.info(f"[00] fetching {PHARMCAT_REPO} @ {commit} -> {cache}")
        cache.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(cache), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(cache), "remote", "add", "origin",
                        PHARMCAT_REPO], check=True)
        subprocess.run(["git", "-C", str(cache), "fetch", "--depth", "1",
                        "origin", commit], check=True)
        subprocess.run(["git", "-C", str(cache), "checkout", "-q",
                        "FETCH_HEAD"], check=True)
    else:
        logger.info(f"[00] cloning {PHARMCAT_REPO} (UNPINNED — default branch HEAD) "
              f"-> {cache}")
        subprocess.run(["git", "clone", "--depth", "1", PHARMCAT_REPO,
                        str(cache)], check=True)
    return cache


def git_commit(repo: pathlib.Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True
                              ).stdout.strip()
    except Exception:
        return "unknown"


def file_sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def build_definition(res: pathlib.Path, gene: str) -> dict:
    raw = pgxlib.read_json(res / "definition/alleles" / f"{gene}_translation.json")
    variants = [{
        "position": v["position"],
        "rsid": v.get("rsid"),
        "ref": v["ref"],
        "alts": v.get("alts") or [],
        "hgvs": v.get("chromosomeHgvsName"),
    } for v in raw["variants"]]

    reference_allele = next(
        (a["name"] for a in raw["namedAlleles"] if a.get("reference")), None)
    if reference_allele is None:
        raise AssertionError(f"{gene}: no reference allele flagged in definitions")

    ref_row = next(a for a in raw["namedAlleles"] if a["name"] == reference_allele)
    reference_bases = list(ref_row["alleles"])
    if any(b is None for b in reference_bases):
        raise AssertionError(f"{gene}: reference allele has unspecified positions")

    named = {}
    for a in raw["namedAlleles"]:
        vec = list(a["alleles"])
        if len(vec) != len(variants):
            raise AssertionError(
                f"{gene}/{a['name']}: {len(vec)} bases for {len(variants)} variants")
        named[a["name"]] = vec

    return {
        "gene": gene,
        "chromosome": raw["chromosome"],
        "genome_build": raw["genomeBuild"],
        "source": raw["source"],
        "version": raw["version"],
        "reference_allele": reference_allele,
        "reference_bases": reference_bases,
        "variants": variants,
        "named_alleles": named,
    }


def build_phenotype(res: pathlib.Path, gene: str) -> dict:
    raw = pgxlib.read_json(res / "phenotype" / f"{gene}.json")
    lookup = {}
    for row in raw["diplotypes"]:
        dip = pgxlib.normalise_diplotype(row["diplotype"])
        # CYP2C9 carries the literal string "n/a" where a diplotype involves an
        # Uncertain/Unknown-function allele and no score can be computed. It is
        # a real CPIC value and also a real recommendation key, so it is kept
        # verbatim in lookup_key and represented as None numerically.
        score = row.get("activityScore")
        lookup[dip] = {
            "phenotype": row["generesult"],
            "activity_score": (float(score)
                               if score not in (None, "", "n/a") else None),
            "lookup_key": row["lookupkey"],
        }
    functions = {a["name"]: a["functionValue"] for a in raw["namedAlleles"]}
    activity_values = {
        a["name"]: (float(a["activityValue"])
                    if a.get("activityValue") not in (None, "", "n/a") else None)
        for a in raw["namedAlleles"]
    }
    return {
        "gene": gene,
        "version": raw["version"],
        "lookup": lookup,
        "functions": functions,
        "activity_values": activity_values,
    }


def build_recommendations(res: pathlib.Path, gene: str, drug: str) -> dict:
    raw = pgxlib.read_json(res / "reporter" / "prescribing_guidance.json")
    population = DRUG_POPULATIONS[drug]

    key_type = "activity_score" if gene == "CYP2C9" else "phenotype"
    entries: dict[str, dict] = {}

    for guideline in raw["guidelines"]:
        if guideline["guideline"]["source"] != "CPIC":
            continue
        for rec in guideline["recommendations"]:
            drugs = {c["name"] for c in (rec.get("relatedChemicals") or [])}
            if drug not in drugs:
                continue
            if population is not None and rec.get("population") != population:
                continue
            keys = [k for k in (rec.get("lookupKey") or []) if isinstance(k, dict)]
            if not keys:
                continue
            key_obj = keys[0]
            if gene not in key_obj:
                continue
            # Collapse joint keys against the round's declared assumptions.
            skip = False
            for other_gene, pinned in FIXED_GENE_ASSUMPTIONS.items():
                if other_gene in key_obj and key_obj[other_gene] != pinned:
                    skip = True
            if skip:
                continue
            extra = set(key_obj) - {gene} - set(FIXED_GENE_ASSUMPTIONS)
            if extra:
                raise AssertionError(
                    f"{drug}/{gene}: recommendation keyed on unresolved genes "
                    f"{sorted(extra)}. Either add a fixed assumption for them or "
                    "remove this drug from the round's drug set.")

            key = key_obj[gene]
            classification = (rec.get("classification") or {}).get(
                "term", "No recommendation")
            text = rec["text"]["html"]
            action = classify_action(
                text, classification,
                bool(rec["dosingInformation"]),
                bool(rec["alternateDrugAvailable"]),
                bool(rec["otherPrescribingGuidance"]))

            payload = {
                "action": action,
                "cpic_classification": classification,
                "recommendation_id": rec["id"],
                "text": strip_html(text),
            }
            if key in entries and entries[key]["recommendation_id"] != rec["id"]:
                if entries[key]["action"] != action or \
                   entries[key]["cpic_classification"] != classification:
                    raise AssertionError(
                        f"{drug}/{gene} key {key!r} resolves to two different "
                        f"recommendations ({entries[key]['recommendation_id']} vs "
                        f"{rec['id']}). Declare a narrower population in "
                        "DRUG_POPULATIONS.")
                continue
            entries[key] = payload

    if not entries:
        raise AssertionError(f"no CPIC recommendations found for {drug}/{gene}")

    return {
        "gene": gene,
        "lookup_key_type": key_type,
        "fixed_gene_assumptions": dict(FIXED_GENE_ASSUMPTIONS) if gene == "TPMT" else {},
        "drug_population": population,
        "drugs": {drug: entries},
    }


def build_frequencies(freq_cache: dict, gene: str, definition: dict,
                      phenotype: dict) -> dict:
    """Intersect the four allele namespaces and renormalise.

    An allele is sampleable only if it is simultaneously
      (a) function-assigned in the CPIC phenotype table and not
          Uncertain/Unknown function,
      (b) present in the allele definition table (so it can be rendered as
          genotype calls),
      (c) present in the population's frequency table with a non-null,
          strictly positive freq_weighted_avg.
    """
    defined = set(definition["named_alleles"])
    assigned = {a for a, f in phenotype["functions"].items()
                if f not in pgxlib.NON_ACTIONABLE_FUNCTIONS}
    in_phenotype = set(phenotype["functions"])

    out = {"gene": gene, "column": freq_cache["column"],
           "source": freq_cache["source"], "fetched": freq_cache["fetched"],
           "populations": {}}

    for population, table in freq_cache["frequencies"][gene].items():
        raw_named = set(table)
        candidates = {}
        excluded = {"no_definition": [], "no_function_assignment": [],
                    "null_frequency": [], "zero_frequency": [],
                    "not_in_frequency_table": []}
        for allele in sorted(in_phenotype | raw_named):
            if allele not in in_phenotype:
                excluded["no_function_assignment"].append(allele)
                continue
            if allele not in assigned:
                excluded["no_function_assignment"].append(allele)
                continue
            if allele not in defined:
                excluded["no_definition"].append(allele)
                continue
            if allele not in table:
                excluded["not_in_frequency_table"].append(allele)
                continue
            value = table[allele]
            if value is None:
                excluded["null_frequency"].append(allele)
                continue
            if value <= 0.0:
                excluded["zero_frequency"].append(allele)
                continue
            candidates[allele] = float(value)

        if not candidates:
            raise AssertionError(
                f"{gene}/{population}: no sampleable alleles after the "
                "namespace intersection. Almost always a name-join failure — "
                "check TPMT *3A/*3B/*3C against a bare *3.")

        total = sum(candidates.values())
        sampleable = {a: v / total for a, v in sorted(candidates.items())}

        out["populations"][population] = {
            "sampleable": sampleable,
            "raw": {a: table.get(a) for a in sorted(candidates)},
            "renormalisation_total": total,
            "excluded": {k: sorted(set(v)) for k, v in excluded.items()},
            "n_sampleable": len(sampleable),
        }
    return out


# ---------------------------------------------------------------------------

def build_reference() -> None:
    root = pathlib.Path(__file__).resolve().parent.parent
    out = pathlib.Path(PGX_REFERENCE_DIR)
    freq_path = pathlib.Path(CPIC_ALLELE_REQUENCY_FILE)

    src = ensure_pharmcat(None, root / ".cache" / "pharmcat", None)
    res = src / PHARMCAT_RES

    freq_cache = pgxlib.read_json(freq_path)
    logger.info(f"[00] frequency snapshot {freq_path.name} "
          f"({freq_cache['fetched']}, {freq_cache['column']})")

    counts = {}
    versions = set()

    for gene in GENES:
        definition = build_definition(res, gene)
        phenotype = build_phenotype(res, gene)
        recommendations = build_recommendations(res, gene, DRUGS[gene])
        frequencies = build_frequencies(freq_cache, gene, definition, phenotype)

        versions.add(definition["version"])
        versions.add(phenotype["version"])

        pgxlib.write_json(out / "allele_definitions" / f"{gene}.json", definition)
        pgxlib.write_json(out / "diplotype_phenotype" / f"{gene}.json", phenotype)
        pgxlib.write_json(out / "allele_functionality" / f"{gene}.json", {
            "gene": gene,
            "functions": phenotype["functions"],
            "activity_values": phenotype["activity_values"],
        })
        pgxlib.write_json(out / "recommendations" / f"{gene}.json", recommendations)
        pgxlib.write_json(out / "frequencies" / f"{gene}.json", frequencies)

        # Instantiate the per-gene loaders now so their assertions fire at
        # BUILD time, not at scoring time.
        loader = pgxlib.PHENOTYPE_LOADERS[gene](phenotype)
        rec_loader = pgxlib.RECOMMENDATION_LOADERS[gene](recommendations)

        intersection = set(definition["named_alleles"]) & set(phenotype["functions"])
        counts[gene] = {
            "definition_alleles": len(definition["named_alleles"]),
            "definition_variants": len(definition["variants"]),
            "phenotype_alleles": len(phenotype["functions"]),
            "phenotype_diplotypes": len(phenotype["lookup"]),
            "definition_intersect_phenotype": len(intersection),
            "function_assigned": len(loader.actionable_alleles()),
            "in_definition_not_phenotype":
                sorted(set(definition["named_alleles"]) - set(phenotype["functions"])),
            "in_phenotype_not_definition":
                sorted(set(phenotype["functions"]) - set(definition["named_alleles"])),
            "sampleable_by_population": {
                p: v["n_sampleable"]
                for p, v in frequencies["populations"].items()},
            "phenotype_vocabulary":
                sorted({r["phenotype"] for r in phenotype["lookup"].values()}),
            "reference_allele": definition["reference_allele"],
            "drug": DRUGS[gene],
            "recommendation_lookup_key_type": rec_loader.lookup_key_type,
            "recommendation_keys": sorted(recommendations["drugs"][DRUGS[gene]]),
        }
        logger.info(f"[00] {gene:8s} def={counts[gene]['definition_alleles']:>3} "
              f"pheno={counts[gene]['phenotype_alleles']:>3} "
              f"n_cap={counts[gene]['definition_intersect_phenotype']:>3} "
              f"sampleable={counts[gene]['sampleable_by_population']} "
              f"ref={definition['reference_allele']}")

    if len(versions) != 1:
        raise AssertionError(f"mixed CPIC data versions in the bundle: {versions}")
    cpic_version = versions.pop()

    # The frequency snapshot is a DATED FILE and the CPIC tables come from a
    # git checkout. If they drift apart, allele names drift with them and the
    # join silently drops alleles — TPMT *3A/*3B/*3C is the worked example, and
    # losing it leaves TPMT ~95% *1 with no non-normal metabolizers and nothing
    # to indicate anything went wrong. Refuse to emit a bundle whose two halves
    # describe different CPIC releases.
    freq_version = freq_cache.get("cpic_data_version")
    if freq_version != cpic_version:
        raise ValueError(
            f"[00] CPIC release mismatch: the PharmCAT tables are "
            f"{cpic_version!r} but the frequency snapshot {freq_path.name} is "
            f"{freq_version!r}. Joining them would silently mismatch allele "
            f"names. Refresh the frequency snapshot for {cpic_version}, or "
            f"build against the commit carrying {freq_version}.")
    if cpic_version != EXPECTED_CPIC_DATA_VERSION:
        raise ValueError(
            f"version {cpic_version!r}, expected "
            f"{EXPECTED_CPIC_DATA_VERSION!r}. Update both constants together, "
            f"or pass --unpin to advance the reference release deliberately.")

    pgxlib.write_json(out / "action_mapping.json", {
        "_PROVENANCE": (
            "NIOME-AUTHORED. This is NOT a CPIC field and must never be "
            "presented as guideline-derived. CPIC publishes a recommendation "
            "strength classification, three structured booleans and prose; the "
            "seven-term action vocabulary used by this subnet is derived from "
            "those here. Treat it as a curated engineering derivation, not as a "
            "published table."),
        "version": ACTION_MAPPING_VERSION,
        "vocabulary": pgxlib.ACTION_VOCABULARY,
        "classification_vocabulary": pgxlib.CLASSIFICATION_VOCABULARY,
        "method": "leading-directive phrase match, corroborated by CPIC booleans",
        "rules": [{"pattern": p, "action": a} for p, a in LEADING_PHRASE_RULES],
        "corroboration": {
            "no_recommendation": "cpic_classification == 'No recommendation'",
            "avoid": "alternateDrugAvailable is True",
            "alternative_therapy": "alternateDrugAvailable is True",
            "reduced_dose": "dosingInformation is True",
        },
    })

    # release.json's population list is DERIVED, not copied. A population that
    # appears for some genes and not others would make every downstream accessor
    # a KeyError waiting to happen, and the declared list in the snapshot cannot
    # be trusted to match what was actually built. Take the intersection across
    # genes and say loudly when it differs from what the snapshot declared.
    per_gene_populations = {
        gene: set(pgxlib.read_json(out / "frequencies" / f"{gene}.json")["populations"])
        for gene in GENES
    }
    snapshot_populations = sorted(set.intersection(*per_gene_populations.values()))
    declared = sorted(freq_cache["populations"])
    if snapshot_populations != declared:
        dropped = sorted(set(declared) - set(snapshot_populations))
        logger.info(f"[00] NOTE: the snapshot declares {declared} but only "
              f"{snapshot_populations} are present for every gene. Dropped from "
              f"release.json: {dropped}. Per-gene coverage: "
              + "; ".join(f"{g}={sorted(p)}" for g, p in sorted(per_gene_populations.items())))

    pgxlib.write_json(out / "release.json", {
        "genes": GENES,
        "drugs": DRUGS,
        "drug_populations": DRUG_POPULATIONS,
        "fixed_gene_assumptions": FIXED_GENE_ASSUMPTIONS,
        "cpic_data_version": cpic_version,
        "assembly": "GRCh38.p14",
        "pharmcat_commit": git_commit(src),
        "frequency_snapshot": {
            "file": freq_path.name,
            "sha256": file_sha256(freq_path),
            "source": freq_cache["source"],
            "column": freq_cache["column"],
            "fetched": freq_cache["fetched"],
            "populations": snapshot_populations,
            "declared_populations": freq_cache["populations"],
        },
        "action_mapping_version": ACTION_MAPPING_VERSION,
        "observation_tiers_supported": ["genotype_calls"],
    })

    pgxlib.write_json(out / "counts_manifest.json", {
        "_comment": (
            "Four allele namespaces disagree and the differences are load-bearing. "
            "'definition' = alleles with a defining-variant set. 'phenotype' = "
            "alleles with a CPIC function assignment. Their intersection caps what "
            "can be generated at the genotype_calls tier. 'sampleable' additionally "
            "requires a non-null positive frequency in that population. The "
            "widely-quoted per-gene figures (49/49/94/47 for CYP2C19/TPMT/CYP2C9/"
            "SLCO1B1) come from CPIC's /allele endpoint, which counts alleles with "
            "no function assignment, and are a fifth namespace again."),
        "cpic_data_version": cpic_version,
        "genes": counts,
    })

    logger.info(f"[00] wrote {out}/ at CPIC data version {cpic_version}")
