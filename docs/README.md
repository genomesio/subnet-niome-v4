# NIOME Subnet Documentation

NIOME (SN55) is a Bittensor subnet for **pharmacogenomic star-allele inference**: miners
recover star-allele diplotypes, CPIC phenotypes and CPIC prescribing actions from
realistic, imperfect genomic observations, and validators score them against ground truth
held by construction.

| Document | Read it if you want to |
|---|---|
| [`architecture.md`](architecture.md) | Understand the round lifecycle, the repo layout, and how validators, miners and the backend fit together. |
| [`miner_guide.md`](miner_guide.md) | Set up, register and run a miner, and implement the submission path. |
| [`submission_format.md`](submission_format.md) | Write a submission that passes structural validation — exact schema, per-gene vocabularies, rejection reasons. |
| [`validation.md`](validation.md) | Know what each of the five validation stages measures and how the final score composes. |
| [`validator_guide.md`](validator_guide.md) | Set up and operate a validator. |
| [`running_on_testnet.md`](running_on_testnet.md) | Register and run against testnet (netuid 289). |
| [`running_on_mainnet.md`](running_on_mainnet.md) | Register and run against mainnet (netuid 55). |

## The task in one paragraph

Each round, the subnet synthesises a population of patient cases from public CPIC allele
frequency tables and hands every miner its own disjoint bundle of cases. A case gives you
genotype calls over the defining positions of the round's active genes — unphased, with
realistic missingness — plus clinical context. You return, per case and per active gene,
the star-allele diplotype, the derived activity score and CPIC phenotype, the CPIC
prescribing action for each queried drug, and a calibrated confidence. The validator
compares against the diplotype the case was generated from.

**The answer is not in any file you can download.** CPIC's tables are public and you are
expected to use them, but the diplotype is a property of your round's generated data,
derived from a seed published only after the round closes.

## Current scope (what the live code does)

- **Genes:** CYP2C19, TPMT, CYP2C9, SLCO1B1, CYP3A5 — the v3 gene set.
- **Drugs:** clopidogrel (CYP2C19), azathioprine (TPMT), celecoxib (CYP2C9),
  simvastatin (SLCO1B1), tacrolimus (CYP3A5). One drug per gene, each exercising a
  different CPIC recommendation lookup-key shape.
- **Observation tier:** `genotype_calls` only. `generate_observations()` raises on any
  other tier, so there are no aligned-read rounds today — nothing about BAMs, coverage or
  sequencing depth applies to a round you can currently receive.
- **Reference data:** CPIC tables via a pinned PharmCAT commit, plus a dated
  ancestry-stratified CPIC frequency snapshot.
