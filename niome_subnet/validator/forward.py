# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2025 Genomes.io

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import asyncio
import bittensor as bt
import httpx
import logging
import niome_subnet.utils.settings as config
import numpy as np
import os
import time

from niome_subnet.api import (
    fetch_task,
    upload_final_submissions_to_server,
)
from niome_subnet.genomics.validation import benchmark_submission
from niome_subnet.genomics.validation.errors import ValidatorFault
from niome_subnet.protocol import GenomicsTaskSynapse
from niome_subnet.storage import SubmissionTooLarge, SubmissionTooLate, get_storage
from niome_subnet.utils import get_miner_uids, miner_score_fraction
from niome_subnet.genomics.subnet import generate_miner_bundles

logger = logging.getLogger(__name__)

URL_ATTEMPTS = 3


async def query_miner(self, uid: int, axon_endpoint: str, synapse: GenomicsTaskSynapse):
    """Send task to a single miner via HTTP and return the response."""
    try:
        url = f"http://{axon_endpoint}/forward"
        body = synapse.model_dump_json().encode()
        headers = bt.http_auth.sign(
            self.wallet,
            method="POST",
            path="/forward",
            body=body,
        )
        headers["Content-Type"] = "application/json"
        async with httpx.AsyncClient(timeout=config.FORWARD_TIMEOUT) as client:
            resp = await client.post(url, content=body, headers=headers)
            resp.raise_for_status()
    except Exception as e:
        logger.error(f"Error querying miner {uid} at {axon_endpoint}: {e}")
        return None

async def create_miner_urls(storage, uid: int) -> tuple[str, str] | None:
    """A miner's (bundle download URL, submission upload URL), retrying transient
    storage errors. Returns None if every attempt fails."""
    for attempt in range(1, URL_ATTEMPTS + 1):
        try:
            bundle_url = storage.create_download_url(
                config.bundle_key(uid), config.BUNDLE_URL_EXPIRY)
            upload_url = storage.create_upload_url(
                config.submission_key(uid), config.SUBMISSION_TIMEOUT)
            return bundle_url, upload_url
        except Exception as e:
            logger.warning(f"Creating URLs for miner {uid} failed (attempt {attempt}/{URL_ATTEMPTS}): {e}")
            if attempt < URL_ATTEMPTS:
                await asyncio.sleep(2 ** attempt)
    logger.error(f"Skipping miner {uid} this broadcast: could not create its URLs")
    return None

async def broadcast_task(self):
    logger.info("Broadcasting task ...")
    try:
        os.makedirs("data", exist_ok=True)

        self.miner_uids = get_miner_uids(self)
        task = fetch_task(self)
        logger.info(f"Fetched task {task.id}")

        storage = get_storage()

        generate_miner_bundles(self.miner_uids, storage)

        if self.task_id != task.id:
            self.collected_uids = []
            self.task_id = task.id

        self.save_state()

        np.random.shuffle(self.miner_uids)

        for uid in self.miner_uids:
            if uid in self.collected_uids:
                continue

            neuron = self.metagraph.neurons[uid]
            axon_endpoint = neuron.axon
            if axon_endpoint is None:
                continue

            urls = await create_miner_urls(storage, uid)
            if urls is None:
                # Not marked as collected, so the next broadcast tries this miner again.
                continue
            bundle_url, presigned_url = urls

            self.collected_uids.append(uid)
            self.save_state()

            synapse = GenomicsTaskSynapse(
                task=task,
                bundle_url=bundle_url,
                presigned_url=presigned_url,
                timeout=config.FORWARD_TIMEOUT,
            )
            await query_miner(self, uid, axon_endpoint, synapse)
    except Exception as e:
        logger.error(f"Error during broadcasting: {e}")

async def run_validation(self):
    logger.info("Validating miners' submissions ...")
    try:
        scores = []

        storage = get_storage()

        for uid in self.miner_uids:
            try:
                storage.download_file(
                    config.submission_key(uid),
                    config.MINER_SUBMISSION_FILE,
                )
                miner_score = benchmark_submission(uid)
                scores.append(miner_score)
                self.save_state()
            except ValidatorFault:
                # The validator's own environment is broken, so every remaining
                # miner would fail the same way. Abandon the round WITHOUT
                # setting weights: committing a field of zeros caused by a local
                # misconfiguration is worse than committing nothing.
                logger.critical(
                    f"Validator fault while scoring uid {uid}; abandoning this "
                    f"round without setting weights", exc_info=True)
                return
            except FileNotFoundError:
                logger.info(f"Skipping miner {uid}: no submission found")
                continue
            except (SubmissionTooLate, SubmissionTooLarge) as e:
                logger.warning(f"Skipping miner {uid}: {e}")
                continue
            except Exception:
                continue

        valid_scores = [score for score in scores if miner_score_fraction(score) > 0]
        logger.info(
            "Score fractions: %s",
            [(score.uid, miner_score_fraction(score)) for score in valid_scores],
        )

        self.set_weights(scores, self.task_id)
        valid_uids = [
            score.uid
            for score in sorted(
                valid_scores, key=miner_score_fraction, reverse=True
            )
        ]
        if len(valid_uids) > 0:
            upload_final_submissions_to_server(self, valid_uids)
        logger.info("Finished validation.")
    except Exception as e:
        logger.error(f"Error during validation: {e}")

async def forward(self):
    """
    The forward function is called by the validator every time step.

    It is responsible for querying the network and scoring the responses.

    Args:
        self (:obj:`bittensor.neuron.Neuron`): The neuron object which contains all the necessary state for the validator.

    """
    try:
        if config.BURNING_RATE == 1.0 and not self.are_weights_committed:
            self.are_weights_committed = True
            self.set_weights([], "")
            weights_dict = {int(uid): float(w) for uid, w in zip(self.uids, self.weights)}
            bt.set_weights(
                self.config.netuid,
                weights_dict,
                wallet=self.wallet,
                version_key=self.spec_version,
                network=self.network,
            )
        else:
            blocks = (self.block - config.BASE_BLOCK_NUMBER) % config.INTERVAL_BLOCKS

            if blocks < config.VALIDATION_BLOCK and not self.is_broadcasting:
                self.is_validating = False
                self.is_broadcasting = True
                asyncio.create_task(broadcast_task(self))
            if blocks >= config.VALIDATION_BLOCK and blocks < config.WEIGHT_SET_BLOCK and not self.is_validating:
                self.is_validating = True
                self.is_broadcasting = False
                self.are_weights_committed = False
                asyncio.create_task(run_validation(self))
    except Exception as e:
        logger.error(f"Error during forward step: {e}")

    time.sleep(5)
