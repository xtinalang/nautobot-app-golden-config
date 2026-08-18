"""Read layer for Backup History Diff -- index-first, with a git-native fallback.

These functions are the single read API the views call. When ``enable_backup_diff_index`` is on and the
index is populated, they serve history / recent-changes / diffs from the database plus the
content-addressed diff cache. When the index is off or empty (ingest never ran), each function
transparently falls back to reading git directly via ``config_diff``, so the feature works with zero extra
infrastructure:

* ``history(device)``           -- per-device version list (index, else git-native).
* ``recent_changes(user, ...)`` -- latest version per device across the fleet (index, else git-native).
* ``backup_diff(device, a, b)`` -- side-by-side diff of two versions: content-addressed cache when both
                                   entries came from the index (hits the diff pre-warmed by ingest),
                                   else a git-native diff keyed by the two commit SHAs.

Models are imported lazily inside the functions (matching ``config_diff`` / ``backup_diff_ingest``) to
avoid an import cycle with the utilities package that ``models`` itself imports.
"""

from django.core.cache import cache
from django.db.models import OuterRef, Subquery
from nautobot.dcim.models import Device

from nautobot_golden_config.utilities.config_diff import (
    MAX_HISTORY_ENTRIES,
    compute_diff,
    get_backup_history,
    get_config_at_commit,
    get_recent_backup_changes,
)
from nautobot_golden_config.utilities.constant import ENABLE_BACKUP_DIFF_INDEX
from nautobot_golden_config.utilities.content_store import get_content_store

# How long a computed diff stays cached. Commits are immutable, so a diff between two content hashes is
# valid indefinitely; the TTL only bounds cache growth.
DIFF_CACHE_TTL = 60 * 60 * 24 * 30

# Row-count ceiling above which a computed diff is NOT cached. A diff may hold up to ``MAX_DIFF_LINES``
# (30K) rows of six keys each, which pickles to several megabytes -- comfortably over memcached's 1 MB
# default item size, where the ``set`` is silently dropped, and a waste of Redis memory besides. Diffs
# this large are rare and slow to transfer either way, so we recompute them instead of caching them.
MAX_CACHED_DIFF_ROWS = 5000


def diff_cache_key(old_blob_sha, new_blob_sha):
    """Return the cache key for a diff between two content hashes.

    Content-addressed and device-independent: because a ``blob_sha`` identifies exact bytes, this key is
    immutable and identical everywhere, which is what lets ingest pre-warm the entry the compare view
    later reads. The ``v1`` namespace lets us invalidate all cached diffs at once if the row shape changes.
    """
    return f"gc:bdiff:v1:{old_blob_sha}:{new_blob_sha}"


def _row_to_entry(row):
    """Shape a ``BackupVersion`` row like the git-native history entry.

    Carries ``blob_sha`` and ``repository`` -- the two things the content-addressed diff path needs -- so
    that path never has to re-resolve the device's ``GoldenConfigSetting`` to recover a repository the
    index row already holds. Callers must ``select_related("repository")``.
    """
    return {
        "sha": row.commit_sha,
        "short_sha": row.commit_sha[:8],
        "blob_sha": row.blob_sha,
        "repository": row.repository,
        "date": row.authored_date,
        "author": row.committer,
        "message": row.message,
    }


def history(device, limit=MAX_HISTORY_ENTRIES):
    """Return a device's version history (newest first): from the index, else git-native.

    Index entries carry a ``blob_sha`` (which enables the content-addressed diff path); the git-native
    fallback entries do not -- that absence is the signal ``backup_diff`` uses to pick its diff strategy.

    Both sources are capped at the same ``MAX_HISTORY_ENTRIES``, so a device with thousands of backup
    commits returns the same bounded list either way (the compare view renders an ``<option>`` per entry
    in each of two dropdowns).

    Args:
        device (Device): The device whose backup history is wanted.
        limit (int): Maximum number of versions to return.

    Returns:
        list[dict]: Version metadata, newest first (may be empty when neither source has history).
    """
    if ENABLE_BACKUP_DIFF_INDEX:
        # Lazy import to avoid an import cycle with the utilities package models imports at load time.
        from nautobot_golden_config.models import BackupVersion  # pylint: disable=import-outside-toplevel

        rows = BackupVersion.objects.for_device(device).select_related("repository")[:limit]
        entries = [_row_to_entry(row) for row in rows]
        if entries:
            return entries
    return get_backup_history(device, max_count=limit)


def recent_changes(user, limit=25):
    """Return the latest version per device across the fleet (newest first), scoped to the user.

    Served from the index when populated; otherwise falls back to walking git via ``config_diff``. Selects
    each device's newest row with a portable correlated subquery -- no Postgres-only ``DISTINCT ON``, so it
    works on both PostgreSQL and MySQL -- and lets the database apply the ``limit`` (rather than pulling one
    row per device into Python to sort).

    Rows are scoped by the *device* the user may view, not by ``restrict()`` on ``BackupVersion`` itself:
    that permission is one no operator would grant on an internal index, so scoping on it would return
    nothing for every non-superuser and silently drop them to the git fallback.

    Args:
        user: The requesting user; devices are scoped with ``restrict(user, "view")``.
        limit (int): Maximum number of rows to return.

    Returns:
        list[dict]: ``{..., "device": Device}`` rows, newest change first (may be empty).
    """
    if ENABLE_BACKUP_DIFF_INDEX:
        # Lazy import to avoid an import cycle with the utilities package models imports at load time.
        from nautobot_golden_config.models import BackupVersion  # pylint: disable=import-outside-toplevel

        viewable = BackupVersion.objects.filter(device__in=Device.objects.restrict(user, "view"))
        # Per device, the pk of its most-recent version -- evaluated in the DB (PostgreSQL + MySQL).
        # Unrestricted on purpose: the outer query already limits us to devices the user may view, and
        # this only has to answer "which row is newest for THIS device".
        latest_pk = (
            BackupVersion.objects.filter(device_id=OuterRef("device_id"))
            .order_by("-authored_date", "-commit_sha")
            .values("pk")[:1]
        )
        # Keep only each device's latest row and let the DB apply the LIMIT.
        rows = list(
            viewable.filter(pk=Subquery(latest_pk))
            .select_related("device", "repository")
            .order_by("-authored_date", "-commit_sha")[:limit]
        )
        if rows:
            return [{**_row_to_entry(row), "device": row.device} for row in rows]
    # All-or-nothing fallback: only when the index is entirely empty. In a mixed state (ingest enabled
    # mid-fleet, or one device's ingest failed) this lists only indexed devices rather than falling back
    # per-device the way history() does -- acceptable under the "index is on or off" assumption.
    return get_recent_backup_changes(user, limit=limit)


def _cache_diff(cache_key, result):
    """Cache a computed diff unless its row list is too big to store safely.

    Above ``MAX_CACHED_DIFF_ROWS`` the pickled payload runs to megabytes, which memcached silently drops
    (1 MB default item size) and Redis stores at real cost. Skipping the write is honest about that
    instead of pretending to cache something the backend discards.
    """
    rows = result[0]
    if len(rows) > MAX_CACHED_DIFF_ROWS:
        return
    cache.set(cache_key, result, timeout=DIFF_CACHE_TTL)


def diff(old_blob_sha, new_blob_sha, repository):
    """Return ``(rows, additions, deletions, too_large)`` for two versions by content hash.

    Cache hit first (pre-warmed by ``backup_diff_ingest.precompute_diff`` for the default pair); on a miss,
    fetch both configs via the content store, compute, and cache. Returns empty when either side's content
    is unavailable.
    """
    cache_key = diff_cache_key(old_blob_sha, new_blob_sha)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    store = get_content_store()
    old_text = store.get(old_blob_sha, repository=repository)
    new_text = store.get(new_blob_sha, repository=repository)
    if old_text is None or new_text is None:
        return [], 0, 0, False

    result = compute_diff(old_text, new_text)
    _cache_diff(cache_key, result)
    return result


def _git_native_diff(device, original, modified):
    """Git-native diff of two history entries, cached by the two commit SHAs.

    Reads each version's config out of git by commit SHA and diffs them. Commits are immutable, so the
    result is cached until the objects age out of the local checkout. Used when the versions lack a
    ``blob_sha`` (the index isn't populated), so the content-addressed path isn't available.
    """
    cache_key = f"gc:backuphistorydiff:v1:{device.pk}:{original['sha']}:{modified['sha']}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    original_text = get_config_at_commit(device, original["sha"])
    modified_text = get_config_at_commit(device, modified["sha"])
    result = compute_diff(original_text, modified_text)
    _cache_diff(cache_key, result)
    return result


def backup_diff(device, original, modified):
    """Return ``(rows, additions, deletions, too_large)`` for two history entries.

    Prefers the content-addressed path when both entries came from the index -- they then carry both a
    ``blob_sha`` and the ``repository`` that holds it, and that path hits the diff cache pre-warmed by
    ingest, so the default comparison is warm before anyone opens the tab. Git-native entries carry
    neither, and fall back to a diff keyed by the two commit SHAs. Returns empty/zero when either side is
    missing.

    Args:
        device (Device): The device whose versions are being compared.
        original (dict | None): The older history entry (rendered on the left).
        modified (dict | None): The newer history entry (rendered on the right).

    Returns:
        tuple[list[dict], int, int, bool]: ``(rows, additions, deletions, too_large)``.
    """
    if original is None or modified is None:
        return [], 0, 0, False
    old_blob = original.get("blob_sha")
    new_blob = modified.get("blob_sha")
    repository = original.get("repository")
    if old_blob and new_blob and repository is not None:
        return diff(old_blob, new_blob, repository)
    return _git_native_diff(device, original, modified)
