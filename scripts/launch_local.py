"""Explicit local startup; never writes configuration or installs dependencies."""
import argparse
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8787)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        import uvicorn
        from app.core.config import settings
    except Exception:
        print('BLOCKED: RUNTIME_OR_CONFIG_INVALID; check installed requirements and configuration.', file=sys.stderr)
        return 78
    missing = [name for name, value in [('TWITTER_KEY_MISSING', settings.TWITTER_API_KEY),
                                        ('TYPESAFE_KEY_MISSING', settings.TYPESAFE_API_KEY)] if not value or not value.strip()]
    print(json.dumps({'startup': 'LOCAL_UI', 'host': '127.0.0.1', 'port': args.port,
                      'workers': 1, 'full_analysis': 'BLOCKED' if missing else 'CONFIGURED_NOT_VERIFIED',
                      'codes': missing}), flush=True)
    uvicorn.run('app.main:app', host='127.0.0.1', port=args.port, workers=1, access_log=False)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
