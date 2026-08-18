"""Unit tests for Backup History Diff index retention and the two-step bulk delete."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils.timezone import now
from nautobot.apps.testing import TestCase
from nautobot.dcim.models import Device
from nautobot.extras.models import DynamicGroup, GitRepository, GraphQLQuery

from nautobot_golden_config import filters, models
from nautobot_golden_config.tests.conftest import create_device, create_helper_repo, create_saved_queries
from nautobot_golden_config.utilities import backup_retention

_VIEWS = "nautobot_golden_config.views"


class BackupRetentionTestCaseMixin:
    """Shared setup: a device, a backup repo, and a GoldenConfigSetting pointed at both."""

    def setUp(self):
        """Create a device and a GoldenConfigSetting whose Dynamic Group contains it."""
        super().setUp()
        # Wiping the git repositories cascades to any GoldenConfigSetting pointing at them, including the
        # one the initial migration ships -- so this builds its own setting rather than mutating
        # GoldenConfigSetting.objects.first(), which is None by this point.
        GitRepository.objects.all().delete()
        self.device = create_device()
        create_helper_repo(name="backup-diff-repo", provides="backupconfigs")
        self.repo = GitRepository.objects.get(name="backup-diff-repo")

        # clean() requires a SoT-Agg query while ENABLE_SOTAGG is on; give it a valid one rather than
        # dropping to a bare save() and skipping validation.
        create_saved_queries()
        self.setting = models.GoldenConfigSetting.objects.create(
            name="backup-retention-setting",
            slug="backup-retention-setting",
            weight=5000,
            backup_repository=self.repo,
            backup_path_template="configs/{{ obj.name }}.cfg",
            # An empty-filter group matches every Device, so the test device is unambiguously in scope.
            dynamic_group=DynamicGroup.objects.create(
                name="backup-retention-all-devices",
                content_type=ContentType.objects.get_for_model(Device),
                filter={},
            ),
            sot_agg_query=GraphQLQuery.objects.get(name="GC-SoTAgg-Query-1"),
        )

    def _version(self, commit_sha, days_ago, blob_sha=None):
        """Create one BackupVersion for the test device, dated ``days_ago`` in the past."""
        return models.BackupVersion.objects.create(
            device=self.device,
            repository=self.repo,
            commit_sha=commit_sha,
            blob_sha=blob_sha or commit_sha,
            path=f"configs/{self.device.name}.cfg",
            authored_date=now() - timedelta(days=days_ago),
            committer="svc-golden-config",
            message="backup",
        )

    def _set_retention(self, days, count=None):
        """Set the retention window (and optional minimum count) on the setting under test."""
        self.setting.backup_retention_days = days
        self.setting.backup_retention_count = count
        self.setting.validated_save()


class CleanUpBackupVersionTableTestCase(BackupRetentionTestCaseMixin, TestCase):
    """``prune_backup_versions`` honours the window, the dry run, and the newest-record guarantee."""

    def test_no_retention_configured_prunes_nothing(self):
        """With backup_retention_days unset, everything is kept regardless of age."""
        self._version("a" * 40, days_ago=5000)
        self._version("b" * 40, days_ago=1)
        results = backup_retention.prune_backup_versions(dry_run=False)
        self.assertEqual(results, [])
        self.assertEqual(models.BackupVersion.objects.count(), 2)

    def test_dry_run_reports_without_deleting(self):
        """A dry run returns what it would prune and leaves the table untouched."""
        self._set_retention(30)
        self._version("a" * 40, days_ago=400)
        self._version("b" * 40, days_ago=1)
        results = backup_retention.prune_backup_versions(dry_run=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].count, 1)
        self.assertEqual(results[0].device, self.device)
        self.assertEqual(results[0].retention_days, 30)
        self.assertEqual(models.BackupVersion.objects.count(), 2, "dry run must not delete")

    def test_apply_prunes_only_records_outside_the_window(self):
        """Records older than the window go; records inside it stay."""
        self._set_retention(30)
        self._version("a" * 40, days_ago=400)
        self._version("b" * 40, days_ago=90)
        self._version("c" * 40, days_ago=1)
        backup_retention.prune_backup_versions(dry_run=False)
        remaining = set(models.BackupVersion.objects.values_list("commit_sha", flat=True))
        self.assertEqual(remaining, {"c" * 40})

    def test_newest_record_is_never_pruned_even_when_stale(self):
        """A device whose only backup predates the window keeps it, so the feature never blanks out."""
        self._set_retention(30)
        self._version("a" * 40, days_ago=5000)
        backup_retention.prune_backup_versions(dry_run=False)
        self.assertEqual(models.BackupVersion.objects.count(), 1, "the latest version must survive")

    def test_nothing_to_prune_returns_no_results(self):
        """All records inside the window means an empty result set, not a zero-count entry."""
        self._set_retention(365)
        self._version("a" * 40, days_ago=2)
        self.assertEqual(backup_retention.prune_backup_versions(dry_run=False), [])

    def test_empty_index_is_a_no_op(self):
        """No index records at all short-circuits before touching settings."""
        self._set_retention(30)
        self.assertEqual(backup_retention.plan_backup_version_pruning(), [])

    # -- the two rules combine in the device's favour -------------------------------------------

    def test_count_keeps_history_the_day_window_would_have_dropped(self):
        """ "30 days and the last 10": a device idle for years still keeps its last 10 versions."""
        for index in range(12):
            self._version(f"{index:040d}", days_ago=400 + index * 30)
        self._set_retention(days=30, count=10)
        backup_retention.prune_backup_versions(dry_run=False)
        # Every version is far outside 30 days, so the count rule alone decides: newest 10 survive.
        self.assertEqual(models.BackupVersion.objects.count(), 10)

    def test_day_window_keeps_more_than_the_count_on_a_busy_device(self):
        """A device changing daily keeps everything inside the window, not just the count."""
        for index in range(25):
            self._version(f"{index:040d}", days_ago=index)  # all within 30 days
        self._set_retention(days=30, count=10)
        backup_retention.prune_backup_versions(dry_run=False)
        self.assertEqual(models.BackupVersion.objects.count(), 25, "the window keeps more than the floor")

    def test_versions_outside_both_rules_are_pruned(self):
        """Only versions failing BOTH the window and the count are removed."""
        for index in range(3):
            self._version(f"in{index:038d}", days_ago=index)  # inside the window
        for index in range(5):
            self._version(f"out{index:037d}", days_ago=500 + index)  # outside both
        self._set_retention(days=30, count=3)
        backup_retention.prune_backup_versions(dry_run=False)
        # 3 inside the window, plus the count floor of 3 which those same 3 already satisfy.
        remaining = models.BackupVersion.objects.count()
        self.assertEqual(remaining, 3)

    def test_count_alone_applies_when_no_window_is_set(self):
        """Setting only the count caps history without any date rule."""
        for index in range(8):
            self._version(f"{index:040d}", days_ago=index * 100)
        self._set_retention(days=None, count=4)
        backup_retention.prune_backup_versions(dry_run=False)
        self.assertEqual(models.BackupVersion.objects.count(), 4)


class BackupVersionFilterSetTestCase(BackupRetentionTestCaseMixin, TestCase):
    """The fleet table's filters resolve by device name and by when the backup was taken."""

    def test_filter_by_device_name_substring(self):
        """``device_name`` matches case-insensitively on a substring, for the fleet search box."""
        self._version("a" * 40, days_ago=1)
        queryset = models.BackupVersion.objects.all()
        matched = filters.BackupVersionFilterSet({"device_name": self.device.name[:4].upper()}, queryset).qs
        self.assertEqual(matched.count(), 1)
        missed = filters.BackupVersionFilterSet({"device_name": "no-such-device"}, queryset).qs
        self.assertEqual(missed.count(), 0)

    def test_filter_by_last_updated_range(self):
        """``authored_date`` gte/lte bound the "last updated" filter."""
        self._version("a" * 40, days_ago=400)
        self._version("b" * 40, days_ago=1)
        queryset = models.BackupVersion.objects.all()
        cutoff = (now() - timedelta(days=30)).isoformat()
        recent = filters.BackupVersionFilterSet({"authored_date__gte": [cutoff]}, queryset).qs
        self.assertEqual([version.commit_sha for version in recent], ["b" * 40])
        old = filters.BackupVersionFilterSet({"authored_date__lte": [cutoff]}, queryset).qs
        self.assertEqual([version.commit_sha for version in old], ["a" * 40])

    def test_latest_per_device_returns_one_row(self):
        """The fleet queryset collapses a device's history to its newest record."""
        self._version("a" * 40, days_ago=10)
        self._version("b" * 40, days_ago=1)
        latest = models.BackupVersion.objects.latest_per_device()
        self.assertEqual([version.commit_sha for version in latest], ["b" * 40])


class BackupVersionBulkDeleteViewTestCase(BackupRetentionTestCaseMixin, TestCase):
    """The two-step bulk delete expands devices to versions and refuses to drop the newest."""

    def setUp(self):
        """Grant the permissions the view requires.

        ``extras.view_gitrepository`` is needed too: every path through this view redirects back to the
        Backup History Diff tool page, which requires it, and the tests follow that redirect.
        """
        super().setUp()
        self.add_permissions(
            "dcim.view_device",
            "extras.view_gitrepository",
            "nautobot_golden_config.delete_backupversion",
        )
        self.url = reverse("plugins:nautobot_golden_config:backupversion_bulk_delete")

    def test_step_one_redirects_to_the_confirmation_page(self):
        """Posting one row per device redirects (POST-redirect-GET) carrying the devices in the query."""
        latest = self._version("b" * 40, days_ago=1)
        self._version("a" * 40, days_ago=10)
        response = self.client.post(self.url, {"pk": [str(latest.pk)]})
        self.assertHttpStatus(response, 302)
        self.assertIn(f"device={self.device.pk}", response.url)

    def test_step_one_expands_to_the_devices_whole_history(self):
        """Following the redirect lists every version those devices have."""
        latest = self._version("b" * 40, days_ago=1)
        self._version("a" * 40, days_ago=10)
        response = self.client.post(self.url, {"pk": [str(latest.pk)]}, follow=True)
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn("aaaaaaaa", content)
        self.assertIn("bbbbbbbb", content)
        self.assertIn("index records only", content)

    def test_confirmation_page_paginates(self):
        """The version list is paginated rather than rendering every record in one response.

        Regression test: this page expands devices into *all* their versions, and retention exists because
        versions accumulate -- so without pagination a handful of long-lived devices is a huge response.
        """
        for index in range(12):
            self._version(f"{index:040d}", days_ago=index + 1)
        response = self.client.get(self.url, {"device": str(self.device.pk), "per_page": 5})
        self.assertHttpStatus(response, 200)
        self.assertEqual(response.context["total_versions"], 12)
        self.assertEqual(len(response.context["versions"]), 5)
        self.assertEqual(response.context["page"].paginator.num_pages, 3)
        # Page 2 is reachable by plain GET, and carries different records.
        page_two = self.client.get(self.url, {"device": str(self.device.pk), "per_page": 5, "page": 2})
        self.assertHttpStatus(page_two, 200)
        self.assertNotEqual(
            [version.pk for version in page_two.context["versions"]],
            [version.pk for version in response.context["versions"]],
        )

    def test_confirmation_page_without_devices_warns(self):
        """A GET with no usable device pk explains itself rather than erroring."""
        response = self.client.get(self.url, follow=True)
        self.assertHttpStatus(response, 200)
        messages = [str(message) for message in response.context["messages"]]
        self.assertTrue(any("Select at least one device" in message for message in messages))

    def test_malformed_pks_do_not_error(self):
        """Non-UUID input is treated as "nothing selected" instead of raising ValidationError (a 500)."""
        version = self._version("a" * 40, days_ago=1)
        for payload in ({"pk": ["not-a-uuid"]}, {"confirm": "true", "version_pk": ["not-a-uuid"]}):
            response = self.client.post(self.url, payload, follow=True)
            self.assertHttpStatus(response, 200)
        response = self.client.get(self.url, {"device": "not-a-uuid"}, follow=True)
        self.assertHttpStatus(response, 200)
        self.assertTrue(models.BackupVersion.objects.filter(pk=version.pk).exists())

    def test_step_two_deletes_selected_versions(self):
        """Confirming with version PKs deletes exactly those records."""
        self._version("b" * 40, days_ago=1)
        stale = self._version("a" * 40, days_ago=10)
        response = self.client.post(self.url, {"confirm": "true", "version_pk": [str(stale.pk)]}, follow=True)
        self.assertHttpStatus(response, 200)
        remaining = set(models.BackupVersion.objects.values_list("commit_sha", flat=True))
        self.assertEqual(remaining, {"b" * 40})

    def test_latest_version_is_refused_server_side(self):
        """A crafted POST naming the newest record must not delete it, template state notwithstanding."""
        latest = self._version("b" * 40, days_ago=1)
        self._version("a" * 40, days_ago=10)
        response = self.client.post(self.url, {"confirm": "true", "version_pk": [str(latest.pk)]}, follow=True)
        self.assertHttpStatus(response, 200)
        self.assertTrue(models.BackupVersion.objects.filter(pk=latest.pk).exists())
        messages = [str(message) for message in response.context["messages"]]
        self.assertTrue(any("cannot be deleted" in message for message in messages))

    def test_no_selection_warns_and_redirects(self):
        """Submitting with nothing selected explains itself instead of deleting anything."""
        self._version("a" * 40, days_ago=1)
        response = self.client.post(self.url, {}, follow=True)
        self.assertHttpStatus(response, 200)
        self.assertEqual(models.BackupVersion.objects.count(), 1)
        messages = [str(message) for message in response.context["messages"]]
        self.assertTrue(any("Select at least one device" in message for message in messages))


class BackupFleetViewTestCase(BackupRetentionTestCaseMixin, TestCase):
    """The landing page shows the filterable table when indexed, and says so when it is not."""

    def setUp(self):
        """Grant the permissions the tool page requires."""
        super().setUp()
        self.add_permissions("dcim.view_device", "extras.view_gitrepository")
        self.url = reverse("plugins:nautobot_golden_config:backuphistorydiff")

    @patch(f"{_VIEWS}.constant.ENABLE_BACKUP_DIFF_INDEX", True)
    def test_index_enabled_renders_the_table(self):
        """With the index on, the fleet table and its filter controls are rendered."""
        self._version("a" * 40, days_ago=1)
        response = self.client.get(self.url)
        self.assertHttpStatus(response, 200)
        self.assertIsNotNone(response.context["table"])
        self.assertFalse(response.context["index_disabled"])
        self.assertIn("Backed-Up Devices", response.content.decode())

    @patch(f"{_VIEWS}.constant.ENABLE_BACKUP_DIFF_INDEX", True)
    def test_fleet_table_renders_selection_checkboxes(self):
        """Regression: ToggleColumn defaults to hidden, so bulk delete had no checkboxes to select.

        Nautobot's generic list views reveal the toggle column themselves; this table is rendered by a
        custom view, so it must ask for ``visible=True`` explicitly.
        """
        self._version("a" * 40, days_ago=1)
        response = self.client.get(self.url)
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn('name="pk"', content, "the fleet table must offer per-row selection checkboxes")
        self.assertIn("Bulk Delete Versions", content)

    @patch(f"{_VIEWS}.constant.ENABLE_BACKUP_DIFF_INDEX", True)
    def test_fleet_page_renders_pagination_controls(self):
        """Regression: ``inc/table.html`` renders the table only -- the pager is a separate include.

        Without it the page silently showed one page of results and no way to reach the rest.
        """
        self._version("a" * 40, days_ago=1)
        response = self.client.get(self.url)
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn('class="paginator', content, "the fleet page must render pagination controls")
        self.assertIsNotNone(response.context["table"].page)

    @patch(f"{_VIEWS}.constant.ENABLE_BACKUP_DIFF_INDEX", False)
    def test_index_disabled_falls_back_with_a_notice(self):
        """With the index off there is no queryset to filter, so the page says so."""
        with patch(f"{_VIEWS}.backup_diff_read.recent_changes", return_value=[]):
            response = self.client.get(self.url)
        self.assertHttpStatus(response, 200)
        self.assertTrue(response.context["index_disabled"])
        self.assertIn("enable_backup_diff_index", response.content.decode())


class BackupFleetBulkDeletePermissionTestCase(BackupRetentionTestCaseMixin, TestCase):
    """The Bulk Delete button is gated on the permission the delete view actually enforces."""

    def setUp(self):
        """Grant only the permissions needed to VIEW the fleet page -- not to delete."""
        super().setUp()
        self.add_permissions("dcim.view_device", "extras.view_gitrepository")
        self.url = reverse("plugins:nautobot_golden_config:backuphistorydiff")

    @patch(f"{_VIEWS}.constant.ENABLE_BACKUP_DIFF_INDEX", True)
    def test_button_is_disabled_without_the_delete_permission(self):
        """Without delete_backupversion the button renders disabled rather than leading to a 403.

        Regression test: it was rendered enabled for everyone, so a user lacking the permission could
        select devices, click through, and get a hard 403 with no explanation.
        """
        self._version("a" * 40, days_ago=1)
        response = self.client.get(self.url)
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn("Bulk Delete Versions", content, "the action stays visible, per the disabled-not-hidden rule")
        self.assertIn("disabled", content)
        self.assertNotIn("backup-history-diff/bulk-delete", content)

    @patch(f"{_VIEWS}.constant.ENABLE_BACKUP_DIFF_INDEX", True)
    def test_button_submits_once_the_permission_is_granted(self):
        """With the permission the button becomes a real submit targeting the delete view."""
        self.add_permissions("nautobot_golden_config.delete_backupversion")
        self._version("a" * 40, days_ago=1)
        response = self.client.get(self.url)
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn(reverse("plugins:nautobot_golden_config:backupversion_bulk_delete"), content)

    def test_delete_view_still_refuses_without_the_permission(self):
        """The template gate is cosmetic; the view is what actually enforces it."""
        version = self._version("a" * 40, days_ago=1)
        response = self.client.post(
            reverse("plugins:nautobot_golden_config:backupversion_bulk_delete"),
            {"pk": [str(version.pk)]},
        )
        self.assertHttpStatus(response, 403)
