import logging
import os
import re
from typing import Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

logger = logging.getLogger(__name__)


def validate_s3_key(s3_key: str) -> str:
    """
    Validate and sanitize S3 key to prevent path traversal attacks.

    Args:
        s3_key: S3 object key to validate

    Returns:
        str: Validated and sanitized S3 key

    Raises:
        ValueError: If key contains invalid characters or patterns
    """
    if not s3_key or not isinstance(s3_key, str):
        raise ValueError("S3 key must be a non-empty string")

    # Remove leading/trailing whitespace
    s3_key = s3_key.strip()

    # Check for path traversal patterns
    if ".." in s3_key or s3_key.startswith("/") or s3_key.endswith("/"):
        raise ValueError("S3 key contains invalid path traversal patterns")

    # Check for invalid characters (basic validation)
    invalid_chars = ["\\", "\x00", "\r", "\n"]
    if any(char in s3_key for char in invalid_chars):
        raise ValueError("S3 key contains invalid characters")

    # Ensure reasonable length (S3 key limit is 1024 UTF-8 bytes)
    if len(s3_key.encode("utf-8")) > 1000:  # Leave some buffer
        raise ValueError("S3 key is too long")

    # Validate format: should be alphanumeric, hyphens, underscores, slashes, dots
    if not re.match(r"^[a-zA-Z0-9/_.-]+$", s3_key):
        raise ValueError("S3 key contains unsupported characters")

    return s3_key


def get_s3_client():
    """Create and return S3 client with Hetzner Object Storage configuration"""
    return boto3.client(
        "s3",
        aws_access_key_id=os.environ["S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["S3_SECRET_KEY"],
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        region_name="auto",  # Hetzner uses 'auto' region
    )


def save_file_to_s3(file_content: str, s3_key: str, bucket_name: Optional[str] = None) -> bool:
    """
    Save file content to S3-compatible storage

    Args:
        file_content: Content to save (string)
        s3_key: S3 object key (path in bucket)
        bucket_name: S3 bucket name (optional, uses env var if not provided)

    Returns:
        bool: True if successful, False otherwise
    """
    if bucket_name is None:
        bucket_name = os.environ["S3_BUCKET_NAME"]

    try:
        # Validate S3 key for security
        s3_key = validate_s3_key(s3_key)
        s3_client = get_s3_client()
        s3_client.put_object(
            Bucket=bucket_name,
            Key=s3_key,
            Body=file_content.encode("utf-8"),
            ContentType="text/plain",
        )
        logger.info(f"✅ Saved to S3: s3://{bucket_name}/{s3_key}")
        return True
    except (ClientError, NoCredentialsError, ValueError) as e:
        logger.error(f"❌ Failed to save to S3: {e}")
        return False


def save_binary_file_to_s3(file_content: bytes, s3_key: str, bucket_name: Optional[str] = None) -> bool:
    """
    Save binary file content to S3-compatible storage

    Args:
        file_content: Binary content to save
        s3_key: S3 object key (path in bucket)
        bucket_name: S3 bucket name (optional, uses env var if not provided)

    Returns:
        bool: True if successful, False otherwise
    """
    if bucket_name is None:
        bucket_name = os.environ["S3_BUCKET_NAME"]

    try:
        # Validate S3 key for security
        s3_key = validate_s3_key(s3_key)
        s3_client = get_s3_client()
        s3_client.put_object(
            Bucket=bucket_name,
            Key=s3_key,
            Body=file_content,
            ContentType="application/octet-stream",
        )
        logger.info(f"✅ Saved binary to S3: s3://{bucket_name}/{s3_key}")
        return True
    except (ClientError, NoCredentialsError, ValueError) as e:
        logger.error(f"❌ Failed to save binary to S3: {e}")
        return False
