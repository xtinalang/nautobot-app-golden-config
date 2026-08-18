"""Unit tests for backup-diff ingestion (the backup-time hook and its routines).

``build_commit_events`` is pure and side-effect-free by design, so it is tested against a real temporary
git repository plus a real ``GoldenConfigSetting`` -- no live backup job needed.
"""

import shutil
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType
from django.utils.timezone import now
from git import Repo
from nautobot.apps.testing import TestCase
from nautobot.dcim.models import Device
from nautobot.extras.models import DynamicGroup, GitRepository, GraphQLQuery

from nautobot_golden_config.models import BackupVersion, GoldenConfigSetting
from nautobot_golden_config.tests.conftest import create_device, create_helper_repo, create_saved_queries
from nautobot_golden_config.utilities import backup_diff_ingest

_INGEST = "nautobot_golden_config.utilities.backup_diff_ingest"
# precompute_diff imports the read layer's diff() lazily inside the function, so it must be patched at its
# source module rather than on the ingest module.
_READ_DIFF = "nautobot_golden_config.utilities.backup_diff_read.diff"


class BackupRepoTestCaseMixin:
    """A temp backup git repository plus a ``GoldenConfigSetting`` pointing at it.

    Shared by the hook tests and the backfill tests: both need real git history and a real path template,
    since the whole point of these code paths is parsing what git actually emits.
    """

    def setUp(self):
        """Build a temp backup repo and point a GoldenConfigSetting at it."""
        super().setUp()
        GitRepository.objects.all().delete()
        self.device = create_device()
        create_helper_repo(name="backup-diff-repo", provides="backupconfigs")
        self.repo_record = GitRepository.objects.get(name="backup-diff-repo")

        self.repo_path = tempfile.mkdtemp(prefix="gc-ingest-")
        self.addCleanup(shutil.rmtree, self.repo_path, True)
        self.repo = Repo.init(self.repo_path)
        with self.repo.config_writer() as config:
            config.set_value("user", "name", "svc-golden-config")
            config.set_value("user", "email", "svc@example.com")

        # Build the setting outright rather than mutating GoldenConfigSetting.objects.first(): under the
        # full suite other tests clear that table, so "first()" is None and the setup blows up depending on
        # test order. This case owns everything it needs.
        create_saved_queries()
        self.setting = GoldenConfigSetting.objects.create(
            name="backup-diff-setting",
            slug="backup-diff-setting",
            weight=5000,
            backup_repository=self.repo_record,
            backup_path_template="configs/{{ obj.name }}.cfg",
            # An empty-filter group matches every Device, so the test device is unambiguously in scope.
            dynamic_group=DynamicGroup.objects.create(
                name="backup-diff-all-devices",
                content_type=ContentType.objects.get_for_model(Device),
                filter={},
            ),
            # clean() requires a SoT-Agg query while ENABLE_SOTAGG is on.
            sot_agg_query=GraphQLQuery.objects.get(name="GC-SoTAgg-Query-1"),
        )

        # filesystem_path is derived from settings; patch it to the temp checkout for the whole test.
        patcher = patch.object(type(self.repo_record), "filesystem_path", property(lambda _self: self.repo_path))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _commit(self, files, message="backup"):
        """Write ``{rel_path: text}`` into the repo and commit; return the SHA."""
        import os  # pylint: disable=import-outside-toplevel

        for rel_path, text in files.items():
            full_path = os.path.join(self.repo_path, rel_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as handle:
                handle.write(text)
            self.repo.index.add([rel_path])
        return self.repo.index.commit(message).hexsha


class BuildCommitEventsTestCase(BackupRepoTestCaseMixin, TestCase):
    """``build_commit_events`` expands one backup commit into one event per changed device."""

    def test_builds_one_event_per_changed_device(self):
        """A commit touching the device's rendered backup path produces an event carrying its blob hash."""
        sha = self._commit({f"configs/{self.device.name}.cfg": "hostname foo\n"}, "backup foo")
        events = backup_diff_ingest.build_commit_events(self.repo_record, sha)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.device_id, str(self.device.id))
        self.assertEqual(event.repo_id, str(self.repo_record.id))
        self.assertEqual(event.commit_sha, sha)
        self.assertEqual(event.path, f"configs/{self.device.name}.cfg")
        self.assertEqual(event.committer, "svc-golden-config")
        self.assertEqual(event.message, "backup foo")
        # blob_sha must be git's hash of the file content at that commit, not the commit SHA.
        self.assertNotEqual(event.blob_sha, sha)
        expected_blob = (self.repo.commit(sha).tree / f"configs/{self.device.name}.cfg").hexsha
        self.assertEqual(event.blob_sha, expected_blob)

    def test_ignores_files_that_are_not_device_backups(self):
        """Changed files with no matching device (README, .gitignore) produce no events."""
        sha = self._commit({"README.md": "hello\n"}, "docs")
        self.assertEqual(backup_diff_ingest.build_commit_events(self.repo_record, sha), [])

    def test_returns_empty_when_the_repo_is_not_on_disk(self):
        """A GitRepository record with no checkout on this node degrades to no events, not an error."""
        missing = tempfile.mkdtemp(prefix="gc-ingest-missing-")
        shutil.rmtree(missing, ignore_errors=True)
        with patch.object(type(self.repo_record), "filesystem_path", property(lambda _self: missing)):
            self.assertEqual(backup_diff_ingest.build_commit_events(self.repo_record, "a" * 40), [])

    def test_events_round_trip_through_their_dict_form(self):
        """``as_dict``/``from_dict`` are lossless, which is what makes the Celery hop safe."""
        sha = self._commit({f"configs/{self.device.name}.cfg": "hostname foo\n"})
        event = backup_diff_ingest.build_commit_events(self.repo_record, sha)[0]
        self.assertEqual(backup_diff_ingest.BackupCommitEvent.from_dict(event.as_dict()), event)

    def test_safe_build_never_raises_when_expansion_fails(self):
        """Ingest is fault-isolated: an exception during expansion is swallowed, not propagated."""
        with patch(f"{_INGEST}.build_commit_events", side_effect=RuntimeError("boom")):
            self.assertEqual(backup_diff_ingest.safe_build_commit_events(self.repo_record, "a" * 40), [])


class WriteBackupVersionTestCase(TestCase):
    """write_backup_version upserts exactly one index row per (device, commit)."""

    def setUp(self):
        """Create a device and a backup repository record."""
        super().setUp()
        GitRepository.objects.all().delete()
        self.device = create_device()
        create_helper_repo(name="backup-diff-repo", provides="backupconfigs")
        self.repo_record = GitRepository.objects.get(name="backup-diff-repo")

    def _event(self, commit_sha, blob_sha, authored_date, message="backup"):
        """Build a BackupCommitEvent for the test device."""
        return backup_diff_ingest.BackupCommitEvent(
            device_id=str(self.device.id),
            repo_id=str(self.repo_record.id),
            commit_sha=commit_sha,
            blob_sha=blob_sha,
            path="configs/foobaz.cfg",
            authored_date=authored_date.isoformat(),
            committer="svc-golden-config",
            message=message,
        )

    def test_replaying_the_same_event_updates_rather_than_duplicates(self):
        """Idempotency: a retried or redelivered Celery task must not create a second row."""
        event = self._event("a" * 40, "1" * 40, now())
        backup_diff_ingest.write_backup_version(event)
        backup_diff_ingest.write_backup_version(event)
        self.assertEqual(BackupVersion.objects.filter(device=self.device, commit_sha="a" * 40).count(), 1)

    def test_precompute_picks_the_immediately_older_version(self):
        """The diff is warmed against the previous version, not an arbitrary older one."""
        moment = now()
        for offset, sha in enumerate(["a", "b", "c"]):
            backup_diff_ingest.write_backup_version(
                self._event(sha * 40, sha * 40, moment - timedelta(minutes=10 - offset))
            )
        newest = self._event("d" * 40, "d" * 40, moment)
        backup_diff_ingest.write_backup_version(newest)

        with patch(_READ_DIFF) as mock_diff:
            backup_diff_ingest.precompute_diff(newest)
        mock_diff.assert_called_once()
        self.assertEqual(mock_diff.call_args[0][0], "c" * 40)
        self.assertEqual(mock_diff.call_args[0][1], "d" * 40)

    def test_precompute_breaks_timestamp_ties_deterministically(self):
        """Commits sharing an authored timestamp resolve by commit_sha instead of arbitrarily.

        A single backup job pushing several devices can stamp identical timestamps, so "previous version"
        has to be a total order: strictly older by date, or same date with a lower commit_sha.
        """
        moment = now()
        backup_diff_ingest.write_backup_version(self._event("a" * 40, "1" * 40, moment))
        backup_diff_ingest.write_backup_version(self._event("b" * 40, "2" * 40, moment))
        newest = self._event("c" * 40, "3" * 40, moment)
        backup_diff_ingest.write_backup_version(newest)

        with patch(_READ_DIFF) as mock_diff:
            backup_diff_ingest.precompute_diff(newest)
        # "b" is the highest commit_sha strictly below "c" at the same timestamp.
        self.assertEqual(mock_diff.call_args[0][0], "2" * 40)

    def test_precompute_is_a_no_op_for_a_devices_first_version(self):
        """With nothing older to compare against, precompute returns without diffing."""
        event = self._event("a" * 40, "1" * 40, now())
        backup_diff_ingest.write_backup_version(event)
        with patch(_READ_DIFF) as mock_diff:
            backup_diff_ingest.precompute_diff(event)
        mock_diff.assert_not_called()


class BackfillIndexTestCase(BackupRepoTestCaseMixin, TestCase):
    """``backfill_index`` rebuilds the index from history already in git.

    This is the code path that makes prune and bulk-delete recoverable, so it is tested against a real
    repository rather than mocks -- including the two ways it recovers *less* than the caller expects.
    """

    def test_indexes_existing_history(self):
        """Every commit touching a tracked device becomes a row, with git's own metadata."""
        first = self._commit({f"configs/{self.device.name}.cfg": "hostname a\n"}, "first backup")
        second = self._commit({f"configs/{self.device.name}.cfg": "hostname b\n"}, "second backup")

        stats = backup_diff_ingest.backfill_index(dry_run=False)

        self.assertEqual(stats["repositories"], 1)
        self.assertEqual(stats["rows"], 2)
        self.assertEqual(stats["written"], 2)
        rows = {row.commit_sha: row for row in BackupVersion.objects.all()}
        self.assertEqual(set(rows), {first, second})
        self.assertEqual(rows[second].message, "second backup")
        self.assertEqual(rows[second].committer, "svc-golden-config")
        self.assertEqual(rows[second].path, f"configs/{self.device.name}.cfg")
        # The blob hash must be the file's content hash, which is what the content-addressed diff needs.
        self.assertEqual(rows[second].blob_sha, (self.repo.commit(second).tree / rows[second].path).hexsha)

    def test_dry_run_counts_without_writing(self):
        """A dry run reports what it would do and leaves the table empty."""
        self._commit({f"configs/{self.device.name}.cfg": "hostname a\n"})
        stats = backup_diff_ingest.backfill_index(dry_run=True)
        self.assertEqual(stats["rows"], 1)
        self.assertEqual(stats["written"], 0)
        self.assertEqual(BackupVersion.objects.count(), 0)

    def test_rerunning_is_idempotent(self):
        """A second pass writes nothing new -- ``ignore_conflicts`` leans on (device, commit_sha)."""
        self._commit({f"configs/{self.device.name}.cfg": "hostname a\n"})
        backup_diff_ingest.backfill_index(dry_run=False)
        stats = backup_diff_ingest.backfill_index(dry_run=False)
        self.assertEqual(stats["written"], 0, "re-running must not report or create duplicate rows")
        self.assertEqual(BackupVersion.objects.count(), 1)

    def test_restores_deleted_records(self):
        """The recovery story: records removed by prune or bulk delete come back from git.

        This is what the retention and bulk-delete copy promises, so it is asserted rather than assumed.
        """
        self._commit({f"configs/{self.device.name}.cfg": "hostname a\n"}, "first backup")
        self._commit({f"configs/{self.device.name}.cfg": "hostname b\n"}, "second backup")
        backup_diff_ingest.backfill_index(dry_run=False)
        before = {(row.commit_sha, row.blob_sha, row.message, row.authored_date) for row in BackupVersion.objects.all()}

        BackupVersion.objects.all().delete()
        backup_diff_ingest.backfill_index(dry_run=False)

        after = {(row.commit_sha, row.blob_sha, row.message, row.authored_date) for row in BackupVersion.objects.all()}
        self.assertEqual(after, before, "restored rows must be identical to the originals")

    def test_reports_when_the_commit_limit_truncated_the_walk(self):
        """Stopping on the cap is surfaced, because it means older history was NOT indexed."""
        for index in range(3):
            self._commit({f"configs/{self.device.name}.cfg": f"hostname {index}\n"}, f"backup {index}")

        capped = backup_diff_ingest.backfill_index(max_commits_per_repo=2, dry_run=True)
        complete = backup_diff_ingest.backfill_index(max_commits_per_repo=50, dry_run=True)

        self.assertEqual(capped["commits"], 2)
        self.assertEqual(capped["capped_repositories"], [self.repo_record.name])
        self.assertEqual(complete["commits"], 3)
        self.assertEqual(complete["capped_repositories"], [], "a complete walk must not claim it was capped")

    def test_counts_paths_that_match_no_in_scope_device(self):
        """Unmapped paths are counted, so a rename-induced miss is not silent.

        A device whose backup path no longer renders the same cannot be matched to its own history. The
        count is what distinguishes "the repo also holds a README" from "the fleet was renamed".
        """
        self._commit(
            {
                f"configs/{self.device.name}.cfg": "hostname a\n",
                "configs/decommissioned-device.cfg": "hostname gone\n",
                "README.md": "notes\n",
            },
            "mixed commit",
        )
        stats = backup_diff_ingest.backfill_index(dry_run=True)

        self.assertEqual(stats["rows"], 1, "only the in-scope device should produce a row")
        self.assertEqual(stats["unmapped_paths"], 2)
        self.assertEqual(stats["unmapped_sample"], ["README.md", "configs/decommissioned-device.cfg"])

    def test_missing_checkout_is_skipped_not_fatal(self):
        """A repository record whose clone is absent on this node degrades to a no-op."""
        missing = tempfile.mkdtemp(prefix="gc-backfill-missing-")
        shutil.rmtree(missing, ignore_errors=True)
        with patch.object(type(self.repo_record), "filesystem_path", property(lambda _self: missing)):
            stats = backup_diff_ingest.backfill_index(dry_run=False)
        self.assertEqual(stats["repositories"], 0)
        self.assertEqual(BackupVersion.objects.count(), 0)
