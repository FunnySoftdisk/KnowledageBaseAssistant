import { test } from 'node:test';
import { strict as assert } from 'node:assert';

import { decodeSseFrame, parseSseStream, type SseFrame } from '../src/renderer/shared/api/sse.ts';

function frames(text: string): SseFrame[] {
  return parseSseStream(text);
}

test('parses a projection frame', () => {
  const parsed = frames('id: 1\nevent: TASK_CREATED\ndata: {"status":"QUEUED"}\n\n');
  assert.deepEqual(parsed, [
    { kind: 'event', id: '1', event: 'TASK_CREATED', data: '{"status":"QUEUED"}' },
  ]);
});

test('parses a terminal frame', () => {
  const parsed = frames('event: terminal\ndata: {"status":"COMPLETED","display":"SUCCESS"}\n\n');
  assert.deepEqual(parsed, [
    { kind: 'event', id: null, event: 'terminal', data: '{"status":"COMPLETED","display":"SUCCESS"}' },
  ]);
});

test('parses a heartbeat comment', () => {
  assert.deepEqual(frames(': ping\n\n'), [{ kind: 'comment', text: ' ping' }]);
});

test('parses a retry field', () => {
  assert.deepEqual(frames('retry: 1000\n\n'), [{ kind: 'retry', ms: 1000 }]);
});

test('normalizes CRLF and joins multi-line data', () => {
  const parsed = frames('id: 2\r\nevent: PLAN_ACTIVATED\r\ndata: first\r\ndata: second\r\n\r\n');
  assert.deepEqual(parsed, [
    { kind: 'event', id: '2', event: 'PLAN_ACTIVATED', data: 'first\nsecond' },
  ]);
});

test('decodes a projection event', () => {
  const decoded = decodeSseFrame({
    kind: 'event',
    id: '1',
    event: 'TASK_CREATED',
    data: '{"status":"QUEUED","display_summary":"sum"}',
  });
  assert.deepEqual(decoded, {
    kind: 'projection',
    event: { sequence: 1, event_type: 'TASK_CREATED', payload: { status: 'QUEUED', display_summary: 'sum' } },
  });
});

test('projection drops unknown payload fields defensively', () => {
  const decoded = decodeSseFrame({
    kind: 'event',
    id: '3',
    event: 'PLAN_ACTIVATED',
    data: '{"status":"RUNNING","plan_version":2,"api_key":"leak"}',
  });
  assert.equal(decoded.kind, 'projection');
  if (decoded.kind === 'projection') {
    assert.deepEqual(decoded.event.payload, { status: 'RUNNING', plan_version: 2 });
  }
});

test('decodes a terminal frame to its display', () => {
  const decoded = decodeSseFrame({
    kind: 'event',
    id: null,
    event: 'terminal',
    data: '{"status":"FAILED","display":"FAILED"}',
  });
  assert.deepEqual(decoded, { kind: 'terminal', display: 'FAILED' });
});

test('decodes heartbeat and retry', () => {
  assert.deepEqual(decodeSseFrame({ kind: 'comment', text: ' ping' }), { kind: 'heartbeat' });
  assert.deepEqual(decodeSseFrame({ kind: 'retry', ms: 1000 }), { kind: 'retry', ms: 1000 });
});

test('ignores non-streamable and malformed frames', () => {
  assert.equal(
    decodeSseFrame({ kind: 'event', id: '1', event: 'OBSERVATION_SET_COMMITTED', data: '{}' }).kind,
    'ignore',
  );
  assert.equal(
    decodeSseFrame({ kind: 'event', id: '0', event: 'TASK_CREATED', data: '{}' }).kind,
    'ignore',
  );
  assert.equal(
    decodeSseFrame({ kind: 'event', id: '1', event: 'TASK_CREATED', data: 'not-json' }).kind,
    'ignore',
  );
  assert.equal(
    decodeSseFrame({ kind: 'event', id: null, event: 'terminal', data: '{"display":"BOGUS"}' }).kind,
    'ignore',
  );
});
