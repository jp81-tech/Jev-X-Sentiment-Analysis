"""Offline cohort evaluation. No API calls, app settings, credentials or order execution."""
import argparse
import json
import math
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import research_core as r


def evaluate(log,settlements,cache_dir,protocol_path,now=None,preview=False):
    now=time.time() if now is None else now;r.need(r.finite(now) and now>=0)
    p,pid=r.protocol(protocol_path);records,record_conflicts=r.unique_records(log)
    index,bad=r.settlement_index(settlements)
    cohort=[x for x in records.values() if x['prompt_version']==p['prompt_version'] and x.get('inputs_version')==p['inputs_version'] and x['status']=='success' and r.ACTIONS[x['decision']['action']]]
    pairs={r.pair_name(x['pair']) for x in cohort}|{'XBT/USD'};cache={pair:r.candles(cache_dir,pair) for pair in pairs}
    rows=[];btc=[];h24=[];incomplete=0;conflicts=set(bad)
    for record in cohort:
        for horizon in ('H24','H72'):
            key=(record['record_id'],horizon,pid)
            if key in bad:continue
            row=index.get(key)
            if row is None:
                if horizon=='H72':incomplete+=1
                continue
            expected=r.settle_record(record,horizon,cache,pid,now,p['placebo_offsets_hours'])
            if expected['settle_status']!='OK':
                if horizon=='H72':incomplete+=1
                continue
            if r.canonical(row)!=r.canonical(expected):conflicts.add(key);continue
            main=row['windows']['main']
            if horizon=='H24':h24.append(dict(main,ts_utc=record['ts_utc'],btc=row['benchmark_self']));continue
            if row['benchmark_self']:btc.append(main['ret']);continue
            rows.append(dict(record_id=record['record_id'],symbol=record['symbol'],ts_utc=record['ts_utc'],
                             entry_ts=main['entry_ts'],alpha=main['alpha'],ret=main['ret'],fade=main['fade'],
                             pre72=row['windows']['pre72']['alpha'],post72=row['windows']['post72']['alpha'],alpha_beta_adj=main['alpha_beta_adj'],
                             first_hit=main['first_hit'],side='BUY' if r.ACTIONS[record['decision']['action']]>0 else 'SELL'))
    if not cohort:result={'verdict':'SAMPLE_TOO_SMALL','n':0,'days':0,'symbols':[],'blocks':0,'greedy_n':0,'greedy_blocks':0}
    else:result=r.evaluate_rows(rows,min(x['ts_utc'] for x in cohort),now,p,preview)
    result.update(protocol_id=pid,prompt_version=p['prompt_version'],inputs_version=p['inputs_version'],
                  excluded_inputs_version=sum(x.get('inputs_version')!=p['inputs_version'] for x in records.values()),incomplete=incomplete,
                  conflicts={'records':record_conflicts,'candles':sum(len(v[1]) for v in cache.values()),'settlements':len(conflicts)})
    # The preregistered gate suppresses outcome numbers unless preview is explicit.
    if result['verdict']!='SAMPLE_TOO_SMALL' or preview:
        anchor=min((x['ts_utc'] for x in cohort),default=now)
        nonbtc=[x for x in h24 if not x['btc']]
        result['descriptive_H24']={key:r.summary([x[key] for x in nonbtc],[math.floor((x['ts_utc']-anchor)/r.H72) for x in nonbtc]) for key in ('ret','alpha')}
        result['descriptive_H24']['btc_n']=sum(x['btc'] for x in h24)
        result['descriptive_BTC']={'n':len(btc),'mean_ret':sum(btc)/len(btc) if btc else None}
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ('log','settlements','candles'):parser.add_argument('--'+flag,required=True)
    parser.add_argument('--protocol',default=str(Path(__file__).resolve().parents[1]/'research_protocol.json'))
    parser.add_argument('--now',type=float);parser.add_argument('--preview',action='store_true')
    args=parser.parse_args(argv)
    try:
        result=evaluate(args.log,args.settlements,args.candles,args.protocol,args.now,args.preview)
        if args.preview:print('PREVIEW — NIE JEST WERDYKTEM')
        print(json.dumps(result,allow_nan=False,sort_keys=True))
        print('VERDICT | n | days');print(f"{result['verdict']} | {result['n']} | {result['days']}")
        return 0
    except (r.InvalidData,OSError,ValueError,TypeError):
        print(json.dumps({'status':'INVALID_INPUT','verdict':'BLOCKED'}));return 78
if __name__=='__main__':raise SystemExit(main())
