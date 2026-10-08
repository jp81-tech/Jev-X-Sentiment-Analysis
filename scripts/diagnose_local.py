"""One bounded public-market or BTC/50 API probe; output is an explicit allowlist.
No app/config import: keys are checked only through local /health booleans.
"""
import argparse
import importlib.metadata
import json
import math
import os
import platform
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.freshness import market_is_fresh, within_validity
from app.core.market_validation import market_numbers_valid

ACTIONS = {'BUY', 'STRONG_BUY', 'HOLD', 'TAKE_PROFIT', 'SELL', 'STRONG_SELL'}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def bounded_count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10000 else None


def freshness(data, now):
    result = {}
    for field in ('request_started_at', 'source_timestamp', 'fetched_at', 'valid_until'):
        value = data.get(field)
        result[field] = value if number(value) else None
    result['age_seconds'] = round(now - data['fetched_at'], 3) if number(data.get('fetched_at')) else None
    return result


def classify(payload, mode, now):
    data = payload if isinstance(payload, dict) else {}
    market = data if mode == 'market' else data.get('market', {})
    market = market if isinstance(market, dict) else {}
    good_market = (market.get('status') == 'ok' and market.get('source') == 'kraken'
                   and market.get('symbol') == 'BTC' and not market.get('is_fallback')
                   and market_is_fresh(market, now) and market_numbers_valid(market))
    report = {'status': 'LIVE_CHECK_OK' if good_market else 'UNKNOWN',
              'market': {'status': 'AVAILABLE' if good_market else 'UNKNOWN',
                         'code': 'MARKET_VALID' if good_market else 'MARKET_UNAVAILABLE_OR_INVALID',
                         'freshness': freshness(market, now)}, 'model_available': False}
    if mode == 'market':
        return report
    social = data.get('social', {})
    social = social if isinstance(social, dict) else {}
    stats = data.get('social_stats', {})
    stats = stats if isinstance(stats, dict) else {}
    tweets = data.get('tweets_sample', [])
    valid_rows = (isinstance(tweets, list) and len(tweets) == 50
                  and all(isinstance(t, dict) and isinstance(t.get('id'), str) and t['id'].strip()
                          and t.get('source') == 'twitterapi.io' for t in tweets))
    good_social = (social.get('symbol') == 'BTC' and social.get('target_count') == 50
                   and social.get('status') == 'ok' and social.get('source') == 'twitterapi.io'
                   and not social.get('is_mock') and not data.get('is_twitter_mock')
                   and social.get('count') == 50 and stats.get('sample_size') == 50
                   and valid_rows and len({t['id'] for t in tweets}) == 50 and within_validity(social, now))
    decision = data.get('decision')
    model_ok = (isinstance(decision, dict) and decision.get('symbol') == 'BTC' and isinstance(decision.get('action'), str) and decision['action'] in ACTIONS
                and number(decision.get('confidence_pct')) and 0 <= decision['confidence_pct'] <= 100
                and not decision.get('is_mock') and not data.get('is_typesafe_mock')
                and data.get('model_status') == 'ok')
    report.update(model_available=model_ok,
                  social={'status': 'AVAILABLE' if good_social else 'UNKNOWN',
                          'code': 'SOCIAL_VALID' if good_social else 'SOCIAL_INCOMPLETE_OR_INVALID',
                          'sample_size': bounded_count(stats.get('sample_size')),
                          'pages': bounded_count(social.get('api_pages_called')), 'freshness': freshness(social, now)},
                  model={'status': 'AVAILABLE' if model_ok else 'UNKNOWN',
                         'code': 'MODEL_RESULT_PRESENT' if model_ok else 'MODEL_UNAVAILABLE_OR_INVALID'})
    report['status'] = 'LIVE_CHECK_OK' if data.get('symbol') == 'BTC' and good_market and good_social and model_ok and data.get('status') == 'success' else 'UNKNOWN'
    return report


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(port, path, body=None, token=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}',
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Content-Type': 'application/json', **({'X-Admin-Token':token} if token else {})})
    started = time.monotonic()
    try:
        with opener.open(req, timeout=150) as response:
            content = response.read(8 * 1024 * 1024 + 1)
            if len(content) > 8 * 1024 * 1024:
                raise ValueError('size')
            data = json.loads(content)
            return data, {'status': 'REPLIED', 'http_code': response.status,
                          'elapsed_seconds': round(time.monotonic() - started, 3)}
    except urllib.error.HTTPError as error:
        code, http_code = ('ACCESS_DENIED' if error.code in (401,403) else 'HTTP_ERROR'), error.code
    except (TimeoutError, socket.timeout):
        code, http_code = 'TIMEOUT', None
    except urllib.error.URLError:
        code, http_code = 'CONNECTION_ERROR', None
    except Exception:
        code, http_code = 'INVALID_RESPONSE', None
    return None, {'status': 'UNKNOWN', 'code': code, 'http_code': http_code,
                  'elapsed_seconds': round(time.monotonic() - started, 3)}


def probe(mode, port, requester=request, clock=time.time):
    health, api = requester(port, '/health')
    result = {'status': 'UNKNOWN', 'api': api, 'model_available': False}
    if api.get('http_code') in (401,403):
        result.update(status='BLOCKED', code='ACCESS_DENIED')
        return result
    if not isinstance(health, dict) or health.get('status') != 'healthy':
        result['code'] = 'LOCAL_API_UNAVAILABLE'
        return result
    if mode == 'analysis':
        missing = [label for field, label in [('twitter_configured', 'TWITTER_KEY_MISSING'),
                                             ('typesafe_configured', 'TYPESAFE_KEY_MISSING')] if health.get(field) is not True]
        if missing:
            result.update(status='BLOCKED', codes=missing)
            return result
    path = '/api/v1/market/BTC' if mode == 'market' else '/api/v1/analyze'
    body = None if mode == 'market' else {'symbol': 'BTC', 'sample_size': 50}
    payload, response = requester(port, path, body)
    if response.get("http_code") in (401,403):
        result.update(status="BLOCKED", code="ACCESS_DENIED", api=response)
        return result
    try:
        result.update(classify(payload, mode, clock()), api=response)
    except Exception:
        result.update(status='UNKNOWN', code='INVALID_RESPONSE', api=response)
    return result


def versions():
    result = {'python': platform.python_version()}
    for package in ('fastapi', 'uvicorn', 'ccxt', 'typesafe-sdk', 'httpx', 'pydantic-settings'):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = 'NOT_INSTALLED'
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['market', 'analysis'])
    parser.add_argument('--port', type=int, default=8787)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--token-env', action='store_true', help='Read ADMIN_TOKEN from process environment only')
    source.add_argument('--token-stdin', action='store_true', help='Read one token line from stdin; do not pass it in argv')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    token = os.environ.get("ADMIN_TOKEN", "") if args.token_env else sys.stdin.readline(4097).rstrip("\r\n") if args.token_stdin else ""
    try:
        token.encode("latin-1")
        header_representable = True
    except UnicodeEncodeError:
        header_representable = False
    if len(token) > 4096 or not header_representable or "\n" in token or "\r" in token:
        print(json.dumps({"status":"BLOCKED", "code":"INVALID_ACCESS_TOKEN"}))
        return 78
    result = probe(args.mode, args.port, requester=lambda port,path,body=None: request(port,path,body,token=token))
    result['versions'] = versions()
    print(json.dumps(result, allow_nan=False))
    return 0 if result['status'] == 'LIVE_CHECK_OK' else 78


if __name__ == '__main__':
    raise SystemExit(main())
