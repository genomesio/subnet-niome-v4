# Architecture

## Roles

| Actor | Responsibility |
|---|---|
| **Backend** (`niome-api.genomes.io`) | Generates the round: the private contract (`contract.json`) and the ground truth (`truth.json`), served to validators as presigned URLs. |
| **Validator** | Builds the reference bundle, renders observations, cuts disjoint per-miner case bundles, broadcasts them, scores returned submissions through five stages, sets weights. |
| **Miner** | Downloads its bundle, resolves diplotypes/phenotypes/recommendations, uploads one JSON submission. |

The division is deliberate: generating the population is deterministic and cheap, scoring
is a string comparison and a table lookup, and the only hard part — recovering the
genotype from imperfect observations — is the part that earns.

## Round lifecycle

Rounds are driven off block height, not wall clock. In
`niome_subnet/validator/forward.py`:

```
blocks = (current_block - BASE_BLOCK_NUMBER) % INTERVAL_BLOCKS
```

With the settings in `niome_subnet/utils/settings.py`
(`BASE_BLOCK_NUMBER = 1843300`, `INTERVAL_BLOCKS = 720`, `VALIDATION_BLOCK = 450`,
`WEIGHT_SET_BLOCK = 700`):

| Phase | Block offset | What happens |
|---|---|---|
| Broadcast | `0 – 449` | `broadcast_task()` — fetch task, build bundles, query every miner once. |
| Validation | `450 – 699` | `run_validation()` — download each submission, score it, set weights. |
| Idle | `700 – 719` | Waiting for the next interval. |

One interval is 720 blocks ≈ 2.4 hours at 12 s/block.

If `BURNING_RATE` is `1.0` the forward loop skips the round entirely and commits an
all-burn weight vector. At the shipped value (`0.02`) it runs normally and routes 2% of
weight to the owner hotkey.

### Broadcast, step by step

1. `get_miner_uids()` returns every metagraph neuron with `trust == 0` (validators are
   filtered out).
2. `fetch_task()` does a hotkey-signed `GET /api/v4/tasks/current`, then downloads
   `contract_url` → `data/contract.json` and `truth_url` → `data/truth.json`.
3. `generate_miner_bundles()`:
   - `generate_observations()` renders every case in `truth.json` into
     `data/cases/<case_id>/`.
   - `export_miner_bundles()` partitions the case pool into **disjoint** per-uid bundles,
     writes the redacted `public_contract.json` into each, tars them, uploads to S3, and
     records `bundle_assignments` back into `data/truth.json`.
4. For each miner uid, two presigned S3 URLs are minted — a GET for the bundle
   (`BUNDLE_URL_EXPIRY = 300 s`) and a PUT for the submission
   (`SUBMISSION_TIMEOUT = 300 s`) — and posted to the miner as a `GenomicsTaskSynapse`.

### Validation, step by step

For each uid, `run_validation()` downloads `niome/SARPP/<uid>.json` from S3 to
`data/submission.json` and calls `benchmark_submission(uid)`, which runs stages 1–2, 3, 4
and 5 in order. Then `set_weights()` converts scores to on-chain weights and
`submit_validation_result()` pushes the score breakdowns to the backend.

A `ValidatorFault` — raised when the validator's *own* environment is broken, e.g. it was
pointed at a redacted contract or has stale artifacts in the working directory — aborts
the round **without setting weights**. Committing a field of zeros caused by local
misconfiguration is worse than committing nothing.

## Per-miner instancing

Bundles are disjoint and derived from `sha256(master_seed ‖ round_id ‖ uid)`. Because the
correct diplotype is one short string identical for every honest solver, without this the
dominant strategy would be mirroring rather than solving. `export_miner_bundles()` asserts
at the end that no two bundles share a case.

Case assignment uses a **keyed sort** on a per-case SHA-256 digest, never an RNG shuffle:
bundle membership decides which `case_id`s are `unknown_case_id` for a given miner, so two
validators on different interpreters must derive identical bundles or honest submissions
get rejected.

## Redaction

`export_miner_bundles.py` builds `public_contract.json` from an **allowlist** of 11 keys:

```
round_id  seed_commitment  active_genes  population  drug_set  drug_genes
drug_populations  observation_tier  reference_release  rules  commit_reveal
```

Adding a key to the private contract does not add it to the public one. After projection
it walks the result recursively and raises if any name from `SECRET_KEYS` — `master_seed`,
`scoring`, `rarity_band`, `is_probe`, `bundle_assignments` and ~20 more — survives nested
inside an allowlisted key. The case `manifest.json` is likewise built to carry nothing that
reveals difficulty: no diplotype, no phenotype, no stratum membership, no no-call count.

Validators read secrets with `pgxlib.require()`, which raises on a missing key. Nothing in
the scoring path uses `contract.get(key, default)` — a defaulted secret would silently
score a whole round at a neutral weight.

## Transport and storage

Miners run a FastAPI server, not a `bt.Axon`. The validator POSTs to
`http://<axon_ip>:<port>/forward` with a `bt.http_auth` signature; the miner verifies it
with `bt.http_auth.verify` and applies `blacklist()`.

`GenomicsTaskSynapse` (`niome_subnet/protocol.py`):

```python
task: Task | None        # id, contract_url, truth_url
bundle_url: str          # presigned GET — the miner's case bundle (.tar.gz)
presigned_url: str       # presigned PUT — where the submission goes
timeout: float | None    # FORWARD_TIMEOUT
```

S3 keys are derived from helpers in `settings.py`, never written as literals, because
presigned URLs are key-bound:

```
niome/SARPP/<uid>.json             # miner submission (PUT by miner, GET by validator)
niome/SARPP/bundles/<uid>.tar.gz   # case bundle   (PUT by validator, GET by miner)
```

## Repo map

```
neurons/
  validator.py                    validator neuron; builds the reference at startup
  miner.py                        miner neuron; `process_task` is the stub you implement
niome_subnet/
  protocol.py                     GenomicsTaskSynapse
  validator/forward.py            round scheduling, broadcast, validation loop
  api/__init__.py                 signed backend calls (task fetch, score submit)
  base/                           neuron/miner/validator base classes, weight setting
  utils/settings.py               all tunables: block schedule, S3 keys, file paths, URLs
  genomics/
    pgxlib.py                     diplotype grammar, per-gene CPIC loaders, Reference
    subnet/build_reference.py     builds data/pgx_reference/ from pinned PharmCAT + CPIC
    subnet/generate_observations.py  renders cases to VCF + clinical context
    subnet/export_miner_bundles.py   redaction, disjoint bundling, S3 upload
    validation/stage12.py         Stage 1 structural gate + Stage 2 diplotype accuracy
    validation/stage3.py          phenotype/prescribing concordance + internal consistency
    validation/stage4.py          confidence calibration (Brier skill)
    validation/stage5.py          difficulty coverage + integrity-probe gate
data/                             round working directory (see below)
cpic_af_data/                     CPIC ancestry-stratified frequency snapshot
task_doc/                         design specifications and the standalone reference release
docs/                             this documentation
```

### `data/` working directory

| Path | Written by | Contents |
|---|---|---|
| `contract.json` | backend | **private** contract: everything below plus `master_seed`, `scoring`, `stratum_design`, `probe_design`, `population_admissibility` |
| `truth.json` | backend + stage 04 | ground-truth diplotypes, probe map, strata, `bundle_assignments` |
| `pgx_reference/` | `build_reference()` | allele definitions, functionality, diplotype→phenotype, recommendations, frequencies, `action_mapping.json`, `release.json`, `counts_manifest.json` |
| `cases/<case_id>/` | `generate_observations()` | `manifest.json`, `clinical_context.json`, `observations/calls.vcf` |
| `bundles/<uid>/` | `export_miner_bundles()` | `public_contract.json`, `bundle_manifest.json`, `cases/` |
| `submission.json` | `run_validation()` | the submission currently being scored |
| `valid_calls.json`, `invalid_calls.json`, `stage3_results.json`, `final_reward.json` | stages 1–4 | per-stage scoring artifacts |

Stage artifacts are written to **fixed filenames**, so one working directory scores one
submission at a time. Stages 3 and 4 cross-check `round_id` and `miner_uid` between
artifacts and raise `ValidatorFault` on a mismatch rather than silently combining one
miner's Stage 2 with another's Stage 3.

## Reference data build

`build_reference()` runs at validator startup and is pinned on two constants that move
together (`niome_subnet/genomics/subnet/build_reference.py`):

```python
PHARMCAT_COMMIT = "66ea91d3bfe7f46e2116fdf7e55af37434df3164"
EXPECTED_CPIC_DATA_VERSION = "2026-07-13-11-40"
```

It shallow-fetches that exact PharmCAT commit into
`niome_subnet/genomics/.cache/pharmcat` (so `git` must be on PATH), reads the CPIC
frequency snapshot from `cpic_af_data/cpic_allele_frequency.json`, and refuses to emit a
bundle if the commit's CPIC tables and the dated snapshot disagree about the release.

### `action_mapping.json` is a subnet artifact, not a CPIC field

CPIC does not publish the seven-term `action` vocabulary. It publishes a strength
classification, three structured booleans and prose. The enum is **derived** by
`classify_action()`: classify on the leading directive of the recommendation text, then
require CPIC's own booleans to corroborate. A mismatch fails the build rather than emitting
a quietly wrong action. The derivation ships in the reference bundle so miners and
validators use exactly the same one, and it is version-pinned via
`reference_release.action_mapping_version` (currently `1.1.0`).

The booleans are read from PharmCAT's `prescribing_guidance.json`, where they are
populated. The same fields in CPIC's REST API are false for every row, so a build
re-pointed at the API would fail corroboration on every `avoid` /
`alternative_therapy` / `reduced_dose` classification. Do not "fix" that by relaxing the
check.

## Per-gene rules are not uniform — the trap that does not crash

Loaded from each gene's own CPIC table at build time, never hand-transcribed, and
enforced by a per-gene loader class in `pgxlib.py`:

| gene | phenotype vocabulary | `lookup_key_type` | what the key looks like |
|---|---|---|---|
| CYP2C19 | metaboliser terms | `phenotype` | `"Poor Metabolizer"` |
| TPMT | metaboliser terms | `phenotype` (joint with NUDT15) | `"Intermediate Metabolizer"` |
| CYP3A5 | metaboliser terms | `phenotype` | `"Normal Metabolizer"` |
| SLCO1B1 | **function** terms | `phenotype` | `"Decreased Function"` |
| CYP2C9 | metaboliser terms | `activity_score` | `"1.0"`, `"0.5"`, `"n/a"` |

Four genes key on the phenotype term, one on a score, and the split does not follow the
phenotype vocabulary: CYP3A5 and CYP2C9 both use metaboliser terms, but CYP3A5's table is
keyed on the term and CYP2C9's on the score. SLCO1B1's key type is `phenotype` too — its
*vocabulary* is function terms, which is a different thing. One generic
`activity_score → phenotype` function, or one generic recommendation lookup, will silently
mislabel at least one gene — and it will not crash, it will just be wrong.

CYP2D6 and DPYD both key on the activity-score string, so the ratio shifts once the tiers
that activate them ship.

CPIC publishes the diplotype→phenotype tables **fully enumerated**, one row per unordered
diplotype. No banding arithmetic needs writing for any gene; anyone reimplementing a band
boundary is doing something wrong.
