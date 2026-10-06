from __future__ import annotations

import contextlib


class ValidatorFault(Exception):
    """The validator's own environment is broken — not the submission.

    A miner-fault error means ONE submission scores zero, which is the correct
    and fair outcome. A validator fault — a missing or redacted contract.json,
    a corrupt truth.json, an unreadable reference directory, a stale artifact
    left behind by an earlier miner — would score EVERY miner zero for the same
    reason, and `set_weights` would then commit those zeros on-chain.

    The two must not share a handler: one is a result, the other is a reason to
    abandon the round. benchmark_submission re-raises this rather than turning
    it into a zero score, and run_validation aborts without setting weights.
    """


@contextlib.contextmanager
def validator_owned(what: str):
    """Re-raise anything raised inside the block as ValidatorFault.

    Wrap reads of artifacts the VALIDATOR owns (contract.json, truth.json, the
    reference directory, the inter-stage artifacts) and the guards that compare
    them. Reads of the miner's submission stay OUTSIDE: a submission that is
    absent, truncated or not JSON is a miner fault and must stay a zero score.
    """
    try:
        yield
    except ValidatorFault:
        raise
    except Exception as e:
        raise ValidatorFault(f"{what}: {type(e).__name__}: {e}") from e
