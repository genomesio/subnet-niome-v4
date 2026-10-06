# NIOME : Bittensor Subnet (SN55) for Pharmacogenomic Star-Allele Inference

Welcome to the NIOME Subnet. This repository contains the validator, the miner skeleton and
the reference-data pipeline for a decentralized pharmacogenomic genotype-to-phenotype
benchmark.

![niome logo image](docs/logo.png)

## Overview

**NIOME** is a Bittensor subnet for **pharmacogenomic star-allele inference**. Each round,
the subnet synthesises a population of patient cases from public CPIC allele-frequency
tables and issues every miner its own disjoint bundle. Miners recover the star-allele
diplotype for each active gene from realistic, imperfect genomic observations, derive the
CPIC phenotype and prescribing action, and report calibrated confidence. Validators score
them against ground truth held by construction.

The work is divided so that the hard part is the part that earns:

> The **subnet** synthesizes the population — deterministic, cheap, done once per round
> from public CPIC frequency tables.
> The **miner** recovers star-allele diplotypes, phenotypes and CPIC prescribing actions
> from realistic, imperfect observational data — genuinely hard, and unsolved for CYP2D6.
> The **validator** compares against ground truth held by construction — a string
> comparison and a table lookup.

The property that makes it work: **the answer is not in any file a miner can download.**
CPIC's tables are public and miners are expected to use them, but the diplotype is a
property of the round's generated data, derived from a seed published only after the round
closes. Scoring is a deterministic table lookup, so validation is cheap enough for
high-frequency rounds and fully auditable afterwards.

## Purpose

Pharmacogenomic star-allele calling is a real, unsolved bioinformatics problem with direct
clinical consequence. Existing benchmarks are thin:

- reference materials cover a few dozen samples, heavily weighted to common European
  haplotypes
- open-source callers (Aldy, Cyrius, StellarPGx, PharmCAT, PyPGx) disagree precisely on the
  hard cases, and there is no scaled arbiter
- no benchmark scores the **full inference chain** — genotype → activity score → phenotype
  → prescribing action — for internal coherence
- no benchmark scores **calibration**, although a clinical pipeline that cannot say "this
  one is ambiguous" is the one that causes harm

NIOME supplies all four: unbounded case volume across ancestry groups, graded scoring
concentrated on rare and structurally complex alleles, an internal-consistency requirement
across the whole chain, and a strictly proper scoring rule on reported confidence.

## System flow

1. **Round generation (backend → validators).** The backend generates the round contract
   and the ground-truth population, served to validators as presigned URLs. Validators
   build the pinned CPIC/PharmVar reference bundle at startup.

2. **Per-miner instancing (validators).** Validators render each case to observational data
   and partition the case pool into **disjoint** bundles, one per miner, seeded from
   `H(master_seed ‖ round_id ‖ uid)`. Because the correct diplotype is one short string
   identical for every honest solver, this makes copying *worthless* rather than merely
   detectable.

3. **Distribution (validators → miners).** Each miner receives two presigned URLs: a GET
   for its bundle and a PUT for its submission. Bundles carry the redacted public contract
   — built from an 11-key allowlist, with a recursive check that no secret survived nested
   inside an allowlisted key.

4. **Inference (miners → validators).** Miners call the diplotype per active gene, derive
   activity score and phenotype by table lookup, issue the CPIC prescribing action for each
   queried drug, and report confidence. Submissions are uploaded to the presigned URL.

5. **Five-stage validation (validators).**
   - **Stage 1** — structural gate: round and uid binding, case membership, diplotype
     syntax, per-gene phenotype vocabulary, copy-number consistency
   - **Stage 2** — diplotype resolution accuracy, graded, weighted by allele rarity and
     difficulty
   - **Stage 3** — phenotype and prescribing concordance with CPIC, **gated on internal
     consistency** with the miner's own diplotype
   - **Stage 4** — confidence calibration under a strictly proper scoring rule
   - **Stage 5** — difficulty coverage across strata, and integrity probes that detect a
     miner who learned the generator rather than the biology

6. **Scoring and rewards (validators → network).**

   ```
   raw_score    = Σ (resolution accuracy × rarity weight × difficulty weight)
   final_reward = raw_score × calibration_factor
   final_score  = final_reward × stratum_coverage_factor × honeypot_integrity_factor
   ```

   **No volume term.** Case count is fixed per round and identical for every miner.

## Current scope

| | |
|---|---|
| **Genes** | CYP2C19, TPMT, CYP2C9, SLCO1B1, CYP3A5 |
| **Drugs** | clopidogrel, azathioprine, celecoxib, simvastatin, tacrolimus — one per gene, each exercising a different CPIC recommendation lookup-key shape |
| **Observation tier** | `genotype_calls` (unphased VCF over defining positions, with realistic missingness). Aligned-read tiers are on the difficulty runway and not yet built. |
| **Populations** | six CPIC biogeographic groups with frequency coverage on these genes |
| **Reference data** | CPIC tables via a pinned PharmCAT commit, plus a dated ancestry-stratified CPIC frequency snapshot |

The difficulty runway adds aligned reads, then DPYD and CYP2D6, then copy number, then
`CYP2D6–CYP2D7` hybrids and phenoconversion. CYP2D6 structural variation is the most
expensive part of the data work and is deliberately off the critical path.

## Core features

**Ground truth by construction.** Each case is generated *from* a known diplotype. Scoring
is a comparison against a known answer, not a consensus or plausibility judgement.

**Per-miner case bundles.** Disjoint and uid-derived, so mirroring another miner's answers
returns answers to different cases.

**Internal-consistency enforcement.** The phenotype, activity score and copy number must be
mechanically derivable from the miner's *own* submitted diplotype. Without this, predicting
the population-modal phenotype and ignoring the data scores well; with it, that strategy
collapses on Stage 2 and Stage 3 simultaneously.

**Integrity probes.** A fixed number of cases per bundle use alleles that are real,
clinically assigned and fully scoreable but carry no frequency estimate in the round's
ancestry group. Ordinary sampling cannot produce one, so a caller that narrowed its search
to the frequency table has no consistent answer — and a one-sided ratio gate scales its
whole submission down.

**Calibration by theorem, not by threshold.** Confidence is scored with a Brier skill score
against the miner's own base rate. Reporting true belief maximises expected score, so there
is no confidence policy to tune or police.

**Determinism.** Every stage is a deterministic function of the contract, the bundle and
the submission: order-independent summation throughout, keyed sorts instead of RNG
shuffles, and no unseeded randomness. Independent validators produce byte-identical
results, which is what makes multi-validator consensus possible.

## Documentation

Start at [`docs/README.md`](docs/README.md).

- [Architecture and round lifecycle](docs/architecture.md)
- [Miner setup](docs/miner_guide.md)
- [Submission format](docs/submission_format.md)
- [Validation and scoring](docs/validation.md)
- [Validator setup](docs/validator_guide.md)
- [Running on testnet](docs/running_on_testnet.md) · [mainnet](docs/running_on_mainnet.md)

Design specifications, the standalone reference release and its test suite live in
[`task_doc/`](task_doc/README.md).

## Quick start

```bash
git clone https://github.com/genomesio/subnet-niome.git
cd subnet-niome
uv sync && source .venv/bin/activate
```

Miner:

```bash
export PYTHONPATH="$PYTHONPATH:$(pwd)"
python neurons/miner.py --netuid 55 --network finney \
  --wallet your_coldkey --wallet-hotkey your_hotkey --axon.port 8091
```

`neurons/miner.py` handles transport, authentication and the request lifecycle;
`process_task()` is a stub you implement. That is the whole competitive surface — read
[`docs/miner_guide.md`](docs/miner_guide.md) and
[`docs/submission_format.md`](docs/submission_format.md) first.

Validator:

```bash
chmod +x entrypoint.sh
./entrypoint.sh --wallet your_coldkey --wallet-hotkey your_hotkey
```

## Community

For real-time discussions, community support and updates,
<a href="https://discord.com/invite/bittensor">join the Bittensor Discord</a>.

## License

This repository is licensed under the MIT License.

```text
# The MIT License (MIT)
# Copyright © 2024 Opentensor Foundation
# Copyright © 2025 Genomes.io

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
```
