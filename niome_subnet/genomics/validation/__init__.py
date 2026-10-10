from niome_subnet.genomics.model import MinerScore
from niome_subnet.genomics.validation.errors import ValidatorFault
from niome_subnet.genomics.validation.stage12 import run_stage12
from niome_subnet.genomics.validation.stage3 import run_stage3
from niome_subnet.genomics.validation.stage4 import run_stage4
from niome_subnet.genomics.validation.stage5 import (
    calibration_breakdown,
    run_stage5,
)


def benchmark_submission(uid: int) -> MinerScore:
    """`uid` is the slot the submission was downloaded from (niome/{uid}.json),
    and is the identity the submission is scored as. Stage 1 rejects a
    submission that declares a different `miner_uid`.

    Raises ValidatorFault if the validator's own environment is broken. That is
    NOT a zero score — the same fault would zero every miner in the round, so
    the caller must abort rather than commit the weights."""
    try:
        run_stage12(uid)
        run_stage3()
        run_stage4()
        score = run_stage5()

        return MinerScore(
            uid=uid,
            breakdown=score["breakdown"],
            final_score=score["final_score"],
            log=""
        )
    except ValidatorFault:
        # Deliberately not converted to a score. Must be ordered before the
        # generic handler below.
        raise
    except Exception as e:
        return MinerScore(
            uid=uid,
            breakdown={
                "rejected": True,
                "rejection_reason": f"validation_error: {type(e).__name__}: {e}",
                "raw_score": 0.0,
                "calibration_factor": 0.0,
                "stratum_coverage_factor": 0.0,
                "honeypot_integrity_factor": 0.0,
                "final_reward": 0.0,
                "achievable_max": 0.0,
                "raw_score_fraction": 0.0,
                "score_fraction": 0.0,
                # Every uid in a round must carry the same breakdown keys, or a
                # consumer indexing a field crashes on exactly the uids that
                # failed here. calibration_breakdown({}) is the one definition
                # of the defaults.
                **calibration_breakdown({}),
            },
            final_score=0.0,
            log=f"Invalid submission: {e}"
        )
