import logging

import boto3
import json
import niome_subnet.utils.settings as config
import requests
import time
import urllib.request

from niome_subnet.genomics.model import MinerScoreDto, Task

logger = logging.getLogger(__name__)


def get(self, url: str):
    timestamp = str(time.time())
    canonical = json.dumps({
        'payload': json.dumps({}, separators=(',', ':'), sort_keys=True),
        'hotkey': self.wallet.hotkey.ss58_address,
        'netuid': str(self.netuid),
        'timestamp': timestamp,
    }, separators=(',', ':'), sort_keys=True)

    signature = self.wallet.hotkey.sign(canonical.encode()).hex()

    for attempt in range(1, config.MAX_TASK_RETRIES + 1):
        try:
            response = requests.get(
                url,
                headers=self.build_signature_headers(
                    signature=signature,
                    hotkey=self.wallet.hotkey.ss58_address,
                    timestamp=timestamp,
                    netuid=str(self.netuid),
                ),
                timeout=config.TASK_REQUEST_TIMEOUT,
            )

            if response.status_code != 200:
                raise RuntimeError(
                    f"Backend returned status {response.status_code}"
                )

            return response.json()
        except Exception as e:
            logger.error(f"Get Error: {str(e)}")
            if attempt < config.MAX_TASK_RETRIES:
                delay = config.BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                logger.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                logger.error("All retries failed")
                raise e

def post(self, url: str, payload: dict):
    timestamp = str(time.time())
    canonical = json.dumps({
        'payload': json.dumps(payload, separators=(',', ':'), sort_keys=True),
        'hotkey': self.wallet.hotkey.ss58_address,
        'netuid': str(self.netuid),
        'timestamp': timestamp,
    }, separators=(',', ':'), sort_keys=True)

    signature = self.wallet.hotkey.sign(canonical.encode()).hex()

    for attempt in range(1, config.MAX_TASK_RETRIES + 1):
        try:
            response = requests.post(
                url,
                headers=self.build_signature_headers(
                    signature=signature,
                    hotkey=self.wallet.hotkey.ss58_address,
                    timestamp=timestamp,
                    netuid=str(self.netuid),
                ),
                json=payload,
                timeout=config.TASK_REQUEST_TIMEOUT,
            )

            if response.status_code != 200:
                raise RuntimeError(
                    f"Backend returned status {response.status_code}"
                )

            return response.json()
        except Exception as e:
            logger.error(f"Post Error: {e}")
            if attempt < config.MAX_TASK_RETRIES:
                delay = config.BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                logger.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                logger.error("All retries failed")
                raise e

def fetch_task(self) -> Task:
    """Generate a synthetic genomic simulation task with retry logic and fallback."""
    data = get(self, config.TASK_URL)

    if "task_id" in data:
        data["id"] = data.pop("task_id")

    task = Task.model_validate(data)

    if not task.contract_url or not task.truth_url:
        logger.error("Invalid response: missing contract_url or hbb_ref_url")
        raise RuntimeError("Invalid response")

    urllib.request.urlretrieve(task.contract_url, config.CONTRACT_FILE)
    urllib.request.urlretrieve(task.truth_url, config.TRUTH_FILE)

    return task

def submit_validation_result(self, miner_scores: list[MinerScoreDto]) -> None:
    """Submit miner scores with retry logic and fallback."""
    payload = {
      "scores": [score.model_dump() for score in miner_scores],
    }
    post(self, config.MINER_SCORE_URL, payload)

def upload_final_submissions_to_server(self, uids: list[int]) -> None:
    """Download submissions by UIDs from S3, and upload to server."""
    try:
        s3_client = boto3.client(
            "s3",
            aws_access_key_id=config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
            region_name=config.AWS_REGION,
        )

        submissions = []

        for uid in uids:
            try:
                obj = s3_client.get_object(
                    Bucket=config.AWS_S3_BUCKET,
                    Key=config.submission_key(uid),
                )
                submission_data = json.loads(obj['Body'].read().decode('utf-8'))
                submissions.append({
                    "uid": int(uid),
                    "submission": submission_data,
                })
            except Exception as e:
                logger.error(f"Error processing UID {uid}: {e}")
                continue

        data = get(self, f"{config.MINER_SUBMISSION_URL}?task_id={self.task_id}")
        presigned_url = data["url"]
        
        response = requests.put(
            presigned_url,
            data=json.dumps(submissions),
            headers={
                "Content-Type": "application/json"
            },
            timeout=300,
        )
        
        response.raise_for_status()
        
        logger.info("Successfully uploaded final submissions")
    except Exception as e:
        logger.error(f"Error in upload_final_submissions_to_server: {e}")
        raise e
