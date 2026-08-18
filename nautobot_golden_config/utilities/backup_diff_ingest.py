"""Backup-diff ingestion -- the backup-time hook and the per-device work it triggers.

When a Golden Config backup commit lands, this module expands that single commit into **one event per
changed device**, then for each event writes the metadata index row, publishes the config content, and
pre-warms the latest-vs-previous diff. It is deliberately fault-isolated: any failure here logs and
returns -- it must NEVER break or slow a backup job.

Everything in this module runs on a Celery worker, never in the backup job itself. The backup job's only
involvement is enqueuing ``tasks.expand_backup_commit(repo_id, commit_sha)`` -- two strings onto the
broker -- because expansion is not cheap: it reads the commit's changed files out of git and renders the
backup path template for every in-scope device.

The public entry points:
    * ``build_commit_events(repository_record, commit_sha)`` -- pure, side-effect-free; returns the list
      of per-device ``BackupCommitEvent`` objects. Unit-testable without a live backup job.
    * ``safe_build_commit_events(repository_record, commit_sha)`` -- the same, wrapped in the backstop
      ``except`` so no ingest failure can ever escape. Returns an empty list on failure.
    * ``run_ingest_routines(event)`` -- the per-device body the worker task executes.

Nothing here imports ``tasks``: Celery wiring lives entirely in ``tasks.py``, which imports this module
one way only.
"""

import logging
from dataclasses import asdict, dataclass
from datetime import datetime

from django.db.models import Q
from git import GitCommandError

from nautobot_golden_config.utilities.config_diff import commit_subject, open_repo, path_device_map

LOGGER = logging.getLogger(__name__)


# A plain data carrier: the eight fields ARE the contract between the hook and the ingest steps, so the
# instance-attribute ceiling does not apply the way it would to a behavioral class.
@dataclass(frozen=True)
class BackupCommitEvent:  # pylint: disable=too-many-instance-attributes
    """Immutable per-device record of one backup commit -- the contract the ingest steps consume.

    Every field is a JSON-serializable primitive so an event can be enqueued onto a Celery task
    (``as_dict``) and rebuilt on the worker (``from_dict``) without custom serialization.

    Fields:
        device_id: Device UUID as a string.
        repo_id: Backup GitRepository UUID as a string.
        commit_sha: Commit that introduced this version; the per-event idempotency key.
        blob_sha: Content hash of this device's file at this commit; the content-store key.
        path: Repo-relative file path of the device's backup.
        authored_date: Commit timestamp, ISO-8601 string; also the history ordering key.
        committer: Git author name (a service account in practice).
        message: First line of the commit message.
    """

    device_id: str
    repo_id: str
    commit_sha: str
    blob_sha: str
    path: str
    authored_date: str
    committer: str
    message: str

    def as_dict(self):
        """Return a JSON-serializable dict, for enqueuing this event onto a Celery task."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        """Rebuild an event from its ``as_dict`` form (on the worker side)."""
        return cls(**data)


def build_commit_events(repository_record, commit_sha):
    """Expand a backup commit into one ``BackupCommitEvent`` per changed device (pure, side-effect-free).

    Args:
        repository_record (GitRepository): The backup repository the commit belongs to.
        commit_sha (str): The commit that was just pushed.

    Returns:
        list[BackupCommitEvent]: One event per changed file that maps to a device. Empty on any failure
        or when no changed file corresponds to a tracked device.
    """
    repo_path = repository_record.filesystem_path
    repo = open_repo(repo_path, context=f"Wanted to ingest commit {commit_sha[:8]}.")
    if repo is None:
        LOGGER.warning("Backup-diff ingest: backup repo not available at %s; skipping.", repo_path)
        return []

    try:
        commit = repo.commit(commit_sha)
        # ``stats.files`` diffs the commit against its first parent (and against the empty tree for the
        # root commit, so a device's very first backup is captured too).
        changed_paths = set(commit.stats.files)
    except (GitCommandError, ValueError):
        LOGGER.exception("Backup-diff ingest: could not read changed files for commit %s.", commit_sha)
        return []
    if not changed_paths:
        return []

    # O(devices in scope) per commit: every in-scope device's backup path is rendered to reverse-map the
    # commit's changed files. Cheap enough on a worker; at very large fleets, storing each device's
    # rendered path at backup time would replace this with a direct lookup.
    path_map = path_device_map(repository=repository_record).get(repo_path, {})
    message = commit_subject(commit.message)

    events = []
    for path in changed_paths:
        device = path_map.get(path)
        if device is None:
            # A changed file that isn't a tracked device backup (README, .gitignore, etc.).
            continue
        try:
            blob_sha = (commit.tree / path).hexsha
        except KeyError:
            # File was deleted in this commit -- no content to index.
            continue
        events.append(
            BackupCommitEvent(
                device_id=str(device.id),
                repo_id=str(repository_record.id),
                commit_sha=commit.hexsha,
                blob_sha=blob_sha,
                path=path,
                authored_date=commit.authored_datetime.isoformat(),
                committer=commit.author.name,
                message=message,
            )
        )
    return events


def safe_build_commit_events(repository_record, commit_sha):
    """Return per-device events for a commit, swallowing any failure. Fault-isolated.

    The ``try`` that ``build_commit_events`` deliberately does not have: an absolute backstop so no git or
    template error can escape ingest. Enqueuing the resulting events is ``tasks.expand_backup_commit``'s
    job, which keeps every Celery reference in ``tasks`` and this module free of an import cycle.

    Args:
        repository_record (GitRepository): The backup repository the commit belongs to.
        commit_sha (str): The commit that was just pushed.

    Returns:
        list[BackupCommitEvent]: One event per changed device; empty on any failure.
    """
    try:
        return build_commit_events(repository_record, commit_sha)
    except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
        LOGGER.exception("Backup-diff ingest: failed to build events for commit %s.", commit_sha)
        return []


def backfill_index(max_commits_per_repo=1000, batch_size=5000, dry_run=True):
    """Populate the index from backup history that already exists in git.

    The ingest hook only records commits made *after* it is enabled, so on an existing fleet the index
    starts empty and the fleet views have nothing to show. This walks each backup repository's history once
    and writes a row per (device, commit), which is what makes the indexed path usable on day one rather
    than only after every device happens to back up again.

    Cost is O(commits), not O(devices x commits): one ``git log --raw`` subprocess per repository provides
    every changed path *and* its blob hash, and rows are written with ``bulk_create``. Existing rows are
    left alone -- ``ignore_conflicts`` leans on the ``(device, commit_sha)`` uniqueness -- so this is safe
    to re-run and safe to run alongside live ingest.

    Two ways this can quietly recover less than the caller expects, both reported rather than swallowed:

    * ``max_commits_per_repo`` bounds the walk. Golden Config commits once per repository per backup job
      run, so the cap counts backup *runs*, not devices -- but history older than it is simply not seen.
      ``capped_repositories`` names the repositories where the walk stopped on the cap rather than on the
      end of history, which is the signal to re-run with a larger value.
    * Paths are mapped back to devices through the CURRENT ``backup_path_template`` and the CURRENT
      Dynamic Group membership. A device that was renamed, whose template changed, or that has left backup
      scope no longer matches its own historical path, so those commits are skipped. ``unmapped_paths``
      counts the distinct paths that matched no in-scope device, which is what distinguishes "that repo
      also holds a README" from "half my fleet was renamed and its history is not coming back".

    Args:
        max_commits_per_repo (int): How deep to walk each repository's history.
        batch_size (int): Rows per ``bulk_create``.
        dry_run (bool): When ``True``, count what would be written without writing.

    Returns:
        dict: ``{"repositories", "commits", "rows", "written", "capped_repositories", "unmapped_paths",
        "unmapped_sample"}``.
    """
    # Lazy imports to avoid an import cycle with the utilities package models imports at load time.
    from nautobot.extras.models import GitRepository  # pylint: disable=import-outside-toplevel

    from nautobot_golden_config.models import BackupVersion  # pylint: disable=import-outside-toplevel
    from nautobot_golden_config.utilities.config_diff import (  # pylint: disable=import-outside-toplevel
        iter_commit_blob_changes,
    )

    repos_by_path = {repo.filesystem_path: repo for repo in GitRepository.objects.all()}
    stats = {
        "repositories": 0,
        "commits": 0,
        "rows": 0,
        "written": 0,
        "capped_repositories": [],
        "unmapped_paths": 0,
        "unmapped_sample": [],
    }
    unmapped = set()
    pending = []
    # Measure inserts by counting the table, NOT by len(bulk_create(...)): with ignore_conflicts=True
    # Django returns every object it was handed, including the ones the database skipped, so trusting it
    # would report a full re-index on every re-run when nothing was actually written.
    count_before = 0 if dry_run else BackupVersion.objects.count()

    def flush():
        """Write the pending batch, leaving rows that already exist untouched."""
        if not pending or dry_run:
            pending.clear()
            return
        BackupVersion.objects.bulk_create(pending, batch_size=batch_size, ignore_conflicts=True)
        pending.clear()

    for repo_path, path_map in path_device_map().items():
        repository = repos_by_path.get(repo_path)
        repo = open_repo(repo_path, context="Backfilling the backup-diff index.")
        if repo is None or repository is None:
            LOGGER.warning("Backup-diff backfill: no usable repository at %s; skipping.", repo_path)
            continue
        stats["repositories"] += 1
        commits_here = 0
        try:
            for sha, authored_date, author, subject, changes in iter_commit_blob_changes(repo, max_commits_per_repo):
                stats["commits"] += 1
                commits_here += 1
                for path, blob_sha in changes:
                    device = path_map.get(path)
                    if device is None:
                        # Either a file that is not a device backup at all (README, .gitignore), or a
                        # device whose current rendered path no longer matches this historical one.
                        unmapped.add(path)
                        continue
                    stats["rows"] += 1
                    pending.append(
                        BackupVersion(
                            device=device,
                            repository=repository,
                            commit_sha=sha,
                            blob_sha=blob_sha,
                            path=path,
                            authored_date=authored_date,
                            committer=author,
                            message=subject,
                        )
                    )
                    if len(pending) >= batch_size:
                        flush()
        except GitCommandError:
            LOGGER.exception("Backup-diff backfill: failed walking history at %s.", repo_path)
            continue
        # Stopping exactly ON the cap means git had more to give. One commit short of it means we reached
        # the end of history, so only the equal case is worth warning about.
        if commits_here >= max_commits_per_repo:
            stats["capped_repositories"].append(repository.name)
    flush()
    stats["unmapped_paths"] = len(unmapped)
    stats["unmapped_sample"] = sorted(unmapped)[:5]
    if not dry_run:
        stats["written"] = BackupVersion.objects.count() - count_before
    return stats


def write_backup_version(event):
    """Upsert one ``BackupVersion`` index row from an event.

    Idempotent on ``(device, commit_sha)`` -- running it repeatedly for the same commit updates the one
    row instead of creating duplicates, so retries/replays are safe. Stores metadata only; no config text.

    Args:
        event (BackupCommitEvent): The per-device event to record.
    """
    # Lazy import to avoid an import cycle with the utilities package models imports at load time.
    from nautobot_golden_config.models import BackupVersion  # pylint: disable=import-outside-toplevel

    BackupVersion.objects.update_or_create(
        device_id=event.device_id,
        commit_sha=event.commit_sha,
        defaults={
            "repository_id": event.repo_id,
            "blob_sha": event.blob_sha,
            "path": event.path,
            "authored_date": datetime.fromisoformat(event.authored_date),
            "committer": event.committer,
            "message": event.message,
        },
    )


def publish_content(event):
    """Make the config text fetchable by ``blob_sha`` via the content store.

    With the default git backend this is a no-op (git already holds the blob); with an object-store
    backend it uploads the blob keyed by its content hash. Delegated to the configured ``ContentStore``.

    Args:
        event (BackupCommitEvent): The per-device event whose content to publish.
    """
    # Lazy import so the content-store module (and any S3 client it may load) is only touched when used.
    from nautobot_golden_config.utilities.content_store import (  # pylint: disable=import-outside-toplevel
        get_content_store,
    )

    get_content_store().publish(event)


def precompute_diff(event):
    """Compute this device's latest-vs-previous diff and cache it.

    Finds the version immediately older than the event's, fetches both configs via the content store,
    computes the side-by-side diff, and caches it keyed by the ``(old_blob, new_blob)`` pair. Because
    commits are immutable, a cached diff is valid indefinitely -- so the default diff is warm *before*
    anyone opens the tab (the "at your fingertips" part).

    Args:
        event (BackupCommitEvent): The per-device event for the newly-committed version.
    """
    # Lazy imports: keep module load light and avoid import cycles.
    from nautobot.extras.models import GitRepository  # pylint: disable=import-outside-toplevel

    from nautobot_golden_config.models import BackupVersion  # pylint: disable=import-outside-toplevel
    from nautobot_golden_config.utilities.backup_diff_read import (  # pylint: disable=import-outside-toplevel
        diff as read_diff,
    )

    event_date = datetime.fromisoformat(event.authored_date)
    # "The version immediately older than this one", under the same (-authored_date, -commit_sha) ordering
    # the history views use. Strictly-older by date, OR same date with a lower commit_sha -- an explicit
    # tie-break, because backup commits can share a timestamp (one job pushing several devices in the same
    # second) and ``authored_date__lte`` + ``exclude(commit_sha=...)`` would pick an arbitrary one of them.
    previous = (
        BackupVersion.objects.filter(device_id=event.device_id)
        .filter(Q(authored_date__lt=event_date) | Q(authored_date=event_date, commit_sha__lt=event.commit_sha))
        .order_by("-authored_date", "-commit_sha")
        .first()
    )
    if previous is None:
        return  # first version for this device -- nothing to diff against yet

    repository = GitRepository.objects.filter(pk=event.repo_id).first()
    # Delegate to the read layer's content-addressed diff: it checks the cache, fetches both configs via
    # the content store, computes, and caches under the same (blob, blob) key the compare view reads --
    # one source of truth for the diff/cache policy and its TTL. Pre-warms the exact entry; return ignored.
    read_diff(previous.blob_sha, event.blob_sha, repository)


def run_ingest_routines(event):
    """Run every ingest step for one event -- the body executed by the Celery worker.

    Idempotent end to end (index upsert keyed on device+commit, content keyed by hash, diff cached by
    blob pair), so a retried or duplicated task is safe to run again.

    Args:
        event (BackupCommitEvent): The per-device event to process.
    """
    write_backup_version(event)
    publish_content(event)  # no-op for the default git content store
    precompute_diff(event)
    LOGGER.debug("Backup-diff ingest: processed %s @ %s", event.path, event.commit_sha[:8])
