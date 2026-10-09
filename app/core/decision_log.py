"""Allowlisted decision journal. No provider text or credentials are serialized."""
import ast
import errno
import fcntl
from functools import lru_cache
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)
ACTIONS = {'STRONG_BUY', 'BUY', 'HOLD', 'TAKE_PROFIT', 'SELL', 'STRONG_SELL'}
REASONS = {'missing_key', 'target_reached', 'page_limit', 'end_of_results', 'pagination_no_progress',
           'rate_limited', 'rate_limit_timeout', 'fetch_timeout', 'provider_error', 'rate_limit_capacity',
           'payment_required', 'http_error', 'malformed_response', 'transport_error'}
SENTIMENTS = {'Neutral', 'Neutral / Mixed', 'Extreme Panic', 'Bearish / Fearful', 'Bullish / Optimistic', 'Euphoric / Greedy'}


def prompt_fingerprint(source):
    """Hash real AST definitions: question text/criteria plus state keys and construction.

    No execution, runtime state, whitespace or source locations enter the digest.
    """
    tree = ast.parse(source)
    service = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'TypeSafeService')
    method = next(n for n in service.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'evaluate_decision')
    def normalized(node):
        if isinstance(node, ast.AST):
            return {'node':type(node).__name__, **{key:normalized(value) for key,value in ast.iter_fields(node) if value is not None and value != []}}
        if isinstance(node,list):return [normalized(v) for v in node]
        return node
    definitions = {}
    for node in method.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ('state', 'questions'):
                    definitions[target.id] = normalized(node.value)
    if set(definitions) != {'state', 'questions'}:
        raise ValueError('Prompt definitions unavailable')
    return hashlib.sha256(json.dumps(definitions, sort_keys=True).encode()).hexdigest()


@lru_cache(maxsize=1)
def prompt_version():
    return prompt_fingerprint((ROOT/'app/services/typesafe_service.py').read_text())


@lru_cache(maxsize=1)
def app_commit():
    configured = os.environ.get('JEV_APP_COMMIT', '')
    try:
        commit = configured or subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, stderr=subprocess.DEVNULL, timeout=2, text=True).strip()
        if not re.fullmatch('[0-9a-fA-F]{40}', commit):
            return 'unknown'
        dirty = subprocess.check_output(['git','status','--porcelain','--untracked-files=normal'], cwd=ROOT, stderr=subprocess.DEVNULL, timeout=2, text=True)
        return commit.lower() + ('+dirty' if dirty else '')
    except (OSError, subprocess.SubprocessError):
        return 'unknown'  # Cannot certify tree cleanliness, even with an override.


# Capture provenance before requests; later disk edits cannot relabel this process.
STARTUP_PROMPT_VERSION = prompt_version()
STARTUP_APP_COMMIT = app_commit()

def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def count(value):
    return value if type(value) is int and value >= 0 else None


def choice(value, allowed):
    return value if isinstance(value, str) and value in allowed else None


def pattern(value, regex):
    return value if isinstance(value, str) and re.fullmatch(regex, value) else None


def numeric_fields(data, names):
    data = data if isinstance(data, dict) else {}
    return {name:number(data.get(name)) for name in names.split()}


def sample_hash(tweets):
    ids = sorted(t['id'] for t in tweets if isinstance(t,dict) and isinstance(t.get('id'),str))
    return hashlib.sha256(json.dumps(ids, ensure_ascii=True, separators=(',',':')).encode()).hexdigest()


def record_for(response, tweets, sample_size, request_started_at, now):
    market, social, stats = (response.get(k) or {} for k in ('market','social','social_stats'))
    decision = response.get('decision')
    clean_decision = None
    if isinstance(decision, dict):
        clean_decision = numeric_fields(decision,'confidence_pct selected_action_probability_pct sentiment_score squeeze_risk_pct catalyst_impact_score')
        clean_decision['action'] = choice(decision.get('action'), ACTIONS)
        probs = decision.get('action_probabilities')
        clean_decision['action_probabilities'] = {k:number(probs.get(k)) for k in sorted(ACTIONS)} if isinstance(probs,dict) else None
        levels = decision.get('trade_levels')
        clean_decision['trade_levels'] = None
        if isinstance(levels,dict):
            clean_levels = numeric_fields(levels,'stop_loss stop_loss_pct target_1 target_1_pct target_2 target_2_pct risk_reward_ratio tick_size')
            entry = levels.get('entry_range')
            clean_levels['entry_range'] = [number(v) for v in entry] if isinstance(entry,(list,tuple)) and len(entry)==2 else None
            clean_levels['method'] = choice(levels.get('method'), {'fixed_percentage_heuristic'})
            clean_decision['trade_levels'] = clean_levels
    clean_market = numeric_fields(market,'price change_24h_pct rsi_14 funding_rate_pct open_interest_usd source_timestamp request_started_at valid_until tick_size')
    clean_market['has_perpetuals'] = market.get('has_perpetuals') if type(market.get('has_perpetuals')) is bool else None
    clean_stats = numeric_fields(stats,'author_diversity_pct avg_engagement polarity_score')
    clean_stats.update({k:count(stats.get(k)) for k in ('sample_size','unique_authors_count')})
    clean_stats.update(sentiment_label=choice(stats.get('sentiment_label'),SENTIMENTS), polarity_method=choice(stats.get('polarity_method'),{'keyword_heuristic'}))
    rejected = social.get('rejected_dates')
    return {'schema_version':1,'record_id':uuid.uuid4().hex,'ts_utc':number(now),
            'request_started_at':number(request_started_at),'app_commit':STARTUP_APP_COMMIT,'prompt_version':STARTUP_PROMPT_VERSION,
            'symbol':pattern(response.get('symbol'),r'[A-Z0-9]{1,15}'), 'pair':pattern(market.get('pair'),r'[A-Z0-9]{1,15}/[A-Z0-9]{1,15}'),
            'sample_size':count(sample_size),'status':choice(response.get('status'),{'success','degraded','unavailable'}),
            'reason':choice(social.get('reason'),REASONS),'sample_hash':sample_hash(tweets),'sample_count':len(tweets),
            'oldest_publication_at':number(social.get('oldest_publication_at')),'newest_publication_at':number(social.get('newest_publication_at')),
            'rejected_dates':{k:count(rejected.get(k)) if isinstance(rejected,dict) else None for k in ('unknown_date','outside_window','future_date')},
            'market':clean_market,'social_stats':clean_stats,'decision':clean_decision,
            'horizons':{'H24':now+86400,'H72':now+259200},'settled':None}


def append_record(record):
    path = Path(os.environ.get('JEV_DECISION_LOG', ROOT/'app/data/decisions.jsonl'))
    content = (json.dumps(record, allow_nan=False, separators=(',',':'))+'\n').encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('Not a regular journal')
        os.fchmod(fd, 0o600)
        start = os.fstat(fd).st_size
        if start and os.pread(fd, 1, start-1) != b'\n':
            raise ValueError('Incomplete journal tail')
        try:
            remaining = memoryview(content)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError(errno.EIO, 'No write progress')
                remaining = remaining[written:]
            os.fsync(fd)
        except BaseException:
            # Under the advisory lock, undo only this call's incomplete append.
            os.ftruncate(fd, start)
            raise
    finally:
        os.close(fd)


def log_response(response, tweets, sample_size, request_started_at, now):
    try:
        record = record_for(response, tweets, sample_size, request_started_at, now)
        append_record(record)
        return {'decision_logged':True,'record_id':record['record_id']}
    except Exception as error:
        category = 'permission' if isinstance(error,PermissionError) else 'storage_full' if isinstance(error,OSError) and error.errno==errno.ENOSPC else 'io' if isinstance(error,OSError) else 'invalid_record'
        correlation = uuid.uuid4().hex
        logger.error('stage=decision_log category=%s correlation_id=%s',category,correlation)
        return {'decision_logged':False,'log_error_category':category,'correlation_id':correlation}
