# Production logging

Apply `docker-compose.logging.yml` after the normal production compose
files. The overlay uses the existing logging implementation and does not replace
application code or change the image.

- General application log: WARNING, ERROR and CRITICAL, with timestamps, module
  names and exception tracebacks. Routine successful API requests and refresh
  rotations stay below this threshold.
- Dedicated payment logger: INFO and above, using the existing `app.payments`
  handler. General DEBUG/INFO tracing remains disabled.
- Existing administrative audit records remain in the database.
- Files are written under `logs/current`; the built-in rotation creates a
  compressed daily archive at 00:00 in the configured application timezone and
  retains seven days. Telegram log delivery is disabled.
- Keep Docker log limits from the base configuration. Existing historical logs
  outside `logs/current` are preserved and are not deleted by this overlay.

Remove temporary diagnostic compose overlays when enabling this configuration,
and stop their scheduled restore timers so they cannot replace this configuration
later. Do not include `cabinet_diagnostics.py` mounts or expose the temporary
`/cabinet/diagnostics` collector. The frontend no longer submits those events.

Verify the effective compose environment, container health, creation of the
normal log files, and removal of the diagnostic endpoint. Do not generate real
payments or revoke user sessions to test logging.
