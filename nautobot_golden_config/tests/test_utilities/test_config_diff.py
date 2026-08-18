"""Unit tests for the git-native backup history/diff helpers.

The git-reading helpers are exercised against a REAL temporary git repository rather than mocks: their
whole job is parsing ``git`` output, so a mock that returns what we think git prints proves nothing. In
particular ``_iter_commit_file_changes`` parses a custom ``git log --format`` and is the code path that
replaced a per-commit ``commit.stats.files`` walk, so its parsing has to be checked against real output.
"""

import shutil
import tempfile
from datetime import datetime

from git import GitCommandError, Repo
from nautobot.apps.testing import TestCase

from nautobot_golden_config.utilities import config_diff


class GitBackedTestCase(TestCase):
    """Base that builds a throwaway git repository with a known commit history."""

    def setUp(self):
        """Create a temp git repo with three commits touching two device files."""
        super().setUp()
        self.repo_path = tempfile.mkdtemp(prefix="gc-backup-diff-")
        self.addCleanup(shutil.rmtree, self.repo_path, True)
        self.repo = Repo.init(self.repo_path)
        with self.repo.config_writer() as config:
            config.set_value("user", "name", "svc-golden-config")
            config.set_value("user", "email", "svc@example.com")

    def _commit(self, files, message):
        """Write ``{rel_path: text}`` into the repo and commit it; return the commit SHA."""
        for rel_path, text in files.items():
            full_path = f"{self.repo_path}/{rel_path}"
            with open(full_path, "w", encoding="utf-8") as handle:
                handle.write(text)
            self.repo.index.add([rel_path])
        return self.repo.index.commit(message).hexsha


class IterCommitFileChangesTestCase(GitBackedTestCase):
    """``_iter_commit_file_changes`` parses ``git log --name-only`` into commit metadata + paths."""

    def test_yields_commits_newest_first_with_changed_paths(self):
        """Each record carries the SHA, an aware datetime, the author, the subject, and its file set."""
        first = self._commit({"a.cfg": "one\n"}, "first backup")
        second = self._commit({"b.cfg": "two\n"}, "second backup")
        third = self._commit({"a.cfg": "one changed\n"}, "third backup")

        records = list(config_diff._iter_commit_file_changes(self.repo, 10))  # pylint: disable=protected-access

        self.assertEqual([record[0] for record in records], [third, second, first])
        self.assertEqual([record[4] for record in records], [{"a.cfg"}, {"b.cfg"}, {"a.cfg"}])
        self.assertEqual([record[3] for record in records], ["third backup", "second backup", "first backup"])
        for record in records:
            self.assertIsInstance(record[1], datetime)
            self.assertIsNotNone(record[1].tzinfo, "authored dates must be timezone-aware for sorting")
            self.assertEqual(record[2], "svc-golden-config")

    def test_respects_max_count(self):
        """The cap bounds how deep the single ``git log`` invocation walks."""
        for index in range(5):
            self._commit({"a.cfg": f"rev {index}\n"}, f"backup {index}")
        records = list(config_diff._iter_commit_file_changes(self.repo, 2))  # pylint: disable=protected-access
        self.assertEqual(len(records), 2)

    def test_handles_commit_touching_multiple_files(self):
        """One commit that writes several device files reports all of them."""
        self._commit({"a.cfg": "one\n", "b.cfg": "two\n"}, "bulk backup")
        records = list(config_diff._iter_commit_file_changes(self.repo, 10))  # pylint: disable=protected-access
        self.assertEqual(records[0][4], {"a.cfg", "b.cfg"})

    def test_subject_containing_the_field_separator_is_preserved(self):
        """A commit subject holding the delimiter byte is re-joined instead of truncated."""
        self._commit({"a.cfg": "one\n"}, "backup \x1f odd subject")
        records = list(config_diff._iter_commit_file_changes(self.repo, 10))  # pylint: disable=protected-access
        self.assertEqual(records[0][3], "backup \x1f odd subject")

    def test_empty_repository_yields_nothing(self):
        """A repo with no commits produces no records rather than raising."""
        empty_path = tempfile.mkdtemp(prefix="gc-backup-diff-empty-")
        self.addCleanup(shutil.rmtree, empty_path, True)
        empty_repo = Repo.init(empty_path)
        # git log on an unborn HEAD exits non-zero; get_recent_backup_changes catches GitCommandError.
        with self.assertRaises(GitCommandError):
            list(config_diff._iter_commit_file_changes(empty_repo, 10))  # pylint: disable=protected-access


class ComputeDiffTestCase(TestCase):
    """``compute_diff`` renders side-by-side rows and enforces the O(n^2) size gate."""

    def test_identical_configs_produce_no_changes(self):
        """Equal text yields only unchanged rows and zero counts."""
        rows, additions, deletions, too_large = config_diff.compute_diff("a\nb\n", "a\nb\n")
        self.assertFalse(too_large)
        self.assertEqual((additions, deletions), (0, 0))
        self.assertTrue(all(row["left_class"] == "" and row["right_class"] == "" for row in rows))

    def test_added_and_removed_lines_are_counted_and_classed(self):
        """Changed lines get the add/del classes and are reflected in the counts."""
        rows, additions, deletions, too_large = config_diff.compute_diff("hostname old\n", "hostname new\n")
        self.assertFalse(too_large)
        self.assertEqual((additions, deletions), (1, 1))
        self.assertEqual(rows[0]["left_class"], "gc-del")
        self.assertEqual(rows[0]["right_class"], "gc-add")

    def test_padding_cells_are_marked_empty(self):
        """When one side is shorter, the missing cell is classed ``gc-empty`` with no line number."""
        rows, additions, deletions, _ = config_diff.compute_diff("", "one\ntwo\n")
        self.assertEqual((additions, deletions), (2, 0))
        self.assertTrue(all(row["left_class"] == "gc-empty" and row["left_no"] is None for row in rows))

    def test_oversized_input_is_gated(self):
        """Above MAX_DIFF_LINES the diff is skipped and ``too_large`` is returned."""
        big = "\n".join(str(index) for index in range(config_diff.MAX_DIFF_LINES + 5))
        rows, additions, deletions, too_large = config_diff.compute_diff(big, big + "\nextra")
        self.assertTrue(too_large)
        self.assertEqual((rows, additions, deletions), ([], 0, 0))

    def test_none_inputs_are_treated_as_empty(self):
        """A missing side (unreadable blob) degrades to an empty config rather than raising."""
        rows, additions, deletions, too_large = config_diff.compute_diff(None, "one\n")
        self.assertFalse(too_large)
        self.assertEqual((additions, deletions), (1, 0))
        self.assertEqual(len(rows), 1)


class ShaValidationTestCase(TestCase):
    """``get_config_at_commit`` refuses anything that is not a git object name."""

    def test_malformed_shas_are_rejected_before_touching_git(self):
        """Non-SHA input returns None without resolving a repository."""
        for bad_sha in ["", None, "not-a-sha", "../../etc/passwd", "abc", "g" * 40, "a" * 41]:
            self.assertIsNone(config_diff.get_config_at_commit(None, bad_sha))
