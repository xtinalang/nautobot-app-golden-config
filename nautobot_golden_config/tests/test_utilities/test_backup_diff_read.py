"""Unit tests for the Backup History Diff read layer (index-first, git-native fallback)."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils.timezone import now
from nautobot.apps.testing import TestCase
from nautobot.extras.models import GitRepository

from nautobot_golden_config.models import BackupVersion
from nautobot_golden_config.tests.conftest import create_device, create_helper_repo
from nautobot_golden_config.utilities import backup_diff_read
from nautobot_golden_config.utilities.config_diff import MAX_HISTORY_ENTRIES

User = get_user_model()

# Patch names in the module under test (where the fallbacks/diff helpers are looked up).
_READ = "nautobot_golden_config.utilities.backup_diff_read"


@patch(f"{_READ}.ENABLE_BACKUP_DIFF_INDEX", True)
class BackupDiffReadTestCase(TestCase):
    """The read layer serves from the ``BackupVersion`` index and falls back to git-native reads.

    The index is opt-in (``enable_backup_diff_index``, off by default), so the whole class runs with the
    flag on; ``BackupDiffIndexDisabledTestCase`` below covers the flag-off behavior.
    """

    def setUp(self):
        """Create a device and a backup repository; grant the base user device-view permission."""
        super().setUp()
        GitRepository.objects.all().delete()
        self.device = create_device()
        create_helper_repo(name="backup-diff-repo", provides="backupconfigs")
        self.repo = GitRepository.objects.get(name="backup-diff-repo")
        # self.user comes from the Nautobot base TestCase and is NOT a superuser -- deliberately, since
        # the read layer must work for ordinary operators (see test_recent_changes_visible_to_non_superuser).
        self.add_permissions("dcim.view_device")

    def _make_version(self, commit_sha, blob_sha, minutes_ago, message="backup"):
        """Create one ``BackupVersion`` index row for the test device."""
        return BackupVersion.objects.create(
            device=self.device,
            repository=self.repo,
            commit_sha=commit_sha,
            blob_sha=blob_sha,
            path="configs/foobaz.cfg",
            authored_date=now() - timedelta(minutes=minutes_ago),
            committer="svc-golden-config",
            message=message,
        )

    # -- history ---------------------------------------------------------------------------------

    def test_history_uses_index_when_populated(self):
        """When index rows exist, ``history`` returns them newest-first and never touches git."""
        self._make_version("a" * 40, "1" * 40, minutes_ago=10)
        self._make_version("b" * 40, "2" * 40, minutes_ago=1)
        with patch(f"{_READ}.get_backup_history") as mock_git:
            result = backup_diff_read.history(self.device)
        mock_git.assert_not_called()
        self.assertEqual([entry["sha"] for entry in result], ["b" * 40, "a" * 40])
        self.assertEqual(result[0]["blob_sha"], "2" * 40)

    def test_history_falls_back_to_git_when_index_empty(self):
        """With no index rows, ``history`` delegates to the git-native reader."""
        sentinel = [{"sha": "c" * 40, "short_sha": "cccccccc", "date": now(), "author": "x", "message": "m"}]
        with patch(f"{_READ}.get_backup_history", return_value=sentinel) as mock_git:
            result = backup_diff_read.history(self.device)
        mock_git.assert_called_once_with(self.device, max_count=MAX_HISTORY_ENTRIES)
        self.assertEqual(result, sentinel)

    # -- recent_changes --------------------------------------------------------------------------

    def test_recent_changes_uses_index_when_populated(self):
        """When index rows exist, ``recent_changes`` returns index entries with the device attached."""
        self._make_version("a" * 40, "1" * 40, minutes_ago=5)
        with patch(f"{_READ}.get_recent_backup_changes") as mock_git:
            result = backup_diff_read.recent_changes(self.user)
        mock_git.assert_not_called()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["device"], self.device)

    def test_recent_changes_falls_back_when_index_empty(self):
        """With no index rows, ``recent_changes`` delegates to the git-native reader (limit passed through)."""
        sentinel = [{"device": self.device, "date": now(), "sha": "c" * 40, "short_sha": "cccccccc", "message": "m"}]
        with patch(f"{_READ}.get_recent_backup_changes", return_value=sentinel) as mock_git:
            result = backup_diff_read.recent_changes(self.user, limit=10)
        mock_git.assert_called_once_with(self.user, limit=10)
        self.assertEqual(result, sentinel)

    def test_recent_changes_visible_to_non_superuser(self):
        """A plain user holding only ``dcim.view_device`` sees index rows.

        Regression test: scoping with ``BackupVersion.objects.restrict(user, "view")`` required
        ``nautobot_golden_config.view_backupversion``, a permission with no UI/API to grant it. Every
        non-superuser therefore got an empty index and silently fell through to the git walk -- two users
        with identical device permissions reading from different sources.
        """
        self._make_version("a" * 40, "1" * 40, minutes_ago=5)
        self.assertFalse(self.user.is_superuser)
        with patch(f"{_READ}.get_recent_backup_changes") as mock_git:
            result = backup_diff_read.recent_changes(self.user)
        mock_git.assert_not_called()
        self.assertEqual([row["device"] for row in result], [self.device])

    def test_recent_changes_excludes_devices_the_user_cannot_view(self):
        """Index rows for devices outside the user's device permissions are not returned."""
        self._make_version("a" * 40, "1" * 40, minutes_ago=5)
        no_access_user = User.objects.create_user(username="bdiff-no-access")
        # No dcim.view_device: the index must yield nothing rather than leaking another device's history.
        rows = backup_diff_read.recent_changes(no_access_user)
        self.assertEqual(rows, [])

    def test_recent_changes_returns_only_the_latest_row_per_device(self):
        """Several versions for one device collapse to that device's newest row."""
        self._make_version("a" * 40, "1" * 40, minutes_ago=30)
        self._make_version("b" * 40, "2" * 40, minutes_ago=20)
        self._make_version("c" * 40, "3" * 40, minutes_ago=1)
        result = backup_diff_read.recent_changes(self.user)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["sha"], "c" * 40)

    # -- history limits --------------------------------------------------------------------------

    def test_history_is_capped(self):
        """The index path caps history the same way the git path does, so the dropdowns stay bounded."""
        for index in range(5):
            self._make_version(f"{index:040d}", f"{index:040d}", minutes_ago=index + 1)
        result = backup_diff_read.history(self.device, limit=3)
        self.assertEqual(len(result), 3)
        # Newest first: minutes_ago 1, 2, 3 -> indexes 0, 1, 2.
        self.assertEqual([entry["sha"] for entry in result], [f"{index:040d}" for index in range(3)])

    def test_history_passes_limit_to_the_git_fallback(self):
        """The git-native fallback gets the same cap, so both sources agree on history length."""
        with patch(f"{_READ}.get_backup_history", return_value=[]) as mock_git:
            backup_diff_read.history(self.device, limit=7)
        mock_git.assert_called_once_with(self.device, max_count=7)

    # -- backup_diff -----------------------------------------------------------------------------

    def test_backup_diff_returns_empty_when_either_side_missing(self):
        """A missing original or modified yields an empty diff."""
        entry = {"sha": "a" * 40, "blob_sha": "1" * 40}
        self.assertEqual(backup_diff_read.backup_diff(self.device, None, entry), ([], 0, 0, False))
        self.assertEqual(backup_diff_read.backup_diff(self.device, entry, None), ([], 0, 0, False))

    def test_backup_diff_uses_content_addressed_path_when_blobs_present(self):
        """Index entries carry a blob_sha and a repository, so ``backup_diff`` takes the cached path."""
        original = {"sha": "a" * 40, "blob_sha": "1" * 40, "repository": self.repo}
        modified = {"sha": "b" * 40, "blob_sha": "2" * 40, "repository": self.repo}
        sentinel = (["row"], 1, 0, False)
        with (
            patch(f"{_READ}.diff", return_value=sentinel) as mock_diff,
            patch(f"{_READ}._git_native_diff") as mock_git,
        ):
            result = backup_diff_read.backup_diff(self.device, original, modified)
        mock_diff.assert_called_once_with("1" * 40, "2" * 40, self.repo)
        mock_git.assert_not_called()
        self.assertEqual(result, sentinel)

    def test_history_rows_carry_the_repository_for_the_cached_diff_path(self):
        """``history`` puts the repository on each index entry, so no extra lookup is needed to diff."""
        self._make_version("a" * 40, "1" * 40, minutes_ago=1)
        entry = backup_diff_read.history(self.device)[0]
        self.assertEqual(entry["repository"], self.repo)
        self.assertEqual(entry["blob_sha"], "1" * 40)

    def test_backup_diff_falls_back_to_git_native_without_blob_sha(self):
        """Entries lacking a blob_sha (git-native history) use the commit-SHA diff path."""
        original = {"sha": "a" * 40}
        modified = {"sha": "b" * 40}
        sentinel = (["row"], 0, 1, False)
        with (
            patch(f"{_READ}._git_native_diff", return_value=sentinel) as mock_git,
            patch(f"{_READ}.diff") as mock_content,
        ):
            result = backup_diff_read.backup_diff(self.device, original, modified)
        mock_git.assert_called_once_with(self.device, original, modified)
        mock_content.assert_not_called()
        self.assertEqual(result, sentinel)

    # -- diff caching ----------------------------------------------------------------------------

    def test_small_diffs_are_cached(self):
        """A normal-sized diff is written to the cache so the next read is warm."""
        with (
            patch(f"{_READ}.get_content_store") as mock_store,
            patch(f"{_READ}.cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            mock_store.return_value.get.side_effect = ["line one\n", "line two\n"]
            backup_diff_read.diff("1" * 40, "2" * 40, self.repo)
        mock_cache.set.assert_called_once()

    def test_oversized_diffs_are_not_cached(self):
        """A diff above MAX_CACHED_DIFF_ROWS is returned but never cached.

        Such a payload pickles to megabytes -- silently dropped by memcached's 1 MB default item size,
        and a waste of memory on Redis -- so it is recomputed rather than stored.
        """
        row_count = backup_diff_read.MAX_CACHED_DIFF_ROWS + 10
        old_text = "\n".join(f"old {index}" for index in range(row_count))
        new_text = "\n".join(f"new {index}" for index in range(row_count))
        with (
            patch(f"{_READ}.get_content_store") as mock_store,
            patch(f"{_READ}.cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            mock_store.return_value.get.side_effect = [old_text, new_text]
            rows, _, _, too_large = backup_diff_read.diff("1" * 40, "2" * 40, self.repo)
        self.assertFalse(too_large)
        self.assertGreater(len(rows), backup_diff_read.MAX_CACHED_DIFF_ROWS)
        mock_cache.set.assert_not_called()

    def test_backup_diff_falls_back_when_repository_unresolved(self):
        """Blob_shas present but no repository on the entries falls back to the git-native diff."""
        original = {"sha": "a" * 40, "blob_sha": "1" * 40, "repository": None}
        modified = {"sha": "b" * 40, "blob_sha": "2" * 40, "repository": None}
        sentinel = (["row"], 0, 0, False)
        with patch(f"{_READ}._git_native_diff", return_value=sentinel) as mock_git:
            result = backup_diff_read.backup_diff(self.device, original, modified)
        mock_git.assert_called_once_with(self.device, original, modified)
        self.assertEqual(result, sentinel)


class DiffCacheKeyTestCase(TestCase):
    """The diff cache key is content-addressed, so it is stable and device-independent."""

    def test_key_is_ordered_and_distinct_per_blob_pair(self):
        """Swapping the two hashes yields a different key (the diff is directional)."""
        forward = backup_diff_read.diff_cache_key("1" * 40, "2" * 40)
        backward = backup_diff_read.diff_cache_key("2" * 40, "1" * 40)
        self.assertNotEqual(forward, backward)
        self.assertEqual(forward, backup_diff_read.diff_cache_key("1" * 40, "2" * 40))


class BackupDiffIndexDisabledTestCase(TestCase):
    """With ``enable_backup_diff_index`` off, the read layer ignores the index entirely."""

    def setUp(self):
        """Create a device, a backup repository, and one index row that must be ignored."""
        super().setUp()
        GitRepository.objects.all().delete()
        self.device = create_device()
        create_helper_repo(name="backup-diff-repo", provides="backupconfigs")
        self.repo = GitRepository.objects.get(name="backup-diff-repo")
        self.add_permissions("dcim.view_device")
        BackupVersion.objects.create(
            device=self.device,
            repository=self.repo,
            commit_sha="a" * 40,
            blob_sha="1" * 40,
            path="configs/foobaz.cfg",
            authored_date=now(),
            committer="svc-golden-config",
            message="backup",
        )

    @patch(f"{_READ}.ENABLE_BACKUP_DIFF_INDEX", False)
    def test_history_ignores_the_index_when_disabled(self):
        """Stale index rows left over from a previous opt-in must not be served once the flag is off."""
        sentinel = [{"sha": "c" * 40, "short_sha": "cccccccc", "date": now(), "author": "x", "message": "m"}]
        with patch(f"{_READ}.get_backup_history", return_value=sentinel) as mock_git:
            result = backup_diff_read.history(self.device)
        mock_git.assert_called_once_with(self.device, max_count=MAX_HISTORY_ENTRIES)
        self.assertEqual(result, sentinel)

    @patch(f"{_READ}.ENABLE_BACKUP_DIFF_INDEX", False)
    def test_recent_changes_ignores_the_index_when_disabled(self):
        """The fleet list likewise goes straight to git rather than reading a disabled index."""
        with patch(f"{_READ}.get_recent_backup_changes", return_value=[]) as mock_git:
            result = backup_diff_read.recent_changes(self.user, limit=5)
        mock_git.assert_called_once_with(self.user, limit=5)
        self.assertEqual(result, [])
