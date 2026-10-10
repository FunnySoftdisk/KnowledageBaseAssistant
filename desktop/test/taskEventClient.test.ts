import { test } from 'node:test';
import { strict as assert } from 'node:assert';

import { TaskEventClient, type TaskEventUpdate } from '../src/renderer/shared/api/taskEventClient.ts';
import type { SseFrame } from '../src/renderer/shared/api/sse.ts';
import type { TaskStatus } from '../src/renderer/shared/types/task.ts';

const TASK_ID = '00000000-0000-0000-0000-000000000001';
const CENTER = () => 0.5;

interface OpenRecord {
  url: string;
  lastEventId: string | null;
  handlers: {
    onOpen: () => void;
    onMessage: (frame: SseFrame) => void;
    onClose: () => void;
  };
  closed: boolean;
}

function makeEventSourceHarness() {
  const opens: OpenRecord[] = [];
  return {
    opens,
    openEventSource(
      url: string,
      lastEventId: string | null,
      handlers: OpenRecord['handlers'],
    ) {
      const record: OpenRecord = { url, lastEventId, handlers, closed: false };
      opens.push(record);
      return {
        close() {
          record.closed = true;
        },
      };
    },
  };
}

function makeScheduler() {
  const scheduled: { fn: () => void; delayMs: number; cancelled: boolean }[] = [];
  return {
    scheduled,
    schedule(fn: () => void, delayMs: number) {
      const entry = { fn, delayMs, cancelled: false };
      scheduled.push(entry);
      return {
        cancel() {
          entry.cancelled = true;
        },
      };
    },
    run(index: number) {
      const entry = scheduled[index];
      if (entry && !entry.cancelled) entry.fn();
    },
  };
}

async function flush(): Promise<void> {
  await new Promise((resolve) => setImmediate(resolve));
}

function projection(id: number, status?: TaskStatus): SseFrame {
  const data = status === undefined ? '{}' : JSON.stringify({ status });
  return { kind: 'event', id: String(id), event: 'TASK_CREATED', data };
}

interface Harness {
  client: TaskEventClient;
  events: TaskEventUpdate[];
  es: ReturnType<typeof makeEventSourceHarness>;
  scheduler: ReturnType<typeof makeScheduler>;
}

function makeHarness(overrides: {
  snapshot?: { status: TaskStatus; lastEventSequence: number };
  result?: unknown;
} = {}): Harness {
  const es = makeEventSourceHarness();
  const scheduler = makeScheduler();
  const events: TaskEventUpdate[] = [];
  const client = new TaskEventClient(TASK_ID, {
    baseUrl: 'http://localhost:8000',
    openEventSource: es.openEventSource,
    fetchSnapshot: async () => ({
      status: overrides.snapshot?.status ?? 'RUNNING',
      lastEventSequence: overrides.snapshot?.lastEventSequence ?? 0,
    }),
    fetchResult: async () => overrides.result ?? { answer: 'ok' },
    schedule: scheduler.schedule,
    random: CENTER,
  });
  client.subscribe((update) => events.push(update));
  return { client, events, es, scheduler };
}

test('opens with null Last-Event-ID on first connect', () => {
  const h = makeHarness();
  h.client.connect();
  assert.equal(h.es.opens.length, 1);
  assert.equal(h.es.opens[0]?.lastEventId, null);
  assert.match(h.es.opens[0]?.url ?? '', /\/api\/v1\/tasks\/.+\/events$/);
});

test('reconnect carries Last-Event-ID and uses server retry cadence', () => {
  const h = makeHarness();
  h.client.connect();
  const first = h.es.opens[0]!;
  first.handlers.onOpen();
  first.handlers.onMessage(projection(1, 'QUEUED'));
  first.handlers.onMessage({ kind: 'retry', ms: 1000 });
  first.handlers.onClose();

  assert.equal(h.scheduler.scheduled.length, 1);
  assert.equal(h.scheduler.scheduled[0]?.delayMs, 1000); // 正常循环用服务端 retry。

  h.scheduler.run(0);
  assert.equal(h.es.opens.length, 2);
  assert.equal(h.es.opens[1]?.lastEventId, '1'); // 续读游标。
});

test('connection error backs off instead of using retry', () => {
  const h = makeHarness();
  h.client.connect();
  const first = h.es.opens[0]!;
  // 未收到 retry 帧即断开（网络异常）。
  first.handlers.onClose();
  assert.equal(h.scheduler.scheduled[0]?.delayMs, 1000); // attempt 0 → 1s（random=0.5 无抖动）。

  h.scheduler.run(0);
  const second = h.es.opens[1]!;
  second.handlers.onClose();
  assert.equal(h.scheduler.scheduled[1]?.delayMs, 2000); // attempt 1 → 2s。
});

test('successful open resets backoff attempt', () => {
  const h = makeHarness();
  h.client.connect();
  const first = h.es.opens[0]!;
  first.handlers.onClose(); // attempt → 1
  h.scheduler.run(0);
  const second = h.es.opens[1]!;
  second.handlers.onOpen(); // 复位 attempt = 0
  second.handlers.onClose();
  assert.equal(h.scheduler.scheduled[1]?.delayMs, 1000); // 又回到 1s。
});

test('sequence gap requests snapshot and reconciles', async () => {
  const h = makeHarness({
    snapshot: { status: 'RUNNING', lastEventSequence: 5 },
  });
  h.client.connect();
  const first = h.es.opens[0]!;
  first.handlers.onOpen();
  first.handlers.onMessage(projection(1, 'QUEUED'));
  first.handlers.onMessage(projection(4, 'RUNNING')); // 间隙（跳过 2、3）。

  assert.ok(h.events.some((u) => u.type === 'snapshot-requested'));
  await flush();
  assert.equal(h.client.getState().lastSequence, 5); // 以快照水位回填。
  assert.equal(h.client.getState().needsSnapshot, false);
});

test('terminal frame freezes state and fetches result', async () => {
  const h = makeHarness({ result: { answerStatus: 'COMPLETE' } });
  h.client.connect();
  const first = h.es.opens[0]!;
  first.handlers.onOpen();
  first.handlers.onMessage(projection(1, 'RUNNING'));
  first.handlers.onMessage({
    kind: 'event',
    id: null,
    event: 'terminal',
    data: '{"status":"COMPLETED","display":"SUCCESS"}',
  });

  assert.ok(h.events.some((u) => u.type === 'terminal' && u.display === 'SUCCESS'));
  await flush();
  assert.ok(h.events.some((u) => u.type === 'result'));
  assert.equal(h.client.getState().terminalDisplay, 'SUCCESS');
  // 终态后新事件被忽略。
  first.handlers.onMessage(projection(2, 'RUNNING'));
  assert.equal(h.client.getState().lastSequence, 1);
});

test('terminal snapshot also fetches result', async () => {
  const h = makeHarness({
    snapshot: { status: 'COMPLETED', lastEventSequence: 2 },
    result: { answerStatus: 'COMPLETE' },
  });
  h.client.connect();
  const first = h.es.opens[0]!;
  first.handlers.onOpen();
  first.handlers.onMessage(projection(1, 'QUEUED'));
  first.handlers.onMessage(projection(4, 'RUNNING')); // 间隙触发快照，快照为终态。
  await flush();
  assert.equal(h.client.getState().terminalDisplay, 'SUCCESS');
  assert.ok(h.events.some((u) => u.type === 'result'));
});

test('disconnect stops reconnection and never emits cancel', () => {
  const h = makeHarness();
  h.client.connect();
  const first = h.es.opens[0]!;
  const before = h.events.length;
  h.client.disconnect();
  assert.equal(first.closed, true);

  first.handlers.onClose(); // 断开后不再调度重连。
  assert.equal(h.scheduler.scheduled.length, 0);
  assert.equal(h.events.length, before); // 断开不派发任何更新（尤其不派发取消语义）。
});

test('reconnect after disconnect resumes from restored cursor', () => {
  const h = makeHarness();
  h.client.connect('7'); // 刷新恢复：带上次游标。
  assert.equal(h.es.opens[0]?.lastEventId, '7');
});

test('emits connection open/retrying around the stream', () => {
  const h = makeHarness();
  h.client.connect();
  const first = h.es.opens[0]!;
  first.handlers.onOpen();
  assert.ok(h.events.some((u) => u.type === 'connection' && u.state === 'open'));
  first.handlers.onClose();
  assert.ok(h.events.some((u) => u.type === 'connection' && u.state === 'retrying'));
});
