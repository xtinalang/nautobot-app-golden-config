"""Content store abstraction for Backup History Diff.

Config *content* for a version is fetched through a ``ContentStore`` so the storage backend is swappable
without touching ingest or read code:

* ``GitContentStore`` (default) -- git already holds the content, so ``publish`` is a no-op and ``get``
  reads the blob straight from the backup repo by its content hash. Zero new infrastructure.
* ``ObjectContentStore`` -- a content-addressed object store (S3/MinIO) for large fleets, where every web
  node fetches a blob by hash instead of holding a full git clone, and falls back to git on a miss.

The backend is chosen by the ``backup_diff_content_store`` plugin setting ("git" | "s3").
"""

import logging
from abc import ABC, abstractmethod

from git import GitCommandError

from nautobot_golden_config.utilities.config_diff import open_repo
from nautobot_golden_config.utilities.constant import PLUGIN_CFG

LOGGER = logging.getLogger(__name__)

# Module-level cache: boto3 S3 clients are thread-safe and reusable, and get_content_store() builds a new
# store per call, so caching the client here avoids recreating it on every diff read. Keyed by
# (endpoint_url, region) rather than cached as a single client, so a future per-repository bucket/endpoint
# can never be served a client built for a different endpoint.
_S3_CLIENTS = {}


class ContentStore(ABC):
    """Interface for storing/fetching a config version's text, keyed by its content hash (blob_sha)."""

    @abstractmethod
    def publish(self, event):
        """Make the content for ``event`` fetchable by ``event.blob_sha``."""

    @abstractmethod
    def get(self, blob_sha, repository=None):
        """Return the config text for ``blob_sha`` (``repository`` needed by the git backend), or None."""


class GitContentStore(ContentStore):
    """Backend where git itself is the content store -- the default, no extra infrastructure."""

    def publish(self, event):  # pylint: disable=unused-argument
        """No-op: the blob already exists in the backup git repo, keyed by its content hash."""
        return

    def get(self, blob_sha, repository=None):
        """Read a blob's text from the repo by its content hash (``git cat-file blob <sha>``)."""
        if repository is None:
            return None
        repo = open_repo(repository.filesystem_path, context=f"Wanted blob {blob_sha[:8]}.")
        if repo is None:
            return None
        try:
            return repo.git.cat_file("blob", blob_sha)
        except GitCommandError:
            LOGGER.debug("GitContentStore: blob %s not available in %s.", blob_sha[:8], repository)
            return None


def _is_object_missing(error):
    """Return ``True`` if an S3 error means "no such object" (a normal miss) rather than a real failure."""
    response = getattr(error, "response", None) or {}
    return response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound")


def _get_s3_client(endpoint_url, region):
    """Return a cached boto3 S3 client for this ``(endpoint_url, region)``, or ``None`` when it can't be built.

    boto3 is an optional dependency -- only the S3 backend needs it -- so a missing library (or any client
    construction error) returns ``None`` and callers degrade to the git backend rather than erroring.
    """
    cache_key = (endpoint_url, region)
    if cache_key in _S3_CLIENTS:
        return _S3_CLIENTS[cache_key]
    try:
        import boto3  # pylint: disable=import-outside-toplevel
    except ImportError:
        LOGGER.warning("ObjectContentStore: boto3 is not installed; install it or set backup_diff_content_store='git'.")
        return None
    try:
        client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)
    except Exception as error:  # noqa: BLE001  pylint: disable=broad-exception-caught
        LOGGER.warning("ObjectContentStore: could not create an S3 client: %s", error)
        return None
    _S3_CLIENTS[cache_key] = client
    return client


class ObjectContentStore(ContentStore):
    """Backend for a content-addressed object store (S3/MinIO) -- the large-fleet / multi-node path.

    Every config version is stored under its content hash (``blob_sha``), so any web node can fetch a blob
    by hash without holding a full git clone -- which is what makes the diff work in multi-pod deployments
    where the backup repo may not be checked out on the pod serving the request. Enable by setting
    ``backup_diff_content_store = "s3"``.

    Plugin settings (all optional except the bucket; unset bucket -> this backend no-ops to git):
        ``backup_diff_s3_bucket``       -- bucket name.
        ``backup_diff_s3_prefix``       -- key prefix, e.g. ``"gc-backup-diff/"`` (default ``""``).
        ``backup_diff_s3_endpoint_url`` -- endpoint for MinIO / S3-compatible stores (default AWS S3).
        ``backup_diff_s3_region``       -- AWS region.
        ``backup_diff_s3_sse``          -- server-side encryption, e.g. ``"AES256"`` or ``"aws:kms"``.

    Credentials come from the standard AWS chain (IAM role / environment / shared config) and are never read
    from the app config. Any misconfiguration or S3 error degrades gracefully to git, so enabling S3 can
    never make a diff *less* available than git alone.
    """

    def __init__(self):
        """Read S3 settings from the plugin config and prepare the git fallback."""
        self._bucket = PLUGIN_CFG.get("backup_diff_s3_bucket") or ""
        self._prefix = PLUGIN_CFG.get("backup_diff_s3_prefix", "")
        self._endpoint_url = PLUGIN_CFG.get("backup_diff_s3_endpoint_url") or None
        self._region = PLUGIN_CFG.get("backup_diff_s3_region") or None
        self._sse = PLUGIN_CFG.get("backup_diff_s3_sse") or None
        self._git = GitContentStore()

    def _key(self, blob_sha):
        """Return the content-addressed object key for a blob hash."""
        return f"{self._prefix}{blob_sha}"

    def publish(self, event):
        """Upload this version's config to the bucket keyed by ``blob_sha`` if it isn't already there.

        Runs during ingest on the worker (which has the git clone): fetches the text from git and PUTs it
        under its content hash so web nodes can later fetch it without a clone. Idempotent -- an existing
        object (same immutable hash) is left untouched. Logs and returns on any error; ingest must never
        fail because of the object store.
        """
        client = _get_s3_client(self._endpoint_url, self._region)
        if client is None or not self._bucket:
            LOGGER.debug("ObjectContentStore: S3 unavailable/unconfigured; skipped publish of %s.", event.blob_sha[:8])
            return
        key = self._key(event.blob_sha)
        try:
            client.head_object(Bucket=self._bucket, Key=key)
            return  # already stored -- content-addressed, so the bytes are identical
        except Exception as error:  # noqa: BLE001  pylint: disable=broad-exception-caught
            if not _is_object_missing(error):
                LOGGER.warning("ObjectContentStore: head_object failed for %s: %s", key, error)
                return
            # object is genuinely missing -- fall through and upload it

        # Lazy import to avoid an import cycle with the utilities package models imports at load time.
        from nautobot.extras.models import GitRepository  # pylint: disable=import-outside-toplevel

        repository = GitRepository.objects.filter(pk=event.repo_id).first()
        text = self._git.get(event.blob_sha, repository=repository)
        if text is None:
            LOGGER.debug("ObjectContentStore: no git content to publish for %s.", event.blob_sha[:8])
            return
        extra = {"ServerSideEncryption": self._sse} if self._sse else {}
        try:
            client.put_object(Bucket=self._bucket, Key=key, Body=text.encode("utf-8"), **extra)
        except Exception as error:  # noqa: BLE001  pylint: disable=broad-exception-caught
            LOGGER.warning("ObjectContentStore: put_object failed for %s: %s", key, error)

    def get(self, blob_sha, repository=None):
        """Return the config text for ``blob_sha`` from the bucket, falling back to git on a miss or error."""
        client = _get_s3_client(self._endpoint_url, self._region)
        if client is not None and self._bucket:
            try:
                response = client.get_object(Bucket=self._bucket, Key=self._key(blob_sha))
                return response["Body"].read().decode("utf-8", "replace")
            except Exception as error:  # noqa: BLE001  pylint: disable=broad-exception-caught
                if not _is_object_missing(error):
                    LOGGER.warning("ObjectContentStore: get_object failed for %s: %s", blob_sha[:8], error)
                # fall through to git on any miss/error
        return self._git.get(blob_sha, repository=repository)


def get_content_store():
    """Return the configured content-store backend (default: git)."""
    backend = PLUGIN_CFG.get("backup_diff_content_store", "git")
    if backend == "s3":
        return ObjectContentStore()
    return GitContentStore()
