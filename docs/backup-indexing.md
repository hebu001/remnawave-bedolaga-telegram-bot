# Backup listing and retention

Archive listing, directory scans, legacy JSON parsing, index writes and retention
run in the dedicated `backup-io` executor: at most two active and two queued jobs
per process. Concurrent callers share one list scan per `BackupService` instance.
Cancellation of an HTTP caller does not cancel that scan or free a worker slot
before its thread actually finishes. A saturated pool reuses the last completed
list when available. A cold list with no cached result returns HTTP 503 with
`Retry-After: 5`; the Telegram interface asks the administrator to retry.
Delete and restore still check the file independently of the displayed list.

Each completed archive has an optional hidden companion
`.backup_<timestamp>.tar.gz.metadata.json` (or the corresponding `.tar` / legacy
`.json` / `.json.gz` name). The versioned JSON index stores only small scalar
listing fields; it does not duplicate SQL, user records, database credentials,
settings or snapshots. Archive size, modification/change timestamps with
nanosecond precision, device and inode identify the indexed file. A mismatch or
invalid index triggers another read. Index writes use a temporary file and atomic
replacement. Read-only/full disks do not hide valid backups: an in-memory index
(up to 256 entries) remains usable for the current process. Repeated reads also
cache malformed metadata until the archive identity changes.

New archives write `metadata.json` first. Listing streams only as far as that
member instead of `tar.getmember()`, which would scan the entire gzip archive.
Metadata members must be ordinary files, at most 1 MiB; no archive member is
extracted to the filesystem during listing. Existing archives with metadata
near the end, and legacy JSON backups, may still require one expensive initial
scan before their sidecar is populated. This work runs off the event loop.

**The list is a metadata view, not a full integrity check.** It does not validate
all SQL or gzip payload bytes. Damage after metadata, or a previously indexed
file whose identity is unchanged, may not be detected by listing. Restore testing
and separate full archive verification remain necessary to establish that a
backup is recoverable.

Retention never opens or decompresses archives. For generated filenames it uses
the UTC creation timestamp in `backup_YYYYMMDD_HHMMSS[_microseconds]`; ties sort
by filename. For legacy/custom filenames without a valid timestamp it uses the
filesystem modification time. The newest configured number is retained (at least
one). Metadata timestamps inside archives do not influence retention. Deleting
an archive also removes its sidecar. Partial files, sidecars and symlinks are not
listed or retained as backups.

Creation writes a private `.partial` file in the backup directory and atomically
publishes it only after the archive is closed and flushed. Filenames include
microseconds to avoid overwriting two backups created in the same second. The
service admits one creation at a time per `BackupService` instance. Temporary
snapshot cleanup runs off the event loop. Cancelling the normal `create_backup`
caller, including the scheduler, waits for the shielded creation task to finish.
This can delay scheduler shutdown by the duration of the backup.

Cancellation protection is not uniform across the internal creation stages.
Archive publication explicitly waits for its worker before staging cleanup even
if the internal task is cancelled directly. Earlier stages still use the existing
`pg_dump` subprocess / `process.communicate()` and `asyncio.to_thread` snapshot
collection. Direct cancellation of the internal creation task during those stages
can reach cleanup before that work has stopped. This local change does not claim
to make every shutdown path safe; subprocess termination and snapshot-worker
lifecycle need separate work before promising that guarantee. The condition was
not rechecked on production. These backup changes need no database migration.

## Adjacent API path validation fix

The backup download/restore/upload/delete routes previously checked a resolved
path with a string prefix. A sibling directory such as `backups-secrets` passed
that check for a `backups` root. The routes now use `Path.is_relative_to`, reject
absolute/traversal/root paths, and verify resolved symlink targets. Ordinary files
in the backup directory, including nested paths, remain usable. Regression tests
use synthetic files to cover sibling traversal and outbound symlinks; no real
secrets or production files are involved. Whether the Web API is currently
publicly enabled on production was not rechecked during this local fix.
