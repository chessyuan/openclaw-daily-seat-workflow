import assert from 'node:assert/strict';
import test from 'node:test';
import { dispatchBoundGesture } from '../src/android-bound-gesture.mjs';
const action={type:'tap',x:10,y:20};
test('single tap sends exactly once and preserves the bound frame',async()=>{
  let n=0;
  const r=await dispatchBoundGesture(async(c,p)=>{
    n++;assert.equal(c,'mobile.ui.act');assert.deepEqual(p,{snapshotId:'verified',action});
    return {payload:{code:'completed'}};
  },'tap-bound','verified',action);
  assert.equal(n,1);assert.equal(r.ok,true);assert.equal(r.completedGestures,1);
});
test('double tap second request is queued before first network round trip completes',async()=>{
  const calls=[];let resolveFirst;
  const first=new Promise(resolve=>{resolveFirst=resolve;});
  const r=await dispatchBoundGesture(async(c,p)=>{
    calls.push(p);
    if(calls.length===1)return first;
    resolveFirst({payload:{code:'completed'}});return {payload:{code:'completed'}};
  },'doubletap-bound','verified',action,async(ms)=>assert.equal(ms,90));
  assert.equal(r.ok,true);assert.equal(r.completedGestures,2);
  assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1]);
});
test('known stale before second dispatch does not send a second tap',async()=>{
  let n=0;
  const r=await dispatchBoundGesture(async()=>{n++;return {payload:{code:'target_stale'}};},
    'doubletap-bound','verified',action,async()=>{await new Promise(r=>setTimeout(r,0));});
  assert.equal(n,1);assert.equal(r.ok,false);assert.equal(r.completedGestures,0);
});
test('partial gesture is explicit, not an entirely rejected action',async()=>{
  let n=0;
  const r=await dispatchBoundGesture(async()=>({payload:{code:++n===1?'completed':'target_stale'}}),
    'doubletap-bound','verified',action,async()=>{});
  assert.equal(r.ok,false);assert.equal(r.completedGestures,1);assert.equal(r.result.code,'target_stale');
});
test('timeout is never hidden by a stale response',async()=>{
  let n=0,release;
  const first=new Promise(resolve=>release=resolve);
  const r=await dispatchBoundGesture(async()=>{
    if(++n===1)return first;
    release({payload:{code:'target_stale'}});throw new Error('private unknown error');
  },'doubletap-bound','verified',action,async()=>{});
  assert.equal(r.ok,false);assert.equal(r.result.code,'timed_out_outcome_unknown');
  assert.ok(!JSON.stringify(r).includes('private'));
});
test('first failed request prevents the delayed second tap',async()=>{
  let n=0;
  const r=await dispatchBoundGesture(async()=>{n++;throw new Error('unknown');},
    'doubletap-bound','verified',action,async()=>{await new Promise(r=>setTimeout(r,0));});
  assert.equal(n,1);assert.equal(r.ok,false);assert.equal(r.result.code,'timed_out_outcome_unknown');
});
