"""Celery tasks for nautobot_golden_config.

Auto-discovered by Nautobot's Celery app (``app.autodiscover_tasks()``), so any ``@nautobot_task``
here is registered on the shared worker at startup.

Backup-diff ingest runs as two task hops, so that NOTHING but a broker publish happens inside the
backup job:

1. ``expand_backup_commit`` -- reads the commit's changed files out of git and renders every in-scope
   device's backup path to map those files back to devices, emitting one event per changed device.
2. ``ingest_backup_commit`` -- per device: writes the index row, publishes content, warms the diff.

The backup job enqueues only step 1, passing two strings.
"""

import logging

from nautobot.core.celery import nautobot_task

from nautobot_golden_config.utilities.backup_diff_ingest import (
    BackupCommitEvent,
    run_ingest_routines,
    safe_build_commit_events,
)

LOGGER = logging.getLogger(__name__)


@nautobot_task
def expand_backup_commit(repository_id, commit_sha):
    """Worker task: expand one backup commit into per-device ingest events.

    Enqueued by the backup-time hook in ``jobs.gc_repo_push``. Everything expensive about ingest lives
    here rather than in the backup job: reading the commit's changed files from git, and rendering the
    backup path template once per in-scope device to map those paths back to devices.

    Args:
        repository_id (str): UUID of the backup ``GitRepository`` the commit landed in.
        commit_sha (str): The commit that was just pushed.
    """
    # Lazy import: keeps module load light and avoids touching the ORM at import time.
    from nautobot.extras.models import GitRepository  # pylint: disable=import-outside-toplevel

    repository = GitRepository.objects.filter(pk=repository_id).first()
    if repository is None:
        LOGGER.warning(
            "Backup-diff ingest: repository %s no longer exists; skipping commit %s.", repository_id, commit_sha
        )
        return

    events = safe_build_commit_events(repository, commit_sha)
    # One task per device rather than a loop inside this one, so a commit touching many devices spreads
    # across workers instead of serializing behind a single task.
    for event in events:
        ingest_backup_commit.delay(event.as_dict())
    if events:
        LOGGER.info("Backup-diff ingest: dispatched %d device event(s) for commit %s.", len(events), commit_sha[:8])


@nautobot_task
def ingest_backup_commit(event_dict):
    """Worker task: rebuild a backup-commit event and run the ingest routines.

    Enqueued by ``expand_backup_commit`` via ``ingest_backup_commit.delay(event.as_dict())``. The body is
    intentionally thin and idempotent (index upsert, content publish, diff precompute), so a Celery
    retry or duplicate delivery is safe to run again.

    Args:
        event_dict (dict): The JSON-safe form of a ``BackupCommitEvent`` (from ``as_dict()``).
    """
    run_ingest_routines(BackupCommitEvent.from_dict(event_dict))
