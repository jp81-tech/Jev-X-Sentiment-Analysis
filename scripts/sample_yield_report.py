"""Offline descriptive sample-yield report; no application settings or provider calls."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.auto_analyze import REASONS, STATUSES, decode, matches, number


class InvalidLog(Exception):
    pass


def require(value):
    if not value:raise InvalidLog()


def read_records(path):
    seen={};records=[];duplicates=0
    with Path(path).open('rb') as stream:
        for line in stream:
            require(line.endswith(b'\n'))
            row=decode(line);require(isinstance(row,dict))
            rid=row.get('record_id');symbol=row.get('symbol');status=row.get('status');ts=row.get('ts_utc');count=row.get('sample_count');reason=row.get('reason')
            require(matches(rid,r'[0-9a-f]{32}') and matches(symbol,r'[A-Z0-9]{1,15}'))
            require(isinstance(status,str) and status in STATUSES and number(ts) and ts>=0)
            require(type(count) is int and 0<=count<=1000 and (reason is None or isinstance(reason,str)))
            stamp=datetime.fromtimestamp(ts,timezone.utc)
            digest=hashlib.sha256(json.dumps(row,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
            if rid in seen:
                require(seen[rid]==digest);duplicates+=1;continue
            seen[rid]=digest
            records.append({'symbol':symbol,'status':status,'reason':reason if reason in REASONS else None,'sample_count':count,'utc_date':stamp.date().isoformat(),'utc_hour':stamp.hour})
    return records,duplicates


def ratio(n,d):return {'numerator':n,'denominator':d,'share':n/d if d else None}


def summarize(records):
    n=len(records);distribution=Counter((r['status'],r['reason'],r['sample_count']) for r in records)
    end=[r for r in records if r['reason']=='end_of_results']
    short_end=[r['sample_count'] for r in end if r['sample_count']<50]
    short=[r['sample_count'] for r in records if r['sample_count']<50]
    return {'records':n,'status_counts':dict(sorted(Counter(r['status'] for r in records).items())),
            'reason_counts':dict(sorted(Counter(r['reason'] or 'unknown' for r in records).items())),
            'distribution':[{'status':status,'reason':reason,'sample_count':count,'records':frequency} for (status,reason,count),frequency in sorted(distribution.items(),key=lambda item:(item[0][0],item[0][1] or '',item[0][2]))],
            'end_of_results':ratio(len(end),n),'end_of_results_below_50':ratio(len(short_end),n),
            'below_50_among_end_of_results':ratio(len(short_end),len(end)),
            'end_of_results_below_50_samples':{'n':len(short_end),'median':statistics.median(short_end) if short_end else None,'min':min(short_end) if short_end else None},
            'below_50_samples':{'n':len(short),'median':statistics.median(short) if short else None,'min':min(short) if short else None},
            'provider_error':ratio(sum(r['reason']=='provider_error' for r in records),n),
            'payment_required':ratio(sum(r['reason']=='payment_required' for r in records),n),
            'provider_failures':ratio(sum(r['reason'] in {'provider_error','payment_required','http_error','malformed_response','transport_error'} for r in records),n)}


def grouped(records,keys):
    groups=defaultdict(list)
    for row in records:groups[tuple(row[k] for k in keys)].append(row)
    return [{**dict(zip(keys,key)),**summarize(rows)} for key,rows in sorted(groups.items())]


def report(path):
    records,duplicates=read_records(path)
    return {'status':'DESCRIPTIVE_ONLY' if records else 'EMPTY','duplicates_ignored':duplicates,'valid_unique_records':len(records),
            'overall':summarize(records),'by_symbol':grouped(records,['symbol']),
            'by_utc_hour':grouped(records,['utc_hour']),'by_utc_date_hour':grouped(records,['utc_date','utc_hour']),
            'by_symbol_utc_hour':grouped(records,['symbol','utc_hour'])}


def table(data):
    lines=['symbol | UTC hour | N | end_of_results <50 / N | median | min','--- | --- | --- | --- | --- | ---']
    for row in data['by_symbol_utc_hour']:
        short=row['end_of_results_below_50'];stats=row['end_of_results_below_50_samples']
        lines.append(f"{row['symbol']} | {row['utc_hour']:02d} | {row['records']} | {short['numerator']}/{short['denominator']} | {stats['median']} | {stats['min']}")
    return '\n'.join(lines)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--log',required=True);args=parser.parse_args(argv)
    try:
        result=report(args.log)
    except (OSError,ValueError,TypeError,OverflowError,InvalidLog):
        print(json.dumps({'status':'BLOCKED','code':'INVALID_LOG','valid_unique_records':None}));return 78
    print(json.dumps(result,allow_nan=False,sort_keys=True));print(table(result));return 0

if __name__=='__main__':raise SystemExit(main())
