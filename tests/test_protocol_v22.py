"""Offline v2.2 contract regressions; all records and candles synthetic."""
import copy
import json
from pathlib import Path
import pytest
from app.core import decision_log as journal
from scripts import research_core as r, collect_candles as collect, settle_decisions as settle, evaluate_decisions as evaluate
from test_research import record, row, write, cache_fixture, cohort, PROTOCOL, ROOT


def test_nonoverlap_indices():
    offsets=r.window_offsets('H72')
    assert offsets=={'main':0,'pre72':-259200,'post72':259200}
    sets=[set(r.required(1800100+d,r.H72)) for d in offsets.values()]
    assert all(len(s)==864 for s in sets)
    assert all(not sets[i]&sets[j] for i in range(3) for j in range(i))
    assert r.window_offsets('H24')=={'main':0}


def test_missing_pre_and_future_post_fail_closed(tmp_path):
    rec=record();cache=cache_fixture(tmp_path,rec)
    now=rec['ts_utc']+144*r.HOUR
    assert r.settle_record(rec,'H72',cache,'a'*64,now)['settle_status']=='OK'
    assert r.settle_record(rec,'H72',cache,'a'*64,now-300)['settle_status']=='INCOMPLETE'
    pre=r.required(rec['ts_utc']-r.H72,r.H72)
    for k in pre:cache['ETH/USD'][0].pop(k)
    assert r.settle_record(rec,'H72',cache,'a'*64,now)['settle_status']=='INCOMPLETE'
    h24=r.settle_record(rec,'H24',cache,'a'*64,now)
    assert h24['settle_status']=='OK' and h24['windows']['main']['beta_estimate']['reason']=='not_H72'


def test_track_empty_decisions_benchmark_first(tmp_path,monkeypatch,capsys):
    log=tmp_path/'log';write(log,[]);calls=[]
    def forbidden(*a):calls.append(a);raise AssertionError('NETWORK')
    result=collect.collect(log,tmp_path/'cache',now=1801000,track='ETH,SOL,XRP',fetcher=forbidden)
    assert result['pairs']==['XBT/USD','ETH/USD','SOL/USD','XRP/USD'] and result['requests']==0 and calls==[]
    assert not (tmp_path/'cache').exists()
    monkeypatch.setattr(collect,'fetch',forbidden)
    assert collect.main(['--log',str(log),'--candles',str(tmp_path/'cache'),'--track','ETH,SOL,XRP'])==0
    assert json.loads(capsys.readouterr().out)['requests']==0

@pytest.mark.parametrize('track',['eth','../ETH','ETH,',' ETH','ETH/USD','A'*16,'ETH,SO L'])
def test_track_validation(tmp_path,track):
    log=tmp_path/'log';write(log,[])
    with pytest.raises(r.InvalidData):collect.collect(log,tmp_path/'cache',track=track)


def sources():
    return [(ROOT/f).read_text() for f in ('app/services/stats_service.py','app/services/twitter_service.py','app/core/config.py')]

@pytest.mark.parametrize('file,old,new',[
    (0,'GREED_KEYWORDS = {','GREED_KEYWORDS = {"fresh_keyword",'),
    (0,'FEAR_KEYWORDS = {','FEAR_KEYWORDS = {"fresh_fear",'),
    (0,'polarity_score < 0.4','polarity_score < 0.5'),
    (0,'total_retweets * 2','total_retweets * 3'),
    (1,'min_faves:2','min_faves:3'),
    (1,'Ethereum','OtherCoin'),
    (2,'Field(24,','Field(25,')])
def test_inputs_semantic_mutations(file,old,new):
    original=sources();changed=original.copy();assert old in changed[file]
    changed[file]=changed[file].replace(old,new,1)
    assert journal.inputs_fingerprint(*changed)!=journal.inputs_fingerprint(*original)


def test_inputs_formatting_import_stability():
    import ast
    original=sources()
    changed=['# harmless comment\nimport collections\n'+ast.unparse(ast.parse(s))+'\n' for s in original]
    assert journal.inputs_fingerprint(*changed)==journal.inputs_fingerprint(*original)
    p,pid=r.protocol(PROTOCOL)
    assert p['inputs_version']==journal.STARTUP_INPUTS_VERSION==journal.inputs_fingerprint(*original)
    rec=journal.record_for({'status':'unavailable'},[],50,1,2)
    assert rec['inputs_version']==p['inputs_version']


def test_version_exclusions_and_old_settlement_not_reused(tmp_path):
    valid=record();missing=record(2,t=record()['ts_utc']-15*86400);missing.pop('inputs_version');other=record(3,t=record()['ts_utc']-10*86400);other['inputs_version']='b'*64
    log=tmp_path/'log';write(log,[valid,missing,other]);cache_fixture(tmp_path/'cache',valid)
    out=tmp_path/'out';audit=tmp_path/'audit';now=valid['ts_utc']+144*r.HOUR
    result=settle.settle(log,tmp_path/'cache',out,audit,PROTOCOL,now=now)
    assert result['excluded_inputs_version']==2 and result['ok_new']==1
    answer=evaluate.evaluate(log,out,tmp_path/'cache',PROTOCOL,now,True)
    assert answer['excluded_inputs_version']==2 and answer['n']==1
    # PV-D1: excluded records must not enter the cohort at all: t0 (days) and incomplete count come from the valid record only.
    assert answer['days']==6 and answer['incomplete']==0 and answer['blocks']==1
    assert set(answer['placebo_tost'])=={'pre72','post72'}
    stale=r.jsonl(out);stale[0].pop('inputs_version');write(out,stale)
    answer=evaluate.evaluate(log,out,tmp_path/'cache',PROTOCOL,now,True)
    assert answer['n']==0 and answer['conflicts']['settlements']==1


def test_needed_literal_quantile_boundary():
    # n100,g10,se.003: n289 uses df27 q.95=1.703288; n290 df28=1.701131.
    # sqrt(2.89)=1.7 fails; sqrt(2.90)=1.702939 passes. .975 requires410.
    result=r.needed_sample({'n':100,'g':10,'se':.003,'mean':.008},.003,.005)
    assert result['n_needed_no_edge']==290
    assert result['n_needed_edge_positive']==410
    assert result['n_needed_no_edge_reason'] is None

@pytest.mark.parametrize('changes,reason',[({'g':1,'se':None},'insufficient_blocks'),({'se':0},'degenerate_se'),({'n':10000001},'search_limit')])
def test_needed_unavailable(changes,reason):
    values={'n':100,'g':10,'se':.003,'mean':.008};values.update(changes)
    result=r.needed_sample(values,.003,.005)
    assert result['n_needed_no_edge'] is None and result['n_needed_no_edge_reason']==reason


def test_needed_small_gate_and_no_verdict_effect():
    rows=cohort();small=r.evaluate_rows(rows[:10],1000000,1000000+40*86400,r.PARAMS)
    assert small['verdict']=='SAMPLE_TOO_SMALL' and 'n_needed_no_edge' in small and 'statistics' not in small
    result=r.needed_sample({'n':100,'g':10,'se':.003,'mean':.005},.003,.005)
    assert result['n_needed_edge_positive'] is None and result['n_needed_edge_positive_reason']=='mean_not_above_cost'
    baseline=r.evaluate_rows(rows,1000000,1000000+40*86400,r.PARAMS)
    for i,x in enumerate(rows):x['alpha_beta_adj']=(-1)**i*1000.0  # PV-D3: alternating sign, huge variance: any verdict derived from it would not be EDGE_POSITIVE
    after=r.evaluate_rows(rows,1000000,1000000+40*86400,r.PARAMS)
    assert baseline['verdict']==after['verdict']=='EDGE_POSITIVE'
    assert after['alpha_beta_adj']['n']==40 and after['alpha_beta_adj']['se']>0


def test_beta_one_point_five_and_signed_adjustment(tmp_path):
    rec=record();cache=cache_fixture(tmp_path,rec);indices=r.required(rec['ts_utc']-r.H72,r.H72)
    asset=100.;btc=100.
    for i,ts in enumerate(indices):
        if i:
            change=(i%7-3)*.0002;btc*=1+change;asset*=1+1.5*change
        cache['ETH/USD'][0][ts]=row(ts,asset);cache['XBT/USD'][0][ts]=row(ts,btc)
    estimate=r.beta_estimate(cache['ETH/USD'][0],cache['XBT/USD'][0],indices)
    assert estimate['beta']==pytest.approx(1.5,abs=1e-10) and estimate['n']==863
    main=r.required(rec['ts_utc'],r.H72)
    cache['ETH/USD'][0][main[-1]]=row(main[-1],110)
    cache['XBT/USD'][0][main[-1]]=row(main[-1],104)
    buy=r.settle_record(rec,'H72',cache,'a'*64,rec['ts_utc']+144*r.HOUR)
    assert buy['windows']['main']['alpha_beta_adj']==pytest.approx(.04,abs=1e-10)
    rec['decision']['action']='SELL'
    sell=r.settle_record(rec,'H72',cache,'a'*64,rec['ts_utc']+144*r.HOUR)
    assert sell['windows']['main']['alpha_beta_adj']==pytest.approx(-.04,abs=1e-10)
    flat={t:row(t) for t in indices}
    assert r.beta_estimate(flat,flat,indices)['reason']=='zero_benchmark_variance'
    assert r.beta_estimate(flat,flat,indices[:100])['reason']=='insufficient_observations'
