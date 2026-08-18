"""Unit tests for the Backup History Diff content store.

The contract that matters here is *graceful degradation*: enabling S3 must never make a diff less
available than git alone, so every S3 failure path has to fall back to git rather than raise.
"""

import shutil
import tempfile
from unittest.mock import MagicMock, patch

from git import Repo
from nautobot.apps.testing import TestCase

from nautobot_golden_config.utilities import content_store

_STORE = "nautobot_golden_config.utilities.content_store"


class GetContentStoreTestCase(TestCase):
    """Backend selection follows the ``backup_diff_content_store`` setting, defaulting to git."""

    def test_defaults_to_git(self):
        """An unset setting yields the zero-infrastructure git backend."""
        with patch.dict(f"{_STORE}.PLUGIN_CFG", {}, clear=True):
            self.assertIsInstance(content_store.get_content_store(), content_store.GitContentStore)

    def test_selects_object_store_when_configured(self):
        """``"s3"`` yields the object-store backend."""
        with patch.dict(f"{_STORE}.PLUGIN_CFG", {"backup_diff_content_store": "s3"}, clear=True):
            self.assertIsInstance(content_store.get_content_store(), content_store.ObjectContentStore)

    def test_unknown_backend_falls_back_to_git(self):
        """A typo in the setting degrades to git rather than breaking the feature."""
        with patch.dict(f"{_STORE}.PLUGIN_CFG", {"backup_diff_content_store": "nonsense"}, clear=True):
            self.assertIsInstance(content_store.get_content_store(), content_store.GitContentStore)


class GitContentStoreTestCase(TestCase):
    """The git backend reads blobs straight out of the backup checkout by content hash."""

    def setUp(self):
        """Create a temp git repo holding one committed file."""
        super().setUp()
        self.repo_path = tempfile.mkdtemp(prefix="gc-content-store-")
        self.addCleanup(shutil.rmtree, self.repo_path, True)
        self.repo = Repo.init(self.repo_path)
        with self.repo.config_writer() as config:
            config.set_value("user", "name", "svc-golden-config")
            config.set_value("user", "email", "svc@example.com")
        with open(f"{self.repo_path}/a.cfg", "w", encoding="utf-8") as handle:
            handle.write("hostname foo\n")
        self.repo.index.add(["a.cfg"])
        commit = self.repo.index.commit("backup")
        self.blob_sha = (commit.tree / "a.cfg").hexsha
        self.repository = MagicMock(filesystem_path=self.repo_path)
        self.store = content_store.GitContentStore()

    def test_publish_is_a_no_op(self):
        """git already holds the content, so publishing stores nothing."""
        self.assertIsNone(self.store.publish(MagicMock(blob_sha=self.blob_sha)))

    def test_get_returns_the_blob_text(self):
        """A known blob hash reads back the exact committed text."""
        self.assertEqual(self.store.get(self.blob_sha, repository=self.repository), "hostname foo")

    def test_get_returns_none_without_a_repository(self):
        """The git backend cannot resolve content without a repository record."""
        self.assertIsNone(self.store.get(self.blob_sha, repository=None))

    def test_get_returns_none_for_an_unknown_blob(self):
        """A hash that is not in the repo is a miss, not an error."""
        self.assertIsNone(self.store.get("0" * 40, repository=self.repository))

    def test_get_returns_none_when_the_checkout_is_missing(self):
        """A repository record whose clone is absent on this node degrades to a miss."""
        missing = tempfile.mkdtemp(prefix="gc-content-store-missing-")
        shutil.rmtree(missing, ignore_errors=True)
        self.assertIsNone(self.store.get(self.blob_sha, repository=MagicMock(filesystem_path=missing)))


class ObjectContentStoreTestCase(TestCase):
    """The S3 backend degrades to git on every failure path."""

    def setUp(self):
        """Reset the module-level client cache so tests don't leak clients into each other."""
        super().setUp()
        content_store._S3_CLIENTS.clear()  # pylint: disable=protected-access
        self.addCleanup(content_store._S3_CLIENTS.clear)  # pylint: disable=protected-access

    def _store(self, **overrides):
        """Build an ObjectContentStore with the given plugin settings."""
        config = {"backup_diff_content_store": "s3", "backup_diff_s3_bucket": "gc-backups", **overrides}
        with patch.dict(f"{_STORE}.PLUGIN_CFG", config, clear=True):
            return content_store.ObjectContentStore()

    def test_get_falls_back_to_git_when_no_client_can_be_built(self):
        """Missing boto3 (or an unbuildable client) routes the read to git."""
        store = self._store()
        with (
            patch(f"{_STORE}._get_s3_client", return_value=None),
            patch.object(content_store.GitContentStore, "get", return_value="from git") as mock_git,
        ):
            self.assertEqual(store.get("1" * 40, repository="repo"), "from git")
        mock_git.assert_called_once()

    def test_get_falls_back_to_git_on_an_s3_error(self):
        """An S3 failure never makes content less available than git alone."""
        store = self._store()
        client = MagicMock()
        client.get_object.side_effect = RuntimeError("s3 down")
        with (
            patch(f"{_STORE}._get_s3_client", return_value=client),
            patch.object(content_store.GitContentStore, "get", return_value="from git"),
        ):
            self.assertEqual(store.get("1" * 40, repository="repo"), "from git")

    def test_get_returns_object_body_on_a_hit(self):
        """A stored object is decoded and returned without touching git."""
        store = self._store(backup_diff_s3_prefix="gc/")
        client = MagicMock()
        client.get_object.return_value = {"Body": MagicMock(read=lambda: b"hostname foo\n")}
        with (
            patch(f"{_STORE}._get_s3_client", return_value=client),
            patch.object(content_store.GitContentStore, "get") as mock_git,
        ):
            self.assertEqual(store.get("1" * 40, repository="repo"), "hostname foo\n")
        mock_git.assert_not_called()
        self.assertEqual(client.get_object.call_args.kwargs["Key"], "gc/" + "1" * 40)

    def test_publish_skips_when_the_object_already_exists(self):
        """Content-addressed keys are immutable, so an existing object is never re-uploaded."""
        store = self._store()
        client = MagicMock()  # head_object succeeding means "already stored"
        with patch(f"{_STORE}._get_s3_client", return_value=client):
            store.publish(MagicMock(blob_sha="1" * 40))
        client.put_object.assert_not_called()

    def test_publish_is_skipped_without_a_bucket(self):
        """An unset bucket makes this backend a no-op rather than an error."""
        store = self._store(backup_diff_s3_bucket="")
        client = MagicMock()
        with patch(f"{_STORE}._get_s3_client", return_value=client):
            store.publish(MagicMock(blob_sha="1" * 40))
        client.head_object.assert_not_called()
        client.put_object.assert_not_called()


class S3ClientCacheTestCase(TestCase):
    """Clients are cached per ``(endpoint_url, region)``, not globally."""

    def setUp(self):
        """Start from an empty client cache."""
        super().setUp()
        content_store._S3_CLIENTS.clear()  # pylint: disable=protected-access
        self.addCleanup(content_store._S3_CLIENTS.clear)  # pylint: disable=protected-access

    def test_distinct_endpoints_get_distinct_clients(self):
        """Two endpoints must not share one client, or a per-repo bucket would hit the wrong store."""
        boto3 = MagicMock()
        boto3.client.side_effect = lambda *args, **kwargs: MagicMock(name=str(kwargs))
        with patch.dict("sys.modules", {"boto3": boto3}):
            first = content_store._get_s3_client("https://minio.example", "us-east-1")  # pylint: disable=protected-access
            second = content_store._get_s3_client("https://other.example", "us-east-1")  # pylint: disable=protected-access
            again = content_store._get_s3_client("https://minio.example", "us-east-1")  # pylint: disable=protected-access
        self.assertIsNot(first, second)
        self.assertIs(first, again, "the same endpoint/region must reuse its cached client")
