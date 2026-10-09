"""Prepare (never install) a disabled macOS LaunchAgent; run bounded offline plans."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

try:
    from scripts import auto_analyze as client
except ModuleNotFoundError:
    import auto_analyze as client

LABEL = 'com.jp81-tech.jev.auto-analyze'
ROOT = Path(__file__).resolve().parents[1]
MARKERS = (LABEL.lower(), 'auto_analyze.py', 'launchd_auto_analyze.py',
           'jev-x-sentiment-analysis', '/api/v1/analyze')


def related(text):
    return any(marker in text.lower() for marker in MARKERS)


def audit(home=None, command=subprocess.run):
    """Read-only, conservative inventory; errors are not interpreted as absence."""
    if sys.platform != 'darwin':
        return {'status': 'UNKNOWN', 'findings': ['MACOS_REQUIRED']}
    home = Path(home or Path.home())
    findings = []
    directories = [home / 'Library/LaunchAgents', Path('/Library/LaunchAgents'),
                   Path('/Library/LaunchDaemons')]
    for directory in directories:
        try:
            if not directory.exists():
                continue
            for path in directory.glob('*.plist'):
                try:
                    with path.open('rb') as stream:
                        value = plistlib.load(stream)
                    if related(str(value)):
                        findings.append('EXISTING_PLIST:' + str(path))
                except (OSError, ValueError, plistlib.InvalidFileException):
                    findings.append('UNREADABLE_PLIST:' + str(path))
        except OSError:
            findings.append('UNREADABLE_DIRECTORY:' + str(directory))
    checks = [('/bin/launchctl', 'print', 'gui/' + str(os.getuid())),
              ('/bin/launchctl', 'print', 'system'),
              ('/usr/bin/crontab', '-l'), ('/bin/ps', '-axo', 'pid=,command=')]
    for argv in checks:
        try:
            result = command(argv, capture_output=True, text=True, timeout=10,
                             env={**os.environ, 'LC_ALL': 'C'})
            if result.returncode:
                if argv[0].endswith('crontab') and result.returncode == 1 and 'no crontab for' in result.stderr:
                    continue
                findings.append('INSPECTION_FAILED:' + argv[0])
                continue
            lines = result.stdout.splitlines()
            if argv[0].endswith('/ps'):
                lines = [line for line in lines if line.strip().split(maxsplit=1)[0:1] != [str(os.getpid())]]
            if any(related(line) for line in lines):
                # Do not print process arguments: they might contain credentials.
                findings.append('EXISTING_REFERENCE:' + argv[0])
        except (OSError, subprocess.TimeoutExpired):
            findings.append('INSPECTION_FAILED:' + argv[0])
    return {'status': 'REVIEW_REQUIRED' if findings else 'NO_MATCH_IN_CHECKED_SOURCES',
            'findings': findings}


def configuration(python, journal, run_log):
    """No shell, token, execute flag, boot run or crash-restart loop."""
    paths = [Path(python), Path(journal), Path(run_log)]
    if not all(p.is_absolute() for p in paths) or paths[1].resolve() == paths[2].resolve():
        raise ValueError('distinct absolute paths required')
    return {'Label': LABEL, 'Disabled': True, 'RunAtLoad': False,
            'KeepAlive': False, 'StartInterval': 18000, 'ProcessType': 'Background',
            'WorkingDirectory': str(ROOT), 'Umask': 0o077,
            'ProgramArguments': [str(paths[0]), '-B', str(Path(__file__).resolve()),
                                 'run', '--log', str(paths[1]), '--run-log', str(paths[2])]}


def run(journal, run_log, execute=False, requester=client.request, clock=client.time.time):
    """Keep the original ledger and its flock, fsync, gaps and rolling limits."""
    journal, run_log = Path(journal), Path(run_log)
    if not journal.is_absolute() or not run_log.is_absolute():
        print(json.dumps({'status': 'BLOCKED', 'code': 'ABSOLUTE_PATHS_REQUIRED'}))
        return 78
    # Missing state must never silently create a fresh paid budget after a restart.
    # Initial state adoption/creation is deliberately an operator decision.
    if execute and (not journal.is_file() or not run_log.is_file()):
        print(json.dumps({'status': 'BLOCKED', 'code': 'EXISTING_STATE_REQUIRED'}))
        return 78
    args = ['--log', str(journal), '--run-log', str(run_log),
            '--symbols', 'ETH,SOL,XRP', '--sample-size', '50', '--min-gap-hours', '5',
            '--max-per-run', '3', '--max-per-day', '16', '--timeout', '150']
    if execute:
        args += ['--execute', '--token-env']
    return client.main(args, requester=requester, clock=clock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('audit')
    render = commands.add_parser('prepare')
    render.add_argument('--python', required=True)
    render.add_argument('--output', required=True)
    runner = commands.add_parser('run')
    runner.add_argument('--execute', action='store_true')
    for sub in (render, runner):
        sub.add_argument('--log', required=True)
        sub.add_argument('--run-log', required=True)
    args = parser.parse_args(argv)
    if args.command == 'run':
        return run(args.log, args.run_log, args.execute)
    result = audit()
    print(json.dumps(result))
    if args.command == 'audit':
        return 0 if result['status'] == 'NO_MATCH_IN_CHECKED_SOURCES' else 78
    if result['status'] != 'NO_MATCH_IN_CHECKED_SOURCES':
        return 78
    output = Path(args.output).absolute()
    # Preparation is staging only. Never write directly into launchd search paths.
    if any(part in ('LaunchAgents', 'LaunchDaemons') for part in output.resolve().parts):
        print('Refusing installation directory', file=sys.stderr)
        return 78
    try:
        config = configuration(args.python, args.log, args.run_log)
        if not Path(args.python).is_file() or not os.access(args.python, os.X_OK):
            raise ValueError('interpreter is not executable')
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            plistlib.dump(config, stream)
        return 0
    except (OSError, ValueError):
        print('Preparation failed; no existing file overwritten', file=sys.stderr)
        return 78


if __name__ == '__main__':
    raise SystemExit(main())
