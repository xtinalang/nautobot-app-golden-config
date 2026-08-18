"""Git-native config history + side-by-side diff helpers for Golden Config backups.

Golden Config already versions every device's backup config in the backup Git repository (one commit
per change). This module reads that history directly -- no config text is duplicated into the database.
It exposes two things:

* ``get_backup_history`` / ``get_config_at_commit`` -- list a device's backup commits and fetch the
  config text at any one of them, keyed by the git commit SHA (git's own content-addressed "hash").
* ``compute_diff`` -- render a GitHub-style side-by-side diff of two config blobs, server-side.
* ``path_device_map`` -- reverse ``backup_path_template``, mapping a repo-relative path back to its device.
"""

import difflib
import logging
import re
from datetime import datetime

from django.template import engines
from git import GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo
from nautobot.apps.utils import render_jinja2

LOGGER = logging.getLogger(__name__)

# A commit SHA supplied by a user (URL param) must look like a git object name before we hand it to
# git. GitPython invokes git via argv (no shell), so this is defense-in-depth, not the only guard.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

# Line-count ceiling for the inline diff. difflib.SequenceMatcher is worst-case O(n^2) on highly
# repetitive input (real configs are full of "!", " exit", boilerplate), and the compare view computes
# the diff ON the web request -- so a too-large diff can tie up a worker for many seconds. Above this
# ceiling the compare page shows a graceful "too large" notice instead of diffing.
MAX_DIFF_LINES = 30000

# How many versions either read path (git-native or index) returns for one device. Shared so the two
# paths can never disagree: the compare view renders an ``<option>`` per entry in each of TWO dropdowns,
# so an uncapped history on a device with thousands of backup commits would emit thousands of elements
# twice. 100 versions is far more history than the compare UI is useful for.
MAX_HISTORY_ENTRIES = 100

# Record/field separators for the ``git log`` custom format below. ASCII SOH/US are control characters
# that cannot appear in a commit subject or a path, so they are unambiguous delimiters.
_LOG_RECORD_SEP = "\x01"
_LOG_FIELD_SEP = "\x1f"


def get_backup_setting(device):
    """Return the highest-weighted ``GoldenConfigSetting`` for a device that has a backup repository, else ``None``.

    The single place the "resolve the device's setting and confirm it has a backup repository" step lives.
    """
    # Imported lazily: this module is imported by views, and importing models at module load would risk
    # an import cycle with the utilities package that models itself imports.
    from nautobot_golden_config.models import GoldenConfigSetting  # pylint: disable=import-outside-toplevel

    setting = GoldenConfigSetting.objects.get_for_device(device)
    if setting is None or setting.backup_repository is None:
        return None
    return setting


def open_repo(filesystem_path, context=""):
    """Open an existing git checkout, or return ``None`` when it isn't one.

    A ``GitRepository`` record can exist while its clone is absent from *this* node's filesystem (never
    synced here, or wiped). Every backup-diff read path treats that as "no history available" rather than
    an error, so they all share this one guard instead of repeating the try/except.

    Args:
        filesystem_path (str): Path to the expected checkout.
        context (str): What the caller was looking for, for the debug log.

    Returns:
        git.Repo | None: The opened repository, or ``None`` when the path is missing or not a git repo.
    """
    try:
        return Repo(filesystem_path)
    except (InvalidGitRepositoryError, NoSuchPathError):
        LOGGER.debug("Backup repository at %s is not a valid git checkout on this node. %s", filesystem_path, context)
        return None


def commit_subject(message):
    """Return the first line of a commit message, decoding bytes and tolerating an empty message."""
    if isinstance(message, bytes):
        message = message.decode("utf-8", "replace")
    message = message.strip()
    return message.splitlines()[0] if message else ""


def _get_backup_repo_and_path(device):
    """Resolve a device's backup Git repo and repo-relative file path.

    Mirrors ``GoldenConfigSetting.get_jinja_template_path_for_device`` but for the *backup* repository:
    the highest-weighted setting for the device gives the ``backup_repository`` (a cloned git checkout on
    the local filesystem) and the ``backup_path_template`` (Jinja) that renders the device's file path.

    Args:
        device (Device): The device whose backup history is wanted.

    Returns:
        tuple[Repo | None, str | None]: ``(repo, rel_path)`` when everything resolves, else
        ``(None, None)`` -- no setting, no backup repository configured, no path template, or the repo
        is not present/valid on disk yet (e.g. never synced on this node).
    """
    setting = get_backup_setting(device)
    if setting is None or not setting.backup_path_template:
        return None, None

    rel_path = render_jinja2(template_code=setting.backup_path_template, context={"obj": device})
    repo = open_repo(setting.backup_repository.filesystem_path, context=f"Wanted history for {device}.")
    if repo is None:
        return None, None
    return repo, rel_path


def get_backup_history(device, max_count=MAX_HISTORY_ENTRIES):
    """Return commit metadata for a device's backup file, newest first.

    Each entry is a dict with ``sha``, ``short_sha``, ``date`` (authored datetime), ``author``, and the
    first line of the commit ``message``. Returns an empty list when the device has no resolvable backup
    repo/path or the file has no commits yet.

    Args:
        device (Device): The device whose backup history is wanted.
        max_count (int): Cap on how many commits to walk (newest first).

    Returns:
        list[dict]: Commit metadata, newest first (may be empty).
    """
    repo, rel_path = _get_backup_repo_and_path(device)
    if repo is None:
        return []
    history = []
    try:
        for commit in repo.iter_commits(paths=rel_path, max_count=max_count):
            history.append(
                {
                    "sha": commit.hexsha,
                    "short_sha": commit.hexsha[:8],
                    "date": commit.authored_datetime,
                    "author": commit.author.name,
                    "message": commit_subject(commit.message),
                }
            )
    except GitCommandError:
        LOGGER.exception("Failed to read backup history for %s.", device)
        return []
    return history


def get_config_at_commit(device, sha):
    """Return the backup config text for a device at a given commit SHA, or ``None`` if unavailable.

    Reads the file straight out of git (``git show <sha>:<rel_path>``) -- the config is never stored by
    this app. Returns ``None`` for a malformed SHA, an unresolvable repo/path, or a commit at which the
    file did not exist.

    Args:
        device (Device): The device whose backup config is wanted.
        sha (str): The git commit SHA (7-40 hex chars) to read the file at.

    Returns:
        str | None: The config text, or ``None`` when it cannot be read.
    """
    if not sha or not _SHA_RE.match(sha):
        return None
    repo, rel_path = _get_backup_repo_and_path(device)
    if repo is None:
        return None
    try:
        # GitPython runs git via argv (no shell), so <sha>:<rel_path> cannot be shell-injected; the SHA
        # is also format-validated above. This only ever reads THIS device's file at THIS commit.
        return repo.git.show(f"{sha}:{rel_path}")
    except GitCommandError:
        LOGGER.debug("Backup config for %s at %s could not be read.", device, sha)
        return None


def _iter_commit_file_changes(repo, max_count):
    """Yield ``(sha, authored_date, author, subject, changed_paths)`` for a repo's newest commits.

    Exactly ONE ``git log`` subprocess per repository. The obvious implementation -- iterating
    ``repo.iter_commits()`` and reading ``commit.stats.files`` -- makes GitPython shell out to
    ``git diff --numstat`` *once per commit*, so walking a few hundred commits on a web request means a
    few hundred process spawns. ``git log --name-only`` returns the same information in one process.

    ``--first-parent`` keeps traversal on the mainline, matching the "diff against the first parent"
    semantics of ``commit.stats.files``. Merge commits themselves list no files under ``--name-only`` and
    are simply skipped -- the backup commit that introduced the change is on the first-parent line anyway.

    Args:
        repo (git.Repo): The opened backup repository.
        max_count (int): Cap on how many commits to walk (newest first).

    Yields:
        tuple[str, datetime, str, str, set[str]]: Commit metadata plus the paths it changed.
    """
    log_format = _LOG_RECORD_SEP + _LOG_FIELD_SEP.join(["%H", "%aI", "%an", "%s"])
    raw = repo.git.log(
        f"--max-count={max_count}",
        "--first-parent",
        "--name-only",
        "--no-renames",
        f"--format={log_format}",
    )
    for record in raw.split(_LOG_RECORD_SEP):
        if not record.strip():
            continue
        header, _, body = record.partition("\n")
        fields = header.split(_LOG_FIELD_SEP)
        if len(fields) < 4:
            # Malformed record (should not happen with our own format string); skip rather than fail.
            continue
        sha, date_str, author = fields[0], fields[1], fields[2]
        # A subject containing the field separator would over-split; re-join everything after the author.
        subject = _LOG_FIELD_SEP.join(fields[3:])
        try:
            authored_date = datetime.fromisoformat(date_str)
        except ValueError:
            continue
        yield sha, authored_date, author, subject.strip(), {line for line in body.splitlines() if line}


def iter_commit_blob_changes(repo, max_count):
    r"""Yield ``(sha, authored_date, author, subject, [(path, blob_sha), ...])`` for a repo's newest commits.

    Like ``_iter_commit_file_changes`` but also carries each changed file's **resulting blob hash**, which
    is what the index needs and what ``--name-only`` cannot provide. ``--raw`` emits it on the same single
    ``git log`` invocation, so backfilling a whole repository's history still costs one subprocess rather
    than one per commit.

    ``--no-abbrev`` is load-bearing, not cosmetic: raw output abbreviates object names to 7 characters by
    default, and ``--full-index`` does NOT override that (it only applies to patch output). An abbreviated
    hash still resolves through ``git cat-file``, so the git content store would appear to work -- but the
    diff cache and the S3 object store are both keyed on this value, so a 7-character hash from backfill
    would never match the 40-character one the ingest hook writes for the very same content.

    Raw lines look like ``:100644 100644 <src_sha> <dst_sha> <status>\\t<path>``. Deletions have an
    all-zero destination hash and are skipped -- there is no content to index.

    Args:
        repo (git.Repo): The opened backup repository.
        max_count (int): Cap on how many commits to walk (newest first).

    Yields:
        tuple[str, datetime, str, str, list[tuple[str, str]]]: Commit metadata plus (path, blob_sha) pairs.
    """
    log_format = _LOG_RECORD_SEP + _LOG_FIELD_SEP.join(["%H", "%aI", "%an", "%s"])
    raw = repo.git.log(
        f"--max-count={max_count}",
        "--first-parent",
        "--raw",
        "--no-abbrev",
        "--no-renames",
        f"--format={log_format}",
    )
    for record in raw.split(_LOG_RECORD_SEP):
        if not record.strip():
            continue
        header, _, body = record.partition("\n")
        fields = header.split(_LOG_FIELD_SEP)
        if len(fields) < 4:
            continue
        sha, date_str, author = fields[0], fields[1], fields[2]
        subject = _LOG_FIELD_SEP.join(fields[3:])
        try:
            authored_date = datetime.fromisoformat(date_str)
        except ValueError:
            continue
        changes = []
        for line in body.splitlines():
            if not line.startswith(":"):
                continue
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) < 5 or not path:
                continue
            dst_sha = parts[3]
            if set(dst_sha) == {"0"}:
                continue  # deletion -- nothing to index
            changes.append((path, dst_sha))
        if changes:
            yield sha, authored_date, author, subject.strip(), changes


def backup_scope_device_count():
    """Return how many devices are in backup scope across all Golden Config Settings.

    Cheap (one COUNT per setting) and used to decide whether the git-native fleet walk is affordable --
    see ``constant.BACKUP_DIFF_MAX_FALLBACK_FLEET``.
    """
    # Imported lazily to avoid an import cycle with the utilities package models imports at load time.
    from nautobot_golden_config.models import GoldenConfigSetting  # pylint: disable=import-outside-toplevel

    settings = (
        GoldenConfigSetting.objects.select_related("dynamic_group")
        .filter(backup_repository__isnull=False)
        .exclude(backup_path_template="")
    )
    return sum(setting.dynamic_group.members.count() for setting in settings)


def path_device_map(repository=None, user=None):
    """Build ``{repo_filesystem_path: {rendered_backup_path: device}}`` for devices in backup scope.

    This is the reverse of ``backup_path_template``: git tells us which *files* a commit changed, and this
    map turns those paths back into the devices they belong to. Shared by the fleet-wide read path
    (``get_recent_backup_changes``) and by ingest (``backup_diff_ingest.build_commit_events``) so the
    rendering rules -- including "a device whose template won't render is skipped" -- are defined once.

    Args:
        repository (GitRepository | None): Limit to one backup repository; ``None`` means all of them.
        user: When given, devices are scoped with ``restrict(user, "view")``.

    Returns:
        dict[str, dict[str, Device]]: repo filesystem path -> rendered relative path -> device.
    """
    # Imported lazily to avoid an import cycle with the utilities package models imports at load time.
    from nautobot_golden_config.models import GoldenConfigSetting  # pylint: disable=import-outside-toplevel

    settings = GoldenConfigSetting.objects.select_related("backup_repository", "dynamic_group").exclude(
        backup_path_template=""
    )
    if repository is not None:
        settings = settings.filter(backup_repository=repository)
    else:
        settings = settings.filter(backup_repository__isnull=False)

    repo_maps = {}
    for setting in settings:
        # ``backup_path_template`` almost always dereferences a related object -- the documented default is
        # `{{obj.location.name|slugify}}/{{obj.name}}.cfg` -- and DynamicGroup.members returns a bare
        # queryset. Without these joins each device costs an extra query per relation the template touches,
        # which on a 10K fleet measured ~9,000 queries and 7s for this one call.
        devices = setting.dynamic_group.members.select_related(
            "location", "platform", "role", "tenant", "device_type", "device_type__manufacturer"
        )
        if user is not None:
            devices = devices.restrict(user, "view")

        # Compile the path template ONCE per setting rather than once per device. ``render_jinja2`` calls
        # ``engines["jinja"].from_string()`` every time, and that compile dominates at fleet scale -- 50K
        # devices measured 17s, essentially all of it recompiling the same string. Using the same engine
        # keeps the Nautobot Jinja environment (netutils filters and all) identical to ``render_jinja2``.
        try:
            path_template = engines["jinja"].from_string(setting.backup_path_template)
        except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
            LOGGER.warning("Backup path template for %s will not compile; skipping its devices.", setting)
            continue

        path_map = repo_maps.setdefault(setting.backup_repository.filesystem_path, {})
        for device in devices:
            try:
                # `"" +` defuses the implicit mark_safe() that django-jinja2's render() applies, matching
                # what render_jinja2 does.
                rel_path = "" + path_template.render(context={"obj": device})
            except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
                # A device whose path template can't render (missing attribute, etc.) is simply skipped.
                LOGGER.debug("Could not render backup path for %s; skipping.", device)
                continue
            path_map.setdefault(rel_path, device)
    return repo_maps


def get_recent_backup_changes(user, limit=25, max_commits_per_repo=500):
    """Return the most recent per-device backup change across the fleet, newest first.

    For every device the ``user`` may view that has a backup repository configured, walks that repo's git
    history and records the single most recent commit that changed the device's backup file. Git-native:
    reads history straight from the repos and stores nothing. Costs one ``git log`` subprocess per backup
    repository (not per commit) -- see ``_iter_commit_file_changes``.

    Args:
        user: The requesting user; devices are scoped with ``restrict(user, "view")``.
        limit (int): Maximum number of rows to return.
        max_commits_per_repo (int): Safety cap on how deep to walk each repo's history.

    Returns:
        list[dict]: ``{device, date, sha, short_sha, author, message}`` rows, newest change first.
    """
    entries = []
    for repo_path, path_map in path_device_map(user=user).items():
        repo = open_repo(repo_path, context="Wanted fleet-wide recent changes.")
        if repo is None:
            continue
        remaining = dict(path_map)  # devices whose latest change we haven't found yet
        try:
            for sha, authored_date, author, subject, changed in _iter_commit_file_changes(repo, max_commits_per_repo):
                if not remaining:
                    break  # found the latest change for every device in this repo
                # Set intersection rather than rescanning every unmatched path on every commit. The keys
                # view is materialized by ``&`` before the pop below, so mutating during the loop is safe.
                for rel_path in remaining.keys() & changed:
                    device = remaining.pop(rel_path)
                    entries.append(
                        {
                            "device": device,
                            "date": authored_date,
                            "sha": sha,
                            "short_sha": sha[:8],
                            "author": author,
                            "message": subject,
                        }
                    )
        except GitCommandError:
            LOGGER.exception("Failed to walk backup history for repo at %s.", repo_path)
            continue

    entries.sort(key=lambda entry: entry["date"], reverse=True)
    return entries[:limit]


def _build_split_diff_from_lines(original_lines, modified_lines):  # pylint: disable=too-many-locals
    """Build a GitHub-style side-by-side diff over already-split line lists.

    Each row is a dict with left (original) and right (modified) line numbers, text, and a CSS class --
    ``"gc-del"``/``"gc-add"`` for changed lines, ``"gc-empty"`` for a padding cell, and ``""`` for
    unchanged lines. Takes pre-split lines so the size gate in ``compute_diff`` and the diff itself share
    one view of what a "line" is.

    Has NO size gate of its own: ``difflib.SequenceMatcher`` is worst-case O(n^2), so callers must cap the
    line count first. ``compute_diff`` is the only caller and does.
    """
    matcher = difflib.SequenceMatcher(a=original_lines, b=modified_lines, autojunk=False)
    rows = []
    additions = 0
    deletions = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                rows.append(
                    {
                        "left_no": i1 + offset + 1,
                        "left_text": original_lines[i1 + offset],
                        "left_class": "",
                        "right_no": j1 + offset + 1,
                        "right_text": modified_lines[j1 + offset],
                        "right_class": "",
                    }
                )
            continue
        old_chunk = original_lines[i1:i2]
        new_chunk = modified_lines[j1:j2]
        for offset in range(max(len(old_chunk), len(new_chunk))):
            has_left = offset < len(old_chunk)
            has_right = offset < len(new_chunk)
            if has_left:
                deletions += 1
            if has_right:
                additions += 1
            rows.append(
                {
                    "left_no": (i1 + offset + 1) if has_left else None,
                    "left_text": old_chunk[offset] if has_left else None,
                    "left_class": "gc-del" if has_left else "gc-empty",
                    "right_no": (j1 + offset + 1) if has_right else None,
                    "right_text": new_chunk[offset] if has_right else None,
                    "right_class": "gc-add" if has_right else "gc-empty",
                }
            )
    return rows, additions, deletions


def compute_diff(original_text, modified_text):
    """Return ``(rows, additions, deletions, too_large)`` with the O(n^2) size gate applied.

    Splits both blobs into lines once (via ``splitlines()`` so carriage-return-only endings from legacy
    console captures are handled) and skips the diff when either side exceeds ``MAX_DIFF_LINES``.

    Why a server-side row list rather than the vendored ``diff2html`` this app already uses in
    ``generate_intended_config.html``: backup history is *precomputed and cached* -- ingest warms the
    latest-vs-previous diff by content hash before anyone opens the tab (see
    ``backup_diff_ingest.precompute_diff``), and a cached row list is what makes that warm read cheap.
    Backup configs are also the largest text this app renders (full running configs, not one feature's
    stanza), so comparing server-side bounds the work with ``MAX_DIFF_LINES`` instead of handing an
    unbounded job to the browser. diff2html remains the right tool for the intended-config view, where
    the diff is one-off, small, and generated on demand.

    Args:
        original_text (str): The older config.
        modified_text (str): The newer config.

    Returns:
        tuple[list[dict], int, int, bool]: ``(rows, additions, deletions, too_large)``. When
        ``too_large`` is ``True`` the first three are empty/zero.
    """
    original_lines = (original_text or "").splitlines()
    modified_lines = (modified_text or "").splitlines()
    if max(len(original_lines), len(modified_lines)) > MAX_DIFF_LINES:
        return [], 0, 0, True
    rows, additions, deletions = _build_split_diff_from_lines(original_lines, modified_lines)
    return rows, additions, deletions, False
