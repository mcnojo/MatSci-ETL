
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError


class S3Store:
    """S3-backed artifact store for prod runs.

    URI scheme is ``s3://``.  Keys follow the layout from aws-pipeline-plan.md:
        s3://{bucket}/{prefix}/documents/{document_id}/runs/{run_id}/...

    The client is created once at init.  Pass ``region_name`` or let boto3
    pick it up from the environment / instance profile.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        region_name: str | None = None,
    ):
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._s3 = boto3.client("s3", region_name=region_name)

    @property
    def bucket(self) -> str:
        return self._bucket

    def read_bytes(self, uri: str) -> bytes:
        bucket, key = self._parse_s3_uri(uri)
        resp = self._s3.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read()

    def write_bytes(self, uri: str, data: bytes) -> None:
        bucket, key = self._parse_s3_uri(uri)
        self._s3.put_object(Bucket=bucket, Key=key, Body=data)

    def uri_for(self, document_id: str, run_id: str, *path_parts: str) -> str:
        segments = ["documents", document_id, "runs", run_id, *path_parts]
        key = "/".join(segments)
        if self._prefix:
            key = f"{self._prefix}/{key}"
        return f"s3://{self._bucket}/{key}"

    def exists(self, uri: str) -> bool:
        bucket, key = self._parse_s3_uri(uri)
        try:
            self._s3.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise

    @staticmethod
    def _parse_s3_uri(uri: str) -> tuple[str, str]:
        parsed = urlparse(uri)
        if parsed.scheme != "s3":
            raise ValueError(f"S3Store expects s3:// URIs, got {uri!r}")
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        if not bucket or not key:
            raise ValueError(f"Malformed S3 URI (missing bucket or key): {uri!r}")
        return bucket, key
