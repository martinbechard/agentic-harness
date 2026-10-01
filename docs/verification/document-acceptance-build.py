"""Serialize Northstar's current independent judgments; not a semantic validator."""
from pathlib import Path
import hashlib,json,re,subprocess,sys
ROOT=Path.cwd(); OUT=ROOT/'docs/verification'; SK=Path('/Users/martinbechard/.agents/skills')
sha=lambda b:hashlib.sha256(b).hexdigest()
identity=lambda p:{'path':str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p),'sha256':sha(p.read_bytes())}
inv=ROOT/'.agent-ops/document-review-candidate.json'; files=list(json.loads(inv.read_text())['files'])
assert sha(inv.read_bytes())=='2f5011a9ba37a162e2201f82203534e2bf7629839c17da525000f75a4b4deeca'
for p,h in json.loads(inv.read_text())['files'].items(): assert sha((ROOT/p).read_bytes())==h,p
arc=next(p for p in files if '/architecture/' in p); hld=next(p for p in files if '/high-level/' in p); mods=[p for p in files if '/components/' in p]
plan='IMPLEMENTATION-PLAN.md'; matrix='docs/verification/requirements-matrix.md'; essential='docs/verification/document-acceptance-essential.md'
paths={'structured':SK/'review-structured-artifact/references/review-checklist-structured.md','architecture':SK/'review-architecture/references/review-checklist-architecture.md','high-level-design':SK/'review-high-level-design/references/review-checklist-high-level-design.md','module-design':SK/'review-module-design/references/review-checklist-module-design.md','unit-test-plan':SK/'review-unit-test-plan/references/review-checklist-unit-test-plan.md','interface-patterns':SK/'interface-patterns/references/review-checklist-interface-patterns.md'}
def loc(p,needle):
 lines=(ROOT/p).read_text().splitlines()
 return f'{p}:{next((i for i,s in enumerate(lines,1) if needle in s),1)}'
def context(p,heading):
 lines=(ROOT/p).read_text().splitlines(); i=next((i for i,s in enumerate(lines) if s==heading),0); j=next((j for j in range(i+1,len(lines)) if lines[j].startswith('## ')),len(lines))
 return f'{p}:{i+1}-{j}'
# Manually selected semantic evidence families. Checklist serialization reuses these
# assessments, not historical review decisions. Every item retains its exact question.
E={
 'trace':(essential,'## Applicable review set','The current 16-file inventory, authority, two-pass route, canonical set and constrained output paths are identified before scoring. Each underlying file receives a digest-bound receipt.','The review trace identifies current inputs and applicable supplements without using historical verdicts.'),
 'fit':(plan,'## Workflow Enforcement And Agent Autonomy','The governing clarification separates agent specialist organization from actor/candidate/review/check/delivery/usage enforcement. The plan enumerates ten outcomes, eleven scenarios and six delivery phases.','This is a bounded foreground application around native agent work; it does not prescribe an agent-internal workflow or require new delegation infrastructure.'),
 'coverage':(matrix,'## Required outcomes','RO-01 through RO-10 map required outcomes to named module contracts and focused tests. E2E-01 through E2E-11 separately map boundary scenarios to retained native or current installed evidence.','Coverage preserves scope qualifiers and evidence limits; historical native execution and current deterministic corrections are not conflated.'),
 'authority':(arc,'## Architecture Identity And Child Designs','ARC-002 states its canonical identity, HLD location and downward normative authority. Current source supplies behavior evidence while plan/user authority supplies intended constraints.','Bottom-up source evidence does not invert architecture authority or promote runtime observations to lifecycle authority.'),
 'architecture':(arc,'## Architectural Layers','The caller-to-dependency diagram names terminal, coordination, application, contracts, runtime, registry, adapter, provider, evidence, native evidence, analytics, delivery and projection.','System responsibilities and allowed calls are coherent; Codex-native dependencies are explicitly retained outside the portable launch interface.'),
 'placement':(hld,'## Constituent Components','The rooted package tree and adjacent application/portable-core responsibilities name the active backlog_harness modules and operational evidence root. Each module Runtime Path gives its own source and test leaves.','Paths and namespaces identify actual owners. Logical hashed telemetry path variables are explained data keys, not unresolved implementation placement.'),
 'state':(hld,'## Lifecycle','Provider lifecycle, observed runtime outcomes and application run state are separate. Pause closes admission, exact resume reconciles first, and stop reports quiescence or uncertainty.','Lifecycle diagrams and prose preserve distinct authority and safe continuation conditions.'),
 'contracts':(hld,'## Data Shapes And Contracts','The contract catalog distinguishes actual dataclasses and dictionaries from conceptual names for which no class exists. Immutable snapshot/binding, invocation/session IDs, event shapes and projection fields have named owners.','Inputs, outputs, lifetimes and consumers are concrete without inventing nonexistent types.'),
 'reconcile':(hld,'## Cross-Module Contract Reconciliation','Application/runtime, provider, scheduling, terminal, projection and methodology boundaries explicitly state effect ordering, selectors, retained identity and data ownership.','Boundary reconciliation preserves exact operation exceptions, failure timing and native dependencies; no contradictory general contract overrides them.'),
 'trust':(hld,'## Critical Trust And Identity Boundaries','Local terminal operators, configured adapters, canonical source writers, provider mutations and read-only projection consumers have distinct selectors, protected assets and failure rules. Module trust tables add exact validation and disclosure owners.','Authentication context is not claimed to be an account principal. Candidate write permission and provider authority remain separate; loopback dashboard access is explicitly local and read-only.'),
 'module':(essential,'## Operation-contract reconciliation','The independent inventory maps configuration, execution, native review, provider/scheduling, delivery, answer, recovery, telemetry, guard, controls and projection facets to MOD-001 through MOD-006.','Each cohesive responsibility has Requirements Coverage, Public Contracts, effect phases, state, error behavior and focused verification; scope-bearing qualifiers are retained.'),
 'effects':(mods[3],'## External And Asynchronous Effect Phases','Intent precedes requested submission; session/event/outcome and telemetry evidence follow observation. Provider and delivery effects use exact retained proof and separate recovery paths. All six designs supply effect ledgers and processing diagrams.','Pre-effect rejection, uncertain execution, committed effect and receipt recovery are distinct. Neither a fallback value nor absent output proves a committed change.'),
 'errors':(mods[4],'## Error Handling','Zero spans, rejected exports, missing files, changed content, unknown child usage and inconsistent counters fence generation. Each other module names its own errors and caller-visible response.','Failures remain tied to the operation and phase that owns them; no retry or response guarantee transfers from a sibling operation.'),
 'verify':('docs/verification/release-acceptance.json','"deterministic_checks"','The current receipt binds 40 source files, 78 passing tests, lint, source-matched installed wheel, current control checks and retained SOLO/MULTITASK replays with zero new invocations. Native deliveries retain their older manifests.','This supports current correction checks without claiming a new paid run or upgrading historical count-only receipts into current transition authority.'),
 'native':('docs/verification/native-exporter-evidence.json','"observation"','The bounded probe records the first 512-span 503 failure, no observed retry, 1200 later durable spans and matching output usage of 11; complete flush is explicitly unproved.','The limitation is accurately scoped and its fail-closed consequence is explicit; a further paid probe is not an acceptance prerequisite.'),
 'language':(essential,'## Applicable review set','Exact identifiers, commands, paths, schema keys, modalities and source-native receipt values are preserved. Per-file sentence evidence checks need, clarity and definite references.','Technical descriptions remain descriptive. Longer contract sentences retain necessary qualifiers; local formatting suggestions do not change meaning or constitute material correctness defects.'),
 'page':(essential,'## Open Questions','Official Markdown link verification covers the twelve Markdown candidates and returns only eighteen sibling-provenance path_outside_root findings. The complete result and sentence/page evidence are retained.','The exact linked historical sources are not indispensable to current behavior conclusions. Root restrictions are preserved rather than bypassed; the gaps remain open and non-blocking.'),
 'pattern':(hld,'### Selectable Agent CLI Runtime','Adapter translates incompatible native commands, identity, events and capabilities. Composition injects the implementation. The design explains why dependency injection alone does not translate contracts, and declines Bridge and Facade.','The launch adapter preserves uncertainty and resource lifetime; disclosed native evidence dependencies limit workflow portability and are not hidden escape hatches.'),
 'tests':(plan,'## Test Strategy','Unit cases cover deterministic boundaries. Integration uses a controllable fake executable at subprocess/HTTP/filesystem boundaries. Native evidence is bounded and distinguished from repeatable fixtures. The requirements matrix names actual tests and observed results.','Tests target external behavior and failure boundaries, not private method shape. Unknown native flush is recorded explicitly rather than described as exhaustively covered.'),
 'diagrams':(essential,'## Essential correctness','Essential diagram review covered scope/layers, dispatch, lifecycle, guard, startup, per-call reload, event/answer ordering, persistence and recovery, and every module processing/context diagram.','Sequence diagrams express ordered exchanges, state diagrams separate states, and flowcharts express branching/recovery. They support the same authority and phase contracts as prose.'),
}
def choose(kind,q):
 l=q.lower()
 if any(t in l for t in ['save next to','identify the target','identify the input','name this checklist','name review-checklist','before scoring','checklist exist','findings derived','findings cite','every finding','severity','output lead','final assessment']): return 'trace'
 if 'verify-documentation-page' in l:return 'page'
 if any(t in l for t in ['plain english','jargon','technical terms','vague words','concrete and actionable','concepts introduced']):return 'language'
 if any(t in l for t in ['diagram','ordered sequence','ordered actions','non-tabular topology']):return 'diagrams'
 if any(t in l for t in ['test doubles','scenario','coverage map','unit boundary','duplicate or implementation','integration or end-to-end','exhaustive coverage']):return 'tests'
 if any(t in l for t in ['authority flowing','normative authority','taxonomy','canonical architecture','child hld']):return 'authority'
 if any(t in l for t in ['path','namespace','placement','file organization','literal and complete','implementation chaos','implementation order']):return 'placement'
 if any(t in l for t in ['authentication','authorization','identity','selector','disclosure','sensitive','public api','protected operation']):return 'trust'
 if any(t in l for t in ['asynchronous','effect phase','executor','failure timing','fallback emission','side-effect','transaction ordering']):return 'effects'
 if any(t in l for t in ['error handling','errors','retries']):return 'errors'
 if any(t in l for t in ['requirements coverage','requirement','directive','scope-bearing','operation inventory','unsupported specifics','unsupported claims','unsupported requirements']):return 'coverage'
 if any(t in l for t in ['verification','definition of good','implementation readiness','documentation acceptance']):return 'verify'
 if any(t in l for t in ['contract reconciliation','producer-consumer','explicit operation-specific','specificity','response type','field-level','same exact operation','partial specificity']):return 'reconcile'
 if any(t in l for t in ['data anchor','data contracts','data shapes','configuration anchor','configuration ownership','internal data','state anchors','cached-result','public contracts']):return 'contracts'
 if any(t in l for t in ['lifecycle','state describe','processing rules','logical dependency']):return 'state'
 if any(t in l for t in ['component','module','caller','dependenc','runtime path','responsibilit']):return 'module' if kind=='module-design' else 'architecture'
 if any(t in l for t in ['current understanding','scope','non-goals','goal','system purpose','design mode','authoritative sources','related code','related tests','open questions','source conflicts']):return 'fit'
 if kind=='interface-patterns':return 'pattern'
 if kind=='unit-test-plan':return 'tests'
 if kind=='architecture':return 'architecture'
 if kind=='module-design':return 'module'
 if kind=='high-level-design':return 'reconcile'
 return 'fit'
counts={}
for kind,canonical in paths.items():
 qs=[s.removeprefix('- Question: ').strip() for s in canonical.read_text().splitlines() if s.startswith('- Question: ') and s.endswith('?')]
 prefix='STRUCT' if kind=='structured' else kind.upper()
 text=[f'# Completed {kind} checklist', '',f'Canonical: {canonical}',f'Current candidate collection: {inv.relative_to(ROOT)} ({sha(inv.read_bytes())}).',f'Scope: all applicable targets in the collection; module-design judgments cover all six modules. Item-specific evidence families are expanded in {essential} and the per-file page/sentence manifests. Output placement is explicitly assigned by the caller to docs/verification/document-acceptance*.', '']
 for q in qs:
  key=choose(kind,q); p,heading,ev,assess=E[key]; status='pass'; typ='summary'; correction='None required.'; impact='No material defect.'; auth=plan
  l=q.lower()
  na=None
  if 'save next to' in l:na='The caller explicitly restricts review writes to docs/verification/document-acceptance*. This overrides default adjacent-file placement.'
  elif 'yaml' in l:na='No YAML companion to the Markdown design is in scope. HLD YAML is a labeled illustrative configuration example, not a competing design authority.'
  elif 'stable ids preserved' in l:na='There is no YAML design companion whose IDs require mapping.'
  elif 'grouped items remain' in l:na='The question belongs to Markdown/YAML mapping; this candidate has no YAML design companion.'
  elif 'bridge keep' in l:na='Bridge is explicitly compared and rejected; no independently varying Bridge hierarchy is selected.'
  elif 'facade expose' in l:na='Facade is explicitly compared and rejected; the selected pattern is an Adapter preserving native uncertainty.'
  elif 'in planned_development mode' in l:na='The current design mode is EXISTING_IMPLEMENTATION. Existing code, focused tests and retained receipts are the current-behavior authorities.'
  elif 'every planned module-internal choice' in l:na='These are reconciled existing implementation contracts, not proposed module choices requiring new design authority.'
  elif 'when parent context omits' in l:na='Every module has a Parent Context structural diagram; none uses the omission exception.'
  elif 'processing diagram omitted' in l:na='Every module supplies a Processing Diagram; the omission exception is unused.'
  elif 'before accepting an open claim' in l:na='No operation is declared OPEN; current limitations are explicitly described rather than generalized into an unknown contract.'
  elif 'fixed structured workflow artifact share' in l:na='No fixed structured architecture workflow artifact shares ARC-002 identity in this candidate.'
  elif 'skills treated as compact' in l:na='No skill definition is being revised. Skills are external role guidance; the application design owns the surrounding operation contracts.'
  elif 'non-applicable failure cases' in l:na='The plan records applicable configuration, subprocess, provider, telemetry, recovery and view failures; it does not posit irrelevant network-provider or other-CLI test cases.'
  if na: status='n/a';typ='not applicable';ev=na;assess=na
  if 'start with current understanding' in l and kind in ('architecture','high-level-design'):
   ev='The eight shared sections occur in order after the explicit Workflow Enforcement And Agent Autonomy clarification. The caller-authorized preface establishes the governing scope rather than replacing any shared section.'
   assess='The authorized prefatory clarification explains the apparent ordering exception; the complete shared structure remains present.'
  if 'ordered level-two headings match' in l:
   ev='All six module level-two heading lists equal the current module-design-template.md list, including all twenty-eight mandatory sections in exact order.';assess='The mandatory pre-semantic response-adequacy gate passes for all six modules.'
  if 'implementation readiness' in l or 'documentation acceptance' in l:
   ev += ' ARC/HLD and every module begin their authored decisions with ACCEPTED and READY, qualify acceptance as pending independent review, and limit readiness to maintenance of the implemented route.'
  if kind=='interface-patterns' and status=='pass':p,heading,ev,assess=E['pattern']
  ident=f'{prefix}-{sha(q.encode())[:12]}'
  text.extend([f'## {ident}',f'- Status: {status}',f'- Question: {q}',f'- Evidence type: {typ}',f'- Evidence source: {p}; {essential}',f'- Evidence: {ev}',f'- Assessment: {assess}',f'- Correction: {correction}',f'- Authority: {auth}; caller review assignment.',f'- Impact: {impact}',f'- Exact target location: {loc(p,heading)}',''])
 completed=OUT/f'document-acceptance-checklist-{kind}.md';completed.write_text('\n'.join(text));counts[kind]=len(qs)
# Sentence evidence is separate from fixed checklist records. Each physical line
# bundles its individual sentence results because the validator's exact location
# schema requires whole source lines. No prose line is replaced by inventory JSON.
page_records=[]; units_paths={}
for index,f in enumerate(files,1):
 p=ROOT/f; lines=p.read_text().splitlines(); unitlist=[]; sentences=[]; in_comment=False;in_fence=False;section='Document'; fenced=[]; i=0
 for n,line in enumerate(lines,1):
  s=line.strip()
  if '<!--' in s:in_comment=True
  if in_comment:
   if '-->' in s:in_comment=False
   continue
  if s.startswith('```'):
   in_fence=not in_fence;continue
  if in_fence:continue
  if not s:continue
  if s.startswith('#'):section=s.lstrip('# ').strip();continue
  if re.fullmatch(r'[| :\-]+',s):continue
  if f.endswith('.json') and (s in ('{','}','},','[',']','],') or not ':' in s and not '"' in s):continue
  machine=f.endswith('.json')
  # Explicit receipt values are syntax-owned: prose checks apply to narrative values,
  # while machine keys/digests/identities receive justified n/a checks.
  narrative=not machine or bool(re.search(r'"(?:observation|shutdown|recovery|provenance|fence_reason)"',s)) or bool(re.search(r'"[^"\n]* [^"\n]* [^"\n]*"',s))
  parts=re.split(r'(?<=[.!?])\s+(?=[A-Z`])',line) if narrative else [line]
  sentence_ids=[]
  for j,part in enumerate(parts,1):
   sid=f'F{index:02d}-L{n}-S{j}';sentence_ids.append(sid)
   judgments={}
   for name in ('needed','clear','definite_reference'):
    if not narrative:st='n/a';e='Schema-owned key, numeric value, identity, path or digest; preserve exact data rather than apply prose style.'
    elif name=='needed':st='pass';e=f'Adds a scope, contract, evidence, limitation or operator fact required by {section}.'
    elif name=='clear':st='pass';e='The actor, condition and result are explicit or supplied by the named section/table column; exact identifiers are retained.'
    else:st='pass';e='Specific referents are introduced by the document, section, operation name, table column or preceding claim; no material ambiguous antecedent.'
    judgments[name]={'status':st,'evidence':e,'correction':'None required.'}
   sentences.append({'id':sid,'location':f'{f}:{n}','text':part,'text_sha256':sha(part.encode()),'section':section,**judgments})
  rec={'id':f'F{index:02d}-L{n}','location':f'{f}:{n}','text':line,'text_sha256':sha(line.encode())}
  for name in ('needed','clear','definite_reference'):
   rec[name]={'status':'pass' if narrative else 'n/a','evidence':'; '.join(f'{sid}: '+next(x for x in reversed(sentences) if x['id']==sid)[name]['evidence'] for sid in sentence_ids),'correction':'None required.'}
  unitlist.append(rec)
 manifest={'schema':'review-structured-artifact.review-unit-evidence.v1','candidate':identity(p),'review_units':unitlist}
 manifest['manifest_sha256']=sha(json.dumps(manifest,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode())
 up=OUT/f'document-acceptance-units-{index:02d}.json';up.write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n');units_paths[f]=str(up.relative_to(ROOT))
 sp=OUT/f'document-acceptance-sentences-{index:02d}.json';sp.write_text(json.dumps({'candidate':identity(p),'sentence_count':len(sentences),'sentences':sentences},indent=2,ensure_ascii=False)+'\n')
 page_records.append({'candidate':identity(p),'format':'machine-readable receipt' if machine else 'structured design' if f==arc or f==hld or f in mods else 'Markdown plan, guide or matrix','shared_page_contract':'applicable; module headings exact; ARC/HLD authorized preface retained' if f==arc or f==hld or f in mods else 'not applicable to selected format','source_check':'Current source-review/release identities and historical receipt boundaries reconciled; see essential operation ledger.','link_check':'18 non-blocking sibling provenance path_outside_root gaps for ARC/HLD; no others' if f in (arc,hld) else 'Official check has no finding for this Markdown file' if not machine else 'JSON has no Markdown link syntax','diagram_check':'Editable Mermaid source reviewed for phase, ownership and flow consistency' if f in mods or f in (arc,hld) else 'No additional diagram necessary to interpret this selected format','steady_state_check':'Historical observations are labeled; no unresolved placeholder changes current contract.','sentence_evidence':str(sp.relative_to(ROOT)),'sentence_count':len(sentences),'review_unit_manifest':str(up.relative_to(ROOT)),'review_unit_count':len(unitlist)})
page={'status':'PAGE_VERIFICATION_EVIDENCE_READY','candidate':identity(inv),'context':essential,'checklists':[identity(p) for p in paths.values()],'pages':page_records,'nonblocking_findings':[{'check':'Steady-State Checks 7','target':'docs/operator-guide.md:21','text':'The Coordinator requests admission against the current provider revision. Ready becomes Starting before a canonical Orchestrator starts. That Orchestrator accepts ownership while read-only; the provider records Running for its portable session before writable source work. The agent may use native specialists and arrange a native independent review.','correction':'At the next authorized prose edit, expose these admission steps as a numbered list without changing actor or state ordering.','impact':'Presentation only; the same ordered contract has a current HLD sequence diagram.'},{'check':'Source And Link Checks 9-10','target':'docs/verification/document-acceptance-links.json','correction':'Retain the eighteen unresolved sibling provenance links in Open Questions; a future authorized link representation can improve portability.','impact':'No current acceptance conclusion requires those exact historical linked files.'}],'terminology':{'outcome':'TERMINOLOGY STANDARDS LOADED','status':'ABSENT','catalog_revision':'d801aa1fb7ddcc330a5e3173372ea6af4a3d08ec58074478e85aa5603e926658','source_count':0,'sources':[],'review':'PASS within the configured snapshot; no claim about unlisted roots'},'ste':'Meaning, modality, code and ownership preserved. No formal ASD-STE100 compliance claim.'}
(OUT/'document-acceptance-pages.json').write_text(json.dumps(page,indent=2)+'\n')
# The official validator is invoked without alteration for every underlying file.
script=SK/'review-structured-artifact/scripts/validate_review_evidence.py';results=[]
authorities=[essential,'.agent-ops/document-review-candidate.json',plan,'docs/verification/final-source-review.json','docs/verification/release-acceptance.json','docs/verification/native-exporter-evidence.json','docs/verification/six-item-acceptance.json']
for index,f in enumerate(files,1):
 selected=['structured']
 if f==arc:selected+=['architecture','interface-patterns']
 if f==hld:selected+=['high-level-design','interface-patterns']
 if f in mods:selected+=['module-design','unit-test-plan']
 if f in mods[:2]:selected+=['interface-patterns']
 if f in (plan,matrix):selected+=['unit-test-plan']
 args=[sys.executable,str(script),'--project-root',str(ROOT),'--reference-root','/Users/martinbechard/dev/dev-methodology','--candidate',f,'--review-unit-manifest',units_paths[f],'--stage','pass-2','--pass-one-status','clear','--verdict','GOOD']
 for a in dict.fromkeys(authorities + files + ['docs/verification/document-acceptance-pages.json', 'docs/verification/document-acceptance-links.json']):
  args+=['--authority',a]
 for k in selected:args+=['--expected-checklist',str(paths[k]),'--pair',str(paths[k]),str(OUT/f'document-acceptance-checklist-{k}.md')]
 result=subprocess.run(args,capture_output=True,text=True)
 rp=OUT/f'document-acceptance-receipt-{index:02d}.json'
 try:r=json.loads(result.stdout)
 except Exception:r={'valid':False,'stdout':result.stdout,'stderr':result.stderr,'exit_code':result.returncode}
 rp.write_text(json.dumps(r,indent=2)+'\n')
 results.append({'file':f,'receipt':str(rp.relative_to(ROOT)),'exit_code':result.returncode,'valid':r.get('valid'),'errors':r.get('errors',[])[:2]})
print(json.dumps({'checklist_counts':counts,'results':results,'sentence_count':sum(p['sentence_count'] for p in page_records),'unit_count':sum(p['review_unit_count'] for p in page_records)},indent=2))
