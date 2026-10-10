# Validation and Scoring

The validator pipeline rewards:

- correct star-allele diplotype resolution from realistic, imperfect observational data
- accuracy concentrated where it matters — rare alleles and low call completeness
- inference chains that are internally coherent from genotype through phenotype to
  prescribing action
- calibrated, honest reporting of uncertainty
- performance that holds across the full difficulty range of a round, and over the full
  allele set rather than just what the generator usually emits

**Ground truth is held by construction.** Each case is generated *from* a known diplotype;
truth is never inferred from submissions. Validation is a comparison against a known
answer, not a consensus or plausibility judgement.

Every gene call carries a persistent `(case_id, gene)` key through all five stages, which
is what makes post-round auditing and forensics possible.

## What is published, and when

The validator source is public and permanent — it is the code in this repository. Per-round
instance data is withheld while the round is open and published when it closes:

| Artifact | During round | After close |
|---|---|---|
| Validator source | published | published |
| Contract, reference bundle, case bundles | published | published |
| Round seed | hash commitment only | published |
| Ground-truth diplotypes | withheld | published |
| Integrity-probe map | withheld | published |
| Rarity, difficulty and stratum weights | withheld | published |

This makes every score independently reproducible after the fact without making any score
predictable in advance. The scoring constants live in the private
`contract.json → scoring` block; they are named in `SECRET_KEYS` and stripped from every
miner-facing artifact, so this document describes mechanisms, not numbers.

## Stage 1 — Structural validation

Verifies that a submission is well-formed, bound to the right round and the right uid, and
pharmacogenomically parseable. Whole-submission checks run first, in a fixed order; then
each gene call is checked independently.

Uid binding is checked **before everything else, including `round_id`**. The scored
identity must be the S3 slot the submission was fetched from, not the uid the submission
declares — a declared uid is just a number anyone can write, and without the check two
colluding miners could both bank one bundle's work.

The full list of rejection reasons, and what triggers each, is in
[`submission_format.md`](submission_format.md#rejection-reasons). Stage 1 contributes no
score of its own; it is a filter.

## Stage 2 — Diplotype resolution accuracy

The scientific core. Each submitted diplotype is compared against ground truth on a
**graded** scale, because the failure modes are not equivalent. Outcome classes are
evaluated in descending credit order and the first match wins:

| class | meaning |
|---|---|
| `EXACT_MATCH` | identical after normalisation, including copy number and suballele |
| `CORE_MATCH_SUBALLELE_OFF` | right core alleles, wrong suballele |
| `FUNCTION_CLASS_MATCH` | wrong alleles that nevertheless carry the same CPIC function assignments |
| `CN_MATCH_ALLELE_WRONG` | right total copy number, wrong allele assignment |
| `ONE_HAPLOTYPE_CORRECT` | one haplotype shared with truth |
| `NO_MATCH` | no correspondence |
| `NO_CALL_AMBIGUOUS` | abstention on a case the round independently classifies as unresolvable — partial credit |
| `NO_CALL_RESOLVABLE` | abstention on a resolvable case — zero |
| `ALTERNATIVE_MATCH` | a listed alternative was right; its credit scaled by the declared probability and then discounted |

`CN_MATCH_ALLELE_WRONG` is **gated on the round declaring copy-number-bearing genes**. At
the `genotype_calls` tier every diplotype has copy number 2, so leaving it enabled would
award its credit to *every* wrong answer and make `ONE_HAPLOTYPE_CORRECT` unreachable — a
substantial free floor for modal farming.

Two weightings multiply the credit:

- **Allele rarity in the round's population**, referenced against the gene's modal
  diplotype frequency and clipped at both ends. Calling the modal diplotype is expected of
  any pipeline and weighted accordingly; rare alleles carry substantially more.
- **Difficulty**, composed from structural-variant class, call-completeness band and
  whether the case is ambiguous.

Credits are summed with `math.fsum`, and calls are sorted by `(case_id, gene)` before
summation, so the result is order-independent to the last bit. With more than one validator
an order-dependent sum is a consensus break, so the class of bug is removed rather than
papered over.

## Stage 3 — Phenotype and prescribing concordance

Three checks, the third of which gates the other two.

**Internal consistency, first.** Stage 3 recomputes the phenotype, the activity score and
the implied copy number from the miner's **own** submitted diplotype under the published
CPIC tables. Any disagreement zeroes the call's entire phenotype and prescribing
contribution and is logged to `inconsistency_report.json`.

This is what makes the other two checks meaningful. Without it, a miner could ignore the
sequence data entirely and predict the population-modal phenotype, which is correct
surprisingly often. Because the phenotype must be mechanically derivable from a diplotype
Stage 2 scores independently, that strategy fails on both stages at once. Internal
inconsistency is reported separately from inaccuracy: being wrong is a scoring outcome,
being incoherent is a defect.

**Phenotype concordance.** Does the submitted phenotype match the phenotype CPIC assigns to
the case's *true* diplotype? Exact, adjacent, or wrong. "Adjacent" is **round-dependent and
derived, not hand-written**: two phenotypes are adjacent when they produce the *same CPIC
action* for some drug in this round's drug set, through that gene's own lookup key. A call
on the wrong side of a clinically actionable boundary is not adjacent.

**Prescribing concordance.** Does the submitted action match the CPIC recommendation for
the true phenotype and this drug, in the declared sub-population?

| outcome | condition |
|---|---|
| `REC_EXACT` | action and `cpic_classification` both right |
| `REC_ACTION_ONLY` | right action, wrong strength |
| `REC_SAME_DIRECTION` | wrong action, same direction class |
| `REC_ABSTAINED` | submitted `no_recommendation` where one exists |
| `REC_WRONG` | wrong, no direction relationship |
| `REC_OPPOSITE` | opposite direction — zero, and logged to `inverted_recommendations.json` as the patient-safety case |
| `REC_MISSING` | no recommendation entry submitted at all — zero |

`REC_MISSING` and `REC_ABSTAINED` are deliberately different. Omitting the block is not
abstention; an earlier version returned abstention credit for it and handed a miner that
never populated `recommendations` a free floor on every call.

Stage 3's composite credit is weighted by **rarity only**, not by difficulty: the clinical
step is a table lookup whose difficulty does not scale with call completeness or structural
complexity, so weighting it by those would double-count signal already captured upstream.
Getting the clinical answer right on a rare genotype does matter more, so rarity stays.

## Stage 4 — Confidence calibration

Reported confidences are scored against whether the call was an **exact match**, with a
strictly proper scoring rule: a Brier score, converted to a skill score against the
miner's own empirical exact-match rate, then mapped linearly to a factor in `[floor, 1]`.

Under a strictly proper scoring rule a miner maximises expected score **only** by reporting
its true belief. There is no threshold to tune and no confidence policy to police;
systematic overconfidence and uniform placeholder confidences both reduce the score
automatically. A miner is rewarded for saying "this case is ambiguous and I am 55% sure"
rather than asserting a guess at full confidence — which is the behaviour a clinical
pipeline should have, and which no existing PGx benchmark measures.

No-calls and probe calls are excluded from the calibration pairs.

**Guards fail to the floor, not to neutral.** Too few scored calls to evaluate, a zero
empirical match rate, or a NaN all resolve to the floor. A guard that resolved an
unevaluable submission to 1.0 would let a maliciously sparse submission claim the maximum
factor by being too small to evaluate; a gate must not rely on another gate to be safe.

**A submission that matched every scored call is the one exception.** Its empirical rate is
1.0, so the reference Brier score `base × (1 − base)` is zero and the skill score is
undefined rather than bad — dividing by a clamped epsilon would send it to −10⁷ and clip a
perfect submission to the floor for declaring 0.9 instead of 1.0, making overconfidence pay.
That case resolves on accuracy alone, to a factor of 1.0, and reports a null skill score.
The N floor still applies first, so it cannot be reached by submitting too little to score.

A full Murphy decomposition (reliability − resolution + uncertainty) and a ten-bin
reliability table are written to `calibration_diagnostics.json` for diagnosis. They are
reported, not scored, and they stay **validator-local**: the miner picks which bin each of
its calls falls into by choosing the confidence, so a bin holding a single call would report
that call's hidden `exact_match` label outright. The aggregate figures a miner needs to tune
confidence are in the breakdown instead — see *Reading the breakdown*.

Applied as a multiplicative gate on the submission's reward.

## Stage 5 — Difficulty coverage and simulator independence

**Difficulty coverage.** Performance is decomposed across difficulty strata on three axes
— allele rarity band, structural-variant class, and data quality (call completeness at
`genotype_calls`) — and composed as a **geometric mean** of per-stratum means, each clipped
to a floor. Geometric composition means strong performance on easy strata cannot compensate
for abandoning the hard ones: a miner that resolves every common diplotype and abstains on
everything rare is scored down sharply, not averaged into the middle.

The denominator is every `(case, gene)` the miner was **issued**. Anything not scored —
omitted, or rejected at Stage 1 — enters its stratum as zero.

Strata holding too few calls to estimate are merged with their neighbours before
composition, one axis at a time in the published `merge_order`. The merge is **local**: only
a neighbourhood containing a deficient stratum collapses, where a neighbourhood is the set
of strata agreeing on every axis except the one being relaxed. An earlier implementation
relaxed an axis *globally* as soon as any single stratum was deficient, which erased that
axis for the whole submission and silently removed two thirds of the coverage gate. Strata
still below the minimum after every axis has been relaxed are kept as they are and reported
in the log — clipping an under-powered stratum is safer than pooling unrelated difficulty
classes to manufacture a denominator.

**Simulator independence.** Every bundle carries a fixed number of **integrity probes**:
cases whose true diplotype uses an allele that is real, clinically assigned and fully
scoreable, but that carries no frequency estimate in the round's declared ancestry group.
Ordinary sampling can never produce one.

A miner that resolves over the full allele set handles a probe exactly as it handles any
other case. A miner that narrowed its search to what the generator emits has no consistent
explanation for one. The factor is the ratio of the miner's mean on probe calls to its mean
on ordinary calls, clipped to `[floor, 1]` — so performing *better* on probes confers no
advantage; the check is one-sided.

The denominator is the **greater** of two candidates: the miner's mean on ordinary calls in
the probes' own difficulty strata, and its mean across every ordinary issued call. Probes
always land in the rarest stratum, so the difficulty-matched set is small and externally
identifiable, and a miner could otherwise raise the ratio by deliberately failing exactly
those calls. Flooring the denominator at the whole-bundle baseline removes that lever:
moving the ratio now requires degrading the whole submission, which costs more through
accuracy and coverage than the gate is worth.

Probes are kept out of the coverage strata, out of the Stage 2 and Stage 3 raw sums, and
out of the achievable ceiling. They are off-population by construction, so their rarity
weight pins to the clip maximum; including them would both inflate and gate the same score.

Probes are identifiable by a miner that looks, and that is deliberate: identifying one
requires having already searched the full allele set, which is the behaviour being
measured.

> **Scope.** This gate covers independence from the generator's **allele prior**. It does
> not cover independence from the observation **renderer** — the missingness model, the
> file conventions, and the read simulator once aligned-read tiers exist. That requires
> held-out renderer configurations, which are specified and not yet built. We would rather
> state the boundary than let "simulator independence" be read as broader than it is.

### Honeypot gate failure modes

If a submission carries fewer than `MIN_HONEYPOT_N` probe calls, or was issued no ordinary
calls at all, the factor **defaults to 1.0** and a warning is written to
`stage5_summary.json`. That submission is scored with *no* integrity gate. Both conditions
are bundling bugs, not miner behaviour — hence a loud warning rather than a penalty.

## Score composition

```
raw_score    = Σ (stage2_credit × rarity × difficulty)
             + Σ (PHENO_W × stage3_credit × rarity)
final_reward = raw_score × calibration_factor
final_score  = final_reward × stratum_coverage_factor × honeypot_integrity_factor
```

Stage 1 is a filter and contributes no score. Stages 4 and 5 produce multiplicative factors
in `[0,1]`, each with a floor.

**There is no volume term.** Every miner in a round receives the same number of cases, and
the cap is enforced by whole-submission rejection. Reward is earned by being more accurate,
more honest and more robust — never by submitting more.

### Reading the breakdown

`final_score` is a **sum**: it scales with `n_cases` and with the tier's difficulty ceiling,
so it is not comparable across rounds. Two normalised figures in the breakdown are:

- `raw_score_fraction` — `raw_score / achievable_max`: how much of the round's available
  credit the miner earned on accuracy alone, before any gate.
- `score_fraction` — the same figure after calibration, coverage and integrity are applied.

`achievable_max` is what this round's issued, non-probe calls would be worth if every one
scored an exact match with full Stage 3 credit, at the weights this round actually drew.

Four Stage 4 aggregates are also in the breakdown, so a miner can tell a calibration gate
from an accuracy problem without guessing:

- `n_calibration_calls` — how many calls carried a usable confidence. This is the
  **denominator for the three figures below**, and it is smaller than the bundle: no-calls,
  probe calls and non-numeric confidences are all excluded.
- `brier_score` — mean squared error of the reported confidences, over those calls.
- `empirical_exact_match_rate` — the exact-match rate over those same calls. It is **not**
  accuracy over the bundle: a submission that no-calls most of its calls and nails the rest
  shows a high rate here next to a low `raw_score_fraction`.
- `brier_skill_score` — `brier_score` against that rate as the reference, and the figure the
  calibration factor is a linear map of. `null` means it was not evaluable (no scored calls,
  or every scored call matched).

Every one of these is an aggregate over the whole submission. Per-call and per-bin
diagnostics stay validator-local, for the reason given under Stage 4.

## Determinism

Every stage is a deterministic function of the contract, the case bundle and the
submission. No stage draws from an unseeded random source and no stage depends on the order
in which cases appear in a submission. Concretely:

- all score sums use `math.fsum` over `(case_id, gene)`-sorted lists
- bundle assignment is a keyed sort on a SHA-256 digest, not an RNG shuffle
- stratum merging visits neighbourhoods in sorted key order and concatenates in sorted
  member order

Independent validators scoring the same submission produce identical results, byte for
byte. This is a hard requirement, not an aspiration — it is what makes multi-validator
consensus possible.

## From score to weight

`set_weights()` (`niome_subnet/base/validator.py`) turns the normalised `score_fraction`
per uid into on-chain weights. `final_score` remains in the result for auditing, but is not
used to rank miners because its achievable ceiling differs between disjoint bundles:

1. `SCORING_SYSTEM` selects the shape. The shipped value is `"top"`: only the top
   `TOP_MINER_COUNT` positive-scoring miners receive weight, allocated by
   `SCORE_DISTRIBUTION` (renormalised if fewer qualify). `"linear"` distributes
   proportionally to score instead.
2. Weights are normalised and clipped to the subnet's `max_weight_limit`.
3. `BURNING_RATE` of the total is routed to `OWNER_HOTKEY`; the rest is the miners'.
4. The vector is converted to u16 and committed with `bt.set_weights`.

Score breakdowns are also pushed to the backend, and the highest-scoring submissions are
archived for post-round analysis.
