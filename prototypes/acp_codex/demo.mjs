import { mkdir, writeFile, readFile, stat } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { Command } from '@langchain/langgraph';
import { CodexClient } from './client.mjs';
import { buildWorkflow, parseDecision } from './workflow.mjs';
import { reviewRequest, validateReview } from './review.mjs';

const root = resolve(process.argv[2] ?? fileURLToPath(new URL(`./runs/${Date.now()}`, import.meta.url)));
if (!process.argv[2]) await mkdir(fileURLToPath(new URL('./runs', import.meta.url)), {recursive:true});
const workspace = join(root, 'workspace');
await mkdir(root, {recursive:false}); // Existing runs/evidence are never overwritten.
await mkdir(workspace);
const skill = join(workspace, '.agents/skills/prototype-format');
await mkdir(skill, {recursive:true});
await writeFile(join(skill, 'SKILL.md'), '---\nname: prototype-format\ndescription: Format the prototype greeting artifact.\n---\nEnd the artifact with the literal line: Native skill observed: NORTHSTAR-ACP\n');
await writeFile(join(workspace,'names.csv'), 'name\nAda\nGrace\n');
const client = new CodexClient(workspace);
const versions={};
for(const name of ['@agentclientprotocol/codex-acp','@agentclientprotocol/sdk','@langchain/langgraph','@openai/codex']) {
  versions[name]=JSON.parse(await readFile(fileURLToPath(new URL(`./node_modules/${name}/package.json`,import.meta.url)),'utf8')).version;
}
const evidence = { versions, kind: 'live ACP prototype', run_id: randomUUID(), root, started: new Date().toISOString() };
const markerSnapshot = async () => {
  const file = join(workspace,'completed-work.txt');
  const bytes = await readFile(file); const info = await stat(file);
  return {sha256:createHash('sha256').update(bytes).digest('hex'), content:bytes.toString(), mtimeMs:info.mtimeMs};
};
const timeout = setTimeout(() => { client.close().catch(error=>{evidence.cleanup_error=String(error);}); }, 600_000);
try {
  await client.start();
  const session = await client.newSession();
  evidence.producer_session = session.sessionId;
  evidence.session_configuration = session.configOptions;
  const graph = buildWorkflow(async text => parseDecision(await client.prompt(session.sessionId, text)));
  const config = {configurable: {thread_id: evidence.run_id}};
  const request = 'Use native tools to read names.csv and record the number of names in completed-work.txt. ' +
    'This preparation must happen exactly once: append one line with that count; never repeat or rewrite it after clarification. ' +
    'Then create greeting.md greeting both people in my preferred language. My preferred language has not been supplied; do not guess it. ' +
    'Use $prototype-format for the artifact. Return only JSON {"state":"input_required" or "completed","text":"your question or completion summary"}. ' +
    'Ask for essential missing information and stop before creating the greeting. Work only in this workspace. No delegation needed.';
  const first = await graph.invoke({prompt:request},config);
  if (!first.__interrupt__?.length || first.calls !== 1) throw new Error('Agent did not genuinely request missing information');
  evidence.question = first.decision.text;
  evidence.before_answer = await markerSnapshot();
  console.log(`Agent question: ${first.decision.text}`);
  const answer = 'Use French. Continue the existing work; do not repeat the completed preparation. Return the same JSON decision format.';
  const last = await graph.invoke(new Command({resume:answer}),config);
  if (last.decision.state !== 'completed' || last.calls !== 2) throw new Error('Same-session continuation failed');
  evidence.after_answer = await markerSnapshot();
  if (JSON.stringify(evidence.before_answer) !== JSON.stringify(evidence.after_answer)) throw new Error('Completed preparation changed on resume');
  const artifact = await readFile(join(workspace,'greeting.md'),'utf8');
  if (!artifact.includes('Native skill observed: NORTHSTAR-ACP')) throw new Error('Native skill output missing');
  evidence.artifact = {path:join(workspace,'greeting.md'), sha256:createHash('sha256').update(artifact).digest('hex'), text:artifact};
  const reviewer = await client.newSession('read-only');
  if (reviewer.sessionId === session.sessionId) throw new Error('Review did not get fresh session');
  evidence.reviewer_session = reviewer.sessionId;
  evidence.review = await client.prompt(reviewer.sessionId, reviewRequest());
  validateReview(evidence.review,evidence.artifact.sha256);
  if (createHash('sha256').update(await readFile(join(workspace,'greeting.md'))).digest('hex') !== evidence.artifact.sha256) throw new Error('Artifact changed during review');
  evidence.status = 'completed';
} catch (error) {
  evidence.status = 'failed'; evidence.error = String(error); process.exitCode = 1;
} finally {
  clearTimeout(timeout);
  await client.close();
  Object.assign(evidence, {auth:client.auth, initialize:client.initialize, responses:client.responses,
    events:client.events, cleanup:client.cleanup, usage_coverage:'ACP native reported usage only; no complete billing claim'});
  await writeFile(join(root,'evidence.json'),JSON.stringify(evidence,null,2)+'\n');
  console.log(`Result: ${evidence.status}; evidence: ${join(root,'evidence.json')}`);
}
