"""Offline deterministic settlement; never fetches data or loads application settings."""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import research_core as r


def settle(log,cache_dir,output,audit,protocol_path,horizon='H72',now=None,dry_run=False):
    now=time.time() if now is None else now;r.need(r.finite(now) and now>=0)
    r.need(len({Path(x).resolve() for x in (log,output,audit)})==3)
    p,pid=r.protocol(protocol_path);records,conflicts=r.unique_records(log)
    existing,bad=r.settlement_index(output)
    eligible=[x for x in records.values() if x['prompt_version']==p['prompt_version'] and x['status']=='success' and r.ACTIONS[x['decision']['action']]]
    pairs={r.pair_name(x['pair']) for x in eligible}|{'XBT/USD'}
    cache={pair:r.candles(cache_dir,pair) for pair in pairs}
    known_audit={r.canonical(x) for x in r.jsonl(audit)}
    results=[];incomplete=[];incomplete_total=0;settlement_conflicts=set(bad)
    for record in sorted(eligible,key=lambda x:x['record_id']):
        row=r.settle_record(record,horizon,cache,pid,now);key=(record['record_id'],horizon,pid)
        if row['settle_status']!='OK':
            incomplete_total+=1
            if r.canonical(row) not in known_audit:
                incomplete.append(row);known_audit.add(r.canonical(row))
            continue
        if key in existing:
            if r.canonical(existing[key])!=r.canonical(row):settlement_conflicts.add(key)
            continue
        if key not in bad:results.append(row)
    if not dry_run:
        r.append_jsonl(output,results)
        r.append_jsonl(audit,incomplete)
    return {'mode':'DRY_RUN' if dry_run else 'OFFLINE','ok_new':len(results),'incomplete':incomplete_total,'audit_new':len(incomplete),
            'conflicts':{'records':conflicts,'candles':sum(len(v[1]) for v in cache.values()),'settlements':len(settlement_conflicts)},'protocol_id':pid}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log',required=True);parser.add_argument('--candles',required=True)
    parser.add_argument('--output',required=True);parser.add_argument('--audit',required=True)
    parser.add_argument('--protocol',default=str(Path(__file__).resolve().parents[1]/'research_protocol.json'))
    parser.add_argument('--horizon',choices=['H24','H72'],default='H72');parser.add_argument('--now',type=float)
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args(argv)
    try:
        result=settle(args.log,args.candles,args.output,args.audit,args.protocol,args.horizon,args.now,args.dry_run)
        print(json.dumps(result,allow_nan=False));return 0
    except (r.InvalidData,OSError,ValueError,TypeError):
        print(json.dumps({'status':'INVALID_INPUT','verdict':'BLOCKED'}));return 78
if __name__=='__main__':raise SystemExit(main())
