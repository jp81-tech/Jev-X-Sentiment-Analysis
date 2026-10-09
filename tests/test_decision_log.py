import errno
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock
import httpx
import pytest
from app.main import app
from app.core import decision_log as journal
from app.api.v1 import analyze as api
from test_pipeline import market, social

SECRET='DO_NOT_PERSIST_SECRET_TEXT_AUTHOR_EXCEPTION'

def decision():
    return {'action':'BUY','confidence_pct':42,'selected_action_probability_pct':61,'rationale':SECRET,
            'trade_levels':{'entry_range':[99,101],'stop_loss':98,'target_1':104,'target_2':108,'method':'fixed_percentage_heuristic','private':SECRET}}

def inputs(monkeypatch,status):
    price=market();price.update(pair='BTC/USD',private=SECRET)
    tweets=social();tweets.update(reason='target_reached',oldest_publication_at=tweets['tweets'][0]['timestamp_epoch'],newest_publication_at=tweets['tweets'][0]['timestamp_epoch'],rejected_dates={'unknown_date':0,'outside_window':0,'future_date':0,'private':SECRET})
    for row in tweets['tweets']:row.update(text=SECRET,author_username=SECRET)
    if status=='degraded':tweets['status']='partial'
    if status=='unavailable':tweets.update(status='unavailable',tweets=[])
    monkeypatch.setattr(api.market_service,'get_market_data',AsyncMock(return_value=price))
    monkeypatch.setattr(api.twitter_service,'fetch_tweets',AsyncMock(return_value=tweets))
    monkeypatch.setattr(api.typesafe_service,'evaluate_decision',AsyncMock(return_value=decision()))

async def analyze():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as client:
        return await client.post('/api/v1/analyze',json={'symbol':'BTC','sample_size':50})

@pytest.mark.asyncio
async def test_three_statuses_three_private_rows(monkeypatch):
    for status in ('success','degraded','unavailable'):
        inputs(monkeypatch,status);response=await analyze();data=response.json()
        assert response.status_code==200 and data['status']==status and data['decision_logged'] is True
    path=Path(os.environ['JEV_DECISION_LOG']);raw=path.read_text();rows=[json.loads(s) for s in raw.splitlines()]
    assert len(rows)==3 and [r['status'] for r in rows]==['success','degraded','unavailable']
    assert len({r['record_id'] for r in rows})==3 and SECRET not in raw
    assert path.stat().st_mode & 0o777==0o600
    required={'schema_version','record_id','ts_utc','request_started_at','app_commit','prompt_version','inputs_version','symbol','pair','sample_size','status','reason','sample_hash','sample_count','oldest_publication_at','newest_publication_at','rejected_dates','market','social_stats','decision','horizons','settled'}
    for row in rows:
        assert set(row)==required and row['schema_version']==1 and len(row['prompt_version'])==64
        assert row['inputs_version']==journal.STARTUP_INPUTS_VERSION
        assert row['horizons']=={'H24':row['ts_utc']+86400,'H72':row['ts_utc']+259200}
        assert row['request_started_at']<=row['ts_utc'] and row['settled'] is None
    assert rows[0]['decision']['trade_levels']['method']=='fixed_percentage_heuristic'
    assert rows[1]['decision'] is None and rows[2]['decision'] is None

def test_sample_hash_and_prompt_mutation():
    assert journal.sample_hash([{'id':'2'},{'id':'1'}])==journal.sample_hash([{'id':'1'},{'id':'2'}])
    assert journal.sample_hash([{'id':'1'}])!=journal.sample_hash([{'id':'2'}])
    source=(journal.ROOT/'app/services/typesafe_service.py').read_text()
    changed=source.replace('Favorable risk-to-reward long entry with positive upside expectation.','Different question criterion.')
    assert changed!=source and journal.prompt_fingerprint(source)==journal.prompt_version()
    assert journal.prompt_fingerprint(source)!=journal.prompt_fingerprint(changed)
    assert journal.prompt_fingerprint(source)==journal.prompt_fingerprint('\n'+source)
    question_changed=source.replace('best immediate trading action','best delayed trading action')
    assert question_changed!=source and journal.prompt_fingerprint(source)!=journal.prompt_fingerprint(question_changed)

@pytest.mark.asyncio
@pytest.mark.parametrize('error,category',[(PermissionError(SECRET),'permission'),(OSError(errno.ENOSPC,SECRET),'storage_full')])
async def test_log_failure_does_not_break_response(monkeypatch,caplog,error,category):
    inputs(monkeypatch,'success')
    def fail(record):raise error
    monkeypatch.setattr(journal,'append_record',fail)
    response=await analyze();data=response.json()
    assert response.status_code==200 and data['status']=='success' and data['decision_logged'] is False
    assert data['log_error_category']==category and 'record_id' not in data
    assert SECRET not in json.dumps({k:v for k,v in data.items() if k in ('decision_logged','log_error_category')})+caplog.text
    assert 'stage=decision_log' in caplog.text and 'correlation_id=' in caplog.text

@pytest.mark.asyncio
async def test_journal_after_model_expiry(monkeypatch):
    inputs(monkeypatch,'success');clock=[api.time.time()]
    monkeypatch.setattr(api.time,'time',lambda:clock[0])
    async def delayed(**kwargs):clock[0]+=700;return decision()
    monkeypatch.setattr(api.typesafe_service,'evaluate_decision',delayed)
    response=await analyze();data=response.json();row=json.loads(Path(os.environ['JEV_DECISION_LOG']).read_text())
    assert data['status']==row['status']=='degraded' and data['decision'] is row['decision'] is None
    assert row['ts_utc']==clock[0]

def test_allowlist_types():
    response={'symbol':'BTC','status':'success','market':{'pair':'BTC/USD','price':True,'tick_size':float('nan')},'social':{'reason':SECRET},'social_stats':{'sentiment_label':SECRET,'polarity_method':SECRET},'decision':decision()}
    response['decision']['trade_levels']['method']=SECRET
    row=journal.record_for(response,[{'id':'1','text':SECRET}],50,1,2)
    assert row['market']['price'] is None and row['market']['tick_size'] is None
    assert SECRET not in json.dumps(row,allow_nan=False)

def test_short_writes_concurrent_records_and_fsync(monkeypatch):
    real_write=journal.os.write;real_fsync=journal.os.fsync;syncs=[]
    monkeypatch.setattr(journal.os,'write',lambda fd,data:real_write(fd,data[:7]))
    def fsync(fd):syncs.append(fd);real_fsync(fd)
    monkeypatch.setattr(journal.os,'fsync',fsync)
    with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(lambda n:journal.append_record({'n':n}),range(30)))
    rows=[json.loads(s) for s in Path(os.environ['JEV_DECISION_LOG']).read_text().splitlines()]
    assert sorted(r['n'] for r in rows)==list(range(30)) and len(syncs)==30

def test_failed_partial_write_preserves_prior_record(monkeypatch):
    journal.append_record({'n':1});real_write=journal.os.write;calls=[]
    def fail(fd,data):
        calls.append(1)
        if len(calls)>1:raise OSError(errno.ENOSPC,SECRET)
        return real_write(fd,data[:3])
    monkeypatch.setattr(journal.os,'write',fail)
    with pytest.raises(OSError):journal.append_record({'n':2})
    assert Path(os.environ['JEV_DECISION_LOG']).read_text()=='{"n":1}\n'

def test_app_commit_dirty_and_override(monkeypatch):
    journal.app_commit.cache_clear();monkeypatch.setenv('JEV_APP_COMMIT','a'*40)
    monkeypatch.setattr(journal.subprocess,'check_output',lambda *a,**kw:' M modified.py\n')
    assert journal.app_commit()=='a'*40+'+dirty'
    journal.app_commit.cache_clear()


def test_fsync_failure_undoes_only_own_append(monkeypatch):
    journal.append_record({'n':1})
    def fail(fd):raise OSError(errno.EIO,SECRET)
    monkeypatch.setattr(journal.os,'fsync',fail)
    with pytest.raises(OSError):journal.append_record({'n':2})
    assert Path(os.environ['JEV_DECISION_LOG']).read_text()=='{"n":1}\n'


def test_incomplete_tail_not_silently_extended():
    path=Path(os.environ['JEV_DECISION_LOG']);path.write_text('{"incomplete":')
    with pytest.raises(ValueError):journal.append_record({'n':1})
    assert path.read_text()=='{"incomplete":'
