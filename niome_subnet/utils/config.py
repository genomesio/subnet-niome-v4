# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2023 Opentensor Foundation

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import os
import subprocess
import argparse
from .logging import setup_events_logger
from niome_subnet.utils.settings import TESTNET_UID


def _nest_config(ns):
    """Convert flat argparse Namespace with dotted keys into nested Namespaces."""
    result = argparse.Namespace()
    for key, val in vars(ns).items():
        parts = key.split('.')
        current = result
        for part in parts[:-1]:
            if not hasattr(current, part):
                setattr(current, part, argparse.Namespace())
            current = getattr(current, part)
        setattr(current, parts[-1], val)
    return result


def is_cuda_available():
    try:
        output = subprocess.check_output(
            ["nvidia-smi", "-L"], stderr=subprocess.STDOUT
        )
        if "NVIDIA" in output.decode("utf-8"):
            return "cuda"
    except Exception:
        pass
    try:
        output = subprocess.check_output(["nvcc", "--version"]).decode("utf-8")
        if "release" in output:
            return "cuda"
    except Exception:
        pass
    return "cpu"


def check_config(cls, config):
    r"""Checks/validates the config namespace object."""
    full_path = os.path.expanduser(
        "{}/{}/{}/netuid{}/{}".format(
            os.path.expanduser("~/.bittensor/miners"),
            config.wallet,
            config.wallet_hotkey,
            config.netuid,
            config.neuron.name,
        )
    )
    config.neuron.full_path = os.path.expanduser(full_path)
    if not os.path.exists(config.neuron.full_path):
        os.makedirs(config.neuron.full_path, exist_ok=True)

    if not config.neuron.dont_save_events:
        setup_events_logger(
            config.neuron.full_path, config.neuron.events_retention_size
        )


def add_args(cls, parser):
    """
    Adds relevant arguments to the parser for operation.
    """
    parser.add_argument("--wallet", type=str, help="Wallet name", default="default")
    parser.add_argument("--wallet-hotkey", type=str, help="Wallet hotkey name", default="default")
    parser.add_argument("--network", type=str, help="Bittensor network (finney/test/local)", default="finney")
    parser.add_argument("--endpoint", type=str, help="Subtensor websocket endpoint", default=None)

    # Legacy v10 aliases — silently map to the new flags
    parser.add_argument("--wallet.name", dest="wallet", help=argparse.SUPPRESS)
    parser.add_argument("--wallet.hotkey", dest="wallet_hotkey", help=argparse.SUPPRESS)
    parser.add_argument("--subtensor.network", dest="network", help=argparse.SUPPRESS)
    parser.add_argument("--subtensor.chain_endpoint", dest="endpoint", help=argparse.SUPPRESS)
    parser.add_argument("--logging.debug", dest="_logging_debug", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--logging.trace", dest="_logging_trace", action="store_true", help=argparse.SUPPRESS)

    parser.add_argument("--netuid", type=int, help="Subnet netuid", default=TESTNET_UID)

    parser.add_argument(
        "--neuron.device",
        type=str,
        help="Device to run on.",
        default=is_cuda_available(),
    )

    parser.add_argument(
        "--neuron.epoch_length",
        type=int,
        help="The default epoch length (how often we set weights, measured in 12 second blocks).",
        default=5,
    )

    parser.add_argument(
        "--mock",
        action="store_true",
        help="Mock neuron and all network components.",
        default=False,
    )

    parser.add_argument(
        "--neuron.events_retention_size",
        type=str,
        help="Events retention size.",
        default=2 * 1024 * 1024 * 1024,  # 2 GB
    )

    parser.add_argument(
        "--neuron.dont_save_events",
        action="store_true",
        help="If set, we dont save events to a log file.",
        default=False,
    )

    parser.add_argument(
        "--wandb.off",
        action="store_true",
        help="Turn off wandb.",
        default=False,
    )

    parser.add_argument(
        "--wandb.offline",
        action="store_true",
        help="Runs wandb in offline mode.",
        default=False,
    )

    parser.add_argument(
        "--wandb.notes",
        type=str,
        help="Notes to add to the wandb run.",
        default="",
    )

    parser.add_argument(
        "--wandb.api_key",
        type=str,
        help="API key for wandb.",
        default="",
    )


def add_miner_args(cls, parser):
    """Add miner specific arguments to the parser."""

    parser.add_argument(
        "--neuron.name",
        type=str,
        help="Trials for this neuron go in neuron.root / (wallet_cold - wallet_hot) / neuron.name. ",
        default="miner",
    )

    parser.add_argument(
        "--blacklist.force_validator_permit",
        action="store_true",
        help="If set, we will force incoming requests to have a permit.",
        default=False,
    )

    parser.add_argument(
        "--blacklist.allow_non_registered",
        action="store_true",
        help="If set, miners will accept queries from non registered entities. (Dangerous!)",
        default=False,
    )

    parser.add_argument(
        "--wandb.project_name",
        type=str,
        default="",
        help="Wandb project to log to.",
    )

    parser.add_argument(
        "--wandb.entity",
        type=str,
        default="",
        help="Wandb entity to log to.",
    )


def add_validator_args(cls, parser):
    """Add validator specific arguments to the parser."""

    parser.add_argument(
        "--neuron.name",
        type=str,
        help="Trials for this neuron go in neuron.root / (wallet_cold - wallet_hot) / neuron.name. ",
        default="validator",
    )

    parser.add_argument(
        "--neuron.timeout",
        type=float,
        help="The timeout for each forward call in seconds.",
        default=10,
    )

    parser.add_argument(
        "--neuron.num_concurrent_forwards",
        type=int,
        help="The number of concurrent forwards running at any time.",
        default=1,
    )

    parser.add_argument(
        "--neuron.disable_set_weights",
        action="store_true",
        help="Disables setting weights.",
        default=False,
    )

    parser.add_argument(
        "--neuron.moving_average_alpha",
        type=float,
        help="Moving average alpha parameter, how much to add of the new observation.",
        default=0.1,
    )

    parser.add_argument(
        "--neuron.axon_off",
        "--axon_off",
        action="store_true",
        # Note: the validator needs to serve an Axon with their IP or they may
        #   be blacklisted by the firewall of serving peers on the network.
        help="Set this flag to not attempt to serve an Axon.",
        default=False,
    )

    parser.add_argument(
        "--neuron.vpermit_tao_limit",
        type=int,
        help="The maximum number of TAO allowed to query a validator with a vpermit.",
        default=4096,
    )

    parser.add_argument(
        "--wandb.project_name",
        type=str,
        help="The name of the project where you are sending the new run.",
        default="niome",
    )

    parser.add_argument(
        "--wandb.testnet_project_name",
        type=str,
        help="The name of the project where you are sending the new run for testnet.",
        default="niome-testnet",
    )

    parser.add_argument(
        "--wandb.entity",
        type=str,
        help="The name of the project where you are sending the new run.",
        default="genomes",
    )


def config(cls):
    """
    Returns the configuration object specific to this miner or validator after adding relevant arguments.
    """
    parser = argparse.ArgumentParser()
    cls.add_args(parser)
    return _nest_config(parser.parse_args())
