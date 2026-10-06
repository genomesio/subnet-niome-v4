# Validator Setup Guide

Validators fetch the round from the NIOME backend, build the reference bundle, cut and
distribute per-miner case bundles, score every submission through the five-stage pipeline,
and commit weights. They must maintain high uptime and stable networking.

See [`architecture.md`](architecture.md) for the round lifecycle and
[`validation.md`](validation.md) for what the stages do.

## 1. Prerequisites

- **OS:** Ubuntu 22.04 or similar Linux.
- **Python:** 3.12 (see `.python-version`).
- **Git:** required at runtime, not just for cloning —
  `build_reference()` shallow-fetches a pinned PharmCAT commit on every start.
- **Node.js / npm:** `entrypoint.sh` installs PM2 through npm if it is not already there.
- **Hardware:** 16+ vCPU, 32 GB RAM, 200 GB SSD is comfortable. No GPU. The heavy step is
  bundle generation: the case pool must hold at least `n_miners × n_cases` cases, and each
  is rendered to VCF and copied into a per-miner bundle.
- **Outbound network:** HTTPS to `niome-api.genomes.io`, to S3, and to GitHub.
- **No Docker required.** At the `genotype_calls` tier the validator reads CPIC tables out
  of the PharmCAT repository directly; it does not run PharmCAT, and it does not need
  `bwa`, `samtools`, `bcftools` or `tabix`.

## 2. Environment setup

```bash
git clone https://github.com/genomesio/subnet-niome.git
cd subnet-niome
```

Dependencies are declared in `pyproject.toml` with a committed `uv.lock`; there is no
`requirements.txt`. `scripts/run_validator.sh` creates `.venv` and runs `uv sync` for you,
so install [`uv`](https://docs.astral.sh/uv/) first:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Credentials

The validator uploads bundles and reads submissions from its own S3 bucket. Copy
`.env.example` to `.env` and fill it in — `niome_subnet/utils/settings.py` loads it at
import:

```bash
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=...
AWS_S3_BUCKET=...
```

The credentials need `PutObject`, `GetObject` and presigning on
`niome/SARPP/*` in that bucket. Miners only ever see presigned URLs, never credentials.

### Registration

```bash
uv pip install bittensor-cli
btcli wallet new_coldkey --wallet.name your_coldkey
btcli wallet new_hotkey  --wallet.name your_coldkey --wallet.hotkey your_hotkey
btcli subnet register --netuid 55 --wallet.name your_coldkey --wallet.hotkey your_hotkey
```

Validating also requires stake sufficient for a validator permit. Backend task fetches are
authenticated by a hotkey signature over a canonical payload including your netuid, so the
hotkey must be registered on the netuid you pass.

## 3. Running the validator

```bash
chmod +x entrypoint.sh        # first time only
./entrypoint.sh
```

Run interactively and it prompts for wallet name, wallet hotkey, and optionally a W&B API
key (leave blank to skip). Or pass them:

```bash
./entrypoint.sh --wallet your_coldkey --wallet-hotkey your_hotkey \
                [--wandb.api_key YOUR_KEY]
```

`entrypoint.sh` installs PM2 if needed and starts `scripts/run_auto_update.sh` as the PM2
process **`niome-validator`**, then tails its logs. The chain is:

```
entrypoint.sh
  └─ pm2: niome-validator → scripts/run_auto_update.sh
        └─ scripts/run_validator.sh
              └─ .venv/bin/python neurons/validator.py --netuid 55 --network finney ...
```

- **`run_auto_update.sh`** polls `origin/main` every 60 seconds; on a new commit it stops
  the validator, pulls with rebase, reinstalls the package, and restarts.
- **`run_validator.sh`** creates and activates `.venv` if needed, runs `uv sync`, exports
  `PYTHONPATH`, and launches the neuron. It currently also downloads GRCh38 chromosome 11
  (`data/chr11.fa`, ~130 MB) — a leftover from the previous use case that the
  pharmacogenomics pipeline does not read. Budget the disk, or delete the block.
- `run_validator.sh` hardcodes `--netuid 55 --network finney`. Edit it for testnet, or run
  `neurons/validator.py` directly.

Useful commands:

```bash
pm2 logs niome-validator
pm2 restart niome-validator
pm2 delete niome-validator
```

### Running the neuron directly

```bash
source .venv/bin/activate
export PYTHONPATH="$PYTHONPATH:$(pwd)"
python neurons/validator.py \
  --netuid 55 --network finney \
  --wallet your_coldkey --wallet-hotkey your_hotkey \
  --wandb.off
```

Dotted aliases (`--wallet.name`, `--wallet.hotkey`, `--subtensor.network`,
`--subtensor.chain_endpoint`) are accepted. W&B is on by default — pass `--wandb.off`, or
`--wandb.offline` to buffer locally.

## 4. What happens on each round

`build_reference()` runs **once at startup**, not per round, and writes
`data/pgx_reference/`. It shallow-fetches the pinned PharmCAT commit into
`niome_subnet/genomics/.cache/pharmcat` and cross-checks it against the dated CPIC
frequency snapshot in `cpic_af_data/`, refusing to emit a bundle if they disagree about the
release. Expect the first start to take a few minutes.

Then, per 720-block interval:

| Block offset | Step |
|---|---|
| `0` | `fetch_task()` → `data/contract.json`, `data/truth.json` |
| `0` | `generate_observations()` → `data/cases/<case_id>/` |
| `0` | `export_miner_bundles()` → `data/bundles/<uid>/`, tar, S3 upload, `bundle_assignments` written back to `truth.json` |
| `0 – 449` | each miner queried once with its two presigned URLs |
| `450 – 699` | each submission downloaded to `data/submission.json` and scored; weights committed |

Per-miner scoring artifacts are written to fixed filenames (`data/valid_calls.json`,
`data/invalid_calls.json`, `data/stage3_results.json`, `data/final_reward.json`, plus
`stage5_summary.json`, `calibration_diagnostics.json`, `inconsistency_report.json` and
`inverted_recommendations.json` in the working directory). **One working directory scores
one submission at a time** — if you want to keep per-miner artifacts, copy them out between
uids.

## 5. Failure handling

The pipeline distinguishes two kinds of failure, and the distinction is load-bearing.

**Miner-owned.** A submission that is absent, truncated, non-JSON, bound to the wrong
round or uid, or structurally invalid scores zero. `benchmark_submission()` catches the
exception and returns a `MinerScore` with `rejected: True` and a reason.

**Validator-owned.** A `ValidatorFault` means the validator's *own* environment is broken:
it was pointed at a redacted public contract, `truth.json` has no `bundle_assignments`, or
the working directory holds artifacts from a different miner or round. The same fault would
zero every miner in the round, so `run_validation()` **abandons the round without setting
weights**. Committing a field of zeros caused by local misconfiguration is worse than
committing nothing.

If you see `abandoning this round without setting weights` in the logs, fix the environment
— do not restart and hope.

## 6. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `KeyError: master_seed` / `contract.json:scoring` | the validator is reading a redacted public contract. `assert_private()` is doing its job; check what `fetch_task()` downloaded. |
| `truth.json carries no bundle_assignments` | bundle export did not run or did not complete. Without it, case membership cannot be checked and cross-bundle submissions would pass. |
| `case pool of N is too small for M miners x K cases` | the backend's pool multiplier is too low for the current miner count. |
| `pool carries N integrity probes, too few for ...` | same, for the probe pool. The gate would be live for some miners and inert for others. |
| `bundles overlap on [...]` | per-miner instancing is broken — do not ship weights from that round. |
| `metagraph carries no miners` | every neuron has trust > 0; check `get_miner_uids()` against the metagraph. |
| `public contract still carries secret keys [...]` | a secret was nested inside an allowlisted contract key. Redaction caught it before publication. |
| `HeadObject 404` on every uid at scoring time | an S3 prefix mismatch. Derive keys only from `submission_key()` / `bundle_key()`. |
| `only N integrity-probe calls < MIN_HONEYPOT_N` in `stage5_summary.json` | that submission was scored with **no** integrity gate. A bundling bug, not miner behaviour. |
| `pharmcat commit mismatch` on startup | a stale `.cache/pharmcat`. Delete it and let the build refetch. |
| W&B `UsageError` at startup | missing or invalid API key. The validator logs a warning and continues without W&B. |

## 7. Configuration reference

Everything tunable lives in `niome_subnet/utils/settings.py`. Changing these changes
consensus behaviour — do not diverge from other validators without coordination.

| Setting | Shipped value | Meaning |
|---|---|---|
| `MAINNET_UID` / `TESTNET_UID` | `55` / `289` | netuids |
| `BASE_BLOCK_NUMBER` | `1843300` | round-schedule epoch |
| `INTERVAL_BLOCKS` | `720` | round length (~2.4 h) |
| `VALIDATION_BLOCK` | `450` | broadcast ends, scoring begins |
| `WEIGHT_SET_BLOCK` | `700` | scoring window ends |
| `FORWARD_TIMEOUT` | `20` s | HTTP timeout for the validator→miner call |
| `SUBMISSION_TIMEOUT` | `300` s | presigned PUT lifetime — the miner's upload budget |
| `BUNDLE_URL_EXPIRY` | `300` s | presigned GET lifetime for the bundle |
| `SCORING_SYSTEM` | `"top"` | `"top"` or `"linear"` weight shape |
| `TOP_MINER_COUNT` / `SCORE_DISTRIBUTION` | `10` / `[0.3, 0.2, 0.2, 0.15, …]` | top-k weight allocation |
| `BURNING_RATE` | `0.02` | share of weight routed to `OWNER_HOTKEY`; `1.0` burns the whole round |
| `AWS_S3_PREFIX` | `niome/SARPP` | S3 key prefix |
| `MAX_TASK_RETRIES` / `BASE_DELAY_SECONDS` | `3` / `2` | backend retry policy (exponential) |
