"""Unit tests for the Backup History Diff views (device tab + standalone tool page)."""

from unittest.mock import patch

from django.urls import reverse
from nautobot.apps.testing import TestCase

from nautobot_golden_config.tests.conftest import create_device

_VIEWS = "nautobot_golden_config.views"


class BackupHistoryDiffToolViewTestCase(TestCase):
    """The standalone "Diffs" tool: device picker, IP lookup, and the recent-changes landing."""

    def setUp(self):
        """Create a device and grant the permissions both views require."""
        super().setUp()
        self.device = create_device()
        self.add_permissions("dcim.view_device", "extras.view_gitrepository")
        self.url = reverse("plugins:nautobot_golden_config:backuphistorydiff")

    @patch(f"{_VIEWS}.constant.ENABLE_BACKUP_DIFF_INDEX", False)
    def test_landing_renders_the_device_list(self):
        """With no query params the tool shows the search form and the backed-up device list.

        Pins the flag off rather than relying on the default: a developer who enables
        ``ENABLE_BACKUP_DIFF_INDEX`` in their environment would otherwise flip this test onto the indexed
        branch and fail it for reasons that have nothing to do with the code under test. The index-backed
        path is covered by ``test_backup_retention.BackupFleetViewTestCase``.
        """
        with patch(f"{_VIEWS}.backup_diff_read.recent_changes", return_value=[]) as mock_recent:
            response = self.client.get(self.url)
        self.assertHttpStatus(response, 200)
        mock_recent.assert_called_once()
        content = response.content.decode()
        self.assertIn("Backed-Up Devices", content)
        self.assertIn("enable_backup_diff_index", content)

    def test_responses_vary_on_hx_request(self):
        """Regression: these pages must advertise ``Vary: HX-Request``.

        They extend Nautobot's base template, which emits less markup for an HTMX request than for a full
        navigation. Without the header a cache keyed only on cookies can serve the stored HTMX partial when
        the user presses Back, so the page comes back as a bare fragment. Nautobot core sets this in its own
        views and there is no middleware doing it globally, so each of ours has to.
        """
        response = self.client.get(self.url)
        self.assertHttpStatus(response, 200)
        self.assertIn("HX-Request", response.headers.get("Vary", ""))

    def test_landing_requires_permissions(self):
        """Without dcim.view_device the tool is not reachable."""
        self.user.is_superuser = False
        self.user.save()
        self.user.object_permissions.clear()
        response = self.client.get(self.url)
        self.assertHttpStatus(response, 403)

    def test_selecting_a_device_renders_its_diff(self):
        """``?device=<pk>`` renders that device's diff and skips the recent-changes query."""
        with (
            patch(f"{_VIEWS}.backup_diff_read.history", return_value=[]),
            patch(f"{_VIEWS}.backup_diff_read.backup_diff", return_value=([], 0, 0, False)),
            patch(f"{_VIEWS}.backup_diff_read.recent_changes") as mock_recent,
        ):
            response = self.client.get(self.url, {"device": str(self.device.pk)})
        self.assertHttpStatus(response, 200)
        mock_recent.assert_not_called()
        self.assertIn("No backup history found", response.content.decode())

    def test_invalid_device_id_reports_the_error(self):
        """A malformed ``?device=`` explains itself instead of rendering a bare Back button.

        Regression test: the view previously discarded ``form.errors``, so an invalid submission left the
        user on a page with no message, no errors, and no way to tell what went wrong.
        """
        response = self.client.get(self.url, {"device": "not-a-uuid"})
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn("That lookup could not be processed", content)
        # The search form is re-rendered so the user can correct the input in place.
        self.assertIn("Show Diff", content)

    def test_unresolvable_ip_warns_without_assuming_the_input_was_an_ip(self):
        """A miss names both accepted forms rather than reporting it as an IP failure."""
        response = self.client.get(self.url, {"ip": "no-such-device"}, follow=True)
        self.assertHttpStatus(response, 200)
        messages = [str(message) for message in response.context["messages"]]
        self.assertTrue(any("exact device name or an IP address" in message for message in messages))

    def test_lookup_by_device_name_resolves(self):
        """The ``ip`` field also accepts an exact device name."""
        with (
            patch(f"{_VIEWS}.backup_diff_read.history", return_value=[]),
            patch(f"{_VIEWS}.backup_diff_read.backup_diff", return_value=([], 0, 0, False)),
        ):
            response = self.client.get(self.url, {"ip": self.device.name})
        self.assertHttpStatus(response, 200)
        self.assertEqual(response.context["device"], self.device)


class BackupHistoryDiffDeviceTabTestCase(TestCase):
    """The per-device tab reachable from the device detail page."""

    def setUp(self):
        """Create a device and grant the permissions the view requires."""
        super().setUp()
        self.device = create_device()
        self.add_permissions("dcim.view_device", "extras.view_gitrepository")
        self.url = reverse("plugins:nautobot_golden_config:backuphistorydiff_devicetab", kwargs={"pk": self.device.pk})

    def test_device_tab_varies_on_hx_request(self):
        """The device tab renders the full device-page chrome, so it needs the same Vary header."""
        with patch(f"{_VIEWS}.backup_diff_read.history", return_value=[]):
            response = self.client.get(self.url)
        self.assertHttpStatus(response, 200)
        self.assertIn("HX-Request", response.headers.get("Vary", ""))

    def test_renders_for_a_device_with_no_history(self):
        """A device with no backups renders the empty-state notice, not an error."""
        with patch(f"{_VIEWS}.backup_diff_read.history", return_value=[]):
            response = self.client.get(self.url)
        self.assertHttpStatus(response, 200)
        self.assertIn("No backup history found", response.content.decode())

    def test_renders_a_diff_and_preserves_the_tab_param(self):
        """With history present the selector form posts back to this tab's URL."""
        history = [
            {"sha": "b" * 40, "short_sha": "bbbbbbbb", "date": "2026-07-22", "author": "svc", "message": "newer"},
            {"sha": "a" * 40, "short_sha": "aaaaaaaa", "date": "2026-07-21", "author": "svc", "message": "older"},
        ]
        rows = [
            {
                "left_no": 1,
                "left_text": "hostname old",
                "left_class": "gc-del",
                "right_no": 1,
                "right_text": "hostname new",
                "right_class": "gc-add",
            }
        ]
        with (
            patch(f"{_VIEWS}.backup_diff_read.history", return_value=history),
            patch(f"{_VIEWS}.backup_diff_read.backup_diff", return_value=(rows, 1, 1, False)),
        ):
            response = self.client.get(self.url)
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn("hostname new", content)
        self.assertIn(self.url, content, "the selector form must post back to this tab")

    def test_unknown_device_is_a_404(self):
        """A pk the user cannot view (or that does not exist) 404s rather than leaking."""
        response = self.client.get(
            reverse(
                "plugins:nautobot_golden_config:backuphistorydiff_devicetab",
                kwargs={"pk": "00000000-0000-0000-0000-000000000000"},
            )
        )
        self.assertHttpStatus(response, 404)
