# Staged macOS LaunchAgent (disabled, dry-run)

This integration prepares a file only. It never calls `launchctl bootstrap`,
`load`, `enable`, `kickstart`, or installs a plist. The generated job has no
`--execute`, `Disabled=true`, `RunAtLoad=false`, `KeepAlive=false`, and a five-hour
`StartInterval`. No immediate startup or failure retry loop is requested.
`Disabled` can be overridden by launchd; absence of `--execute` is the separate
paid-request safeguard. A dry-run invocation opens no sockets and writes no client
state; Python is launched with `-B` to suppress bytecode writes.

## Inventory before preparing anything

On the actual Mac, from the intended permanent checkout:

```sh
python3 -B scripts/launchd_auto_analyze.py audit
```

The read-only audit checks user and machine LaunchAgents/LaunchDaemons, loaded
GUI and system jobs, the current user's crontab and process command lines. It
recognizes the client, wrapper, repository name, fixed label and analyze endpoint.
It prints findings, not raw process arguments or credentials. Any recognized
reference (even a disabled plist) or failed inspection blocks preparation. Reuse
and review an existing job rather than adding a second label. The audit does not
stop, unload or edit anything.

An empty inventory is **not proof** that no schedule exists: opaque shell wrappers,
other users' crontabs, remote jobs and a racing installation are outside its
coverage. Inspect those separately before any future activation. Audit again at
activation time. This repository and the Linux test environment cannot establish
the running state of a user's Mac.

## Prepare a reviewable file, without activation

Use the exact same permanent decision journal and run ledger as every existing
manual/scheduled client. Do not create a new ledger per checkout or deployment.
The decision journal must match the backend's configured journal location.

```sh
python3 -B scripts/launchd_auto_analyze.py prepare \
  --python /absolute/path/to/venv/bin/python \
  --log /absolute/permanent/path/decisions.jsonl \
  --run-log /absolute/permanent/path/auto_analyze_runs.jsonl \
  --output /absolute/staging/com.jp81-tech.jev.auto-analyze.plist
```

Preparation reruns inventory, refuses launchd installation directories and never
overwrites an existing output. Paths are encoded with plistlib, not shell
interpolation. No token is put in the plist. Inspect it with `plutil -lint` and
`plutil -p` on macOS. This document intentionally performs no activation.

## Execution and restart contract

The wrapper's `run` command is dry-run unless explicitly passed `--execute`.
Paid execution is deliberately absent from the generated configuration. Future
activation and adding that flag are separate operator decisions after inventory,
state reconciliation and credential review. Execution reads `ADMIN_TOKEN` from
the environment only; it does not load `.env` or configure launchd credentials.

Limits remain ETH/SOL/XRP, 50 posts, five-hour gaps, three attempts per invocation,
16 reservations in a rolling 24 hours and a 150-second request timeout. No model,
prompt, research protocol or client algorithm is modified. This is a count limit,
not a dollar-denominated budget.

The existing client's nonblocking `flock` covers the whole execution, including
API calls, on the shared run ledger. It protects against another direct client
using that same ledger as well as another scheduled invocation. A second process
exits blocked, without API calls. Kernel locks release on exit/crash; reservations
are fsynced before requests and remain after crashes, timeouts and reboots.
There is no automatic retry/refund or backlog replay. The original client decides
which symbols remain eligible; an analysis error can still allow subsequent
eligible symbols within the existing limits. Health failure stops the run.

The wrapper additionally blocks paid execution if either state file is missing.
First-time state initialization or adoption must be reviewed manually; it cannot
distinguish an intentionally empty ledger from a truncated one. Corrupt or future
records fail closed through the original client. Never delete, replace, truncate
or rotate the ledger while jobs run: replacing its inode defeats its lock and
deleting history resets the count. Keep permanent state outside disposable
checkouts, retain backups, and restore the original history before resuming.
Separate hosts or different ledger files do not share limits or locks.

No stdout/stderr log files are configured, avoiding unbounded new diagnostic
files. For diagnosis run the dry-run command manually to inspect its JSON output.
Persistent decision/run ledgers are distinct from diagnostics and must not be
rotated casually. Native launchd load/wake/reboot behavior still needs a macOS
acceptance test in dry-run; the offline tests do not claim that validation.

## Offline checks

```sh
python -B -m unittest discover -s tests -p test_launchd_offline.py -v
```

The standard-library suite injects synthetic API responses and blocks socket
connections. It covers disabled configuration, dry startup, separate-process
overlap, exhausted and nearly exhausted budgets, API outage/failure, crash/restart,
missing/corrupt/future state, audit findings, and preparation without overwrite or
installation. No backend or provider keys are needed.

References: [Apple job configuration](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)
and [timed jobs](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html).
