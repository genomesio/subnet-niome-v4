import logging
from typing import Optional

logger = logging.getLogger(__name__)


class MockSubtensor:
    """Minimal mock subtensor for testing."""

    def __init__(self, netuid: int, n: int = 16, wallet=None, network: str = "mock"):
        self.network = network
        self.netuid = netuid
        self._neurons: list[_MockNeuron] = []
        self._block = 1000

        # Register the validator at uid=0
        if wallet is not None:
            self._neurons.append(_MockNeuron(
                uid=0,
                hotkey=wallet.hotkey.ss58_address,
                coldkey=wallet.coldkey.ss58_address,
            ))

        for i in range(1, n + 1):
            self._neurons.append(_MockNeuron(
                uid=i,
                hotkey=f"5mock_miner_hotkey_{i}",
                coldkey="5mock_coldkey",
            ))

    @property
    def block(self) -> int:
        return self._block

    class _NeuronsNS:
        def __init__(self, outer):
            self._outer = outer

        def uid(self, hotkey_ss58: str, netuid: int) -> Optional[int]:
            for n in self._outer._neurons:
                if n.hotkey == hotkey_ss58:
                    return n.uid
            return None

    @property
    def neurons(self):
        return self._NeuronsNS(self)

    class _SubnetsNS:
        def __init__(self, outer):
            self._outer = outer

        def metagraph(self, netuid: int, block: Optional[int] = None, *, commitments: bool = True):
            return MockMetagraph(netuid, subtensor=self._outer)

    @property
    def subnets(self):
        return self._SubnetsNS(self)

    class _HyperparametersNS:
        def min_allowed_weights(self, netuid: int) -> int:
            return 0

        def max_weight_limit(self, netuid: int) -> float:
            return 1.0

    @property
    def hyperparameters(self):
        return self._HyperparametersNS()

    def execute(self, intent, wallet):
        logger.debug(f"MockSubtensor.execute: {intent}")
        return True, "mock success"


class _MockNeuron:
    def __init__(self, uid: int, hotkey: str, coldkey: str):
        self.uid = uid
        self.hotkey = hotkey
        self.coldkey = coldkey
        self.active = True
        self.validator_permit = uid == 0
        self.last_update = 1000
        self.trust = 0.0
        # uid 0 is the validator in this mock; miners carry vtrust 0.
        self.validator_trust = 1.0 if uid == 0 else 0.0
        self.total_stake = 0.0
        self.axon = "127.0.0.1:8091" if uid > 0 else None
        self.rank = 0.0
        self.consensus = 0.0
        self.incentive = 0.0
        self.dividends = 0.0


class MockMetagraph:
    """Minimal mock metagraph for testing."""

    def __init__(self, netuid: int = 1, subtensor: "MockSubtensor" = None):
        self.netuid = netuid
        if subtensor is not None:
            self.neurons = list(subtensor._neurons)
        else:
            self.neurons = [
                _MockNeuron(uid=i, hotkey=f"5mock_hotkey_{i}", coldkey="5mock_coldkey")
                for i in range(16)
            ]

    @property
    def hotkeys(self) -> list:
        return [n.hotkey for n in self.neurons]

    def __len__(self) -> int:
        return len(self.neurons)

    def __iter__(self):
        return iter(self.neurons)

    def __getitem__(self, uid: int):
        return self.neurons[uid]
