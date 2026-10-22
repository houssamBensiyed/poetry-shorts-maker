/* Run with: node n8n/check-workflows.js
 * Also supports the SpiderMonkey shell: js140 n8n/check-workflows.js
 * Executes exported Code nodes and control-flow paths without contacting services.
 * This is a focused test harness, not the n8n execution engine.
 */
const readText = typeof read === 'function' ? read : (path) => require('node:fs').readFileSync(path, 'utf8');
const log = typeof print === 'function' ? print : console.log;
const demo = JSON.parse(readText('n8n/poetry-shorts-visual-demo.json'));
const live = JSON.parse(readText('n8n/poetry-shorts-api-runner.json'));
function assert(condition, message) { if (!condition) throw new Error(message); }
function copy(value) { return JSON.parse(JSON.stringify(value)); }

function validateGraph(flow) {
  const names = new Set(flow.nodes.map(node => node.name));
  assert(names.size === flow.nodes.length, 'Duplicate node names');
  assert(new Set(flow.nodes.map(node => node.id)).size === flow.nodes.length, 'Duplicate node IDs');
  assert(flow.active === false, 'Workflows must import inactive');
  const executable = flow.nodes.filter(node => !node.type.endsWith('.stickyNote'));
  const outputs = {code: 1, manualTrigger: 1, if: 2, splitInBatches: 2, wait: 1, httpRequest: 1, stopAndError: 0};
  const reachable = new Set();
  function visit(name) {
    if (reachable.has(name)) return;
    reachable.add(name);
    for (const branch of flow.connections[name]?.main ?? []) for (const edge of branch) visit(edge.node);
  }
  for (const [name, connection] of Object.entries(flow.connections)) {
    assert(names.has(name), 'Unknown connection source ' + name);
    const source = flow.nodes.find(node => node.name === name);
    const kind = source.type.split('.').pop();
    assert(kind in outputs, 'Unsupported built-in node type ' + kind);
    assert(connection.main.length <= outputs[kind], 'Invalid output index on ' + name);
    for (const branch of connection.main) for (const edge of branch) {
      assert(names.has(edge.node) && edge.index === 0 && edge.type === 'main', 'Invalid edge');
    }
  }
  for (const node of executable) {
    assert(!node.credentials, 'No exported credentials expected');
    if (node.type.endsWith('.code')) new Function('$input', '$', '$runIndex', node.parameters.jsCode);
  }
  const trigger = executable.find(node => node.type.endsWith('.manualTrigger'));
  assert(Boolean(trigger), 'Missing manual trigger');
  visit(trigger.name);
  assert(executable.every(node => reachable.has(node.name)), 'Disconnected processing node');
}

function runCode(flow, name, items, upstream = {}, runIndex = 0) {
  const node = flow.nodes.find(node => node.name === name);
  const input = {all: () => copy(items), first: () => copy(items[0])};
  const select = name => ({first: () => ({json: copy(upstream[name])})});
  const result = new Function('$input', '$', '$runIndex', node.parameters.jsCode)(input, select, runIndex);
  assert(Array.isArray(result) && result.every(item => item.json && typeof item.json === 'object'), 'Invalid Code node output: ' + name);
  return result;
}

function runDemo(settingsOverride = {}) {
  const nodes = new Map(demo.nodes.map(node => [node.name, node]));
  const queue = [{name: 'Run Demo', items: [{json: {}}]}];
  const loops = new Map();
  let results = null;
  let steps = 0;
  while (queue.length) {
    assert(++steps < 500, 'Unbounded demo loop');
    const {name, items} = queue.shift();
    const node = nodes.get(name);
    const kind = node.type.split('.').pop();
    let outputs;
    if (kind === 'manualTrigger') outputs = [items];
    else if (kind === 'code') {
      const output = runCode(demo, name, items);
      if (name === 'Demo Settings') Object.assign(output[0].json.settings, settingsOverride);
      if (name === 'Demo Results - Inspect Here') results = output[0].json;
      outputs = [output];
    } else if (kind === 'if') {
      const expression = node.parameters.conditions.conditions[0].leftValue.slice(3, -2).trim();
      outputs = [[], []];
      for (const item of items) {
        const value = new Function('$json', 'return (' + expression + ');')(item.json);
        assert(typeof value === 'boolean', 'IF condition must be boolean');
        outputs[value ? 0 : 1].push(item);
      }
    } else if (kind === 'splitInBatches') {
      if (!loops.has(name)) loops.set(name, {remaining: copy(items), processed: []});
      else loops.get(name).processed.push(...copy(items));
      const context = loops.get(name);
      outputs = context.remaining.length ? [[], [context.remaining.shift()]] : [context.processed, []];
    } else throw new Error('Demo may not execute external nodes: ' + kind);
    outputs.forEach((output, index) => {
      if (!output.length) return;
      for (const edge of demo.connections[name]?.main[index] ?? []) queue.push({name: edge.node, items: copy(output)});
    });
  }
  assert(results && results.jobs.length === 4, 'Demo did not finish all jobs');
  return results;
}

validateGraph(demo);
validateGraph(live);
assert(demo.nodes.every(node => !['httpRequest', 'executeCommand'].includes(node.type.split('.').pop())), 'Demo must be offline');
const standard = runDemo();
assert(standard.completed_this_run === 2 && standard.failed_this_run === 1 && standard.skipped === 1, 'Unexpected default results');
const failed = standard.jobs.find(entry => entry.job.row === 102);
assert(failed.job.status === 'FAILED' && failed.retention.complete_output, 'Upload failure must preserve complete output');
assert(failed.checkpoints['upload.json'] && !failed.checkpoints['uploaded.json'], 'Upload checkpoints incorrect');
assert(standard.jobs.find(entry => entry.job.row === 104).outcome === 'completed', 'A failed job stopped the queue');
const success = standard.jobs.find(entry => entry.job.row === 101);
assert(JSON.stringify(success.plan.footage.map(interval => interval.start)) === '[0,0.4,5]', 'Short sentence changed footage');
assert(success.plan.footage[0].end === 0.4 && success.plan.footage[1].end === 5 && success.plan.footage[2].end === 10, 'Footage has gaps');
assert(success.plan.captions[1].end === 5 && success.plan.captions[2].end === 10, 'Caption silence persistence changed');
assert(success.caption_events[4].end === 3 && success.caption_events[8].end === 10, 'Caption reveal timing changed');
assert(success.selected_images.length === 3, 'Unselected images were buffered');
assert(success.job.output_url.endsWith('-demo.mp4') && success.rendered.file_exists === false, 'Demo result claims a real file');
assert(runDemo({injectFailure: false}).completed_this_run === 3, 'Failure toggle did not work');
for (const failureStage of ['download', 'transcribe', 'plan', 'images', 'captions', 'render', 'upload']) {
  const result = runDemo({failureStage});
  const failure = result.jobs.find(entry => entry.job.row === 102);
  assert(result.failed_this_run === 1 && result.completed_this_run === 2, 'Broken failure branch: ' + failureStage);
  assert(failure.error.stage === failureStage, 'Failure occurred at wrong stage');
  assert(Boolean(failure.checkpoints['failure.json'].output) === (failureStage === 'upload'), 'Wrong output retention: ' + failureStage);
}

// Test the real monitor with fake HTTP responses; never call the project API here.
const config = runCode(live, 'Connection Settings', [{json: {}}])[0].json;
for (const trigger of ['accepted', 'already_running']) {
  assert(runCode(live, 'Validate Start Response', [{json: {status: trigger}}])[0].json.trigger === trigger, 'Trigger handling');
}
const upstream = {'Connection Settings': config, 'Validate Start Response': {trigger: 'accepted'}};
function evaluate(status, runIndex = 0) {
  return runCode(live, 'Evaluate Processing Status', [{json: status}], upstream, runIndex)[0].json;
}
assert(evaluate({status: 'processing'}).continuePolling, 'Active worker should be polled');
assert(evaluate({status: 'processing'}, config.maxPolls - 1).outcome === 'monitor_timeout', 'Polling is unbounded');
assert(evaluate({status: 'idle', pending: 0, stage: 'Run finished', error: ''}).outcome === 'finished', 'Clean completion');
assert(evaluate({status: 'idle', pending: 0, error: 'Upload failed'}).outcome === 'finished_with_job_errors', 'Job failure hidden');
assert(evaluate({status: 'idle', pending: 1, error: ''}).outcome === 'incomplete_or_stopped', 'Remaining jobs hidden');
assert(evaluate({status: 'idle', pending: 0, stage: 'Stopped', error: ''}).outcome === 'incomplete_or_stopped', 'Cancellation hidden');
assert(evaluate({status: 'error', error: 'Missing credentials'}).outcome === 'run_error', 'Run failure hidden');
assert(evaluate({status: 'idle', stage: 'Ready', pending: 0, error: ''}).outcome === 'unconfirmed_idle', 'Worker restart mistaken for completed run');
let rejected = false;
try { evaluate({status: 'unexpected'}); } catch (_) { rejected = true; }
assert(rejected, 'Malformed status must be rejected');
const start = live.nodes.find(node => node.name === 'Start Processing - GET run');
assert(start.retryOnFail === false, 'Starting a batch must not auto-retry');
log('PASS: workflow graphs, offline demo, 7 failure stages, continuation, timeline/captions, and bounded live polling.');
