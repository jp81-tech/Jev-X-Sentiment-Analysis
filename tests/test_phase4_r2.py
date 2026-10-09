import asyncio
import importlib.util
import json
import os
from pathlib import Path
import threading
import pytest
from app.core import decision_log as journal
from scripts import repair_decision_log as repair
from test_decision_log import inputs, analyze, SECRET
from test_research import record, row, write, cache_fixture, PROTOCOL, cohort
from scripts import research_core as r, collect_candles as collect, settle_decisions as settle

@pytest.mark.parametrize('failed',['XBT/USD','ADA/USD','ETH/USD'])
def test_collector_failure_is_per_pair(tmp_path,failed):
    log=tmp_path/'log';write(log,[record(symbol='ADA'),record(i=2)])
    calls=[]
    def fetch(pair):
        calls.append(pair)
        if pair==failed:raise OSError(SECRET)
        return {'error':[],'result':{'pair':[[0,'100','101','99','100','0','1',1],[300,'100','101','99','100','0','1',1]],'last':0}}
    result=collect.collect(log,tmp_path/'cache',now=1801000,execute=True,fetcher=fetch,sleep=lambda _:None)
    assert calls==['XBT/USD','ADA/USD','ETH/USD']
    assert result['status']=='PARTIAL' and result['pair_results'][failed]['status']=='FAILED'
    assert sum(v['status']=='OK' for v in result['pair_results'].values())==2
    assert SECRET not in json.dumps(result)

def test_collector_corrupt_cache_and_cli_exit(tmp_path,monkeypatch,capsys):
    log=tmp_path/'log';write(log,[record()]);cache=tmp_path/'cache';cache.mkdir()
    bad=r.candle_path(cache,'ETH/USD');bad.write_text('{BROKEN')
    calls=[]
    def fetch(pair):calls.append(pair);raise OSError(SECRET)
    result=collect.collect(log,cache,now=1801000,execute=True,fetcher=fetch,sleep=lambda _:None)
    assert calls==['XBT/USD'] and result['status']=='BLOCKED' and bad.read_text()=='{BROKEN'
    monkeypatch.setattr(collect,'collect',lambda *a,**kw:result)
    assert collect.main(['--log',str(log),'--candles',str(cache),'--execute'])==78
    assert SECRET not in str(capsys.readouterr())

def test_audit_dedup_and_coverage_history(tmp_path):
    rec=record();log=tmp_path/'log';write(log,[rec]);cache=tmp_path/'cache';out=tmp_path/'out';audit=tmp_path/'audit'
    def run():return settle.settle(log,cache,out,audit,PROTOCOL,now=rec['ts_utc']+432000)
    first=run();old=audit.read_bytes();second=run()
    assert first['audit_new']==1 and second['incomplete']==1 and second['audit_new']==0 and audit.read_bytes()==old
    write(r.candle_path(cache,'ETH/USD'),[row(r.required(rec['ts_utc'],r.H72)[0])])
    assert run()['audit_new']==1 and len(r.jsonl(audit))==2
    cache_fixture(cache,rec);assert run()['ok_new']==1 and len(r.jsonl(audit))==2
    assert run()['ok_new']==0

def test_min_greedy_independent_gate():
    rows=[];base=cohort(.02)
    # 12 time blocks; 3 overlapping entries/block, only one accepted per block.
    for block in range(12):
        for j in range(3):
            item=dict(base[block]);t=1000000+block*r.H72+j*60
            item.update(record_id=f'{block*3+j:032x}',symbol='ETH' if block%2 else 'SOL',ts_utc=t,entry_ts=t+200)
            rows.append(item)
    result=r.evaluate_rows(rows,1000000,1000000+40*86400,r.PARAMS)
    assert result['n']==36 and result['blocks']==12 and result['greedy_blocks']==12 and result['greedy_n']==12
    assert result['verdict']=='SAMPLE_TOO_SMALL'

def journal_file(tmp_path):
    path=tmp_path/'journal';write(path,[record()]);path.chmod(0o600);return path

def test_tail_repair_dryrun_quarantine_and_rerun(tmp_path):
    path=journal_file(tmp_path);prefix=path.read_bytes();tail=b'{"schema_version":1,"rec'
    path.write_bytes(prefix+tail)
    assert repair.repair(path)['status']=='TRUNCATED_TAIL' and path.read_bytes()==prefix+tail
    result=repair.repair(path,True);q=path.parent/result['quarantine']
    assert q.read_bytes()==tail and q.stat().st_mode&0o777==0o600
    assert path.read_bytes()==prefix and repair.repair(path,True)['status']=='UNCHANGED'
    assert len(list(tmp_path.glob('*.tail-*')))==1

@pytest.mark.parametrize('failure',['write','fsync'])
def test_tail_quarantine_failure_preserves_journal(tmp_path,monkeypatch,failure):
    path=journal_file(tmp_path);path.write_bytes(path.read_bytes()+b'{"cut":');before=path.read_bytes()
    def fail(*a):raise OSError(SECRET)
    monkeypatch.setattr(repair.os,failure,fail)
    with pytest.raises(OSError):repair.repair(path,True)
    assert path.read_bytes()==before

@pytest.mark.parametrize('content',[b'{BAD}\n{"cut":',b'{"x":NaN}\n{"cut":',b'{"complete":1}',b'garbage',b'{"bad":BROKEN'])
def test_repair_rejects_prior_corruption_and_complete_tail(tmp_path,content):
    path=tmp_path/'journal';path.write_bytes(content);path.chmod(0o600)
    with pytest.raises(Exception):repair.repair(path,True)
    assert path.read_bytes()==content and not list(tmp_path.glob('*.tail-*'))

def test_repair_symlink_and_permissions(tmp_path):
    path=journal_file(tmp_path);link=tmp_path/'link';link.symlink_to(path)
    with pytest.raises(OSError):repair.repair(link,True)
    path.chmod(0o644)
    with pytest.raises(r.InvalidData):repair.repair(path,True)

def test_startup_provenance_survives_disk_edit(tmp_path,monkeypatch):
    source=journal.ROOT/'app/services/typesafe_service.py';target=tmp_path/'app/services/typesafe_service.py';target.parent.mkdir(parents=True);target.write_text(source.read_text())
    modulepath=tmp_path/'app/core/decision_log.py';modulepath.parent.mkdir();modulepath.write_text(Path(journal.__file__).read_text())
    monkeypatch.setattr(journal.subprocess,'check_output',lambda command,**kw:'a'*40 if 'rev-parse' in command else '')
    spec=importlib.util.spec_from_file_location('isolated_journal',modulepath);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    original=module.STARTUP_PROMPT_VERSION
    target.write_text('invalid changed source');monkeypatch.setenv('JEV_APP_COMMIT','b'*40)
    module.prompt_version.cache_clear();module.app_commit.cache_clear()
    result=module.record_for({'status':'unavailable'},[],50,1,2)
    assert result['prompt_version']==original and result['app_commit']=='a'*40

@pytest.mark.asyncio
async def test_append_runs_off_loop_and_error_correlation(monkeypatch,caplog):
    inputs(monkeypatch,'success');entered=threading.Event();release=threading.Event()
    def append(record):
        entered.set()
        if not release.wait(2):raise AssertionError('Loop did not release writer')
        raise PermissionError('UNIQUE_APPEND_EXCEPTION_SECRET')
    monkeypatch.setattr(journal,'append_record',append)
    task=asyncio.create_task(analyze())
    try:
        for _ in range(100):
            if entered.is_set():break
            await asyncio.sleep(.001)
        assert entered.is_set() and not task.done()
    finally:release.set()
    response=await task;data=response.json()
    assert response.status_code==200 and data['decision_logged'] is False
    assert data['correlation_id'] in caplog.text and data['log_error_category']=='permission'
    assert 'UNIQUE_APPEND_EXCEPTION_SECRET' not in response.text+caplog.text


def test_quarantine_directory_fsync_precedes_truncate(tmp_path,monkeypatch):
    path=journal_file(tmp_path);path.write_bytes(path.read_bytes()+b'{"cut":');before=path.read_bytes()
    real=repair.os.fsync;calls=[]
    def fail_directory(fd):
        calls.append(fd)
        if len(calls)==2:raise OSError('directory flush failed')
        real(fd)
    monkeypatch.setattr(repair.os,'fsync',fail_directory)
    with pytest.raises(OSError):repair.repair(path,True)
    assert path.read_bytes()==before
    assert len(list(tmp_path.glob('*.tail-*')))==1

def test_repair_cli_neutral_failure(tmp_path,capsys):
    path=tmp_path/'journal';path.write_text('SECRET_SENTINEL');path.chmod(0o600)
    assert repair.main(['--log',str(path),'--apply'])==78
    assert 'SECRET_SENTINEL' not in str(capsys.readouterr())
