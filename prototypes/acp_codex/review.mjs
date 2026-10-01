/** Exact acceptance contract shared by first review and bounded clarification. */
export function reviewRequest(clarification = false) {
  return (clarification ? 'Clarification of the earlier review wording; the candidate is unchanged. The producer was asked to record the COUNT, not the names. Reassess the same candidate against these exact original requirements. ' : 'Independent review in this new session. ') +
    'Read greeting.md and completed-work.txt using native tools. Requirements: greeting.md must greet Ada and Grace in French and end with the literal line Native skill observed: NORTHSTAR-ACP. ' +
    'completed-work.txt must contain exactly 2 followed by a newline, the count, NOT person names. ' +
    'Compute the actual SHA256 of greeting.md yourself with a native tool and report that value as candidate; do not trust any supplied digest. ' +
    'Return JSON {"verdict":"ACCEPT" or "REJECT","candidate":"sha256","evidence":"specific findings"}. No edits.';
}

export function validateReview(text, expectedHash) {
  const review=JSON.parse(text.trim().replace(/^```(?:json)?\s*/,'').replace(/\s*```$/,''));
  if(review.verdict!=='ACCEPT' || review.candidate!==expectedHash || typeof review.evidence!=='string' || !review.evidence.trim()) {
    throw new Error('Independent review did not accept exact artifact with supporting evidence');
  }
  return review;
}
