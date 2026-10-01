/** Workflow owns pause/resume; Codex owns interpretation and native tool execution. */
import { Annotation, StateGraph, START, END, MemorySaver, interrupt } from '@langchain/langgraph';

export function parseDecision(text) {
  const trimmed = text.trim().replace(/^```(?:json)?\s*/, '').replace(/\s*```$/, '');
  const value = JSON.parse(trimmed);
  if (!['input_required', 'completed'].includes(value.state) || typeof value.text !== 'string' || !value.text.trim()) {
    throw new Error('Agent did not return a valid application decision');
  }
  return value;
}

export function buildWorkflow(assess, checkpointer = new MemorySaver()) {
  const State = Annotation.Root({ prompt: Annotation(), decision: Annotation(), calls: Annotation() });
  return new StateGraph(State)
    .addNode('assess', async state => ({ decision: await assess(state.prompt), calls: (state.calls ?? 0) + 1 }))
    .addNode('ask', state => ({ prompt: interrupt({ question: state.decision.text }) }))
    .addEdge(START, 'assess')
    .addConditionalEdges('assess', state => state.decision.state === 'input_required' ? 'ask' : END)
    .addEdge('ask', 'assess')
    .compile({ checkpointer });
}
