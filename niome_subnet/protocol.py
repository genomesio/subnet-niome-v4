"""
Protocol definitions for the Drug Response Prediction Subnet.

This module defines the communication protocols between validators and miners
for drug response prediction tasks using synthetic genomic data.
"""
from typing import Optional
from pydantic import BaseModel
from niome_subnet.genomics.model import Task


class GenomicsTaskSynapse(BaseModel):
    """Protocol for genomics simulation tasks."""

    task: Optional[Task] = None
    bundle_url: str = ""      # presigned GET: the miner's case bundle
    presigned_url: str = ""   # presigned PUT: where the submission goes
    timeout: Optional[float] = None
