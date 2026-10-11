const assert=require('node:assert/strict');
const a=require('../plugins/netease/adapter.js');
const grant={compatibility:{version:'3.1.42.123456',status:'unverified'}};
assert.throws(()=>a.authorizeClient('3.1.42'),/实验性安装/);
a.authorizeClient('3.1.42',grant);
assert.throws(()=>a.authorizeClient('3.1.43',grant),/实验性安装/);
assert.throws(()=>a.authorizeClient('3.1.42',{compatibility:{...grant.compatibility,status:'blocked'}}),/实验性安装/);
let reads=0;
const reader={state:state=>{reads++;return {...state,'async:lyric':{resourceTrackId:'new'}};}};
const state={playing:{resourceTrackId:'new',resourceName:'fixture',resourceDuration:60,playingState:2},
    'async:lyric':{resourceTrackId:'old',displayType:'default',lyricLines:[{time:1,lyric:'stale'}]}};
assert.equal(a.adaptiveLyrics(state,reader).adapter,'state');
assert.equal(reads,0,'A stale explicit lyric ID must never be relabelled');
assert.deepEqual(a.snapshot(state,1200,true,'3.1.42',grant).lyrics,[]);
const missing={...state,'async:lyric':{displayType:'default',lyricLines:[]}};
assert.equal(a.adaptiveLyrics(missing,reader).adapter,'cache');
assert.equal(reads,1);
const direct={...state,'async:lyric':{...state['async:lyric'],resourceTrackId:'new'}};
assert.equal(a.adaptiveLyrics(direct,reader).adapter,'state');
assert.equal(a.snapshot(direct,1200,true,'3.1.42',grant).position_ms,1200);
assert.throws(()=>a.snapshot({...state,playing:{...state.playing,resourceArtists:{}}},0,true,'3.1.42',grant),/接口/);
assert.throws(()=>a.snapshot({...state,playing:{...state.playing,resourceDuration:NaN}},0,true,'3.1.42',grant),/接口/);
console.log('Capability selection, opt-in family isolation and unchanged transport passed (fixtures only)');

const fs=require('node:fs'),vm=require('node:vm');
async function runtimeProbe(streams,config,brokenSocket=false){
    let loaded,dispatches=0,tick,clock=10000;
    const sent=[],entries=[];
    const playing={...state.playing,playId:'current'};
    const store={getState:()=>({...state,playing}),subscribe:()=>{},dispatch:()=>dispatches++};
    const player={__reactFiberFixture:{memoizedProps:{value:{store}}}};
    const anchor={parentElement:{querySelector:()=>entries[0]},insertAdjacentElement:(_where,item)=>entries.push(item)};
    class Socket {static OPEN=1;constructor(){this.readyState=0;Socket.current=this;}send(raw){if(brokenSocket)throw new Error('socket disappeared');sent.push(JSON.parse(raw));}close(){}}
    const element=()=>({style:{},isConnected:true,insertAdjacentElement:(_where,item)=>entries.push(item)});
    const context={FloatingLyricsAdapter:{...a,playbackStreams:()=>streams},
        plugin:{pluginPath:'fixture',onLoad:fn=>loaded=fn,onConfig:()=>{}},
        betterncm:{ncm:{getNCMVersion:()=> '3.1.42'},fs:{readFileText:async()=>JSON.stringify({token:'a'.repeat(64),...config})}},
        document:{body:{},querySelector:()=>player,querySelectorAll:()=>[anchor],createElement:element},
        MutationObserver:class{observe(){}},localStorage:{getItem:()=>null,setItem:()=>{}},WebSocket:Socket,
        setInterval:fn=>tick=fn,Date:{now:()=>clock},console:{warn:()=>{}},Promise,addEventListener:()=>{}};
    context.window=context;
    vm.runInNewContext(fs.readFileSync(require.resolve('../plugins/netease/index.js'),'utf8'),context);
    await loaded();
    if(Socket.current){Socket.current.readyState=1;Socket.current.onopen();}
    return {sent,entries,tick,dispatches,advance:()=>{clock+=2000;}};
}
(async()=>{
    const valid={audioPlayerPlayProgress$:{subscribe:()=>({unsubscribe(){}})},audioPlayerSeek$:{subscribe:()=>({unsubscribe(){}})}};
    const rejected=await runtimeProbe(valid,{});
    assert.equal(rejected.entries.length,0);
    const accepted=await runtimeProbe(valid,grant);
    assert.equal(accepted.entries.length,2);
    assert.equal(accepted.sent[0].client,'3.1.42');
    assert.equal(accepted.sent[0].kind,'snapshot');
    const missing=await runtimeProbe(null,grant);
    assert.equal(missing.sent[0].kind,'diagnostic');
    assert.equal(missing.sent[0].code,'playback_missing');
    assert.equal(missing.dispatches,0,'Missing playback interfaces cannot fetch lyrics or dispatch playback commands');
    missing.advance();missing.tick();
    assert.equal(missing.sent.length,2,'An unavailable interface must keep diagnostic heartbeats alive');
    await runtimeProbe(null,grant,true);
    console.log('Plugin runtime: new-family opt-in and missing-progress diagnostics passed (fixtures only)');
})().catch(error=>{console.error(error);process.exitCode=1;});
