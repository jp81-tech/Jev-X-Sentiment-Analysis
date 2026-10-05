// Offline DOM contract test. No browser/network or external packages required.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const elements = new Map();
const copied = [];
function element(id) {
    if (!elements.has(id)) elements.set(id, {textContent:'',innerHTML:'',className:'',disabled:false,hidden:true,value:'BTC',
        classList:{add(){},remove(){},toggle(){}},listeners:{},addEventListener(event,fn){this.listeners[event]=fn;},
        querySelector(){return element('btn-text');}});
    return elements.get(id);
}
const context = vm.createContext({document:{getElementById:element,querySelectorAll:()=>[],addEventListener(){}},
    console:{error(){}},alert(){},setTimeout(){return 1;},clearTimeout(){},Date,Number,
    navigator:{clipboard:{writeText(text){copied.push(text);return Promise.resolve();}}},fetch:async()=>{throw Error('offline failure');}});
vm.runInContext(fs.readFileSync('app/static/js/app.js','utf8'),context);
const now = Date.now()/1000;
const data = {symbol:'BTC',status:'success',model_status:'ok',market:{status:'ok',price:.00001,rsi_14:0,request_started_at:now,freshness_basis:"source_timestamp",fetched_at:now,source_timestamp:now,valid_until:now+120},social:{status:'ok',fetched_at:now,valid_until:now+600,target_count:50},social_stats:{sample_size:50},decision:{action:'BUY',confidence_pct:42,trade_levels:{entry_range:[.00000995,.00001002],stop_loss:.00000962,target_1:.00001045,target_2:.00001085},action_probabilities:{STRONG_BUY:0,BUY:61,HOLD:35,TAKE_PROFIT:0,SELL:4,STRONG_SELL:0}}};
context.data = data;
const render = ()=> vm.runInContext('updateUI(data)',context);
function valid(){context.data=structuredClone(data);render();assert.equal(element('copy-levels-btn').disabled,false);assert.equal(element('confidence-val').textContent,'42%');assert(element('lvl-entry').textContent.includes('0.00000995'));assert.equal(element('ticker-rsi').textContent,0);}
function invalid(){assert.equal(element('copy-levels-btn').disabled,true);assert.equal(element('lvl-entry').textContent,'—');assert.equal(element('confidence-val').textContent,'—');assert.equal(vm.runInContext('currentDecision',context),null);}
let checks=0;
for (const mutate of [d=>{d.decision.is_mock=true;},d=>{d.market.is_fallback=true;},d=>{d.market.valid_until=1;},d=>{d.decision=null;d.status='unavailable';},d=>{d.social.status='partial';},d=>{d.is_twitter_mock=true;},d=>{d.market.source_timestamp=now-121;},d=>{d.social.valid_until=now-1;}]) {valid();mutate(context.data);render();invalid();checks++;}
valid();context.data.market.source_timestamp=null;context.data.market.freshness_basis="request_start";render();assert.equal(element('copy-levels-btn').disabled,false);checks++;
context.data.market.request_started_at=now-121;render();invalid();checks++;
valid();context.data.decision.action_probabilities=null;render();assert.equal(element('dist-bars-container').textContent,'Distribution unavailable');assert.equal(element('confidence-val').textContent,'42%');checks++;
for (const value of [null, NaN, Infinity, undefined]) {
    valid();context.data.market.change_24h_pct=value;context.data.market.volume_24h_usd=value;render();
    assert.equal(element('ticker-change').textContent,'—');assert.equal(element('ticker-vol').textContent,'—');checks++;
}
valid();context.data.market.change_24h_pct=0;context.data.market.volume_24h_usd=0;render();
assert.equal(element('ticker-change').textContent,'0%');assert.equal(element('ticker-vol').textContent,'$0.0M');checks++;
valid();vm.runInContext('setupEventListeners()',context);element('symbol-input').listeners.input();invalid();checks++;
async function raceHarness(source) {
    const nodes = new Map();
    const requests = [], runs = [], alerts = [];
    const ready = {};
    function node(id) {
        if (!nodes.has(id)) {
            const classes = new Set(id === 'loading-spinner' ? ['hidden'] : []);
            nodes.set(id, {textContent:'',innerHTML:'',className:'',disabled:false,hidden:true,
                value:id === 'sample-size-slider' ? '50' : 'BTC',listeners:{},
                classList:{add:c=>classes.add(c),remove:c=>classes.delete(c),contains:c=>classes.has(c),
                    toggle(c,on){if(on)classes.add(c);else classes.delete(c);}},
                addEventListener(event,fn){this.listeners[event]=fn;},querySelector(){return node('btn-text');}});
        }
        return nodes.get(id);
    }
    const sandbox = vm.createContext({document:{getElementById:node,querySelectorAll:()=>[],
            addEventListener(event,fn){ready[event]=fn;}},
        console:{error(){}},alert:message=>alerts.push(message),Date,Number,
        setTimeout(){return 1;},clearTimeout(){},navigator:{clipboard:{writeText(){throw Error('unexpected copy');}}},
        pendingRuns:runs,
        fetch(url,options){
            assert.equal(url,'/api/v1/analyze');
            let resolve,reject;
            const promise = new Promise((ok,no)=>{resolve=ok;reject=no;});
            requests.push({resolve,reject,body:JSON.parse(options.body)});
            return promise;
        }});
    vm.runInContext(source,sandbox);
    // Observe promises without replacing application logic; event listeners call the real function.
    vm.runInContext('const actualRunAnalysis = runAnalysis; runAnalysis = (...args) => { const p = actualRunAnalysis(...args); pendingRuns.push(p); return p; };',sandbox);
    ready.DOMContentLoaded();
    function startA(){node('analyze-btn').listeners.click();assert.equal(requests[0].body.symbol,'BTC');}
    function edit(){node('symbol-input').value='ETH';node('symbol-input').listeners.input();}
    function startB(){edit();node('symbol-input').listeners.keydown({key:'Enter'});assert.equal(requests[1].body.symbol,'ETH');}
    async function finish(index,error=false){
        if(error) requests[index].reject(new Error(`failure ${index}`));
        else {
            const payload = structuredClone(data);
            payload.symbol=requests[index].body.symbol;
            payload.decision.symbol=payload.symbol;
            requests[index].resolve({ok:true,json:async()=>payload});
        }
        await runs[index];
    }
    function loading(expected,label){
        assert.equal(node('analyze-btn').disabled,expected,`${label}: loading button`);
        assert.equal(node('loading-spinner').classList.contains('hidden'),!expected,`${label}: loading spinner`);
        assert.equal(node('btn-text').textContent,expected?'Ingesting & Analyzing...':'Analyze Asset',`${label}: loading text`);
    }
    function empty(){assert.equal(vm.runInContext('currentDecision',sandbox),null);assert.equal(node('copy-levels-btn').disabled,true);assert.equal(node('lvl-entry').textContent,'—');}
    function eth(){assert.equal(vm.runInContext('currentDecision.symbol',sandbox),'ETH');assert.equal(node('ticker-symbol').textContent,'ETH/USD');assert.equal(node('copy-levels-btn').disabled,false);}
    return {startA,startB,edit,finish,loading,empty,eth,alerts};
}

async function checkRaces(source) {
    let count=0;
    for (const order of ['A-first','B-first','A-error/B-pending']) {
        const h=await raceHarness(source);
        h.startA();h.loading(true,order);h.startB();h.loading(true,order);
        if(order==='B-first') {
            await h.finish(1);h.loading(false,order);h.eth();
            await h.finish(0);h.loading(false,order);h.eth();
        } else {
            await h.finish(0,order==='A-error/B-pending');h.loading(true,order);h.empty();
            await h.finish(1);h.loading(false,order);h.eth();
        }
        assert.deepEqual(h.alerts,[],`${order}: stale alert`);count++;
    }
    for(const error of [false,true]) {
        const h=await raceHarness(source);
        const label=`edit-without-B/A-${error?'error':'success'}`;
        h.startA();h.edit();h.empty();h.loading(true,label);
        await h.finish(0,error);h.loading(false,label);h.empty();
        assert.deepEqual(h.alerts,[],`${label}: stale alert`);count++;
    }
    const h=await raceHarness(source);
    h.startA();await h.finish(0,true);h.loading(false,'current-error');h.empty();
    assert.deepEqual(h.alerts,['Analysis Error: failure 0']);count++;
    return count;
}

async function main() {
    valid();await vm.runInContext('runAnalysis("ETH",50)',context);invalid();checks++;
    for (const pair of ['BTC/USD', undefined]) {
        valid();context.data.market.pair=pair;render();
        assert.equal(element('ticker-symbol').textContent,'BTC/USD');
        element('copy-levels-btn').listeners.click();
        assert(copied.at(-1).includes('Asset: BTC/USD\n'));
        assert(!copied.at(-1).includes('/USDT'));checks++;
    }
    const copiedBefore=copied.length;
    element('symbol-input').listeners.input();
    element('copy-levels-btn').listeners.click();
    assert.equal(copied.length,copiedBefore);
    assert.equal(element('ticker-symbol').textContent,'—');checks++;
    const source=fs.readFileSync('app/static/js/app.js','utf8');
    checks += await checkRaces(source);
    // Red control: restore the former unconditional finally only in memory.
    const guarded = `        if (fetchId === requestFetchId) {
            analyzeBtn.disabled = false;
            spinner.classList.add("hidden");
            btnText.textContent = "Analyze Asset";
        }`;
    assert(source.includes(guarded),'Mutation target must match current application source');
    const mutated=source.replace(guarded,`        analyzeBtn.disabled = false;
        spinner.classList.add("hidden");
        btnText.textContent = "Analyze Asset";`);
    await assert.rejects(()=>checkRaces(mutated),/A-first: loading button/);
    console.log('Mutation control: old finally rejected (A-first loading button)');
    console.log(`${checks} UI checks passed`);
}
main().catch(error=>{console.error(error);process.exitCode=1;});
