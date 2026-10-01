/** Resume only an existing independent reviewer; never regenerate producer work. */
import {readFile,open,stat} from 'node:fs/promises';
import {dirname,resolve,join} from 'node:path';
import {createHash} from 'node:crypto';
import {CodexClient} from './client.mjs';
import {reviewRequest,validateReview} from './review.mjs';

const [originalArgument,outputArgument]=process.argv.slice(2);
if(!originalArgument||!outputArgument) throw new Error('Usage: node recover-review.mjs original-evidence.json NEW-recovery-evidence.json');
const originalPath=resolve(originalArgument), outputPath=resolve(outputArgument);
const originalBytes=await readFile(originalPath);
const original=JSON.parse(originalBytes);
if(!original.reviewer_session || original.reviewer_session===original.producer_session) throw new Error('No existing independent reviewer identity');
if(original.before_answer.sha256!==original.after_answer.sha256 || original.before_answer.mtimeMs!==original.after_answer.mtimeMs) throw new Error('Original run lacks unchanged preparation evidence');
const workspace=join(dirname(originalPath),'workspace');
const artifactPath=join(workspace,'greeting.md'), markerPath=join(workspace,'completed-work.txt');
if(resolve(original.artifact.path)!==artifactPath) throw new Error('Artifact path does not match original run');
const digest=bytes=>createHash('sha256').update(bytes).digest('hex');
const verify=async()=>{
  const artifact=await readFile(artifactPath),marker=await readFile(markerPath),info=await stat(markerPath);
  if(digest(artifact)!==original.artifact.sha256 || digest(marker)!==original.after_answer.sha256 || info.mtimeMs!==original.after_answer.mtimeMs || marker.toString()!=='2\n') throw new Error('Original candidate or preparation changed');
  return {artifact_sha256:digest(artifact),marker_sha256:digest(marker),marker_mtime_ms:info.mtimeMs};
};
const before=await verify();
const output=await open(outputPath,'wx'); // reserve unique evidence before any generation
const client=new CodexClient(workspace);
const evidence={kind:'review-only clarification recovery',original_evidence:originalPath,
  original_evidence_sha256:digest(originalBytes),producer_session:original.producer_session,
  reviewer_session:original.reviewer_session,before,started:new Date().toISOString()};
try {
  await client.start();
  // Native session persistence across processes; no LangGraph restart guarantee.
  await client.loadSession(original.reviewer_session,'read-only');
  evidence.review=await client.prompt(original.reviewer_session,reviewRequest(true));
  evidence.verdict=validateReview(evidence.review,original.artifact.sha256);
  evidence.after=await verify();
  evidence.status='completed';
} catch(error) {evidence.status='failed';evidence.error=String(error);process.exitCode=1;}
finally {
  try {await client.close();} catch(error) {evidence.status='failed';evidence.cleanup_error=String(error);process.exitCode=1;}
  Object.assign(evidence,{auth:client.auth,responses:client.responses,events:client.events,cleanup:client.cleanup,
    usage_coverage:'ACP native reported usage only; original run retained separately'});
  await output.writeFile(JSON.stringify(evidence,null,2)+'\n');await output.close();
  console.log(`Review recovery: ${evidence.status}; evidence: ${outputPath}`);
}
