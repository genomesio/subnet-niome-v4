# Running on Mainnet (netuid 55)

Mainnet emits real TAO and registration costs real TAO. Run on
[testnet](running_on_testnet.md) first and confirm your miner actually uploads a scoring
submission before paying to register here.

**Safety**
- Never expose private keys.
- Keep the coldkey offline; the hotkey is the only key the neuron needs.
- Do not reuse your testnet password.

## 1. Install

```bash
git clone https://github.com/genomesio/subnet-niome.git
cd subnet-niome
curl -LsSf https://astral.sh/uv/install.sh | sh   # if needed
uv sync
source .venv/bin/activate
uv pip install bittensor-cli
```

## 2. Wallet and registration

```bash
btcli wallet new_coldkey --wallet.name your_coldkey
btcli wallet new_hotkey  --wallet.name your_coldkey --wallet.hotkey your_hotkey
```

Fund the coldkey, check the current cost, then register:

```bash
btcli subnet list                      # shows the current registration cost for netuid 55
btcli subnet register \
  --netuid 55 \
  --wallet.name your_coldkey \
  --wallet.hotkey your_hotkey \
  --subtensor.network finney
```

Confirm your uid — every submission is bound to it:

```bash
btcli wallet overview --wallet.name your_coldkey --subtensor.network finney
```

Registration is competitive and uids are recycled: the lowest-performing uid is deregistered
when a new neuron registers. A miner that never uploads a submission will be replaced.

## 3. Run a miner

```bash
export PYTHONPATH="$PYTHONPATH:$(pwd)"

python neurons/miner.py \
  --netuid 55 \
  --network finney \
  --wallet your_coldkey \
  --wallet-hotkey your_hotkey \
  --axon.port 8091
```

Run it under `pm2` or `tmux`. The validator queries each miner **once** per round during
the broadcast window, so a miner that is down when its turn comes earns nothing that round.

Checklist before you expect a score:

- `process_task()` implemented — the shipped stub uploads nothing. See
  [`miner_guide.md`](miner_guide.md).
- `miner_uid` in the submission is **your uid**, not your hotkey.
- `round_id` copied from `public_contract.json`.
- Submission uploaded within ~5 minutes of receiving the task; the presigned PUT URL
  expires.
- Your axon port is reachable from the internet and registered on chain.

## 4. Run a validator

Validating requires stake for a validator permit and AWS credentials in `.env`. Full
prerequisites, round cadence and troubleshooting are in
[`validator_guide.md`](validator_guide.md).

```bash
chmod +x entrypoint.sh
./entrypoint.sh --wallet your_coldkey --wallet-hotkey your_hotkey
```

This starts the PM2 process `niome-validator`, which runs the auto-updater (polling
`origin/main` every 60 s) around `neurons/validator.py` on `--netuid 55 --network finney`.

## 5. Monitoring

```bash
pm2 logs niome-validator                                      # validator
btcli wallet overview --wallet.name your_coldkey --subtensor.network finney
btcli subnet metagraph --netuid 55 --subtensor.network finney
```

Validator score breakdowns are pushed to the NIOME backend each round and, when W&B is
configured, logged to the validator's W&B project.
