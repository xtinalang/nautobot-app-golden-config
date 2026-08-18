"""Retention for the Backup History Diff index.

Each ``GoldenConfigSetting`` may declare ``backup_retention_days``; records older than that window are
pruned from the ``BackupVersion`` index for devices in that setting's scope.

Two guarantees the callers depend on:

* **Index only.** Nothing here touches the backup Git repository. Pruning drops GC's *record* of a
  version; the configuration itself remains retrievable by commit SHA, so a prune is undone by
  re-indexing rather than being data loss.
* **The newest record per device is never pruned**, even when it is older than the retention window. The
  history and diff views default to it, so removing it would blank the feature for that device.

Kept out of ``jobs.py`` so the logic is unit-testable without running a Job -- the same split
``backup_diff_ingest`` uses.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from itertools import groupby
from operator import itemgetter

from django.utils.timezone import now

LOGGER = logging.getLogger(__name__)

# Records deleted per statement. Retention on a fleet that has been backing up for months can select far
# more than a single `pk__in` should carry.
DELETE_BATCH_SIZE = 5000


@dataclass(frozen=True)
class PruneResult:
    """What retention did (or would do) for one device."""

    device: object  # a dcim.Device; not annotated as such to keep models out of this module's imports
    retention_days: int | None
    count: int
    retention_count: int | None = None


def _stale_pks_for_device(ordered, retention_days, retention_count, current_time):
    """Return the pks to prune for one device, given its versions newest-first.

    The rules are OR'd on the *keep* side, not the prune side: a version survives if it is inside the day
    window OR among the newest N. So "30 days and the last 10" means what it sounds like -- a month of
    history, and never fewer than the last 10 even on a device that has not changed in years. Setting only
    one of the two applies just that rule.

    Args:
        ordered (list[tuple]): ``(pk, authored_date)`` newest first.
        retention_days (int | None): Day window, or ``None`` to skip the time rule.
        retention_count (int | None): Minimum versions to keep, or ``None`` to skip the count rule.
        current_time (datetime): "Now", passed in so one plan uses a single consistent clock.

    Returns:
        list: The pks outside every keep rule. The newest is never included.
    """
    cutoff = current_time - timedelta(days=retention_days) if retention_days else None
    stale = []
    for position, (pk, authored_date) in enumerate(ordered):
        if position == 0:
            continue  # the newest record is never pruned -- the views default to it
        within_window = cutoff is not None and authored_date >= cutoff
        within_count = retention_count is not None and position < retention_count
        if not (within_window or within_count):
            stale.append(pk)
    return stale


def plan_backup_version_pruning():
    """Return the per-device prune plan, newest-record-protected, without deleting anything.

    Resolves each device's setting through ``get_device_to_settings_map`` so weighting is respected: a
    device in more than one setting's Dynamic Group must be governed by its highest-weighted setting, not
    an arbitrary one.

    Reads every version in ONE query and groups in Python rather than querying per device. The per-device
    form was O(devices) round trips, which on a large fleet is thousands of queries for what is a single
    ordered scan -- the same trap ``config_diff.path_device_map`` documents.

    Returns:
        list[tuple[PruneResult, list]]: ``(result, stale_pks)`` per device with something to prune.
    """
    # Lazy imports: this module is imported by jobs.py, and importing models/helper at load time would
    # risk an import cycle with the utilities package that models itself imports.
    from nautobot.dcim.models import Device  # pylint: disable=import-outside-toplevel

    from nautobot_golden_config.models import BackupVersion  # pylint: disable=import-outside-toplevel
    from nautobot_golden_config.utilities.helper import (  # pylint: disable=import-outside-toplevel
        get_device_to_settings_map,
    )

    # Single ordered scan of the whole index; grouped below. device_id leads the ordering so groupby's
    # "runs of equal keys" requirement holds.
    rows = BackupVersion.objects.order_by("device_id", "-authored_date", "-commit_sha").values_list(
        "device_id", "pk", "authored_date"
    )
    by_device = {
        device_id: [(pk, authored_date) for _, pk, authored_date in group]
        for device_id, group in groupby(rows.iterator(), key=itemgetter(0))
    }
    if not by_device:
        return []

    devices = Device.objects.filter(pk__in=list(by_device))
    settings_map = get_device_to_settings_map(queryset=devices)
    device_by_pk = {device.pk: device for device in devices}
    current_time = now()

    plan = []
    for device_id, setting in settings_map.items():
        retention_days = getattr(setting, "backup_retention_days", None)
        retention_count = getattr(setting, "backup_retention_count", None)
        if not retention_days and not retention_count:
            continue  # retention not configured for this scope -- keep everything
        stale_pks = _stale_pks_for_device(by_device.get(device_id, []), retention_days, retention_count, current_time)
        if stale_pks:
            plan.append(
                (
                    PruneResult(device_by_pk[device_id], retention_days, len(stale_pks), retention_count),
                    stale_pks,
                )
            )
    return plan


def prune_backup_versions(dry_run=True, batch_size=DELETE_BATCH_SIZE):
    """Apply (or preview) index retention and return the per-device results.

    Args:
        dry_run (bool): When ``True``, compute the plan but delete nothing.
        batch_size (int): How many records to delete per statement.

    Returns:
        list[PruneResult]: One entry per device that had records outside its retention window.
    """
    # Lazy import to avoid an import cycle with the utilities package models imports at load time.
    from nautobot_golden_config.models import BackupVersion  # pylint: disable=import-outside-toplevel

    plan = plan_backup_version_pruning()
    if not dry_run:
        stale_pks = [pk for _, pks in plan for pk in pks]
        # Batched rather than one `pk__in` over the whole plan: a first prune on a fleet that has been
        # backing up for a while can be six figures of records, which is an unreasonable single statement
        # (and an unreasonable single lock) to hand the database.
        for start in range(0, len(stale_pks), batch_size):
            BackupVersion.objects.filter(pk__in=stale_pks[start : start + batch_size]).delete()
        if stale_pks:
            LOGGER.info("Backup retention: pruned %d index record(s).", len(stale_pks))
    return [result for result, _ in plan]
