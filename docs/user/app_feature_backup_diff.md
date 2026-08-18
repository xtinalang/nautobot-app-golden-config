# Navigate Backup History Diff

Golden Config already commits a device's backup configuration to the backup Git repository every time it
changes, one commit per change. Backup History Diff reads that history and shows a side-by-side comparison
of any two versions, so you can answer "what changed on this device, and when?" without leaving Nautobot or
cloning the repository yourself.

Because the comparison is a plain line-based text diff, it works for any vendor and any format the backup
job produces: Cisco IOS, Arista EOS, Juniper, FortiOS, YAML, JSON, and anything else.

## Requirements

Backup History Diff appears only when `enable_backup` is `True`, and reads the backup repository and path
template from the Golden Config Setting that applies to the device. A device shows history once its backup
job has committed at least one configuration.

Viewing a diff requires the `dcim.view_device` and `extras.view_gitrepository` permissions.

!!! warning
    A backup configuration is the device's running configuration, which typically contains password
    hashes, SNMP community strings, and shared keys. Anyone who can view a device and a Git repository
    can read those values here. Scope these permissions accordingly.

## Getting There

There are two ways in.

**From a device.** Open any device's detail page and click the **Backup History Diff** button. This shows
that device's history directly.

**From the navigation menu.** Go to **Golden Config > Diffs > Backup History Diff**. This opens the
standalone tool, which lists your backed-up devices with a **View Diff** link on each row. You can also
look a device up by name using the dropdown, or paste an IP address, which matches against a device's
primary IP or any of its interface IP addresses.

The list is scoped to devices you have permission to view.

## The Device List

The landing page lists one row per device, showing that device's most recent backup, so it reads as a
backup inventory rather than an activity feed. Sort by clicking any column header. Columns can be shown and
hidden with the table's configuration button, and the list can be exported.

The list also honours filters supplied in the URL, applied in the database, so a filtered view can be
bookmarked or shared:

- `?device_name=<text>` — case-insensitive substring match on the device name.
- `?authored_date__gte=<timestamp>` / `?authored_date__lte=<timestamp>` — bound the window to find devices
  that changed recently, or devices that have not been backed up in a long time.
- `?q=<text>` — searches device name, commit message, and commit hash.

!!! note
    The device list needs the `enable_backup_diff_index` setting enabled, because sorting and filtering an
    entire fleet is a database operation. With the index disabled the page still works, but shows a simpler
    Git-derived list of recent changes without filter controls. See
    [Install and Configure](../admin/install.md#backup-history-diff).

## Retention

Backup version *records* accumulate as backups run. To bound that, set **Backup Version Retention (days)**
and optionally **Backup Version Retention (minimum count per device)** on a Golden Config Setting. Records
are pruned by the **Clean Up Backup Version Table** job, and because settings are scoped by Dynamic Group and
resolved by weight, different parts of the fleet can keep different amounts of history.

The two rules combine in the device's favour: a version is kept if it is **within the day window OR among
the most recent N**. Setting 30 days and 10 therefore means "a month of history, and never fewer than the
last 10" — a device that has not changed in two years still keeps ten versions to compare, while a device
that changes daily keeps everything from the last month rather than being truncated to ten.

Set only the day window to apply just the time rule; set only the count to keep a fixed number of versions
regardless of age.

The job takes a **Dry Run** option, enabled by default, which reports what it would prune without deleting
anything. Schedule it like any other Nautobot job once you are satisfied with the preview.

You can also prune by hand: select devices on the device list and choose **Bulk Delete Versions**. That
shows every version of the selected devices so you can pick exactly which records to remove.

!!! important
    Retention and bulk delete remove **index records only**. The configurations themselves stay in the
    backup Git repository and are never modified, so anything pruned here remains retrievable from Git by
    commit hash. A device's most recent version is never pruned or deletable, since the diff and history
    views default to it.

### Rebuilding pruned records

The **Sync Backup Version Table** job re-reads each backup repository and recreates index records from
the commits it finds, so a prune or a bulk delete is usually recoverable. Two limits decide how much comes
back, and the job reports both rather than leaving you to guess:

- **How far back it reads.** The job's *Max Commits Per Repository* bounds the walk. Golden Config makes one
  commit per repository per backup run, so this counts backup runs rather than devices, but history older
  than the limit is not indexed. If the walk stops on the limit the job says which repositories were
  affected; re-run with a larger value to go deeper.
- **Whether a device still matches its own history.** Paths are mapped back to devices using the backup path
  template and Dynamic Group membership *as they are now*. A device that has been renamed, whose template
  changed, or that has left backup scope will not match the paths its older commits were written under, so
  that history cannot be re-indexed as-is. The job reports how many paths matched no in-scope device; a
  handful is normal (`README`, `.gitignore`), a large number is the signal that something moved.

Because of those two limits, treat pruning and bulk delete as a one-way trim of *recent* history rather than
a reversible operation.

**If the repository itself is missing on the node**, sync it first. Golden Config does not clone backup
repositories itself; it expects the checkout to already be on disk. Go to **Extensibility > Git
Repositories**, open the backup repository, and click **Sync** (Nautobot's own job, which clones or pulls
from the remote), then run **Sync Backup Version Table**. In a multi-worker deployment the repository has to
be present on the worker that runs the sync job, because it reads the local checkout.

!!! note
    Running a **Backup** job does not rebuild history. Index records are written only when a backup actually
    produces a new commit, so devices whose configuration has not changed contribute nothing. Backup records
    history going forward; **Sync Backup Version Table** is the only way to recover history from the past.

## Reading a Diff

Two dropdowns select which versions to compare. **Original (older)** is rendered on the left and
**Modified (newer)** on the right; the view always orders them chronologically, so the left side is never
the newer configuration regardless of which order you pick them in. By default it compares the two most
recent backups.

Changing either dropdown reloads the comparison. The selection is carried in the URL as `?a=<older-sha>`
and `?b=<newer-sha>`, so a specific comparison can be bookmarked or shared with a colleague.

Above the diff, two cards summarize each version: its commit timestamp, the author of the commit (in
practice the service account the backup job runs as), the short commit hash, and the commit message. The
diff header shows the number of added and removed lines.

In the diff itself:

- Green rows are lines present only in the newer configuration.
- Red rows are lines present only in the older configuration.
- Grey cells are padding where one side has no corresponding line.
- Unstyled rows are unchanged and are shown for context.

Two messages are worth calling out:

- *"No differences"* means the two commits you selected are byte-identical. This is normal when a backup
  job commits for a reason other than a configuration change.
- *"These configs are too large to diff inline"* means at least one side exceeds the line ceiling the
  comparison enforces to protect the web worker. Open the commits in the backup repository to view them in
  full.

## Where the Data Comes From

Nothing here duplicates your configurations into the database. The diff reads the backup file out of the
backup Git repository by commit hash, and Git remains the source of truth.

Two optional settings change *how* that data is reached, without changing what you see:

- `enable_backup_diff_index` records each backup commit's metadata (device, commit, timestamp, author) in
  the database as it is pushed, and pre-computes each device's newest diff. History and recent-changes
  lists are then answered by a database query rather than by walking Git, which matters on large fleets or
  deep histories. No configuration text is stored, only facts about each version.
- `backup_diff_content_store` set to `"s3"` also stores each version's text in an object store keyed by its
  content hash, for deployments where the node serving the page has no clone of the backup repository.

Both are off by default, and both fall back to reading Git if they are unavailable, so the feature works
with no extra infrastructure. See [Install and Configure](../admin/install.md#backup-history-diff) for
details.
