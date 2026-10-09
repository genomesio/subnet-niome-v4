# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2025 genomes.io

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
import argparse
import bittensor as bt
import copy
import logging
import numpy as np
import threading
import time

from datetime import datetime, timezone
from typing import List, Union
from traceback import print_exception

from niome_subnet.api import submit_validation_result
from niome_subnet.base.neuron import BaseNeuron
from niome_subnet.genomics.model import MinerScore, MinerScoreDto
from niome_subnet.utils import (
    add_validator_args,
    convert_weights_and_uids_for_emit,
    fetch_metagraph_with_retry,
    process_scores_linear,
    process_scores_top,
    score_fractions_by_uid,
    process_weights_for_netuid,
)

from niome_subnet.utils.settings import BURNING_RATE, OWNER_HOTKEY, SCORING_SYSTEM

logger = logging.getLogger(__name__)


class BaseValidatorNeuron(BaseNeuron):
    """
    Base class for Bittensor validators. Your validator should inherit from this class.
    """

    neuron_type: str = "ValidatorNeuron"

    @classmethod
    def add_args(cls, parser: argparse.ArgumentParser):
        super().add_args(parser)
        add_validator_args(cls, parser)

    def __init__(self, config=None):
        super().__init__(config=config)

        # Save a copy of the hotkeys to local memory.
        self.hotkeys = copy.deepcopy(self.metagraph.hotkeys)

        # Set up initial scoring weights for validation
        logger.info("Building validation weights.")
        n = len(self.metagraph)
        self.scores = np.zeros(n, dtype=np.float32)
        self.file_names = np.array([""] * n, dtype=object)

        # Init sync with the network. Updates the metagraph.
        self.sync()

        # Serve axon to enable external connections.
        if not self.config.neuron.axon_off:
            self.serve_axon()
        else:
            logger.warning("axon off, not serving ip to chain.")

        # Create asyncio event loop to manage async tasks.
        self.loop = asyncio.get_event_loop()

        # Instantiate runners
        self.should_exit: bool = False
        self.is_running: bool = False
        self.thread: Union[threading.Thread, None] = None
        self.lock = asyncio.Lock()
        self.is_broadcasting: bool = False
        self.is_validating: bool = False
        self.task_id: str = ""

    def serve_axon(self):
        """Register this validator's IP on chain via ServeAxon intent."""
        logger.info("serving ip to chain...")
        try:
            import socket
            ip = socket.gethostbyname(socket.gethostname())
            port = getattr(self.config.neuron, "axon_port", 8091)
            self.subtensor.execute(
                bt.ServeAxon(
                    netuid=self.config.netuid,
                    ip=ip,
                    port=port,
                ),
                self.wallet,
            )
            logger.info(
                f"Served axon on network: {self.config.network} netuid: {self.config.netuid}"
            )
        except Exception as e:
            logger.error(f"Failed to serve axon with exception: {e}")

    async def concurrent_forward(self):
        coroutines = [
            self.forward() for _ in range(self.config.neuron.num_concurrent_forwards)
        ]
        await asyncio.gather(*coroutines)

    def run(self):
        """
        Initiates and manages the main loop for the validator on the Bittensor network.
        """

        # Check that validator is registered on the network.
        self.sync()

        logger.info(f"Validator starting at block: {self.block}")

        # This loop maintains the validator's operations until intentionally stopped.
        # A failed step must never end the loop: chain reads can fail transiently
        # (node hiccup, reorg of the unfinalized tip), and the validator has to
        # ride those out rather than exit and wait for pm2 to restart it.
        try:
            while True:
                try:
                    # Run multiple forwards concurrently.
                    self.loop.run_until_complete(self.concurrent_forward())

                    # Check if we should exit.
                    if self.should_exit:
                        break

                    if self.should_set_weights():
                        pass

                    # Sync metagraph.
                    self.sync()

                    self.step += 1

                except KeyboardInterrupt:
                    raise

                # In case of unforeseen errors, log and continue with the next step.
                except Exception as err:
                    logger.error(f"Error during validation step {self.step}: {str(err)}")
                    logger.debug(str(print_exception(type(err), err, err.__traceback__)))
                    if self.should_exit:
                        break
                    time.sleep(12)

        # If someone intentionally stops the validator, it'll safely terminate operations.
        except KeyboardInterrupt:
            logger.info("Validator killed by keyboard interrupt.")
            exit()

    def run_in_background_thread(self):
        """
        Starts the validator's operations in a background thread upon entering the context.
        """
        if not self.is_running:
            logger.debug("Starting validator in background thread.")
            self.should_exit = False
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            self.is_running = True
            logger.debug("Started")

    def stop_run_thread(self):
        """
        Stops the validator's operations that are running in the background thread.
        """
        if self.is_running:
            logger.debug("Stopping validator in background thread.")
            self.should_exit = True
            self.thread.join(5)
            self.is_running = False
            logger.debug("Stopped")

    def build_signed_headers(self) -> dict:
        timestamp = int(datetime.now(tz=timezone.utc).timestamp())
        message = f"<Signature>{timestamp}</Signature>"
        signature = self.wallet.hotkey.sign(message.encode())
        return {
            "X-Validator-Hotkey": self.wallet.hotkey.ss58_address,
            "X-Validator-Signature": signature.hex(),
            "X-Validator-Timestamp": str(timestamp),
        }

    def __enter__(self):
        self.run_in_background_thread()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.is_running:
            logger.debug("Stopping validator in background thread.")
            self.should_exit = True
            self.thread.join(5)
            self.is_running = False
            logger.debug("Stopped")

    def set_weights(self, scores: List[MinerScore], task_id: str):
        """
        Sets the validator weights on-chain.
        Miners receive total weight * (1 - BURNING_RATE)
        OWNER_HOTKEY receives the remaining BURNING_RATE
        """
        owner_uid = self.metagraph.hotkeys.index(OWNER_HOTKEY)
        n = len(self.metagraph)
        metagraph_uids = np.array([neuron.uid for neuron in self.metagraph.neurons])

        scores_array = score_fractions_by_uid(scores, n)

        if SCORING_SYSTEM == "linear":
            processed_scores = process_scores_linear(scores_array)
        else:
            processed_scores = process_scores_top(scores_array)

        processed_weight_uids, processed_weights = process_weights_for_netuid(
            uids=metagraph_uids,
            weights=processed_scores,
            netuid=self.config.netuid,
            owner_uid=owner_uid,
            subtensor=self.subtensor,
            metagraph=self.metagraph,
        )

        full_miner_weights = np.zeros(n, dtype=np.float32)
        if len(processed_weight_uids) > 0:
            full_miner_weights[np.asarray(processed_weight_uids)] = processed_weights

        full_sum = np.sum(full_miner_weights)
        if full_sum > 0:
            full_miner_weights /= full_sum

        final_weights = full_miner_weights * (1 - BURNING_RATE)

        if 0 <= owner_uid < n:
            final_weights[owner_uid] += BURNING_RATE
        else:
            logger.warning(f"OWNER_UID {owner_uid} out of range!")

        final_norm = np.sum(final_weights) or 1.0
        final_weights /= final_norm

        logger.info(f"Weights: {final_weights}")

        miner_score_dtos: list[MinerScoreDto] = [
            MinerScoreDto(
                task_id=task_id,
                uid=ms.uid,
                hotkey=self.metagraph.hotkeys[ms.uid],
                breakdown=ms.breakdown,
                final_score=ms.final_score,
                log=ms.log,
                weight=float(final_weights[ms.uid]),
            )
            for ms in scores
            if 0 <= ms.uid < n
        ]

        if task_id and miner_score_dtos:
            submit_validation_result(self, miner_scores=miner_score_dtos)
            logger.info(f"Submitted validation results for task {task_id} to server.")

        final_uids = np.where(final_weights > 1e-8)[0].tolist()
        final_weight_values = final_weights[final_uids].tolist()

        self.uids, self.weights = convert_weights_and_uids_for_emit(
            uids=final_uids, weights=final_weight_values
        )

        weights_dict = {int(uid): float(w) for uid, w in zip(self.uids, self.weights)}
        bt.set_weights(
            self.config.netuid,
            weights_dict,
            wallet=self.wallet,
            version_key=self.spec_version,
            network=self.network,
        )
        logger.info("set_weights on chain successfully!")

        self.save_state()

    def resync_metagraph(self):
        """Resyncs the metagraph and updates the hotkeys and scores based on the new metagraph."""
        # Copies state of metagraph before syncing.
        previous_hotkeys = copy.deepcopy(self.metagraph.hotkeys)

        # Sync the metagraph. Pinned below the finalized head + retried, so a
        # reorg of the unfinalized tip cannot take the validator down.
        self.metagraph = fetch_metagraph_with_retry(self.subtensor, self.netuid, commitments=False)

        # Check if the metagraph hotkeys have changed.
        if previous_hotkeys == self.metagraph.hotkeys:
            return

        for uid, hotkey in enumerate(self.hotkeys):
            if uid < len(self.metagraph.hotkeys) and hotkey != self.metagraph.hotkeys[uid]:
                self.scores[uid] = 0
                self.file_names[uid] = ""

        n = len(self.metagraph)
        if len(self.scores) != n:
            new_scores = np.zeros(n, dtype=np.float32)
            new_file_names = np.array([""] * n, dtype=object)
            min_len = min(len(self.scores), n)
            new_scores[:min_len] = self.scores[:min_len]
            new_file_names[:min_len] = self.file_names[:min_len]
            self.scores = new_scores
            self.file_names = new_file_names

        self.hotkeys = copy.deepcopy(self.metagraph.hotkeys)

    def save_state(self):
        """Saves the state of the validator to a file."""
        try:
            np.savez(
                self.config.neuron.full_path + "/state.npz",
                step=self.step,
                uids=self.uids,
                weights=self.weights,
                miner_uids=self.miner_uids,
                collected_uids=self.collected_uids,
                task_id=self.task_id,
            )
        except Exception as e:
            logger.error(f"Failed to save state with exception: {e}")

    def load_state(self):
        """Loads the state of the validator from a file."""
        logger.info("Loading validator state.")
        try:
            state = np.load(self.config.neuron.full_path + "/state.npz")
            if isinstance(state["step"], (int, np.integer, np.ndarray)):
                self.step = int(state["step"])
            if isinstance(state["uids"], np.ndarray):
                self.uids = state["uids"].tolist()
            if isinstance(state["weights"], np.ndarray):
                self.weights = state["weights"].tolist()
            if "task_id" in state:
                self.task_id = str(state["task_id"])
            if "miner_uids" in state:
                self.miner_uids = state["miner_uids"].tolist()
            if "collected_uids" in state:
                self.collected_uids = state["collected_uids"].tolist()
        except Exception as e:
            logger.error(f"Failed to load state with exception: {e}")
