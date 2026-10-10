"""Real-interpreter stdlib probes; never import the backend in child processes."""
import json
from pathlib import Path
import subprocess
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
PROBE=r'''
import ast,hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]);temp=pathlib.Path(sys.argv[2]);temp.mkdir()
source=(root/'app/core/decision_log.py').read_text()
functions=[n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name in ('inputs_fingerprint','prompt_fingerprint')]
scope={'ast':ast,'hashlib':hashlib,'json':json}
exec(compile(ast.Module(body=functions,type_ignores=[]),'pure_functions','exec'),scope)
version=scope['inputs_fingerprint'](*[(root/p).read_text() for p in ('app/services/stats_service.py','app/services/twitter_service.py','app/core/config.py')])
prompt=scope['prompt_fingerprint']((root/'app/services/typesafe_service.py').read_text())
variants={}
original=[(root/p).read_text() for p in ('app/services/stats_service.py','app/services/twitter_service.py','app/core/config.py')]
for literal in ("b'x'",'...','1j','[0.0,1.0]',"'...'"):
 tree=ast.parse(original[0]);method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='process_tweets')
 method.body.append(ast.parse('literal_probe = '+literal).body[0]);changed=original.copy();changed[0]=ast.unparse(tree)
 variants[literal]=scope['inputs_fingerprint'](*changed)

sys.path.insert(0,str(root))
from scripts import research_core as r,settle_decisions as settle,evaluate_decisions as evaluate
rec={'schema_version':1,'record_id':'1'*32,'ts_utc':1800100,'prompt_version':prompt,'inputs_version':version,'status':'success','symbol':'ETH','pair':'ETH/USD','sample_size':50,'sample_count':50,'sample_hash':'a'*64,'decision':{'action':'BUY','trade_levels':{'stop_loss':90,'target_1':110,'target_2':120}}}
def write(path,rows):
 path.parent.mkdir(parents=True,exist_ok=True);path.write_text(''.join(r.canonical(x)+'\n' for x in rows))
write(temp/'log',[rec]);indices=sorted({t for delta in r.window_offsets('H72').values() for t in r.required(rec['ts_utc']+delta,r.H72)})
for pair in ('ETH/USD','XBT/USD'):
 write(r.candle_path(temp/'cache',pair),[{'open_ts':t,'o':100,'h':101,'l':99,'c':100,'v':1} for t in indices])
now=rec['ts_utc']+144*r.HOUR
settled=settle.settle(temp/'log',temp/'cache',temp/'out',temp/'audit',root/'research_protocol.json',now=now)
result=evaluate.evaluate(temp/'log',temp/'out',temp/'cache',root/'research_protocol.json',now,True)
print(json.dumps({'literal_fingerprints':variants,'python':sys.version.split()[0],'inputs_version':version,'prompt_version':prompt,'settle_ok_new':settled['ok_new'],'settle_excluded':settled['excluded_inputs_version'],'evaluate_n':result['n'],'evaluate_excluded':result['excluded_inputs_version']}))
'''


def test_real_runtime_fingerprint_and_joins(tmp_path):
    other=Path('/opt/homebrew/bin/python3')
    if not other.exists():pytest.skip('Optional second real interpreter not installed')
    results=[]
    for i,python in enumerate((sys.executable,str(other))):
        process=subprocess.run([python,'-B','-c',PROBE,str(ROOT),str(tmp_path/str(i))],capture_output=True,text=True,timeout=30)
        assert process.returncode==0,process.stderr
        results.append(json.loads(process.stdout))
    assert results[0]['inputs_version']==results[1]['inputs_version']
    assert results[0]['literal_fingerprints']==results[1]['literal_fingerprints']
    assert len(set(results[0]['literal_fingerprints'].values()))==5
    expected=json.loads((ROOT/'research_protocol.json').read_text())
    for result in results:
        assert result['inputs_version']==expected['inputs_version']
        assert result['prompt_version']==expected['prompt_version']
        assert result['settle_ok_new']==result['evaluate_n']==1
        assert result['settle_excluded']==result['evaluate_excluded']==0
