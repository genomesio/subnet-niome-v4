from pydantic import BaseModel
from typing import Generic, TypeVar


class Task(BaseModel):
    """Data model for a genomics simulation task."""
    id: str
    contract_url: str   # presigned URL to fetch the contract
    truth_url: str    # presigned URL to fetch the HBB reference data


class ValidationContext:
    """Container for all validation metadata."""
    miner_uid: int
    miner_hotkey: str
    validator_uid: int
    validator_hotkey: str

    def __init__(self, miner_uid: int = 0, miner_hotkey: str = "", validator_uid: int = 0, validator_hotkey: str = ""):
        self.miner_uid = miner_uid
        self.miner_hotkey = miner_hotkey
        self.validator_uid = validator_uid
        self.validator_hotkey = validator_hotkey


class TaskPayload(BaseModel):
    """Payload structure for task generation requests."""
    timestamp: float
    hotkey: str
    uuid: str
    netuid: str


PayloadType = TypeVar('PayloadType', bound=BaseModel)

class SignedRequest(BaseModel, Generic[PayloadType]):
    """Generic signed request structure."""
    payload: PayloadType
    payload_raw: str
    signature: str


class MinerScore(BaseModel):
    uid: int
    breakdown: dict
    final_score: float
    log: str


class MinerScoreDto(BaseModel):
    """Data transfer object for miner score submission."""
    task_id: str
    uid: int
    hotkey: str
    breakdown: dict
    final_score: float
    log: str
    weight: float
