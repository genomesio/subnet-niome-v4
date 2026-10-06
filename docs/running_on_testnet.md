# Running on Testnet (netuid 289)

Testnet is where you shake out your setup before paying real registration costs. Emissions
are not real TAO, but registration still costs test TAO.

**Safety**
- Never expose private keys.
- Use a dedicated testnet wallet. Do not reuse your mainnet password.

## 1. Install

```bash
git clone https://github.com/genomesio/subnet-niome.git
cd subnet-niome
curl -LsSf https://astral.sh/uv/install.sh | sh   # if needed
uv sync
source .venv/bin/activate
uv pip install bittensor-cli
```

## 2. Create a testnet wallet

```bash
btcli wallet new_coldkey --wallet.name niome_test
btcli wallet new_hotkey  --wallet.name niome_test --wallet.hotkey miner_test
```

Get test TAO from the Bittensor Discord faucet channel, then confirm:

```bash
btcli wallet balance --wallet.name niome_test --subtensor.network test
```

## 3. Register on netuid 289

```bash
btcli subnet register \
  --netuid 289 \
  --wallet.name niome_test \
  --wallet.hotkey miner_test \
  --subtensor.network test
```

Confirm your uid, which every submission is bound to:

```bash
btcli subnet list --subtensor.network test
btcli wallet overview --wallet.name niome_test --subtensor.network test
```

## 4. Run a miner

```bash
export PYTHONPATH="$PYTHONPATH:$(pwd)"

python neurons/miner.py \
  --netuid 289 \
  --network test \
  --wallet niome_test \
  --wallet-hotkey miner_test \
  --axon.port 8091
```

`--netuid` defaults to `TESTNET_UID` (289), so omitting it targets testnet — but pass it
explicitly so the command reads the same on both networks.

Your port must be reachable from the internet; the validator POSTs to
`http://<your_ip>:<port>/forward`. Verify from another host:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://<your_ip>:8091/forward -d '{}'
```

A `4xx` is the correct answer — the request is unsigned and gets rejected. A connection
timeout means your port is not open.

Then continue with [`miner_guide.md`](miner_guide.md): the skeleton acknowledges tasks but
`process_task()` is a stub, so an unmodified miner uploads nothing and scores zero.

## 5. Run a validator

Validating on testnet needs stake for a validator permit, AWS credentials in `.env`, and a
backend task source for netuid 289. Task fetches are signed with your hotkey and carry the
netuid, so the hotkey must be registered there.

`scripts/run_validator.sh` hardcodes `--netuid 55 --network finney`, so for testnet run
the neuron directly:

```bash
source .venv/bin/activate
export PYTHONPATH="$PYTHONPATH:$(pwd)"

python neurons/validator.py \
  --netuid 289 \
  --network test \
  --wallet niome_test \
  --wallet-hotkey validator_test \
  --wandb.off
```

W&B runs are routed to `wandb.testnet_project_name` automatically when `--netuid` is 289.

See [`validator_guide.md`](validator_guide.md) for prerequisites, round cadence and
troubleshooting.

## 6. Testing without the chain

To exercise the scoring pipeline on your own machine, work against the standalone reference
release in `task_doc/release/`: it carries numbered generator scripts, both validator
stages, two example miner submissions and the test suite
(`tests/test_determinism.py`, `test_redaction.py`, `test_scoring_invariants.py`,
`test_adversarial_fixtures.py` and others). That is the fastest loop for miner development
— no registration, no round timing.

Note that the reference release binds submissions on `miner_hotkey` while the live subnet
binds on `miner_uid`. See
[`submission_format.md`](submission_format.md#miner_uid).
