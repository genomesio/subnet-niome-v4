import os
from dotenv import load_dotenv

load_dotenv()

# bittensor 10.x defaults BT_NO_PARSE_CLI_ARGS=true, which skips argument
# parsing and leaves config.neuron as None. Override before importing bittensor.
os.environ.setdefault("BT_NO_PARSE_CLI_ARGS", "false")

# ---- AWS Settings -----
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION")
AWS_S3_BUCKET = os.getenv("AWS_S3_BUCKET")

# S3 layout. Presigned URLs are key-bound, so the writer and the reader of an
# object MUST derive its key from the same helper: a literal written out twice
# is a prefix mismatch waiting to happen, and it surfaces only as a HeadObject
# 404 on every uid at scoring time.
AWS_S3_PREFIX = "niome/SARPP"

def submission_key(uid) -> str:
    """Key a miner's submission is PUT to and later scored from."""
    return f"{AWS_S3_PREFIX}/{uid}.json"

def bundle_key(uid) -> str:
    """Key a miner's case bundle is uploaded to and presigned for download."""
    return f"{AWS_S3_PREFIX}/bundles/{uid}.tar.gz"


# ---- General Settings -----
TESTNET_UID = 289
MAINNET_UID = 55

FORWARD_TIMEOUT = 20


# ---- Scoring Settings -----
TOP_MINER_COUNT = 10
SCORE_DISTRIBUTION = [0.3, 0.2, 0.2, 0.15, 0.05, 0.03, 0.025, 0.02, 0.015, 0.01]


# ---- Backend Request -----
BASE_URL = "https://niome-api.genomes.io"
MINER_SCORE_URL = f"{BASE_URL}/api/v4/miners/scores"
MINER_SUBMISSION_URL = f"{BASE_URL}/api/v4/miners/submission-url"
TASK_URL = f"{BASE_URL}/api/v4/tasks/current"


# ---- Data -----
PGX_REFERENCE_DIR = "data/pgx_reference"
CPIC_ALLELE_REQUENCY_FILE = "cpic_af_data/cpic_allele_frequency.json"
CONTRACT_FILE = "data/contract.json"
TRUTH_FILE = "data/truth.json"
CASES_DIR = "data/cases"
BUNDLES_DIR = "data/bundles"
MINER_SUBMISSION_FILE = "data/submission.json"
VALID_OUT_FILE = "data/valid_calls.json"
INVALID_OUT_FILE = "data/invalid_calls.json"
STAGE3_RESULTS_FILE = "data/stage3_results.json"
FINAL_REWARD_FILE = "data/final_reward.json"


# ---- Timeout Values -----
TASK_REQUEST_TIMEOUT = 60  # seconds
BASE_DELAY_SECONDS = 2  # seconds
SUBMISSION_TIMEOUT = 300  # seconds
BUNDLE_URL_EXPIRY = 300  # seconds; presigned bundle-download URL lifetime


# ---- Other Settings -----
MAX_TASK_RETRIES = 3
MAX_SUBMIT_RETRIES = 3

WANDB_MAX_LOGS = 60_000

SCORING_SYSTEM = "top"  # "linear", "top"
BURNING_RATE = 0.02
OWNER_HOTKEY = "5DJ5fT174AY8GzbYHnamYQCJd4cTcj2Zf7ogUvBhry1KfYVd"

BASE_BLOCK_NUMBER = 1843300
INTERVAL_BLOCKS = 720
VALIDATION_BLOCK = 450
WEIGHT_SET_BLOCK = 700
