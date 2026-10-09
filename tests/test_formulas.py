"""Frozen numeric oracles: independently computed with integer/rational rounding.
Source aggregate fixtures: 02-A3-B2-real-data.txt (no raw posts/authors/journal).
"""
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from app.services.typesafe_service import TypeSafeService
from app.services.stats_service import StatsService
from app.api.v1 import analyze as api
from test_pipeline import model, market, social

# Literal prices/ticks/expected results, not calculated by the tested implementation.
EXPECTED = [[2490.5, 0.01, 'BUY', {'entry_range': [2478.05, 2495.48], 'stop_loss': 2395.86, 'target_1': 2602.57, 'target_2': 2702.19, 'stop_loss_pct': -3.800040152579803, 'target_1_pct': 4.499899618550492, 'target_2_pct': 8.499899618550492, 'risk_reward_ratio': 2.2367920540997464, 'tick_size': 0.01, 'method': 'fixed_percentage_heuristic'}], [2490.5, 0.01, 'SELL', {'entry_range': [2485.52, 2502.95], 'stop_loss': 2585.14, 'target_1': 2378.43, 'target_2': 2278.81, 'stop_loss_pct': 3.800040152579803, 'target_1_pct': -4.499899618550492, 'target_2_pct': -8.499899618550492, 'risk_reward_ratio': 2.2367920540997464, 'tick_size': 0.01, 'method': 'fixed_percentage_heuristic'}], [1.38084, 1e-05, 'BUY', {'entry_range': [1.37394, 1.3836], 'stop_loss': 1.32837, 'target_1': 1.44298, 'target_2': 1.49821, 'stop_loss_pct': -3.7998609542017903, 'target_1_pct': 4.500159323310449, 'target_2_pct': 8.499898612438805, 'risk_reward_ratio': 2.2368972746331237, 'tick_size': 1e-05, 'method': 'fixed_percentage_heuristic'}], [1.38084, 1e-05, 'SELL', {'entry_range': [1.37808, 1.38774], 'stop_loss': 1.43331, 'target_1': 1.3187, 'target_2': 1.26347, 'stop_loss_pct': 3.7998609542017903, 'target_1_pct': -4.500159323310449, 'target_2_pct': -8.499898612438805, 'risk_reward_ratio': 2.2368972746331237, 'tick_size': 1e-05, 'method': 'fixed_percentage_heuristic'}], [109.96, 0.01, 'BUY', {'entry_range': [109.41, 110.18], 'stop_loss': 105.78, 'target_1': 114.91, 'target_2': 119.31, 'stop_loss_pct': -3.8013823208439432, 'target_1_pct': 4.501636958894143, 'target_2_pct': 8.503092033466714, 'risk_reward_ratio': 2.236842105263158, 'tick_size': 0.01, 'method': 'fixed_percentage_heuristic'}], [109.96, 0.01, 'SELL', {'entry_range': [109.74, 110.51], 'stop_loss': 114.14, 'target_1': 105.01, 'target_2': 100.61, 'stop_loss_pct': 3.8013823208439432, 'target_1_pct': -4.501636958894143, 'target_2_pct': -8.503092033466714, 'risk_reward_ratio': 2.236842105263158, 'tick_size': 0.01, 'method': 'fixed_percentage_heuristic'}]]

@pytest.mark.asyncio
@pytest.mark.parametrize('price,tick,direction,expected',EXPECTED)
@pytest.mark.parametrize('strong',[False,True])
async def test_frozen_level_formulas(model,price,tick,direction,expected,strong):
    action=('STRONG_' if strong else '')+direction
    model.response.answers['trade_action'].choice=action
    result=await TypeSafeService('synthetic').evaluate_decision('ETH',market(price,tick),{'sample_size':50})
    assert model.calls==1 and result is not None
    actual=result['trade_levels'];assert set(actual)==set(expected)
    for key,value in expected.items():
        assert actual[key]==(value if isinstance(value,str) else pytest.approx(value,rel=1e-12,abs=1e-10)),key

@pytest.mark.asyncio
async def test_half_up_tie_and_mirror(model):
    result=await TypeSafeService('synthetic').evaluate_decision('ETH',market(100,1),{'sample_size':50})
    assert result['trade_levels']['entry_range']==[100,100]
    assert result['trade_levels']['target_1']==105 and result['trade_levels']['target_2']==109
    levels=[]
    for action in ['BUY','SELL']:
        model.response.answers['trade_action'].choice=action
        result=await TypeSafeService('synthetic').evaluate_decision('ETH',market(100,.001),{'sample_size':50})
        levels.append(result['trade_levels'])
    buy,sell=levels
    assert buy['entry_range'][0]+sell['entry_range'][1]==200
    assert buy['entry_range'][1]+sell['entry_range'][0]==200
    for key in ['stop_loss','target_1','target_2']:assert buy[key]+sell[key]==200
    for key in ['stop_loss_pct','target_1_pct','target_2_pct']:assert buy[key]==pytest.approx(-sell[key],abs=1e-12)

@pytest.mark.asyncio
@pytest.mark.parametrize('symbol,price,tick',[('ETH',2497.47,.01),('ETH',2490.5,.01),('SOL',109.96,.01),('XRP',1.38084,.00001)])
async def test_aggregate_market_fixtures_allow_one_model(model,symbol,price,tick):
    assert await TypeSafeService('synthetic').evaluate_decision(symbol,market(price,tick),{'sample_size':50}) is not None
    assert model.calls==1

@pytest.mark.asyncio
async def test_coarse_tick_prevents_model_and_journals_degraded(model,monkeypatch,caplog):
    service=TypeSafeService('synthetic')
    assert await service.evaluate_decision('ETH',market(20,5),{'sample_size':50}) is None
    assert model.calls==0 and 'category=levels_infeasible' in caplog.text
    monkeypatch.setattr(api,'typesafe_service',service)
    monkeypatch.setattr(api.market_service,'get_market_data',AsyncMock(return_value=market(20,5)))
    monkeypatch.setattr(api.twitter_service,'fetch_tweets',AsyncMock(return_value=social()))
    result=await api.analyze_asset(api.AnalyzeRequest(symbol='ETH',sample_size=50))
    assert result['status']=='degraded' and result['decision'] is None and result['decision_logged'] is True
    assert model.calls==0
    records=[json.loads(x) for x in Path(os.environ['JEV_DECISION_LOG']).read_text().splitlines()]
    assert len(records)==1 and records[0]['status']=='degraded' and records[0]['sample_count']==50 and records[0]['decision'] is None


def tweet(i,text='',author=None,likes=0,retweets=0,replies=0):
    return dict(id=str(i),text=text,author_username=author,likes=likes,retweets=retweets,replies=replies)

@pytest.mark.parametrize('greed,fear,score,label',[
    (3,7,-.4,'Extreme Panic'),(61,139,-.39,'Bearish / Fearful'),
    (89,111,-.11,'Bearish / Fearful'),(9,11,-.1,'Neutral / Mixed'),
    (11,9,.1,'Neutral / Mixed'),(111,89,.11,'Bullish / Optimistic'),
    (139,61,.39,'Bullish / Optimistic'),(7,3,.4,'Euphoric / Greedy'),
    (697,303,.39,'Bullish / Optimistic'),(349,151,.4,'Euphoric / Greedy'),
    (303,697,-.39,'Bearish / Fearful'),(151,349,-.4,'Extreme Panic'),
    (547,453,.09,'Neutral / Mixed'),(548,452,.1,'Neutral / Mixed'),(553,447,.11,'Bullish / Optimistic')])
def test_rounded_sentiment_thresholds(greed,fear,score,label):
    rows=[tweet(i,'buy') for i in range(greed)]+[tweet(greed+i,'sell') for i in range(fear)]
    result=StatsService.process_tweets(rows)
    assert result['greed_mentions']==greed and result['fear_mentions']==fear
    assert result['polarity_score']==score and result['sentiment_label']==label


def test_post_set_tokens_authors_weight_and_rounding():
    rows=[tweet(1,'#BULLISH bull bull $ETH BUY buy sell sell','Alice',1,2),
          tweet(2,'SELL','alice',2,0),tweet(3,'no keywords',None,0,1)]
    result=StatsService.process_tweets(rows)
    assert result['greed_mentions']==3 and result['fear_mentions']==2 and result['polarity_score']==.2
    assert result['unique_authors_count']==2 and result['author_diversity_pct']==66.7
    assert result['total_likes']==3 and result['total_retweets']==3 and result['avg_engagement']==3.0
    assert result['sentiment_label']=='Bullish / Optimistic'
    changed=[dict(row,replies=999999) for row in rows]
    assert StatsService.process_tweets(changed)==result
    missing=StatsService.process_tweets([{'id':'a','text':''},{'id':'b','text':''}])
    assert missing['unique_authors_count']==1 and missing['author_diversity_pct']==50.0


def test_empty_matches_zero_sentiment():
    empty=StatsService.process_tweets([]);zero=StatsService.process_tweets([tweet(1)])
    assert empty['sentiment_label']==zero['sentiment_label']=='Neutral / Mixed'
    assert empty['polarity_score']==0 and empty['sample_size']==0 and empty['stratified_sample']==[]


def test_stratified_fifty_and_overlap_dedup():
    rows=[tweet(i,likes=i) for i in range(60)]
    result=StatsService.process_tweets(rows)['stratified_sample']
    assert len(result)==50
    assert [r['likes'] for r in result]==list(range(59,34,-1))+list(range(25))
    assert [r['type'] for r in result]==['high_engagement']*25+['latest_breaking']*25
    overlap=StatsService.process_tweets([tweet(i,likes=60-i) for i in range(60)])['stratified_sample']
    assert len(overlap)==25 and all(r['type']=='high_engagement' for r in overlap)
    duplicates=StatsService.process_tweets([tweet('same',likes=1),tweet('same',likes=2)])['stratified_sample']
    assert len(duplicates)==1 and duplicates[0]['likes']==2
