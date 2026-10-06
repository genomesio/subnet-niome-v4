# The MIT License (MIT)
# Copyright © 2023 Yuma Rao

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

import copy
import logging
from abc import ABC, abstractmethod

import bittensor as bt

from niome_subnet import __spec_version__ as spec_version
from niome_subnet.utils import check_config, add_args, config, ttl_get_block, fetch_metagraph_with_retry
from niome_subnet.mock import MockSubtensor, MockMetagraph

from niome_subnet.utils.settings import BASE_BLOCK_NUMBER, INTERVAL_BLOCKS, WEIGHT_SET_BLOCK

logger = logging.getLogger(__name__)


class BaseNeuron(ABC):
    """
    Base class for Bittensor miners. This class is abstract and should be inherited by a subclass. It contains the core logic for all neurons; validators and miners.

    In addition to creating a wallet, subtensor, and metagraph, this class also handles the synchronization of the network state via a basic checkpointing mechanism based on epoch length.
    """

    neuron_type: str = "BaseNeuron"

    @classmethod
    def check_config(cls, config):
        check_config(cls, config)

    @classmethod
    def add_args(cls, parser):
        add_args(cls, parser)

    @classmethod
    def config(cls):
        return config(cls)

    subtensor: "bt.Subtensor"
    wallet: "bt.Wallet"
    metagraph: "bt.Metagraph"
    spec_version: int = spec_version

    @property
    def block(self):
        return ttl_get_block(self)

    def __init__(self, config=None):
        base_config = copy.deepcopy(config or BaseNeuron.config())
        self.config = self.config()
        # Merge any extra attrs from base_config into self.config
        for key, val in vars(base_config).items():
            if not hasattr(self.config, key):
                setattr(self.config, key, val)
        self.check_config(self.config)

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            force=True,
        )
        allowed_levels = {logging.INFO, logging.ERROR, logging.WARNING}
        level_filter = lambda record: record.levelno in allowed_levels
        for handler in logging.getLogger().handlers:
            handler.addFilter(level_filter)
        for noisy in ("httpx", "httpcore", "urllib3", "botocore", "boto3", "s3transfer"):
            logging.getLogger(noisy).setLevel(logging.DEBUG)

        self.device = self.config.neuron.device

        logger.info("Setting up bittensor objects.")

        # The chain every call in this process must target. bt.set_weights()
        # takes network= per call and defaults to finney, so anything that
        # commits weights MUST pass this: reading the metagraph from test while
        # writing weights to finney looks like "hotkey not registered on netuid
        # N", because on finney it genuinely isn't.
        self.network = self.config.endpoint if self.config.endpoint else self.config.network

        if self.config.mock:
            self.wallet = MockWallet(
                name=self.config.wallet,
                hotkey=self.config.wallet_hotkey,
            )
            self.subtensor = MockSubtensor(self.config.netuid, wallet=self.wallet)
            self.metagraph = MockMetagraph(self.config.netuid, subtensor=self.subtensor)
        else:
            self.wallet = bt.Wallet(
                name=self.config.wallet,
                hotkey=self.config.wallet_hotkey,
            )
            # v11: Client takes network= for both named networks and raw ws:// endpoints.
            # retry_forever keeps the WS alive through node hiccups.
            self.subtensor = bt.Subtensor(self.network, retry_forever=True)
            self.metagraph = fetch_metagraph_with_retry(self.subtensor, self.config.netuid)

        try:
            self.netuid = int(self.config.netuid)
        except Exception:
            self.netuid = getattr(self.metagraph, "netuid", None)

        self.uids: list[int] = []
        self.weights: list[int] = []
        self.task_id: str = ""
        self.miner_uids: list[int] = []
        self.collected_uids: list[int] = []
        self.are_weights_committed: bool = False

        logger.info(f"Wallet: {self.wallet}")
        logger.info(f"Subtensor: {self.subtensor}")
        logger.info(f"Metagraph: {self.metagraph}")

        self.check_registered()

        self.uid = self.metagraph.hotkeys.index(self.wallet.hotkey.ss58_address)
        logger.info(
            f"Running neuron on subnet: {self.config.netuid} with uid {self.uid}"
        )
        self.step = 0

    @abstractmethod
    def run(self): ...

    def sync(self):
        """
        Wrapper for synchronizing the state of the network for the given miner or validator.
        """
        self.check_registered()

        if self.should_sync_metagraph():
            self.resync_metagraph()

    def check_registered(self):
        uid = self.subtensor.neurons.uid(self.wallet.hotkey.ss58_address, self.config.netuid)
        if uid is None:
            logger.error(
                f"Wallet: {self.wallet} is not registered on netuid {self.config.netuid}."
                f" Please register the hotkey using `btcli subnets register` before trying again"
            )
            exit()

    def should_sync_metagraph(self):
        """
        Check if enough epoch blocks have elapsed since the last checkpoint to sync.
        """
        return (
            self.block - self.metagraph.neurons[self.uid].last_update
        ) > self.config.neuron.epoch_length

    def should_set_weights(self) -> bool:
        if self.neuron_type == "MinerNeuron":
            return False

        if self.step == 0:
            return False

        if self.config.neuron.disable_set_weights:
            return False

        if len(self.uids) == 0 or len(self.uids) != len(self.weights):
            return False

        blocks = (self.block - BASE_BLOCK_NUMBER) % INTERVAL_BLOCKS - WEIGHT_SET_BLOCK

        if blocks >= 0 and blocks < 5 and not self.are_weights_committed:
            self.are_weights_committed = True
            return True
        else:
            return False

    def save_state(self):
        logger.debug(
            "save_state() not implemented for this neuron."
        )

    def load_state(self):
        logger.debug(
            "load_state() not implemented for this neuron."
        )


class MockWallet:
    """Minimal mock wallet for testing without a real keystore."""

    class _MockKeypair:
        def __init__(self, ss58_address: str):
            self.ss58_address = ss58_address

        def sign(self, data) -> bytes:
            return b"\x00" * 64

    def __init__(self, name: str = "mock", hotkey: str = "mock-hotkey"):
        self.name = name
        self.hotkey = self._MockKeypair(f"5mock_{name}_{hotkey}")
        self.coldkey = self._MockKeypair(f"5mock_cold_{name}")
