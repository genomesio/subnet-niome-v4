# Miner Setup Guide

To mine on NIOME (SN55) you set up a machine, register a hotkey, run the miner server, and
implement the submission path. The miner skeleton in `neurons/miner.py` handles transport,
authentication and the request lifecycle; **`process_task()` is a stub you implement** —
that is the whole competitive surface.

Read [`submission_format.md`](submission_format.md) before you write a line of calling
code, and [`validation.md`](validation.md) to understand what earns.

## 1. Prerequisites

- **OS:** Ubuntu 22.04 or similar Linux. Not supported on Windows.
- **Python:** 3.12 (see `.python-version`).
- **Git:** required.
- **Hardware:** 8+ vCPU and 16 GB RAM is comfortable. No GPU is needed — at the
  `genotype_calls` tier the task is combinatorial diplotype resolution over a few hundred
  defining positions, not model inference. A few GB of disk for the reference bundle and
  your round bundles.
- **Networking:** one inbound TCP port reachable from the internet (default `8091`), and
  outbound HTTPS to S3.

## 2. Environment setup

```bash
git clone https://github.com/genomesio/subnet-niome.git
cd subnet-niome
```

The project is managed with [`uv`](https://docs.astral.sh/uv/) and declares its
dependencies in `pyproject.toml` with a committed `uv.lock`. There is no
`requirements.txt`.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if you do not have uv
uv sync
source .venv/bin/activate
```

## 3. Wallet creation and registration

```bash
uv pip install bittensor-cli

btcli wallet new_coldkey --wallet.name your_coldkey
btcli wallet new_hotkey  --wallet.name your_coldkey --wallet.hotkey your_hotkey
```

Fund the coldkey with enough TAO to cover the registration fee, which fluctuates with
subnet competition, then register:

```bash
btcli subnet register --netuid 55 --wallet.name your_coldkey --wallet.hotkey your_hotkey
```

Note your **uid** — every submission is bound to it. Use netuid `289` to practise on
testnet first; see [`running_on_testnet.md`](running_on_testnet.md).

## 4. Running the miner

```bash
export PYTHONPATH="$PYTHONPATH:$(pwd)"

python neurons/miner.py \
  --netuid 55 \
  --network finney \
  --wallet your_coldkey \
  --wallet-hotkey your_hotkey \
  --axon.port 8091
```

The dotted Bittensor-style spellings (`--wallet.name`, `--wallet.hotkey`,
`--subtensor.network`, `--subtensor.chain_endpoint`) are accepted as aliases.

Use `pm2` or `tmux` so the miner stays up. The validator queries each miner **once** per
round during the broadcast window; a miner that is down when its turn comes gets nothing
that round.

By default the miner refuses requests from non-registered hotkeys and from hotkeys without
a validator permit. `--blacklist.allow_non_registered` and
`--blacklist.force_validator_permit` relax that; both are security risks and the miner
logs a warning when you do.

## 5. What arrives, and what you must return

The validator POSTs a signed request to `http://<your_ip>:<port>/forward`. The base class
verifies the signature and the blacklist, then calls your `forward()`, which parses a
`GenomicsTaskSynapse` and fires `process_task()` as a background task — it returns an empty
acknowledgement immediately rather than holding the connection open.

```python
class GenomicsTaskSynapse(BaseModel):
    task: Optional[Task] = None   # id, contract_url, truth_url (not miner-readable)
    bundle_url: str = ""          # presigned GET: your case bundle (.tar.gz)
    presigned_url: str = ""       # presigned PUT: where your submission goes
    timeout: Optional[float] = None
```

> ⚠️ The stub signature is `process_task(self, task, presigned_url)` and **drops
> `bundle_url`**. You need it — widen the signature and pass `synapse.bundle_url` through.

### Your budget

Both presigned URLs are minted with a 300-second lifetime
(`BUNDLE_URL_EXPIRY` / `SUBMISSION_TIMEOUT`). **You have about five minutes from receiving
the task to having your submission uploaded.** Download the bundle and PUT the result
inside that window; the inbound HTTP call itself is on a short timeout
(`FORWARD_TIMEOUT`, 20 s), which is why the work happens off the request.

### Bundle layout

```
<uid>/
  public_contract.json     the 11 miner-facing contract keys
  bundle_manifest.json     round_id, miner_uid, miner_seed, n_cases, case_ids
  cases/
    case_0164/
      manifest.json            case_id, round_id, tier, genes, loci, file names
      clinical_context.json    age band, sex, self-reported ancestry, indication,
                               queried_drugs, co-medications
      observations/calls.vcf   genotype calls over the active loci
```

`bundle_manifest.json` → `case_ids` is the exact set your submission must answer.

### The observations

VCF 4.2, one record per **defining position** of each active gene, `INFO` carrying
`GENE=<symbol>`:

```
#CHROM POS       ID          REF ALT QUAL FILTER INFO           FORMAT SAMPLE
chr10  94761900  rs12248560  C   .   .    PASS   GENE=CYP2C19   GT     0/0
chr10  94762856  rs1564657013 A  .   .    PASS   GENE=CYP2C19   GT     ./.
```

- **Unphased.** `GT` uses `/`, never `|`. Resolving the diplotype from unphased genotypes
  is where the difficulty at this tier comes from — hets at two defining positions admit
  more than one diplotype.
- No-calls are `./.`. They are **not** uniformly distributed: roughly twice as much of the
  missingness budget is aimed at informative positions as at uninformative ones, because
  that is what actually creates ambiguity.
- **No copy-number information.** Every diplotype at this tier has `total_copy_number` 2.
- Some cases are constructed to be **genuinely ambiguous** — more than one diplotype is
  consistent with the calls, and those diplotypes do not share a phenotype. Recognising
  them is part of the task; see `no_call` and `diplotype_alternatives`.

### The reference bundle

`pgx_reference/` is published per reference release and is identical for every miner and
for the validator. Use it freely — the validator uses the same files.

```
release.json              pinned CPIC/PharmVar versions, assembly, checksums
counts_manifest.json      per-gene allele counts across every namespace
action_mapping.json       how CPIC guidance maps to the seven-term action vocabulary
allele_definitions/       defining-variant sets per star allele, GRCh38 coordinates, rsIDs
allele_functionality/     CPIC allele function assignments and activity values
diplotype_phenotype/      CPIC diplotype → phenotype (and activity score) tables
recommendations/          CPIC drug recommendation tables, keyed per gene
frequencies/              ancestry-stratified allele frequencies per population
```

`diplotype_phenotype/` is a **complete lookup, not a formula**: CPIC enumerates every
unordered diplotype. If you find yourself writing activity-score band boundaries, you are
reimplementing something the table already answers, and you will get it wrong for at least
one gene.

Each gene's `recommendations/<GENE>.json` declares its own `lookup_key_type`. Read it.
Passing a phenotype term to CYP2C9 — which keys on the activity-score string — finds
nothing, does not raise, and silently costs you every CYP2C9 recommendation in the round.

### Then upload

Build the JSON described in [`submission_format.md`](submission_format.md) and `PUT` it to
`synapse.presigned_url`. Set `miner_uid` to **your uid** (not your hotkey) and `round_id`
to `public_contract.round_id`.

## 6. Which alleles to consider

`frequencies/<GENE>.json` records, per population, the `sampleable` set with renormalised
frequencies, the raw values, and an `excluded` breakdown giving the reason each remaining
allele was dropped.

**The obvious shortcut is to enumerate only over alleles that carry a frequency estimate
in the round's `population`. Do not.** Frequency is a **prior**, not a filter: an allele
with no published estimate in a population is not an allele that cannot occur in a patient
from it.

Your candidate set should be every allele that is definition-backed and has an actionable
function assignment — the `sampleable` set **plus** the alleles under
`excluded.null_frequency` and `excluded.zero_frequency`. Those two buckets sit *after* the
definition and function checks in the build, so everything in them renders and scores
exactly like any other allele; the only thing they lack is a frequency estimate here. Give
them the mass in `rules.off_prior_floor` (1e-6) rather than zero, so a diplotype using one
stays reachable when it is the only consistent explanation.

### Integrity probes

A fixed number of cases in your bundle are **integrity probes**: their true diplotype uses
at least one of those no-estimate alleles. Ordinary sampling cannot produce one, so a
caller that filtered by frequency has no consistent answer for a probe and will fail it.

Probe calls contribute to **no** score sum. They determine
`honeypot_integrity_factor`, a multiplicative gate on your whole submission with a floor of
0.20 — so failing them costs up to four-fifths of your reward while leaving every other
number you can see unchanged.

The gate is a ratio: your mean score on probe calls over your own mean on ordinary calls,
where the denominator is the **greater** of your mean in the probes' difficulty strata and
your mean across every ordinary call. Answering selected ordinary cases badly therefore
does not improve the ratio — it lowers your accuracy score and your difficulty coverage
while the denominator stays put.

You can work out which cases are probes, and you are welcome to. Working it out requires
searching the full allele set, which is the whole of what is being asked.

## 7. Recommended strategy

1. Parse the reference bundle once per release — definitions, functionality,
   diplotype→phenotype, recommendations, action mapping, frequencies.
2. Enumerate **every** diplotype consistent with the observed unphased calls, treating
   no-called positions as unconstrained. (`pgxlib.consistent_diplotypes()` is the
   validator's own implementation of this.)
3. Rank by a Hardy–Weinberg prior conditioned on the round's `population`, with
   `off_prior_floor` for alleles lacking an estimate. The posterior over the consistent set
   is a well-calibrated `confidence` essentially for free.
4. Derive activity score and phenotype **from your own called diplotype**, mechanically,
   by table lookup. Never predict the phenotype independently — Stage 3's internal
   consistency check zeroes incoherent calls, so a decoupled pipeline fails Stage 2 and
   Stage 3 at once.
5. Look up the recommendation using **that gene's own key type**, for the sub-population
   named in `drug_populations`, applying `fixed_gene_assumptions` where the CPIC key spans
   genes the round does not call.
6. Report genuine posterior probabilities as `confidence`, and list real alternatives in
   `diplotype_alternatives` when the data is ambiguous.
7. Spend effort on the hard strata. Easy cases are worth little; rare alleles and
   low-completeness cases are where reward concentrates, and the coverage gate means you
   cannot skip them.

Existing open-source tooling is permitted and encouraged as a baseline — **Aldy, Cyrius,
StellarPGx, PharmCAT, PyPGx**. They disagree with each other on exactly the cases that
carry the most weight, so matching any single one of them is not a winning strategy.

### Measured strategy comparison

From the reference release (`task_doc/release/miner/`), scored through all five stages on
a 50-case bundle:

| strategy | behaviour | final score, % of honest |
|---|---|---:|
| `posterior` | the honest reference implementation | 100% |
| `naive` | first consistent diplotype in canonical order, flat confidence 0.8 | 64.5% |
| `support_restricted` | posterior, candidate set narrowed to sampleable alleles | 25.5% |
| `overconfident` | ~70% accurate, confidence 1.0 throughout | 22.0% |
| `decoupled` | modal phenotypes with unrelated diplotypes | 11.7% |
| `modal` | population-modal diplotype everywhere, ignoring the data | 2.4% |
| `abstain` | perfect on modal, `no_call` on everything else | 0.2% |

`naive` and `posterior` differ by only 3 points of raw exact-match accuracy (93.0% vs
96.0%); the gap in final score comes almost entirely from calibration.

## 8. What you are not asked to predict

Pharmacokinetic parameters, plasma concentration curves, clinical outcomes, adverse-event
probabilities. The task is genotype resolution and guideline-conformant inference.
Everything downstream of the phenotype is a table lookup that you and the validator perform
from the same published files — which is precisely why it is not the part being scored.

The part being scored is the part that is still hard: **reading the genotype correctly.**
