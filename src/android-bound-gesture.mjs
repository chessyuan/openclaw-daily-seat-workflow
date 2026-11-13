// Two taps are one explicitly requested gesture, not an error retry. Keep the
// exact verified snapshot for both; never observe/rebind behind the caller.
export async function dispatchBoundGesture(call, name, snapshotId, action,
    pause = ms => new Promise(resolve => setTimeout(resolve, ms))) {
  const params = {snapshotId, action};
  const attempts = [];
  let firstState = null;
  const send = () => call('mobile.ui.act', params).then(
    response => ({result: response?.payload ?? response}),
    () => ({result: {code:'timed_out_outcome_unknown'}}));
  const first = send().then(value => { firstState = value; return value; });
  attempts.push(first);
  if (name === 'doubletap-bound') {
    // Do not add an entire mobile-network round trip to the double-tap gap.
    // Android's existing invoke mutex serializes these requests on the phone.
    await pause(90);
    if (!firstState || firstState.result?.code === 'completed') attempts.push(send());
  }
  const results = (await Promise.all(attempts)).map(x => x.result);
  const completedGestures = results.filter(r => r?.code === 'completed').length;
  const required = name === 'doubletap-bound' ? 2 : 1;
  const unknown = results.find(r => !r?.code || r.code === 'timed_out_outcome_unknown');
  const failure = results.find(r => r?.code !== 'completed');
  const result = unknown ?? failure ?? results.at(-1);
  return {ok: completedGestures === required, result, completedGestures,
    gestureResults: results.map(r => r?.code ?? 'unknown')};
}
