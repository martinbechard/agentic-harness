import test from 'node:test';
import assert from 'node:assert/strict';
import { Command } from '@langchain/langgraph';
import { buildWorkflow, parseDecision } from './workflow.mjs';
import { environment } from './client.mjs';

test('checkpointed assessment is not replayed when human answers', async () => {
  const calls=[];
  const graph=buildWorkflow(async prompt => {calls.push(prompt); return calls.length===1 ? {state:'input_required',text:'Which language?'} : {state:'completed',text:'Artifact'};});
  const config={configurable:{thread_id:'offline'}};
  const first=await graph.invoke({prompt:'Prepare greeting'},config);
  assert.equal(first.calls,1); assert.equal(first.__interrupt__.length,1);
  const last=await graph.invoke(new Command({resume:'French'}),config);
  assert.deepEqual(calls,['Prepare greeting','French']);
  assert.equal(last.calls,2); assert.equal(last.decision.state,'completed');
});
test('missing or invalid agent decision fails instead of advancing', () => {
  assert.throws(()=>parseDecision('done'));
  assert.throws(()=>parseDecision('{"state":"done","text":"yes"}'));
  assert.throws(()=>parseDecision('{"state":"completed","text":""}'));
  assert.equal(parseDecision('{"state":"input_required","text":"Language?"}').state,'input_required');
});
test('child environment strips billing and gateway overrides without global edits', () => {
  const env=environment({PATH:'/bin',HOME:'/tmp',OPENAI_API_KEY:'secret',CODEX_API_KEY:'secret',MODEL_PROVIDER:'gateway',DEFAULT_AUTH_REQUEST:'secret',CODEX_CONFIG:'unsafe',APP_SERVER_LOGS:'/secret'});
  assert.equal(env.OPENAI_API_KEY,undefined);assert.equal(env.CODEX_API_KEY,undefined);
  assert.equal(env.MODEL_PROVIDER,undefined); assert.equal(env.DEFAULT_AUTH_REQUEST,undefined);
  assert.equal(JSON.parse(env.CODEX_CONFIG).forced_login_method,'chatgpt');
  assert.equal(JSON.parse(env.CODEX_CONFIG)['features.memories'],false);
  assert.equal(env.HOME,'/tmp');
});

test('commentary cannot contaminate the final structured message', async()=>{
  const {appendMessage}=await import('./client.mjs');
  const text=new Map(), last=new Map();
  for(const [messageId,chunk] of [['commentary','I will inspect files.'],['final','{"state":"completed",'],['final','"text":"Ready"}']]) {
    appendMessage(text,last,'session',{messageId,content:{text:chunk}});
  }
  assert.equal(parseDecision(text.get('session')).text,'Ready');
});

test('review acceptance binds verdict, exact candidate, and substantive evidence', async()=>{
  const {validateReview,reviewRequest}=await import('./review.mjs');
  const candidate='a'.repeat(64);
  const result=fields=>JSON.stringify({verdict:'ACCEPT',candidate,evidence:'Native hash and file checks passed',...fields});
  assert.equal(validateReview(result({}),candidate).verdict,'ACCEPT');
  assert.throws(()=>validateReview(result({candidate:'b'.repeat(64)}),candidate));
  assert.throws(()=>validateReview(result({verdict:'REJECT'}),candidate));
  assert.throws(()=>validateReview(result({evidence:' '}),candidate));
  assert.throws(()=>validateReview(result({evidence:undefined}),candidate));
  assert.match(reviewRequest(true),/exactly 2 followed by a newline, the count, NOT person names/);
});
