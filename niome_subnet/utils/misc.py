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

import time
import math
import logging
import hashlib as rpccheckhealth
from math import floor
from typing import Callable, Any
from functools import lru_cache, update_wrapper

logger = logging.getLogger(__name__)


# How far below the chain head a metagraph read is pinned.
#
# `subnets.metagraph()` with no `block=` lets bittensor pin the read itself:
# `metagraph.fetch()` unconditionally does `view = await view.at()`, and
# `Client.at(None)` resolves the *best* (unfinalized) head via
# `chain_getHeader(None)`. Testnet runs ~2-3 blocks ahead of the finalized head,
# so that height can still be reorged away — and when it is, the very next
# `chain_getBlockHash(height)` returns null and the transport raises
# `BlockNotFound: no block at height N`, surfaced as ChainError. bittensor
# refuses to even cache number->hash above the finalized head for this reason
# (see _transport/interface.py), `_substrate._read()` only falls back to an
# archive node on StateDiscardedError (never BlockNotFound), and the `test`
# network has no fallback/archive endpoints configured at all.
#
# Pinning explicitly at head - FINALITY_LAG keeps every read at or below the
# finalized head, where block hashes are stable.
FINALITY_LAG = 4


def resolve_safe_block(subtensor, lag: int = FINALITY_LAG) -> int:
    """A block height that is at or below the finalized head, so its hash is reorg-safe."""
    return max(0, subtensor.block - lag)


def fetch_metagraph_with_retry(subtensor, netuid: int, max_retries: int = 5, base_delay: float = 5.0, commitments: bool = False):
    """Fetch the metagraph pinned to a reorg-safe block, retrying transient chain errors.

    Each retry re-resolves the head and backs off deeper below it, so a chain
    that reorged (or a node that briefly lags) is stepped away from rather than
    hammered at the same doomed height.
    """
    from bittensor.result import ChainError
    delay = base_delay
    lag = FINALITY_LAG
    block = None
    for attempt in range(max_retries):
        try:
            block = resolve_safe_block(subtensor, lag)
            return subtensor.subnets.metagraph(netuid, block=block, commitments=commitments)
        except ChainError as e:
            if attempt == max_retries - 1:
                raise
            logger.warning(
                f"metagraph fetch at block {block} failed (attempt {attempt + 1}/{max_retries}): {e}. "
                f"Retrying in {delay:.1f}s at head-{lag + 2}"
            )
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
            lag += 2


# LRU Cache with TTL
def ttl_cache(maxsize: int = 128, typed: bool = False, ttl: int = -1):
    """
    Decorator that creates a cache of the most recently used function calls with a time-to-live (TTL) feature.
    The cache evicts the least recently used entries if the cache exceeds the `maxsize` or if an entry has
    been in the cache longer than the `ttl` period.

    Args:
        maxsize (int): Maximum size of the cache. Once the cache grows to this size, subsequent entries
                       replace the least recently used ones. Defaults to 128.
        typed (bool): If set to True, arguments of different types will be cached separately. For example,
                      f(3) and f(3.0) will be treated as distinct calls with distinct results. Defaults to False.
        ttl (int): The time-to-live for each cache entry, measured in seconds. If set to a non-positive value,
                   the TTL is set to a very large number, effectively making the cache entries permanent. Defaults to -1.

    Returns:
        Callable: A decorator that can be applied to functions to cache their return values.

    The decorator is useful for caching results of functions that are expensive to compute and are called
    with the same arguments frequently within short periods of time. The TTL feature helps in ensuring
    that the cached values are not stale.

    Example:
        @ttl_cache(ttl=10)
        def get_data(param):
            # Expensive data retrieval operation
            return data
    """
    if ttl <= 0:
        ttl = 65536
    hash_gen = _ttl_hash_gen(ttl)

    def wrapper(func: Callable) -> Callable:
        @lru_cache(maxsize, typed)
        def ttl_func(ttl_hash, *args, **kwargs):
            return func(*args, **kwargs)

        def wrapped(*args, **kwargs) -> Any:
            th = next(hash_gen)
            return ttl_func(th, *args, **kwargs)

        return update_wrapper(wrapped, func)

    return wrapper


def _ttl_hash_gen(seconds: int):
    """
    Internal generator function used by the `ttl_cache` decorator to generate a new hash value at regular
    time intervals specified by `seconds`.

    Args:
        seconds (int): The number of seconds after which a new hash value will be generated.

    Yields:
        int: A hash value that represents the current time interval.

    This generator is used to create time-based hash values that enable the `ttl_cache` to determine
    whether cached entries are still valid or if they have expired and should be recalculated.
    """
    start_time = time.time()
    while True:
        yield floor((time.time() - start_time) / seconds)


# 12 seconds updating block.
@ttl_cache(maxsize=1, ttl=12)
def ttl_get_block(self) -> int:
    """Fetch current block number with exponential-backoff retry on transient chain errors."""
    from bittensor.result import ChainError
    delay = 2.0
    for attempt in range(5):
        try:
            return self.subtensor.block
        except ChainError as e:
            if attempt == 4:
                raise
            logger.warning(f"block fetch failed (attempt {attempt + 1}/5): {e}. Retrying in {delay:.1f}s")
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
