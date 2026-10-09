"""Offline repair of only a truncated journal tail; dry-run unless --apply."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import research_core as r


def repair(path,apply=False):
    path=Path(path)
    fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        info=os.fstat(fd)
        r.need(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode)==0o600)
        with os.fdopen(os.dup(fd),'rb') as stream:data=stream.read()
        boundary=data.rfind(b'\n')+1
        prefix,tail=data[:boundary],data[boundary:]
        for line in prefix.splitlines():
            value=json.loads(line,parse_constant=r.reject_constant,object_pairs_hook=r.unique_keys)
            r.validate_record(value)
        result={'mode':'APPLY' if apply else 'DRY_RUN','status':'TRUNCATED_TAIL' if tail else 'UNCHANGED','tail_bytes':len(tail),'prefix_records':len(prefix.splitlines())}
        if not tail:return result
        # Repair only recognizable truncation, never arbitrary malformed tail data.
        text=tail.decode('utf-8')
        r.need(text.lstrip().startswith('{'))
        try:json.loads(text,parse_constant=r.reject_constant,object_pairs_hook=r.unique_keys)
        except json.JSONDecodeError as error:
            remainder=text[error.pos:].strip()
            partial_literal=error.msg=='Expecting value' and remainder in ('t','tr','tru','f','fa','fal','fals','n','nu','nul')
            r.need(error.pos>=len(text.rstrip()) or error.msg.startswith('Unterminated string') or partial_literal)
        else:raise r.InvalidData('Tail is not truncated JSON')
        if not apply:return result
        quarantine=path.with_name(path.name+'.tail-'+uuid.uuid4().hex)
        qfd=os.open(quarantine,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            remaining=memoryview(tail)
            while remaining:
                written=os.write(qfd,remaining)
                if written<=0:raise OSError('No write progress')
                remaining=remaining[written:]
            os.fsync(qfd)
        finally:os.close(qfd)
        parent=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(parent)
        finally:os.close(parent)
        # Quarantine content and directory entry are durable before modification.
        os.ftruncate(fd,boundary);os.fsync(fd)
        result.update(status='REPAIRED',quarantine=quarantine.name)
        return result
    finally:os.close(fd)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log',required=True);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args(argv)
    try:print(json.dumps(repair(args.log,args.apply)));return 0
    except Exception:print(json.dumps({'status':'BLOCKED','code':'INVALID_JOURNAL_OR_IO_ERROR'}));return 78
if __name__=='__main__':raise SystemExit(main())
