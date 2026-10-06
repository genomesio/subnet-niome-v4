"""
pgxlib.py — the one shared module in the PoC.

Everything in here is READ-ONLY reference logic plus pure helpers. It contains no
randomness, no I/O side effects beyond reading files, and no scoring constants.

Why a shared module rather than three standalone script trees: the per-gene
phenotype and recommendation loaders must exist exactly once. Triplicating them
across subnet/, miner/ and validator/ is precisely the way three copies drift and
one of them silently mislabels a gene.

Scoring constants are NOT here, and they are not in the validator stages either —
they live in `contract.json`, written by 01_generate_contract.py and read back
through `require()`. That is what lets the validator source be published while a
round is open without publishing the weights, and it makes the round-close
disclosure a file copy rather than a code release.

Scripts bootstrap this with:

    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import pgxlib
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import pathlib
import re
from typing import Any


# ---------------------------------------------------------------------------
# Deterministic JSON
# ---------------------------------------------------------------------------

def write_json(path: str | pathlib.Path, obj: Any) -> None:
    """Canonical JSON: sorted keys, fixed separators, trailing newline.

    Every artifact in this PoC is written through here so that byte-identical
    content produces byte-identical files regardless of dict insertion order.
    The determinism fixture depends on this.
    """
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, sort_keys=True, indent=2, separators=(",", ": "),
                  ensure_ascii=False)
        fh.write("\n")


def read_json(path: str | pathlib.Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def canonical_payload(submission: dict) -> str:
    """The bytes a commitment is taken over.

    Canonical (sorted-key, fixed-separator) JSON of everything except the
    binding fields themselves, so that the commitment covers the answers and
    the binding fields are compared separately. Deterministic across processes
    and independent of case order in the submitted file.
    """
    body = {k: v for k, v in submission.items()
            if k not in ("round_id", "miner_uid", "submission_nonce")}
    if isinstance(body.get("cases"), list):
        body["cases"] = sorted(body["cases"], key=lambda c: c.get("case_id", ""))
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def sha256_hex(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
    return h.hexdigest()


def require(mapping: dict, key: str, where: str) -> Any:
    """Fail-loud accessor.

    Hard rule: never `contract.get(secret, default)` for a scoring secret. A
    defaulting lookup turns a misconfigured validator — one pointed at the
    redacted public contract by mistake — into one that silently scores the
    entire round at a neutral weight instead of failing. Every secret lookup
    goes through here.
    """
    if key not in mapping:
        raise KeyError(
            f"required key {key!r} missing from {where}. "
            "This usually means the validator was pointed at a redacted "
            "miner-facing artifact instead of the private one. Refusing to "
            "fall back to a default."
        )
    return mapping[key]


# ---------------------------------------------------------------------------
# Star-allele grammar
# ---------------------------------------------------------------------------

# Core allele, optional suballele, optional xN multiplication, optional
# tandem (+) units. The tandem and xN branches are unused at the
# genotype_calls tier but the grammar accepts them so that the Stage 1
# parser does not have to change when CYP2D6 lands.
_HAPLOTYPE_RE = re.compile(
    r"^\*[0-9]+[A-Za-z]?(?:\.[0-9]+)?(?:x[0-9]+)?(?:\+\*[0-9]+[A-Za-z]?(?:\.[0-9]+)?(?:x[0-9]+)?)*$"
)


def is_valid_haplotype(hap: str) -> bool:
    return bool(_HAPLOTYPE_RE.match(hap))


def parse_diplotype(diplotype: str) -> tuple[str, str]:
    """Split 'A/B' into its two haplotypes. Raises ValueError on bad syntax."""
    if not isinstance(diplotype, str) or diplotype.count("/") != 1:
        raise ValueError(f"invalid diplotype syntax: {diplotype!r}")
    a, b = diplotype.split("/")
    if not (is_valid_haplotype(a) and is_valid_haplotype(b)):
        raise ValueError(f"invalid diplotype syntax: {diplotype!r}")
    return a, b


def normalise_diplotype(diplotype: str) -> str:
    """Canonical unordered form. `*4/*41` and `*41/*4` both -> `*4/*41`.

    Sorted by (core allele number, full string) so that `*2` precedes `*10`
    rather than sorting lexically.
    """
    a, b = parse_diplotype(diplotype)
    return "/".join(sorted((a, b), key=_haplotype_sort_key))


def _haplotype_sort_key(hap: str) -> tuple:
    first = hap.split("+")[0]
    m = re.match(r"^\*([0-9]+)([A-Za-z]?)", first)
    return (int(m.group(1)), m.group(2), hap) if m else (10**9, "", hap)


def core_allele(hap: str) -> str:
    """`*4.001` -> `*4`; `*3A` -> `*3A` (TPMT letter suffixes are core alleles,
    not suballeles — CPIC names them `*3A`/`*3B`/`*3C` with no bare `*3`)."""
    return hap.split(".")[0].split("x")[0]


def implied_copy_number(diplotype: str) -> int:
    """Total gene copies implied by a diplotype.

    *5 (whole-gene deletion) contributes 0, *NxM contributes M, a tandem
    contributes one unit per element, anything else contributes 1.

    NOTE: the *5 == deletion rule is CYP2D6-specific. SLCO1B1*5 and CYP2C9*5
    are ordinary SNV alleles that contribute 1. This function therefore takes
    no gene argument and deliberately does NOT special-case *5 — copy number is
    fixed at 2 for every gene in this PoC's scope. The deletion rule arrives
    with CYP2D6 in v5 and must be gene-aware when it does. See
    README.md "Deferred".
    """
    total = 0
    for hap in parse_diplotype(diplotype):
        for unit in hap.split("+"):
            m = re.search(r"x([0-9]+)$", unit)
            total += int(m.group(1)) if m else 1
    return total


# ---------------------------------------------------------------------------
# Per-gene phenotype loaders
# ---------------------------------------------------------------------------
#
# There is deliberately NO generic activity_score -> phenotype function here.
# Each gene gets its own loader class, each asserts the invariants that are
# true of *that gene's* CPIC table and false of at least one other gene's.
# If CPIC changes a gene's semantics, the assertion fires at build time
# instead of the dataset being silently mislabelled.

METABOLIZER_VOCABULARY = {
    "Ultrarapid Metabolizer", "Rapid Metabolizer", "Normal Metabolizer",
    "Likely Intermediate Metabolizer", "Intermediate Metabolizer",
    "Possible Intermediate Metabolizer", "Likely Poor Metabolizer",
    "Poor Metabolizer", "Indeterminate",
}

FUNCTION_VOCABULARY = {
    "Increased Function", "Normal Function", "Possible Decreased Function",
    "Decreased Function", "Poor Function", "Indeterminate",
}

# Phenotypes that carry no CPIC prescribing action and must never appear in
# generated truth. Alleles of Uncertain/Unknown function produce these.
NON_ACTIONABLE_PHENOTYPES = {"Indeterminate"}

# Allele function terms that disqualify an allele from truth generation.
# Alleles with no CPIC function assignment cannot be scored meaningfully and are
# excluded from truth generation. They remain legal *submissions*.
NON_ACTIONABLE_FUNCTIONS = {"Uncertain function", "Unknown function"}


class _PhenotypeLoader:
    """Base: holds a complete enumerated diplotype -> phenotype table.

    CPIC publishes these fully enumerated — n(n+1)/2 rows for n alleles — so
    lookup is total and no banding arithmetic is ever performed here.
    Subclasses add per-gene assertions; the base class does no interpretation.
    """

    gene: str = ""
    uses_activity_score: bool = False
    vocabulary: set[str] = set()

    def __init__(self, table: dict):
        self.gene = table["gene"]
        self.lookup: dict[str, dict] = table["lookup"]
        self.functions: dict[str, str] = table["functions"]
        self.activity_values: dict[str, float | None] = table.get("activity_values", {})
        self._check()

    def _check(self) -> None:
        raise NotImplementedError

    # -- public API --------------------------------------------------------
    def phenotype(self, diplotype: str) -> str | None:
        row = self.lookup.get(normalise_diplotype(diplotype))
        return row["phenotype"] if row else None

    def activity_score(self, diplotype: str) -> float | None:
        row = self.lookup.get(normalise_diplotype(diplotype))
        return row["activity_score"] if row else None

    def recommendation_key(self, diplotype: str) -> str | None:
        """The string this gene's CPIC recommendation table is keyed on."""
        row = self.lookup.get(normalise_diplotype(diplotype))
        return row["lookup_key"] if row else None

    def known_alleles(self) -> set[str]:
        return set(self.functions)

    def actionable_alleles(self) -> set[str]:
        return {a for a, f in self.functions.items()
                if f not in NON_ACTIONABLE_FUNCTIONS}

    def _assert_complete(self) -> None:
        n = len(self.functions)
        expected = n * (n + 1) // 2
        if len(self.lookup) != expected:
            raise AssertionError(
                f"{self.gene}: diplotype table is not complete — "
                f"{len(self.lookup)} rows for {n} alleles, expected {expected}. "
                "A partial table would make Stage 3's internal-consistency "
                "check a partial function, which it must not be."
            )


class CYP2C19PhenotypeLoader(_PhenotypeLoader):
    """CYP2C19: DIRECT diplotype -> phenotype. No activity score.

    Distinguishing feature vs. CYP2C9: CPIC assigns CYP2C19 no activity values
    at all, and its vocabulary carries Rapid/Ultrarapid classes driven by *17.
    """

    uses_activity_score = False
    vocabulary = METABOLIZER_VOCABULARY

    def _check(self) -> None:
        assert self.gene == "CYP2C19"
        if any(v is not None for v in self.activity_values.values()):
            raise AssertionError(
                "CYP2C19 must have no activity values — CPIC maps its "
                "diplotypes to phenotype directly. Non-null values here mean "
                "the loader was pointed at the wrong gene's table."
            )
        if any(r["activity_score"] is not None for r in self.lookup.values()):
            raise AssertionError("CYP2C19 diplotype rows must carry no activity score")
        bad = {r["phenotype"] for r in self.lookup.values()} - self.vocabulary
        if bad:
            raise AssertionError(f"CYP2C19 phenotype terms outside vocabulary: {bad}")
        if "Rapid Metabolizer" not in {r["phenotype"] for r in self.lookup.values()}:
            raise AssertionError(
                "CYP2C19 must contain a Rapid Metabolizer class (*17-driven). "
                "Its absence means this is not the CYP2C19 table."
            )
        self._assert_complete()


class TPMTPhenotypeLoader(_PhenotypeLoader):
    """TPMT: DIRECT diplotype -> phenotype. No activity score.

    Distinguishing feature: TPMT core alleles carry LETTER suffixes —
    *3A/*3B/*3C — and there is no bare *3. A name join that assumes bare
    numeric cores drops *3A (3.4% European) and *3C (1.6% East Asian), which
    together are almost all of the non-normal TPMT burden. This loader asserts
    they are present.
    """

    uses_activity_score = False
    vocabulary = METABOLIZER_VOCABULARY

    def _check(self) -> None:
        assert self.gene == "TPMT"
        if any(v is not None for v in self.activity_values.values()):
            raise AssertionError("TPMT must have no activity values")
        missing = {"*3A", "*3B", "*3C"} - set(self.functions)
        if missing:
            raise AssertionError(
                f"TPMT is missing letter-suffixed core alleles {missing}. "
                "CPIC names these *3A/*3B/*3C with no bare *3; losing them "
                "collapses TPMT to ~95% *1 with no non-normal metabolizers."
            )
        if "*3" in self.functions:
            raise AssertionError(
                "TPMT table contains a bare *3, which CPIC's phenotype and "
                "frequency tables do not use. Two namespaces have been mixed."
            )
        bad = {r["phenotype"] for r in self.lookup.values()} - self.vocabulary
        if bad:
            raise AssertionError(f"TPMT phenotype terms outside vocabulary: {bad}")
        self._assert_complete()


class CYP2C9PhenotypeLoader(_PhenotypeLoader):
    """CYP2C9: ACTIVITY SCORE, with its own band boundaries.

    Distinguishing feature: every diplotype row carries a non-null activity
    score, and CPIC's *recommendation* tables for CYP2C9 are keyed on that
    score string ("1.0", "0.5"), not on the phenotype term. That second fact
    is a separate trap from the phenotype-vocabulary one and is asserted by
    CYP2C9RecommendationLoader.

    The band boundaries are NOT reimplemented here. CPIC publishes the
    phenotype per diplotype directly, so it is read, never derived. Computing
    bands locally would be hand-transcribing values that change between CPIC
    releases — the exact failure this loader design exists to prevent.
    """

    uses_activity_score = True
    vocabulary = METABOLIZER_VOCABULARY

    def _check(self) -> None:
        assert self.gene == "CYP2C9"
        if not any(v is not None for v in self.activity_values.values()):
            raise AssertionError(
                "CYP2C9 must carry per-allele activity values — its absence "
                "means this is a direct-mapping gene's table."
            )
        scored = [r for r in self.lookup.values() if r["activity_score"] is not None]
        if not scored:
            raise AssertionError("CYP2C9 diplotype rows must carry activity scores")
        bad = {r["phenotype"] for r in self.lookup.values()} - self.vocabulary
        if bad:
            raise AssertionError(f"CYP2C9 phenotype terms outside vocabulary: {bad}")
        self._assert_complete()


class SLCO1B1PhenotypeLoader(_PhenotypeLoader):
    """SLCO1B1: FUNCTION terms, not metaboliser terms.

    Distinguishing feature: the gene result vocabulary is
    Increased/Normal/Possible Decreased/Decreased/Poor *Function*. A generic
    phenotype function that emits 'Normal Metabolizer' here produces a label
    that is not in CPIC's SLCO1B1 vocabulary at all.

    Note also the capitalisation split, which is real and easy to miss: the
    per-ALLELE function term is 'Normal function' (lowercase f) while the
    per-DIPLOTYPE gene result is 'Normal Function' (capital F).
    """

    uses_activity_score = False
    vocabulary = FUNCTION_VOCABULARY

    def _check(self) -> None:
        assert self.gene == "SLCO1B1"
        results = {r["phenotype"] for r in self.lookup.values()}
        bad = results - self.vocabulary
        if bad:
            raise AssertionError(f"SLCO1B1 phenotype terms outside vocabulary: {bad}")
        if results & METABOLIZER_VOCABULARY - FUNCTION_VOCABULARY:
            raise AssertionError(
                "SLCO1B1 must not use metaboliser terms. Finding them here "
                "means a generic phenotype function has been applied."
            )
        if "Normal Function" not in results:
            raise AssertionError("SLCO1B1 must contain the term 'Normal Function'")
        self._assert_complete()


class CYP3A5PhenotypeLoader(_PhenotypeLoader):
    """CYP3A5: DIRECT diplotype -> phenotype. No activity score.

    Distinguishing feature, and the reason this gene earns its place: CYP3A5 is
    an EXPRESSION gene and its clinical direction is INVERTED relative to every
    other gene in the set.

      * `*1` is the functional, expressing allele -> Normal Metabolizer.
      * `*3` is the non-expressing allele -> `*3/*3` is a Poor Metabolizer.
      * `*3` is the MAJORITY allele in most populations (0.924 in Europeans), so
        the modal diplotype is `*3/*3` and the modal PHENOTYPE is Poor
        Metabolizer.

    Everywhere else in this reference set the modal diplotype is the Normal
    phenotype and a Poor Metabolizer means "reduce the dose". For CYP3A5 and
    tacrolimus the opposite holds: an expresser clears the drug faster and needs
    an INCREASED dose, while a Poor Metabolizer takes the standard one. A caller
    or a validator carrying "poor metaboliser implies reduce" as an assumption
    starts failing here, which is exactly where we want that assumption to
    surface.
    """

    uses_activity_score = False
    vocabulary = METABOLIZER_VOCABULARY

    def _check(self) -> None:
        assert self.gene == "CYP3A5"
        if any(v is not None for v in self.activity_values.values()):
            raise AssertionError("CYP3A5 must have no activity values")
        if self.functions.get("*1") != "Normal function":
            raise AssertionError(
                "CYP3A5 *1 must be Normal function — it is the expressing "
                "allele. A different value means the table was misread.")
        if self.functions.get("*3") != "No function":
            raise AssertionError(
                "CYP3A5 *3 must be No function. *3 is the non-expressing "
                "allele and the majority allele in most populations; losing it "
                "removes the whole point of the gene.")
        if self.phenotype("*3/*3") != "Poor Metabolizer":
            raise AssertionError(
                "CYP3A5 *3/*3 must be a Poor Metabolizer. This is the "
                "inverted-direction check: for CYP3A5 the COMMON diplotype is "
                "the impaired phenotype.")
        if self.phenotype("*1/*1") != "Normal Metabolizer":
            raise AssertionError("CYP3A5 *1/*1 must be a Normal Metabolizer")
        bad = {r["phenotype"] for r in self.lookup.values()} - self.vocabulary
        if bad:
            raise AssertionError(f"CYP3A5 phenotype terms outside vocabulary: {bad}")
        self._assert_complete()


PHENOTYPE_LOADERS = {
    "CYP2C19": CYP2C19PhenotypeLoader,
    "TPMT": TPMTPhenotypeLoader,
    "CYP2C9": CYP2C9PhenotypeLoader,
    "SLCO1B1": SLCO1B1PhenotypeLoader,
    "CYP3A5": CYP3A5PhenotypeLoader,
}


# ---------------------------------------------------------------------------
# Per-gene recommendation loaders — the SECOND instance of the same trap
# ---------------------------------------------------------------------------
#
# CPIC keys its recommendation tables on different things per gene:
#   CYP2C19, TPMT, SLCO1B1 -> the phenotype term
#   CYP2C9                 -> the activity score STRING ("1.0", "0.5", "1.5")
# and several drugs are keyed on more than one gene jointly.

ACTION_VOCABULARY = [
    "standard_dose", "reduced_dose", "increased_dose",
    "alternative_therapy", "avoid", "monitor", "no_recommendation",
]

CLASSIFICATION_VOCABULARY = ["Strong", "Moderate", "Optional", "No recommendation"]


class _RecommendationLoader:
    gene: str = ""
    lookup_key_type: str = ""

    def __init__(self, table: dict):
        self.gene = table["gene"]
        self.lookup_key_type = table["lookup_key_type"]
        self.drugs: dict[str, dict] = table["drugs"]
        self.fixed_gene_assumptions: dict[str, str] = table.get(
            "fixed_gene_assumptions", {})
        self._check()

    def _check(self) -> None:
        raise NotImplementedError

    def lookup(self, drug: str, key: str) -> dict | None:
        return self.drugs.get(drug, {}).get(key)

    def drug_list(self) -> list[str]:
        return sorted(self.drugs)


class _PhenotypeKeyedRecommendationLoader(_RecommendationLoader):
    def _check(self) -> None:
        if self.lookup_key_type != "phenotype":
            raise AssertionError(
                f"{self.gene} recommendations must be keyed on the phenotype "
                f"term, got {self.lookup_key_type!r}")


class CYP2C19RecommendationLoader(_PhenotypeKeyedRecommendationLoader):
    pass


class SLCO1B1RecommendationLoader(_PhenotypeKeyedRecommendationLoader):
    pass


class TPMTRecommendationLoader(_PhenotypeKeyedRecommendationLoader):
    """TPMT: keyed on phenotype, but every CPIC thiopurine recommendation is
    keyed on TPMT *and NUDT15 jointly*. TPMT has no single-gene-resolvable
    CPIC drug.

    Resolution: the round contract declares
    `fixed_gene_assumptions: {"NUDT15": "Normal Metabolizer"}` and publishes it
    to miners, collapsing the joint table to a TPMT-only one. The assumption is
    visible to everyone rather than hidden in the validator.
    """

    def _check(self) -> None:
        super()._check()
        if self.fixed_gene_assumptions.get("NUDT15") is None:
            raise AssertionError(
                "TPMT recommendations require a declared NUDT15 assumption — "
                "every CPIC thiopurine recommendation is keyed on TPMT x "
                "NUDT15 jointly and is undefined without one.")


class CYP2C9RecommendationLoader(_RecommendationLoader):
    """CYP2C9: keyed on the ACTIVITY SCORE STRING, not the phenotype term.

    This is a distinct trap from the phenotype-vocabulary one, and an easy one
    to miss. A recommendation lookup that passes
    'Intermediate Metabolizer' to CYP2C9 finds nothing and silently degrades
    to no_recommendation on every call.
    """

    def _check(self) -> None:
        if self.lookup_key_type != "activity_score":
            raise AssertionError(
                "CYP2C9 recommendations must be keyed on the activity score "
                f"string, got {self.lookup_key_type!r}")
        for drug, keys in self.drugs.items():
            for k in keys:
                if k in METABOLIZER_VOCABULARY:
                    raise AssertionError(
                        f"CYP2C9/{drug} has a phenotype-term key {k!r}; CPIC "
                        "keys this gene on the activity score.")


class CYP3A5RecommendationLoader(_PhenotypeKeyedRecommendationLoader):
    """CYP3A5: keyed on the phenotype term, single gene, one drug (tacrolimus).

    Unlike TPMT, CPIC's tacrolimus recommendation is resolvable from CYP3A5
    alone — no joint key, no fixed-gene assumption needed.
    """


RECOMMENDATION_LOADERS = {
    "CYP2C19": CYP2C19RecommendationLoader,
    "TPMT": TPMTRecommendationLoader,
    "CYP2C9": CYP2C9RecommendationLoader,
    "SLCO1B1": SLCO1B1RecommendationLoader,
    "CYP3A5": CYP3A5RecommendationLoader,
}


# ---------------------------------------------------------------------------
# Reference bundle
# ---------------------------------------------------------------------------

class Reference:
    """Loaded pgx_reference/. Identical for subnet, miner and validator."""

    def __init__(self, root: str | pathlib.Path):
        self.root = pathlib.Path(root)
        self.release = read_json(self.root / "release.json")
        self.counts = read_json(self.root / "counts_manifest.json")
        self.action_mapping = read_json(self.root / "action_mapping.json")
        self.genes = list(self.release["genes"])

        self.phenotype: dict[str, _PhenotypeLoader] = {}
        self.recommendation: dict[str, _RecommendationLoader] = {}
        self.definitions: dict[str, dict] = {}
        self.frequencies: dict[str, dict] = {}

        for g in self.genes:
            self.phenotype[g] = PHENOTYPE_LOADERS[g](
                read_json(self.root / "diplotype_phenotype" / f"{g}.json"))
            self.recommendation[g] = RECOMMENDATION_LOADERS[g](
                read_json(self.root / "recommendations" / f"{g}.json"))
            self.definitions[g] = read_json(
                self.root / "allele_definitions" / f"{g}.json")
            self.frequencies[g] = read_json(
                self.root / "frequencies" / f"{g}.json")

    # -- sampling support --------------------------------------------------
    def sampleable_alleles(self, gene: str, population: str) -> dict[str, float]:
        """Renormalised allele frequencies over the generable allele set.

        An allele is sampleable only if it is in ALL of:
          - the CPIC phenotype table (has a function assignment),
          - the allele definition table (has a defining-variant set, so it can
            be rendered as genotype calls),
          - the population's frequency table with a non-null, strictly positive
            freq_weighted_avg,
        and its function is not Uncertain/Unknown.
        """
        populations = self.frequencies[gene]["populations"]
        if population not in populations:
            raise KeyError(
                f"{gene} has no frequency table for population {population!r}. "
                f"The reference bundle carries {sorted(populations)} for this "
                f"gene. A population present for some genes and not others "
                f"means the frequency snapshot is ragged — 00_build_reference.py "
                f"derives release.json's population list from the intersection "
                f"across genes, so this should be unreachable through the "
                f"pinned bundle.")
        return dict(populations[population]["sampleable"])

    def off_prior_alleles(self, gene: str, population: str) -> list[str]:
        """Alleles excluded from the sampleable set ONLY on frequency grounds.

        An allele qualifies when it has a defining-variant set and an actionable
        CPIC function assignment, but its `freq_weighted_avg` in this population
        is null (no published estimate) or zero. Such an allele renders as
        genotype calls and scores through the CPIC tables exactly like any
        other; it simply carries no frequency mass in this population, so
        ordinary sampling can never produce it.

        These are the alleles integrity probes are drawn from. The reference
        build applies its checks in the order phenotype -> function ->
        definition -> frequency, so anything recorded under `null_frequency` or
        `zero_frequency` has already passed the first three.
        """
        populations = self.frequencies[gene]["populations"]
        if population not in populations:
            raise KeyError(
                f"{gene} has no frequency table for population {population!r}; "
                f"the bundle carries {sorted(populations)} for this gene.")
        excluded = populations[population]["excluded"]
        out = set(excluded.get("null_frequency", []))
        out |= set(excluded.get("zero_frequency", []))
        return sorted(out)

    def callable_alleles(self, gene: str, population: str) -> list[str]:
        """Every allele a correct caller must consider: sampleable + off-prior.

        Deliberately wider than `sampleable_alleles`, which is the set the
        GENERATOR can draw from. A caller that searches only the sampleable set
        is fitting the generator rather than the biology; integrity probes exist
        to make that visible.
        """
        return sorted(set(self.sampleable_alleles(gene, population))
                      | set(self.off_prior_alleles(gene, population)))

    def calling_prior(self, gene: str, population: str,
                      off_prior_floor: float) -> dict[str, float]:
        """Allele prior over `callable_alleles`, renormalised to 1.

        Sampleable alleles keep their published frequency. Off-prior alleles get
        `off_prior_floor` each — small enough never to outrank a real estimate,
        non-zero so that a diplotype using one stays reachable when it is the
        only consistent explanation of the observation.
        """
        prior = dict(self.sampleable_alleles(gene, population))
        for allele in self.off_prior_alleles(gene, population):
            prior.setdefault(allele, off_prior_floor)
        total = math.fsum(prior.values())
        return {a: v / total for a, v in sorted(prior.items())}

    def diplotype_frequency(self, gene: str, population: str,
                            diplotype: str) -> float:
        """Hardy-Weinberg frequency of an unordered diplotype."""
        f = self.sampleable_alleles(gene, population)
        a, b = parse_diplotype(normalise_diplotype(diplotype))
        fa, fb = f.get(a, 0.0), f.get(b, 0.0)
        return fa * fb if a == b else 2.0 * fa * fb

    def modal_diplotype_frequency(self, gene: str, population: str) -> float:
        f = self.sampleable_alleles(gene, population)
        best = 0.0
        for a, b in itertools.combinations_with_replacement(sorted(f), 2):
            v = f[a] * f[b] if a == b else 2.0 * f[a] * f[b]
            best = max(best, v)
        return best

    def modal_diplotype(self, gene: str, population: str) -> str:
        f = self.sampleable_alleles(gene, population)
        best, best_v = None, -1.0
        for a, b in itertools.combinations_with_replacement(sorted(f), 2):
            v = f[a] * f[b] if a == b else 2.0 * f[a] * f[b]
            if v > best_v:
                best, best_v = normalise_diplotype(f"{a}/{b}"), v
        return best


# ---------------------------------------------------------------------------
# Genotype-call helpers (shared by generator, miner and validator)
# ---------------------------------------------------------------------------

def haplotype_variant_vector(definition: dict, allele: str) -> list[str]:
    """Per-variant base for one named allele, reference-filled where CPIC
    leaves the position unspecified for that allele."""
    named = definition["named_alleles"]
    if allele not in named:
        raise KeyError(f"{allele} not in {definition['gene']} allele definitions")
    ref = definition["reference_bases"]
    return [b if b is not None else ref[i] for i, b in enumerate(named[allele])]


def genotype_from_haplotypes(definition: dict, hap_a: str,
                             hap_b: str) -> list[dict]:
    """Unphased genotype calls at every defining position of the gene.

    Returns one record per variant with the multiset of observed bases. Phase
    is deliberately discarded — that is where the ambiguity at this tier comes
    from.
    """
    va = haplotype_variant_vector(definition, hap_a)
    vb = haplotype_variant_vector(definition, hap_b)
    out = []
    for i, var in enumerate(definition["variants"]):
        out.append({
            "position": var["position"],
            "rsid": var["rsid"],
            "ref": var["ref"],
            "bases": sorted([va[i], vb[i]]),
        })
    return out


def rarity_weight(params: dict, f_true: float, f_modal: float) -> float:
    """Weight a call by how rare its TRUE diplotype is in the round's population.

    `1 + log10(f_modal / f_true)`, clipped to [clip_min, clip_max]. Saturates at
    the maximum once the true diplotype is 10**(clip_max-1) times rarer than the
    modal one. `f_true_floor` keeps the logarithm finite for a diplotype with no
    frequency mass at all, which is what an integrity probe is.

    Parameterised, never constant: the values live in contract.scoring.
    """
    f = max(f_true, params["f_true_floor"])
    if f_modal <= 0:
        return params["clip_min"]
    raw = 1.0 + math.log10(f_modal / f)
    return min(max(raw, params["clip_min"]), params["clip_max"])


def difficulty_weight(params: dict, strata: dict) -> float:
    """Product of the three per-axis multipliers for a call's stratum."""
    return (params["sv_mult"][strata["sv_class"]]
            * params["depth_mult"][strata["depth_band"]]
            * params["ambig_mult"][strata["ambiguity"]])


def consistent_diplotypes(definition: dict, observed: list[dict],
                          candidate_alleles: list[str]) -> list[str]:
    """Every diplotype over `candidate_alleles` consistent with the observed
    unphased calls. Positions with `bases == None` (no-call) are unconstrained.

    This is the exact ambiguity computation, not an approximation: a case is
    `ambiguous` iff this returns more than one diplotype whose phenotypes
    differ. Used by the generator to assign the resolvability stratum and by
    the baseline miner to enumerate alternatives.
    """
    obs = {}
    for rec in observed:
        if rec["bases"] is None:
            continue
        obs[rec["position"]] = sorted(rec["bases"])

    pos_index = {v["position"]: i for i, v in enumerate(definition["variants"])}
    vectors = {a: haplotype_variant_vector(definition, a)
               for a in candidate_alleles}

    out = []
    for a, b in itertools.combinations_with_replacement(sorted(candidate_alleles), 2):
        va, vb = vectors[a], vectors[b]
        ok = True
        for position, bases in obs.items():
            i = pos_index[position]
            if sorted([va[i], vb[i]]) != bases:
                ok = False
                break
        if ok:
            out.append(normalise_diplotype(f"{a}/{b}"))
    return sorted(set(out), key=lambda d: (_haplotype_sort_key(d.split("/")[0]),
                                           _haplotype_sort_key(d.split("/")[1])))


def render_vcf(case_id: str, gene_blocks: dict[str, dict],
               reference_build: str) -> str:
    """A minimal VCF 4.2 over the active loci. Unphased (GT uses '/'), with
    './.' for no-calls. No copy-number field: this tier carries none."""
    lines = [
        "##fileformat=VCFv4.2",
        f"##reference={reference_build}",
        f"##source=NIOME_UC03_PGx_StarAllele_genotype_calls",
        f"##case_id={case_id}",
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Unphased genotype">',
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE",
    ]
    rows = []
    for gene, block in gene_blocks.items():
        chrom = block["chromosome"]
        for rec in block["calls"]:
            ref = rec["ref"]
            if rec["bases"] is None:
                alt, gt = ".", "./."
            else:
                alts = sorted({b for b in rec["bases"] if b != ref})
                alt = ",".join(alts) if alts else "."
                idx = {ref: 0}
                for i, a in enumerate(alts, start=1):
                    idx[a] = i
                gt = "/".join(str(idx[b]) for b in sorted(rec["bases"]))
            rows.append((chrom, rec["position"],
                         f"{chrom}\t{rec['position']}\t{rec['rsid'] or '.'}\t"
                         f"{ref}\t{alt}\t.\tPASS\tGENE={gene}\tGT\t{gt}"))
    for _, _, line in sorted(rows, key=lambda r: (r[0], r[1])):
        lines.append(line)
    return "\n".join(lines) + "\n"
