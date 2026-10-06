# Submission Format

The authoritative definition is `niome_subnet/genomics/validation/stage12.py`
(`stage1_whole_submission`, `stage1_gene_call`) and `niome_subnet/genomics/pgxlib.py`
(`parse_diplotype`, `implied_copy_number`). This document describes what those enforce.

One JSON object per round, uploaded to the presigned PUT URL from the task.

```json
{
  "round_id": "3dc2919a-e6a7-4d47-8100-760e00dfdd11",
  "miner_uid": 42,
  "submission_nonce": "3f9a1c...",
  "cases": [
    {
      "case_id": "case_0164",
      "gene_calls": {
        "CYP2C19": {
          "diplotype": "*2/*17",
          "total_copy_number": 2,
          "activity_score": null,
          "phenotype": "Intermediate Metabolizer",
          "confidence": 0.94,
          "no_call": false,
          "diplotype_alternatives": [
            {"diplotype": "*2/*38", "probability": 0.04}
          ],
          "evidence": ["rs4244285:0/1", "rs12248560:0/1"]
        },
        "CYP2C9": {
          "diplotype": "*1/*3",
          "total_copy_number": 2,
          "activity_score": 1.0,
          "phenotype": "Intermediate Metabolizer",
          "confidence": 0.88,
          "no_call": false
        }
      },
      "recommendations": [
        {
          "drug": "clopidogrel",
          "gene": "CYP2C19",
          "phenotype_used": "Intermediate Metabolizer",
          "action": "avoid",
          "cpic_classification": "Strong"
        },
        {
          "drug": "celecoxib",
          "gene": "CYP2C9",
          "phenotype_used": "Intermediate Metabolizer",
          "action": "standard_dose",
          "cpic_classification": "Moderate"
        }
      ]
    }
  ]
}
```

## Submission level

### `round_id`
Must equal `public_contract.round_id`. A mismatch rejects the entire submission.

### `miner_uid`
**Your metagraph uid, as an integer or a string — not your hotkey.** This is checked
*first*, before `round_id`, against the S3 slot the submission was uploaded to. A mismatch
is `uid_binding_mismatch` and rejects the submission in full.

> The standalone reference release under `task_doc/release/` binds on `miner_hotkey`
> instead. The live subnet binds on `miner_uid`; that is what `stage1_whole_submission`
> checks. If you are porting code from the reference release, change this field.

Bundles are uid-derived and disjoint, so a submission answering someone else's bundle also
fails on `unknown_case_id`.

### `submission_nonce`
Hex string. Carried for the commit–reveal protocol and included in the commitment hash.
Commit–reveal is **not enforced today** — `run_stage12` passes `commitment = None`, so the
hash check is skipped — but the field belongs in the schema from the start, so populate it.

### `cases`
Array of case results. Must contain exactly the `case_id`s from your
`bundle_manifest.json` — no extras, no duplicates.

Missing cases are **not** a rejection: they score zero and still count in the Stage 5
coverage denominator. Omitting hard cases does not hide them from the coverage gate.

## Per gene call

Keys of `gene_calls` must be in `contract.active_genes`; anything else is
`gene_not_active`. One entry per `(case_id, gene)` — a repeat is `duplicate_gene_call` and
drops that call.

### `diplotype`
PharmVar star-allele notation, two haplotypes separated by `/`. Copy number as `xN`;
tandems joined with `+`.

Ordering is not scored — `*4/*41` and `*41/*4` are equivalent. Suballele notation
(`*4.001`) is accepted; core-allele notation is accepted and scored slightly lower where
the truth is a specific suballele. Every named allele must exist in the round's reference
release, or the call is `unknown_allele`.

⚠️ **Some core alleles carry letter suffixes and there is no bare numeric form.** TPMT is
named `*3A`, `*3B`, `*3C`; there is no TPMT `*3`. Submitting `*3` is `unknown_allele`.
These are core alleles, not suballeles. A naive name join that drops them leaves TPMT
~95% `*1` with essentially no non-normal metabolizers, and nothing crashes.

⚠️ **An all-reference call set does not mean `*1/*1`.** CYP2C19's GRCh38 reference
haplotype is `*38`. Read `allele_definitions/<GENE>.json` → `reference_allele` rather than
assuming.

### `total_copy_number`
Must equal `pgxlib.implied_copy_number(diplotype)` exactly, or the call is rejected with
`copy_number_inconsistent` — a hard rejection for that gene call, not a scoring penalty.
At the `genotype_calls` tier every diplotype has copy number 2.

The deletion rule is **gene-specific**: `CYP2D6*5` is a whole-gene deletion contributing
0, while `SLCO1B1*5` and `CYP2C9*5` are ordinary SNV alleles contributing 1 each.

### `activity_score`
Required for genes in `rules.require_activity_score` (CYP2C9 in the current round);
`missing_activity_score` otherwise. Must be the value that follows from **your own**
diplotype under `diplotype_phenotype/<GENE>.json` — read it, do not compute it.

CYP2C9 diplotypes involving an Uncertain- or Unknown-function allele carry the literal
CPIC value `"n/a"`, which is also a valid recommendation key; represent the numeric field
as `null` in that case.

### `phenotype`
The CPIC term that follows from **your own** diplotype. Stage 1 builds the accepted set
per gene by reading the distinct `phenotype` values out of that gene's own
`diplotype_phenotype/<GENE>.json`. **There is no union vocabulary anywhere in the
submission path** — a term valid for one gene and absent from another's table is
`invalid_phenotype_term` for that call.

| gene | vocabulary | accepted terms | n |
|---|---|---|---:|
| CYP2C19 | metaboliser | Ultrarapid / Rapid / Likely Intermediate / Intermediate / Likely Poor / Poor / Normal Metabolizer, Indeterminate | 8 |
| TPMT | metaboliser | Possible Intermediate / Intermediate / Poor / Normal Metabolizer, Indeterminate | 5 |
| CYP2C9 | metaboliser | Intermediate / Poor / Normal Metabolizer, Indeterminate | 4 |
| CYP3A5 | metaboliser | Possible Intermediate / Intermediate / Poor / Normal Metabolizer, Indeterminate | 5 |
| SLCO1B1 | **function** | Increased / Possible Decreased / Decreased / Poor / Normal **Function**, Indeterminate | 6 |

Four traps in that table:

1. **No gene uses every metaboliser term.** The widest is CYP2C19 at eight. Building one
   metaboliser enum and applying it everywhere is the most common way to fail Stage 1 on a
   gene you were otherwise calling correctly.
2. **`Rapid Metabolizer` is CYP2C19 and nothing else.** `Likely Intermediate` / `Likely
   Poor` are CYP2C19-only; `Possible Intermediate` is TPMT and CYP3A5 only. "Likely" and
   "Possible" are different terms with different genes, not two spellings of one hedge, and
   neither is a synonym for `Indeterminate`.
3. **SLCO1B1 is not on metaboliser terms at all**, and carries a real capitalisation
   split: the per-*allele* function term is `Normal function` (lowercase f) while the
   per-*diplotype* gene result is `Normal Function` (capital F). **Submit the gene-result
   form.**
4. **`Indeterminate` never appears in ground truth** but is a legal submission. It carries
   no CPIC prescribing action, so if you submit it, emit `no_recommendation` for that drug.

### `confidence`
Your probability in `[0,1]` that the submitted diplotype is **exactly** correct. Required
when `rules.require_confidence` is true; otherwise `invalid_confidence`.

Scored with a strictly proper scoring rule against your own base rate, so reporting your
true belief maximises expected score. A constant value for every call is a losing strategy.

### `no_call`
Set `true` to abstain on a gene; `diplotype`, `activity_score`, `phenotype` and
`total_copy_number` may then be `null`. Permitted when `rules.allow_no_call` is true,
otherwise `no_call_not_permitted`.

Abstaining on a case the round independently classifies as genuinely ambiguous earns
partial credit. Abstaining on a resolvable case earns zero — though it does not damage
your calibration, since no-calls are excluded from the calibration pairs. Abstaining on
everything hard is caught by the Stage 5 coverage gate.

### `diplotype_alternatives` (optional)
`[{diplotype, probability}, ...]`. If your top call is wrong but a listed alternative is
right, you receive that alternative's credit scaled by the probability you assigned **and
then discounted**, so listing many low-probability options gains close to nothing. Each
alternative must also parse as a diplotype.

⚠️ `confidence` plus the sum of all `probability` values must not exceed `1.0`, or the
call is `probability_mass_exceeded`. Round your confidence **up** and each alternative
**down** so declared mass cannot cross the line through floating-point rounding alone.

### `evidence` (optional)
Free-form strings, stored for auditability. **Not scored.**

## Per recommendation

One entry per `(case, queried drug)`. Omit the entry for any gene you `no_call`. Keyed
internally on `(case_id, gene)`.

| field | requirement |
|---|---|
| `drug` | must be in `contract.drug_set` |
| `gene` | must equal `contract.drug_genes[drug]` |
| `phenotype_used` | the phenotype you applied; equals your submitted `phenotype` while `phenoconversion_active` is false. For genes keyed on the activity score, still report the **term** here — the lookup uses the score, the report uses the term. |
| `action` | one of `standard_dose`, `reduced_dose`, `increased_dose`, `alternative_therapy`, `avoid`, `monitor`, `no_recommendation` |
| `cpic_classification` | `Strong`, `Moderate`, `Optional`, `No recommendation` |

Derive `action` via `pgx_reference/action_mapping.json` from the CPIC recommendation your
phenotype (or activity score) resolves to, **for the sub-population named in
`contract.drug_populations[drug]`**.

Omitting the recommendation block entirely is **not** abstention and earns no credit —
`REC_MISSING`, scored zero. Submitting `no_recommendation` where a recommendation exists
is a legible abstention and earns a little.

### `drug_populations` affects your score

CPIC publishes several recommendations per phenotype for some drugs, one per clinical
sub-population; the round declares which one it scores against. `null` means the drug has
a single population. These are CPIC's own strings, and they are clinical indications —
*why* the drug is being prescribed — not ancestry groups.

For clopidogrel, where the current round declares `CVI ACS PCI`:

| label | meaning |
|---|---|
| `CVI ACS PCI` | cardiovascular indication — acute coronary syndrome and/or percutaneous coronary intervention (a heart attack or a stent) |
| `CVI non-ACS non-PCI` | other cardiovascular indications |
| `NVI` | neurovascular indications (stroke, TIA) |

6 of the 8 CYP2C19 phenotypes resolve differently across those three: 4 differ in the
action, 2 more agree on the action but differ in strength. **Read `drug_populations` from
the contract; do not assume.** A reference implementation that assumed `NVI` where the
round declared `CVI ACS PCI` measured at 97.1% of the correct score with 28 of 50 CYP2C19
recommendations scoring below an exact match — a small but entirely systematic and
entirely avoidable handicap.

### `rules.fixed_gene_assumptions`

Some CPIC recommendations are keyed on genes the round does not call. Rather than drop
those drugs, the round **pins** the uncalled gene to a declared phenotype and publishes the
assumption. Use the pinned value; do not attempt to infer it.

`{"NUDT15": "Normal Metabolizer"}` is what makes thiopurines usable. **TPMT has no
single-gene-resolvable CPIC drug** — all three thiopurines are keyed on TPMT × NUDT15
jointly — so without the assumption TPMT would have no scoreable drug at all.

### CYP3A5 runs the other way

CYP3A5 is an **expression** gene. `*1` expresses the enzyme and `*3` does not — and `*3`
is the **majority** allele in every population the subnet runs. So the modal diplotype is
`*3/*3`, the modal phenotype is Poor Metabolizer, and CPIC's tacrolimus guidance tells a
**Normal** Metabolizer to **increase** the dose while a Poor Metabolizer takes the standard
one.

Every other gene in the set reads "poor metaboliser ⇒ reduce". If your prescribing step has
learned a direction rather than performing the lookup, CYP3A5 is where it breaks — and it
breaks on the rare calls that carry the most weight.

## Rejection reasons

### Whole submission
Checked in this order, before any per-call work. Any hit rejects everything.

| reason | cause |
|---|---|
| `uid_binding_mismatch` | `miner_uid` is not the uid whose S3 slot this submission came from |
| `round_id_mismatch` | `round_id` is not the contract's |
| `uid_mismatch` | no bundle was assigned to that uid this round |
| `commit_mismatch` | revealed payload does not reproduce the commitment (inactive today) |
| `submission_exceeds_max_cases` | more than `rules.max_cases` cases |
| `duplicate_case_id` | the same `case_id` twice |
| `unknown_case_id` | a `case_id` not in your bundle |

### Per gene call
The call is dropped and scores zero; the rest of the submission continues.

`gene_not_active` · `no_call_not_permitted` · `invalid_diplotype_syntax` ·
`unknown_allele` · `copy_number_inconsistent` · `missing_activity_score` ·
`invalid_confidence` · `invalid_phenotype_term` · `probability_mass_exceeded` ·
`duplicate_gene_call`

A rejected call still counts as zero in the Stage 5 coverage denominator. Malforming a
call does not hide it.

## Internal coherence is enforced separately

Stage 3 recomputes `phenotype`, `activity_score` and `total_copy_number` from **your own**
submitted diplotype under the published CPIC tables. If any disagrees, the call's entire
phenotype *and* prescribing contribution is zeroed and the reason is logged to
`inconsistency_report.json`.

Being wrong is survivable; being incoherent is not. Predicting the phenotype independently
of your diplotype is strictly worse than deriving it, **even when the independent
prediction would be more often right.**
