import random
from typing import List

import numpy as np


def check_uid_availability(metagraph, uid: int, vpermit_tao_limit: int) -> bool:
    """Check if uid is available. The UID should be available if it is serving and has less than vpermit_tao_limit stake."""
    neuron = metagraph.neurons[uid]
    # Filter non serving axons.
    if neuron.axon is None:
        return False
    # Filter validator permit > vpermit_tao_limit stake.
    if neuron.validator_permit:
        if float(neuron.total_stake) > vpermit_tao_limit:
            return False
    return True


def get_miner_uids(self) -> np.ndarray:
    """
    Filter out uids that are validators in the metagraph.
    """
    uids = []
    for neuron in self.metagraph.neurons:
        uid = neuron.uid
        # skip validators
        if neuron.trust > 0:
            continue

        try:
            current_block = int(self.block)
        except Exception:
            current_block = int(getattr(self, "current_block", 0))

        epoch_length = getattr(self.config.neuron, "epoch_length", None)
        if epoch_length is None:
            epoch_length = getattr(self, "epoch_length", 0)

        uids.append(uid)

    uids = np.array(uids)
    return uids
