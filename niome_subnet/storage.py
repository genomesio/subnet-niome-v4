"""Validator storage backed by AWS S3 or Google Drive.

The backend is chosen once per process: S3 when AWS credentials are
configured, otherwise Google Drive when a service account is configured.

Miners never get credentials. They receive two plain HTTP URLs:
  - a download URL for their case bundle (plain GET);
  - an upload URL for their submission (plain PUT).
For S3 both are presigned URLs. Google Drive has no presigned URLs, so:
  - the bundle is shared as "anyone with the link can view" and the miner gets
    a direct download link. Each round uploads a fresh file, so links from
    earlier rounds point at deleted files and stop working;
  - the upload URL is a resumable upload session, which needs no credentials
    and only accepts content for the one file it was opened for.

Keys are S3-style paths ("niome/SARPP/12.json"); always derive them from
`settings.submission_key()` / `settings.bundle_key()`.
"""
import json
import logging
import time
from datetime import datetime
from functools import lru_cache, wraps

import boto3
import requests
from botocore.exceptions import ClientError

import niome_subnet.utils.settings as config

logger = logging.getLogger(__name__)

# A valid submission is far smaller than this; the cap stops a miner from
# filling the validator's disk or memory.
MAX_SUBMISSION_BYTES = 20 * 1024 * 1024


class SubmissionTooLate(Exception):
    pass


class SubmissionTooLarge(Exception):
    pass


def _copy_capped(chunks, f, key: str) -> None:
    """Write chunks to f, stopping once more than MAX_SUBMISSION_BYTES has been read."""
    written = 0
    for chunk in chunks:
        written += len(chunk)
        if written > MAX_SUBMISSION_BYTES:
            raise SubmissionTooLarge(f"{key} is larger than {MAX_SUBMISSION_BYTES} bytes")
        f.write(chunk)


class S3Storage:
    name = "s3"

    def __init__(self):
        self.bucket = config.AWS_S3_BUCKET
        self.s3 = boto3.client(
            "s3",
            aws_access_key_id=config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
            region_name=config.AWS_REGION,
        )

    def create_upload_url(self, key: str, expires: int) -> str:
        return self.s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires,
        )

    def create_download_url(self, key: str, expires: int) -> str:
        return self.s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires,
        )

    def _get_capped_object(self, key: str) -> dict:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise FileNotFoundError(key) from e
            raise
        if obj["ContentLength"] > MAX_SUBMISSION_BYTES:
            obj["Body"].close()
            raise SubmissionTooLarge(f"{key} is {obj['ContentLength']} bytes")
        return obj

    def download_file(self, key: str, path: str) -> None:
        obj = self._get_capped_object(key)
        with open(path, "wb") as f:
            _copy_capped(obj["Body"].iter_chunks(1024 * 1024), f, key)

    def upload_file(self, path: str, key: str) -> None:
        self.s3.upload_file(Filename=path, Bucket=self.bucket, Key=key)

    def get_bytes(self, key: str) -> bytes:
        return self._get_capped_object(key)["Body"].read()


DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
DRIVE_API = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
# Direct download for files shared with "anyone with the link"; confirm=t skips
# the virus-scan page Drive shows for larger files.
DRIVE_PUBLIC_DOWNLOAD = "https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_TIMEOUT = 60  # seconds
UPLOAD_TIMEOUT = 300  # seconds
# Stored on each submission file so late uploads can be rejected (see below).
DEADLINE_PROPERTY = "upload_deadline"
READ_ATTEMPTS = 3
TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def _is_transient(e: Exception) -> bool:
    from google.auth.exceptions import TransportError

    if isinstance(e, (requests.ConnectionError, requests.Timeout,
                      requests.exceptions.ChunkedEncodingError, TransportError)):
        return True
    if isinstance(e, requests.HTTPError) and e.response is not None:
        status = e.response.status_code
        if status in TRANSIENT_STATUS:
            return True
        # Drive reports rate limiting as 403 with one of these reasons.
        if status == 403 and ("rateLimitExceeded" in e.response.text
                              or "userRateLimitExceeded" in e.response.text):
            return True
    return False


def _retry_transient(fn):
    """Retry on network errors, rate limits and 5xx responses. Other errors raise at once."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        for attempt in range(1, READ_ATTEMPTS + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                if attempt == READ_ATTEMPTS or not _is_transient(e):
                    raise
                logger.warning(f"{fn.__name__} failed (attempt {attempt}/{READ_ATTEMPTS}), retrying: {e}")
                time.sleep(2 ** attempt)
    return wrapper


def _escape_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _parse_drive_time(value: str) -> float:
    # Drive returns RFC 3339 UTC timestamps, e.g. "2026-10-17T08:15:30.123Z".
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class GoogleDriveStorage:
    """Maps S3-style keys to a folder tree under GOOGLE_DRIVE_ROOT_FOLDER_ID.

    Upload session URLs stay valid for about a week, much longer than an S3
    presigned URL. To keep the same guarantees as S3:
      - every new upload URL is for a freshly created file, so URLs handed out
        in earlier rounds point at deleted files and stop working;
      - the file records its upload deadline, and download_file() rejects it
        if it was modified after that deadline.
    """
    name = "gdrive"

    def __init__(self):
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=DRIVE_SCOPES
        )
        self.session = AuthorizedSession(credentials)
        self.root_folder_id = config.GOOGLE_DRIVE_ROOT_FOLDER_ID
        self._folder_cache: dict[tuple[str, str], str] = {}

    # -- Drive helpers ------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        params = {"supportsAllDrives": "true", **kwargs.pop("params", {})}
        resp = self.session.request(method, url, params=params, timeout=DRIVE_TIMEOUT, **kwargs)
        resp.raise_for_status()
        return resp

    def _find_children(self, name: str, parent_id: str, mime_type: str | None = None) -> list[dict]:
        query = f"name = '{_escape_query(name)}' and '{parent_id}' in parents and trashed = false"
        if mime_type:
            query += f" and mimeType = '{mime_type}'"
        resp = self._request("GET", DRIVE_API, params={
            "q": query,
            "fields": "files(id, size, modifiedTime, appProperties)",
            "orderBy": "modifiedTime desc",
            "includeItemsFromAllDrives": "true",
        })
        return resp.json().get("files", [])

    def _folder_id(self, parts: list[str]) -> str:
        parent_id = self.root_folder_id
        for name in parts:
            cache_key = (parent_id, name)
            if cache_key not in self._folder_cache:
                existing = self._find_children(name, parent_id, FOLDER_MIME)
                if existing:
                    folder_id = existing[0]["id"]
                else:
                    folder_id = self._request("POST", DRIVE_API, params={"fields": "id"}, json={
                        "name": name, "mimeType": FOLDER_MIME, "parents": [parent_id],
                    }).json()["id"]
                self._folder_cache[cache_key] = folder_id
            parent_id = self._folder_cache[cache_key]
        return parent_id

    def _locate(self, key: str) -> tuple[str, str]:
        parts = [p for p in key.split("/") if p]
        if not parts:
            raise ValueError(f"Invalid storage key: {key!r}")
        return self._folder_id(parts[:-1]), parts[-1]

    def _find_file(self, key: str) -> dict:
        parent_id, name = self._locate(key)
        files = self._find_children(name, parent_id)
        if not files:
            raise FileNotFoundError(key)
        return files[0]

    def _replace_with_empty_file(self, key: str, mime_type: str, app_properties: dict | None = None) -> str:
        """Delete every file at `key` and create a new empty one. Returns its ID."""
        parent_id, name = self._locate(key)
        for old in self._find_children(name, parent_id):
            self._request("DELETE", f"{DRIVE_API}/{old['id']}")
        metadata = {"name": name, "parents": [parent_id], "mimeType": mime_type}
        if app_properties:
            metadata["appProperties"] = app_properties
        return self._request("POST", DRIVE_API, params={"fields": "id"}, json=metadata).json()["id"]

    def _start_update_session(self, file_id: str) -> str:
        resp = self._request(
            "PATCH", f"{DRIVE_UPLOAD_API}/{file_id}",
            params={"uploadType": "resumable"},
            json={},
        )
        return resp.headers["Location"]

    def _check_size(self, file: dict, key: str) -> None:
        size = int(file.get("size") or 0)
        if size > MAX_SUBMISSION_BYTES:
            raise SubmissionTooLarge(f"{key} is {size} bytes")

    # -- storage operations -------------------------------------------------

    @_retry_transient
    def create_upload_url(self, key: str, expires: int) -> str:
        deadline = int(time.time()) + int(expires)
        file_id = self._replace_with_empty_file(
            key, "application/json", {DEADLINE_PROPERTY: str(deadline)}
        )
        return self._start_update_session(file_id)

    @_retry_transient
    def create_download_url(self, key: str, expires: int) -> str:
        # Drive links cannot expire on their own; the file is replaced every
        # round instead (see upload_file), which kills older links.
        file = self._find_file(key)
        self._request("POST", f"{DRIVE_API}/{file['id']}/permissions", params={"fields": "id"}, json={
            "type": "anyone", "role": "reader",
        })
        return DRIVE_PUBLIC_DOWNLOAD.format(file_id=file["id"])

    @_retry_transient
    def download_file(self, key: str, path: str) -> None:
        file = self._find_file(key)
        self._check_size(file, key)
        deadline = (file.get("appProperties") or {}).get(DEADLINE_PROPERTY)
        if deadline and _parse_drive_time(file["modifiedTime"]) > int(deadline):
            raise SubmissionTooLate(f"{key} was uploaded after its deadline")

        resp = self._request("GET", f"{DRIVE_API}/{file['id']}", params={"alt": "media"}, stream=True)
        with open(path, "wb") as f:
            # Backup check in case the reported size is missing or wrong.
            _copy_capped(resp.iter_content(chunk_size=1024 * 1024), f, key)

    @_retry_transient
    def upload_file(self, path: str, key: str) -> None:
        """Replace `key` with the local file, as a new Drive file."""
        file_id = self._replace_with_empty_file(key, "application/octet-stream")
        session_url = self._start_update_session(file_id)
        with open(path, "rb") as f:
            requests.put(session_url, data=f, timeout=UPLOAD_TIMEOUT).raise_for_status()

    @_retry_transient
    def get_bytes(self, key: str) -> bytes:
        file = self._find_file(key)
        self._check_size(file, key)
        return self._request("GET", f"{DRIVE_API}/{file['id']}", params={"alt": "media"}).content


@lru_cache(maxsize=1)
def get_storage():
    if config.AWS_ACCESS_KEY_ID and config.AWS_SECRET_ACCESS_KEY and config.AWS_S3_BUCKET:
        logger.info("Using AWS S3 storage")
        return S3Storage()
    if config.GOOGLE_SERVICE_ACCOUNT_FILE and config.GOOGLE_DRIVE_ROOT_FOLDER_ID:
        logger.info("Using Google Drive storage")
        return GoogleDriveStorage()
    raise RuntimeError(
        "No storage configured: set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_S3_BUCKET "
        "or GOOGLE_SERVICE_ACCOUNT_FILE/GOOGLE_DRIVE_ROOT_FOLDER_ID"
    )


def get_json(key: str):
    return json.loads(get_storage().get_bytes(key).decode("utf-8"))
