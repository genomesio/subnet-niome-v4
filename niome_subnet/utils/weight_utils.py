import logging
import numpy as np
from typing import Any, Tuple, List, Union
from niome_subnet.utils.settings import (
    SCORE_DISTRIBUTION,
    TOP_MINER_COUNT,
)
from niome_subnet.utils.misc import fetch_metagraph_with_retry

logger = logging.getLogger(__name__)

U32_MAX = 4294967295
U16_MAX = 65535


def normalize_max_weight(x: np.ndarray, limit: float = 0.1) -> np.ndarray:
    r"""Normalizes the numpy array x so that sum(x) = 1 and the max value is not greater than the limit.
    Args:
        x (:obj:`np.ndarray`):
            Array to be max_value normalized.
        limit: float:
            Max value after normalization.
    Returns:
        y (:obj:`np.ndarray`):
            Normalized x array.
    """
    epsilon = 1e-7  # For numerical stability after normalization

    weights = x.copy()
    values = np.sort(weights)

    if x.sum() == 0 or len(x) * limit <= 1:
        return np.ones_like(x) / x.size
    else:
        estimation = values / values.sum()

        if estimation.max() <= limit:
            return weights / weights.sum()

        # Find the cumulative sum and sorted array
        cumsum = np.cumsum(estimation, 0)

        # Determine the index of cutoff
        estimation_sum = np.array(
            [(len(values) - i - 1) * estimation[i] for i in range(len(values))]
        )
        n_values = (estimation / (estimation_sum + cumsum + epsilon) < limit).sum()

        # Determine the cutoff based on the index
        cutoff_scale = (limit * cumsum[n_values - 1] - epsilon) / (
            1 - (limit * (len(estimation) - n_values))
        )
        cutoff = cutoff_scale * values.sum()

        # Applying the cutoff
        weights[weights > cutoff] = cutoff

        y = weights / weights.sum()

        return y


def convert_weights_and_uids_for_emit(
    uids: np.ndarray, weights: np.ndarray
) -> Tuple[List[int], List[int]]:
    r"""Converts weights into integer u32 representation that sum to MAX_INT_WEIGHT.
    Args:
        uids (:obj:`np.ndarray,`):
            Array of uids as destinations for passed weights.
        weights (:obj:`np.ndarray,`):
            Array of weights.
    Returns:
        weight_uids (List[int]):
            Uids as a list.
        weight_vals (List[int]):
            Weights as a list.
    """
    uids = np.asarray(uids)
    weights = np.asarray(weights)

    if np.min(weights) < 0:
        raise ValueError(
            "Passed weight is negative cannot exist on chain {}".format(weights)
        )
    if np.min(uids) < 0:
        raise ValueError("Passed uid is negative cannot exist on chain {}".format(uids))
    if len(uids) != len(weights):
        raise ValueError(
            "Passed weights and uids must have the same length, got {} and {}".format(
                len(uids), len(weights)
            )
        )
    if np.sum(weights) == 0:
        logger.debug("nothing to set on chain")
        return [], []  # Nothing to set on chain.

    weight_vals = []
    weight_uids = []
    for i, (weight_i, uid_i) in enumerate(list(zip(weights, uids))):
        uint16_val = round(
            float(weight_i) * int(U16_MAX)
        )
        if uint16_val != 0:  # Filter zeros
            weight_vals.append(uint16_val)
            weight_uids.append(int(uid_i))

    return weight_uids, weight_vals


def process_weights_for_netuid(
    uids,
    weights: np.ndarray,
    netuid: int,
    owner_uid: int,
    subtensor=None,
    metagraph=None,
    exclude_quantile: int = 0,
) -> Union[
    tuple[
        np.ndarray[Any, np.dtype[Any]],
        Union[
            Union[
                np.ndarray[Any, np.dtype[np.floating[Any]]],
                np.ndarray[Any, np.dtype[np.complexfloating[Any, Any]]],
            ],
            Any,
        ],
    ],
    tuple[np.ndarray[Any, np.dtype[Any]], np.ndarray],
    tuple[Any, np.ndarray],
]:
    # Get latest metagraph from chain if metagraph is None.
    if metagraph is None:
        metagraph = fetch_metagraph_with_retry(subtensor, netuid)

    # Cast weights to floats.
    if not isinstance(weights, np.ndarray) or weights.dtype != np.float32:
        weights = weights.astype(np.float32)

    # Network configuration parameters from subtensor.
    quantile = exclude_quantile / U16_MAX
    min_allowed_weights = subtensor.hyperparameters.min_allowed_weights(netuid)
    max_weight_limit = subtensor.hyperparameters.max_weight_limit(netuid)

    # Find all non-zero weights.
    non_zero_weight_idx = np.argwhere(weights > 0).squeeze()
    non_zero_weight_idx = np.atleast_1d(non_zero_weight_idx)
    non_zero_weight_uids = uids[non_zero_weight_idx]
    non_zero_weights = weights[non_zero_weight_idx]

    n = len(metagraph)

    if non_zero_weights.size == 0:
        all_weights = np.zeros(n, dtype=np.float32)
        owner_indices = np.where(uids == owner_uid)[0]
        if len(owner_indices) == 0:
            logger.error(
                f"owner_uid {owner_uid} not found in uids. "
                "Falling back to uniform distribution."
            )
            final_weights = np.ones(n) / n
            return np.arange(len(final_weights)), final_weights
        owner_idx = owner_indices[0]
        all_weights[owner_idx] = 1.0
        normalized_weights = normalize_max_weight(x=all_weights, limit=max_weight_limit)
        final_non_zero = np.where(normalized_weights > 0)[0]
        final_uids = uids[final_non_zero]
        final_weights = normalized_weights[final_non_zero]
        return final_uids, final_weights

    if n < min_allowed_weights:
        logger.warning(
            "Metagraph size smaller than min_allowed_weights, returning all ones."
        )
        final_weights = np.ones(n) / n
        return np.arange(len(final_weights)), final_weights

    if non_zero_weights.size < min_allowed_weights:
        logger.warning(
            "Number of non-zero weights less than min_allowed_weights, "
            "creating minimal weights for all."
        )
        weights = np.ones(n) * 1e-5
        weights[non_zero_weight_idx] += non_zero_weights
        logger.debug("final_weights: %s", weights)
        normalized_weights = normalize_max_weight(x=weights, limit=max_weight_limit)
        return np.arange(len(normalized_weights)), normalized_weights

    # Compute the exclude quantile and find the weights in the lowest quantile
    max_exclude = max(0, len(non_zero_weights) - min_allowed_weights) / len(
        non_zero_weights
    )
    exclude_quantile = min([quantile, max_exclude])
    lowest_quantile = np.quantile(non_zero_weights, exclude_quantile)

    # Exclude all weights below the allowed quantile.
    non_zero_weight_uids = non_zero_weight_uids[lowest_quantile <= non_zero_weights]
    non_zero_weights = non_zero_weights[lowest_quantile <= non_zero_weights]

    # Normalize weights and return.
    normalized_weights = normalize_max_weight(
        x=non_zero_weights, limit=max_weight_limit
    )
    logger.debug("final_weights: %s", normalized_weights)

    return non_zero_weight_uids, normalized_weights


def process_scores_linear(scores: np.ndarray) -> np.ndarray:
    """
    Convert raw miner scores into a normalized weight vector using linear proportional distribution:
    - All miners with positive scores receive weights proportional to their score.
    - Miners with zero or negative scores receive zero weight.
    - Output always sums to exactly 1.0 (if any positive scores exist).
    """
    logger.debug("Processing scores: %s", scores)

    weights = np.zeros_like(scores, dtype=np.float32)

    # Filter positive scores
    positive_mask = scores > 0
    positive_scores = scores[positive_mask]

    if positive_scores.size == 0:
        return weights  # all zeros

    total_score = np.sum(positive_scores)

    # Safety check (very unlikely but good practice)
    if total_score == 0:
        # fallback to uniform distribution among positive miners
        weights[positive_mask] = 1.0 / positive_scores.size
        return weights

    # Linear proportional distribution
    weights[positive_mask] = positive_scores / total_score

    return weights


def process_scores_top(scores: np.ndarray) -> np.ndarray:
    """
    Convert raw miner scores into a final normalized weight vector:
    - Only the top TOP_MINER_COUNT miners (with positive scores) receive weight.
    - Weights are assigned according to SCORE_DISTRIBUTION, normalized if fewer
      positive miners than TOP_MINER_COUNT.
    - All other miners receive zero weight.
    - Output always sums to exactly 1.0 (if any positive scores exist).
    """
    positive_score_num = int(np.sum(scores > 0))
    if positive_score_num == 0:
        return np.zeros_like(scores)

    # Sort indices by descending score; only consider positive miners
    sorted_indices = np.argsort(-scores)

    ratios = np.array(SCORE_DISTRIBUTION)
    weights = np.zeros_like(scores, dtype=np.float32)

    k = min(TOP_MINER_COUNT, positive_score_num)
    top_ratios = ratios[:k]
    top_sum = np.sum(top_ratios)
    norm_ratios = top_ratios / top_sum if top_sum > 0 else np.ones(k) / k

    for i in range(k):
        weights[sorted_indices[i]] = norm_ratios[i]

    return weights
