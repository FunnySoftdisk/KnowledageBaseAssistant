import { test } from 'node:test';
import { strict as assert } from 'node:assert';

import {
  cancelTask,
  createBrowserResultFetcher,
  createBrowserSnapshotFetcher,
  createTask,
  fetchEventSource,
  login,
  SseStreamDecoder,
} from '../src/renderer/browserAdapter.ts';
import { buildCreateTaskBody } from '../src/renderer/shared/types/createTask.ts';
import type { EventSourceHandlers } from '../src/renderer/shared/api/taskEventClient.ts';

type Fetcher = typeof globalThis.fetch;

function sseResponse(chunks: string[], status = 200): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(stream, { status, headers: { 'Content-Type': 'text/event-stream' } });
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function makeHandlers() {
  const messages: unknown[] = [];
  let opened = 0;
  let closed = 0;
  const handlers: EventSourceHandlers = {
    onOpen: () => {
      opened += 1;
    },
    onMessage: (frame) => {
      messages.push(frame);
    },
    onClose: () => {
      closed += 1;
    },
  };
  return { handlers, messages, opened: () => opened, closed: () => closed };
}

async function flush(): Promise<void> {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

test('SseStreamDecoder splits frames across chunk boundaries', () => {
  const decoder = new SseStreamDecoder();
  assert.deepEqual(decoder.push('id: 1\nevent: TASK_CREATED\n'), []);
  const frames = decoder.push('data: {"status":"QUEUED"}\n\nid: 2\nev');
  assert.deepEqual(frames, [
    { kind: 'event', id: '1', event: 'TASK_CREATED', data: '{"status":"QUEUED"}' },
  ]);
  const rest = decoder.push('ent: PLAN_ACTIVATED\ndata: {}\n\n');
  assert.deepEqual(rest, [{ kind: 'event', id: '2', event: 'PLAN_ACTIVATED', data: '{}' }]);
});

test('SseStreamDecoder flushes trailing frame without terminator', () => {
  const decoder = new SseStreamDecoder();
  decoder.push('event: terminal\ndata: {"status":"COMPLETED","display":"SUCCESS"}');
  assert.deepEqual(decoder.flush(), [
    { kind: 'event', id: null, event: 'terminal', data: '{"status":"COMPLETED","display":"SUCCESS"}' },
  ]);
});

test('fetchEventSource sends Last-Event-ID header and delivers frames', async () => {
  let capturedUrl = '';
  let capturedInit: RequestInit | undefined;
  const fetcher: Fetcher = async (url, init) => {
    capturedUrl = String(url);
    capturedInit = init;
    return sseResponse(['id: 1\nevent: TASK_CREATED\ndata: {"status":"QUEUED"}\n\n']);
  };

  const h = makeHandlers();
  const connection = fetchEventSource('http://x/api/v1/tasks/t/events', '7', h.handlers, {
    fetch: fetcher,
  });
  await flush();

  assert.equal(capturedUrl, 'http://x/api/v1/tasks/t/events');
  assert.equal((capturedInit?.headers as Record<string, string>)['Last-Event-ID'], '7');
  assert.equal(h.opened(), 1);
  assert.equal(h.messages.length, 1);
  assert.equal(h.closed(), 1); // 有界流自然结束 → onClose。
  connection.close();
});

test('close() aborts without firing onClose', async () => {
  let aborted = false;
  const fetcher: Fetcher = async (_url, init) => {
    init?.signal?.addEventListener('abort', () => {
      aborted = true;
    });
    // 永不结束的流：挂起。
    return new Response(new ReadableStream<Uint8Array>({ start() {} }), { status: 200 });
  };

  const h = makeHandlers();
  const connection = fetchEventSource('http://x/events', null, h.handlers, { fetch: fetcher });
  await flush();
  assert.equal(h.opened(), 1);

  connection.close();
  await flush();
  assert.equal(aborted, true);
  assert.equal(h.closed(), 0);
});

test('non-OK response fires onClose without onOpen', async () => {
  const fetcher = (async () => new Response('', { status: 500 })) as typeof globalThis.fetch;
  const h = makeHandlers();
  fetchEventSource('http://x/events', null, h.handlers, { fetch: fetcher });
  await flush();
  assert.equal(h.opened(), 0);
  assert.equal(h.closed(), 1);
});

test('createBrowserSnapshotFetcher maps detail and validates', async () => {
  const fetcher = (async () =>
    jsonResponse({ status: 'RUNNING', last_event_sequence: 5 })) as typeof globalThis.fetch;
  assert.deepEqual(await createBrowserSnapshotFetcher('http://x', fetcher)('t'), {
    status: 'RUNNING',
    lastEventSequence: 5,
  });
});

test('createBrowserSnapshotFetcher rejects invalid status', async () => {
  const fetcher = (async () =>
    jsonResponse({ status: 'BOGUS', last_event_sequence: 0 })) as typeof globalThis.fetch;
  await assert.rejects(
    createBrowserSnapshotFetcher('http://x', fetcher)('t'),
    /TASK_SNAPSHOT_STATUS_INVALID/,
  );
});

test('createBrowserSnapshotFetcher rejects invalid sequence', async () => {
  const fetcher = (async () =>
    jsonResponse({ status: 'RUNNING', last_event_sequence: -1 })) as typeof globalThis.fetch;
  await assert.rejects(
    createBrowserSnapshotFetcher('http://x', fetcher)('t'),
    /TASK_SNAPSHOT_SEQUENCE_INVALID/,
  );
});

test('createBrowserResultFetcher returns parsed JSON', async () => {
  const fetcher = (async () => jsonResponse({ answerStatus: 'COMPLETE' })) as typeof globalThis.fetch;
  assert.deepEqual(await createBrowserResultFetcher('http://x', fetcher)('t'), {
    answerStatus: 'COMPLETE',
  });
});

test('fetchEventSource sends Authorization header when token provided', async () => {
  let capturedInit: RequestInit | undefined;
  const fetcher: Fetcher = async (_url, init) => {
    capturedInit = init;
    return sseResponse(['id: 1\nevent: TASK_CREATED\ndata: {"status":"QUEUED"}\n\n']);
  };
  const h = makeHandlers();
  const connection = fetchEventSource('http://x/events', null, h.handlers, {
    fetch: fetcher,
    token: 'tok',
  });
  await flush();
  const headers = capturedInit?.headers as Record<string, string>;
  assert.equal(headers['Authorization'], 'Bearer tok');
  connection.close();
});

test('createBrowserSnapshotFetcher sends Authorization header when token provided', async () => {
  let capturedInit: RequestInit | undefined;
  const fetcher: Fetcher = async (_url, init) => {
    capturedInit = init;
    return jsonResponse({ status: 'RUNNING', last_event_sequence: 0 });
  };
  await createBrowserSnapshotFetcher('http://x', fetcher, 'tok')('t');
  const headers = capturedInit?.headers as Record<string, string>;
  assert.equal(headers['Authorization'], 'Bearer tok');
});

test('login posts credentials and returns LoginResponse', async () => {
  let capturedInit: RequestInit | undefined;
  const fetcher: Fetcher = async (_url, init) => {
    capturedInit = init;
    return jsonResponse({
      access_token: 'tok',
      token_type: 'bearer',
      expires_in_seconds: 3600,
      user_id: 'u1',
      organization_id: 'o1',
      display_name: 'Demo',
      must_change_password: false,
    });
  };
  const response = await login('http://x', 'alice', 'pw', fetcher);
  assert.equal(response.access_token, 'tok');
  assert.equal(capturedInit?.method, 'POST');
  assert.deepEqual(JSON.parse(String(capturedInit?.body)), { username: 'alice', password: 'pw' });
});

test('login throws on non-OK response', async () => {
  const fetcher = (async () => new Response('', { status: 401 })) as typeof globalThis.fetch;
  await assert.rejects(login('http://x', 'alice', 'pw', fetcher), /LOGIN_HTTP_401/);
});

test('createTask sends auth + idempotency headers and returns TaskCreateResponse', async () => {
  let capturedUrl = '';
  let capturedInit: RequestInit | undefined;
  const fetcher: Fetcher = async (url, init) => {
    capturedUrl = String(url);
    capturedInit = init;
    return jsonResponse({
      task_id: 't1',
      input_id: 'i1',
      status: 'QUEUED',
      deadline_at: '2026-10-09T00:00:00Z',
      active_plan_version: null,
    });
  };
  const body = buildCreateTaskBody('00000000-0000-0000-0000-000000000001', '查一下');
  const created = await createTask('http://x', 'tok', body, 'idem-1', fetcher);
  assert.equal(created.task_id, 't1');
  assert.equal(capturedUrl, 'http://x/api/v1/tasks');
  assert.equal(capturedInit?.method, 'POST');
  const headers = capturedInit?.headers as Record<string, string>;
  assert.equal(headers['Authorization'], 'Bearer tok');
  assert.equal(headers['Idempotency-Key'], 'idem-1');
  assert.deepEqual(JSON.parse(String(capturedInit?.body)), body);
});

test('createTask throws on non-OK response', async () => {
  const fetcher = (async () => new Response('', { status: 400 })) as typeof globalThis.fetch;
  const body = buildCreateTaskBody('00000000-0000-0000-0000-000000000001', 'q');
  await assert.rejects(createTask('http://x', 'tok', body, 'k', fetcher), /CREATE_TASK_HTTP_400/);
});

test('cancelTask sends auth header and returns TaskCancelResponse', async () => {
  let capturedUrl = '';
  let capturedInit: RequestInit | undefined;
  const fetcher: Fetcher = async (url, init) => {
    capturedUrl = String(url);
    capturedInit = init;
    return jsonResponse({ task_id: 't1', status: 'CANCEL_REQUESTED' });
  };
  const result = await cancelTask('http://x', 'tok', 't1', fetcher);
  assert.equal(result.task_id, 't1');
  assert.equal(result.status, 'CANCEL_REQUESTED');
  assert.equal(capturedUrl, 'http://x/api/v1/tasks/t1/cancel');
  assert.equal(capturedInit?.method, 'POST');
  const headers = capturedInit?.headers as Record<string, string>;
  assert.equal(headers['Authorization'], 'Bearer tok');
});

test('cancelTask throws on non-OK response', async () => {
  const fetcher = (async () => new Response('', { status: 409 })) as typeof globalThis.fetch;
  await assert.rejects(cancelTask('http://x', 'tok', 't1', fetcher), /CANCEL_TASK_HTTP_409/);
});

test('cancelTask rejects invalid status', async () => {
  const fetcher = (async () =>
    jsonResponse({ task_id: 't1', status: 'BOGUS' })) as typeof globalThis.fetch;
  await assert.rejects(cancelTask('http://x', 'tok', 't1', fetcher), /CANCEL_TASK_STATUS_INVALID/);
});
