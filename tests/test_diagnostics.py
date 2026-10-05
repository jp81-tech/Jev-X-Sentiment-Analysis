"""Offline-only diagnostic classification and output redaction."""
import json
import pytest
from scripts import diagnose_local as diagnostic

NOW = 2_000_000_000


def valid_payload():
    return {'symbol': 'BTC', 'status': 'success', 'model_status': 'ok',
            'market': {'status': 'ok', 'symbol': 'BTC', 'source': 'kraken', 'price': 50000,
                       'rsi_14': 50, 'fetched_at': NOW, 'request_started_at': NOW,
                       'valid_until': NOW+120, 'source_timestamp': None, 'freshness_basis': 'request_start'},
            'social': {'symbol': 'BTC', 'target_count': 50, 'status': 'ok', 'source': 'twitterapi.io', 'count': 50, 'fetched_at': NOW,
                       'valid_until': NOW+600, 'api_pages_called': 3},
            'social_stats': {'sample_size': 50},
            'tweets_sample': [{'id': str(i), 'source': 'twitterapi.io', 'text': 'DO_NOT_OUTPUT_TWEET'} for i in range(50)],
            'decision': {'symbol': 'BTC', 'action': 'BUY', 'confidence_pct': 51, 'reason': 'DO_NOT_OUTPUT_ADVICE'}}


def test_valid_analysis_and_no_payload_secrets():
    payload = valid_payload()
    payload.update(api_key='DO_NOT_OUTPUT_KEY', url='https://DO_NOT_OUTPUT_URL')
    payload['social']['reason'] = 'DO_NOT_OUTPUT_ERROR'
    result = diagnostic.classify(payload, 'analysis', NOW)
    assert result['status'] == 'LIVE_CHECK_OK' and result['model_available']
    assert result['social']['pages'] == 3
    output = json.dumps(result)
    assert 'DO_NOT_OUTPUT' not in output and 'BUY' not in output


@pytest.mark.parametrize('mutation', ['null', 'stale-market', 'stale-social', 'partial', 'duplicate', 'mock', 'bad-decision'])
def test_http200_is_not_success(mutation):
    payload = valid_payload()
    if mutation == 'null': payload['decision'] = None
    elif mutation == 'stale-market': payload['market']['valid_until'] = NOW
    elif mutation == 'stale-social': payload['social']['valid_until'] = NOW
    elif mutation == 'partial': payload['social_stats']['sample_size'] = 49
    elif mutation == 'duplicate': payload['tweets_sample'][1]['id'] = '0'
    elif mutation == 'mock': payload['decision']['is_mock'] = True
    else: payload['decision']['action'] = ['DO_NOT_OUTPUT']
    assert diagnostic.classify(payload, 'analysis', NOW)['status'] == 'UNKNOWN'


def test_missing_keys_blocks_before_analysis():
    calls = []
    def request(*args):
        calls.append(args)
        return {'status': 'healthy', 'twitter_configured': False, 'typesafe_configured': True}, {'http_code': 200}
    result = diagnostic.probe('analysis', 8787, request)
    assert result['status'] == 'BLOCKED' and result['codes'] == ['TWITTER_KEY_MISSING']
    assert len(calls) == 1


def test_exactly_one_analysis_no_retry():
    calls = []
    def request(port, path, body=None):
        calls.append((path, body))
        if path == '/health':
            return {'status': 'healthy', 'twitter_configured': True, 'typesafe_configured': True}, {'http_code': 200}
        return valid_payload(), {'http_code': 200}
    assert diagnostic.probe('analysis', 8787, request, lambda: NOW)['status'] == 'LIVE_CHECK_OK'
    assert calls == [('/health', None), ('/api/v1/analyze', {'symbol': 'BTC', 'sample_size': 50})]


def test_http_error_contains_no_exception_text(monkeypatch):
    import urllib.error
    class BadOpener:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError('https://PRIVATE_URL', 401, 'PRIVATE_KEY', {}, None)
    monkeypatch.setattr(diagnostic.urllib.request, 'build_opener', lambda *args: BadOpener())
    payload, report = diagnostic.request(8787, '/health')
    assert payload is None and report['http_code'] == 401
    assert 'PRIVATE' not in json.dumps(report)


def test_market_without_keys_and_api_unknown_separate():
    def request(port, path, body=None):
        return ({'status': 'healthy'} if path == '/health' else valid_payload()['market']), {'http_code': 200}
    result = diagnostic.probe('market', 8787, request, lambda: NOW)
    assert result['status'] == 'LIVE_CHECK_OK' and result['model_available'] is False
    unavailable = diagnostic.probe('market', 8787, lambda *a: (None, {'code': 'CONNECTION_ERROR'}))
    assert unavailable['code'] == 'LOCAL_API_UNAVAILABLE'


@pytest.mark.parametrize('section', ['top', 'market', 'social', 'decision'])
@pytest.mark.parametrize('missing', [False, True])
def test_symbol_binding_required(section, missing):
    payload = valid_payload()
    target = payload if section == 'top' else payload[section]
    if missing:
        target.pop('symbol')
    else:
        target['symbol'] = 'ETH_DO_NOT_ECHO'
    result = diagnostic.classify(payload, 'analysis', NOW)
    assert result['status'] == 'UNKNOWN'
    assert 'ETH_DO_NOT_ECHO' not in json.dumps(result)


@pytest.mark.parametrize('value', [None, 49, 100, '50'])
def test_target_count_matches_requested_sample(value):
    payload = valid_payload()
    if value is None:
        payload['social'].pop('target_count')
    else:
        payload['social']['target_count'] = value
    assert diagnostic.classify(payload, 'analysis', NOW)['status'] == 'UNKNOWN'
