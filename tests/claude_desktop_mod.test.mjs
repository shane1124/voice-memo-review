import assert from 'node:assert/strict';
import test from 'node:test';
import { register } from '../integrations/claude-desktop/hooks/register.ts';

function harness({ surface = 'desktop', analyzer = 'none', transcriber = 'command-asr' } = {}) {
  const hooks = new Map();
  const h = { analyzer, transcriber, jobs: [], calls: [], prompts: [], timers: [], toasts: [], drop: false };
  const $ = {
    session: { surfaces: async () => [surface] },
    command: { register: async () => {} },
    ui: { toast: async (text) => h.toasts.push(text) },
    mcp: {
      call: async (server, tool, args) => {
        assert.equal(server, 'voice-memo-review');
        h.calls.push(tool);
        let value;
        if (tool === 'vmr_status') value = { analyzer_auto_provider: h.analyzer, transcriber_provider: h.transcriber };
        else if (tool === 'vmr_list_recordings') {
          assert.equal(args.status, 'awaiting_analysis');
          value = h.listOverride ? await h.listOverride() : { recordings: h.jobs };
        } else assert.fail(`Unexpected direct MCP call: ${tool}`);
        return { content: [{ type: 'text', text: JSON.stringify(value) }], isError: false };
      },
    },
    clock: {
      every: (period, fn) => {
        assert.equal(period, 15_000);
        const timer = { fn, stopped: false, cancel() { this.stopped = true; } };
        h.timers.push(timer);
        return timer;
      },
    },
    prompt: { submit: async ({ text }) => {
      h.prompts.push(text);
      return h.drop ? { drop: 'test refusal' } : { text };
    } },
  };
  register((event, matcher, callback) => {
    if (typeof matcher === 'function') hooks.set(event, matcher);
    else hooks.set(`${event}:${matcher.command}`, callback);
  });
  h.fire = (hook, e = {}) => hooks.get(hook)($, e, async (input) => input);
  h.start = (kind = 'composer') => h.fire('command.run:vmr-desktop-start', { origin: { kind } });
  h.stop = () => h.fire('command.run:vmr-desktop-stop');
  return h;
}

const job = (recording_id, status = 'awaiting_analysis') => ({ recording_id, status, transcript_path: '/synthetic/transcript.md' });

// These tests use no real credentials, transcripts, or model calls.
test('installation is inert and only a user in Desktop can opt in', async () => {
  const h = harness();
  await h.fire('session.start');
  assert.deepEqual(h.calls, []);
  assert.deepEqual(h.timers, []);
  await h.start('plugin');
  assert.deepEqual(h.timers, []);
  const terminal = harness({ surface: 'terminal' });
  await terminal.start();
  assert.deepEqual(terminal.calls, []);
  await h.start();
  await h.start();
  assert.equal(h.timers.length, 1);
});

test('does not arm while a background analyzer or delegated ASR is selected', async () => {
  for (const options of [{ analyzer: 'claude-cli' }, { transcriber: 'antigravity-flash' }]) {
    const h = harness(options);
    await h.start();
    assert.deepEqual(h.timers, []);
  }
});

test('submits a single validated job per turn, never queued titles or path values', async () => {
  const h = harness();
  h.jobs = [job('job_next'), job('job_old'), job('job_\nnever send'), job('job_done', 'done')];
  await h.start();
  const tick = h.timers[0].fn;
  await h.fire('turn.start', { turnId: 'already-busy' });
  await tick();
  assert.equal(h.prompts.length, 0);
  await h.fire('turn.complete', { turnId: 'already-busy' });
  await tick();
  await tick();
  assert.equal(h.prompts.length, 1);
  assert.match(h.prompts[0], /"job_old"/);
  assert.doesNotMatch(h.prompts[0], /never send|job_done|\/synthetic/);
  await h.fire('turn.start', { turnId: 'desktop-1' });
  await h.fire('turn.complete', { turnId: 'desktop-1' });
  await tick();
  assert.match(h.prompts[1], /"job_next"/);
  await h.fire('turn.start', { turnId: 'desktop-2' });
  await h.fire('turn.complete', { turnId: 'desktop-2' });
  await tick();
  assert.equal(h.prompts.length, 2);
});

test('stopping during the local queue read cancels submission', async () => {
  const h = harness();
  let resolveList;
  h.listOverride = () => new Promise((resolve) => { resolveList = resolve; });
  await h.start();
  const active = h.timers[0].fn();
  await h.stop();
  resolveList({ recordings: [job('job_late')] });
  await active;
  assert.equal(h.timers[0].stopped, true);
  assert.equal(h.prompts.length, 0);
});

test('pauses instead of retrying a rejected submission or ignoring settings changes', async () => {
  for (const rejection of ['prompt', 'settings']) {
    const h = harness();
    h.jobs = [job('job_valid')];
    await h.start();
    if (rejection === 'prompt') h.drop = true;
    else h.analyzer = 'claude-cli';
    await h.timers[0].fn();
    assert.equal(h.timers[0].stopped, true);
    assert.equal(h.prompts.length, rejection === 'prompt' ? 1 : 0);
    assert.equal(h.toasts.length, 1);
  }
});
