/**
 * DEV-07 桌面端真实后端 E2E：用浏览器 `fetch` 版 adapter 驱动真实后端，
 * 跑通「登录→建任务→SSE 重连→取消→终态 CANCELLED」，验证桌面真实接线与
 * 「失败/取消绝不显示为成功」。
 *
 * 门控：读 `KS_E2E_BASE_URL`（默认 http://127.0.0.1:8000）。后端不可达时 `skip`，
 * 与 server 端真实 PG 测试的「未配置即跳过」约定一致；可达时跑真实链路。
 * 复用 `browserAdapter.ts` 的 `login`/`createTask`/`cancelTask`/`createBrowserTaskEventDeps`
 * 与 `TaskEventClient`，不复制编排逻辑，也不直接连 Temporal/Bifrost/数据库。
 */

import { test } from 'node:test';
import { strict as assert } from 'node:assert';
import { randomUUID } from 'node:crypto';

import {
  login,
  createTask,
  cancelTask,
  createBrowserTaskEventDeps,
} from '../src/renderer/browserAdapter.ts';
import { TaskEventClient, type TaskEventUpdate } from '../src/renderer/shared/api/taskEventClient.ts';
import { buildCreateTaskBody } from '../src/renderer/shared/types/createTask.ts';

const BASE_URL = process.env.KS_E2E_BASE_URL ?? 'http://127.0.0.1:8000';
const USERNAME = process.env.KS_E2E_USERNAME ?? 'e2e-user';
const PASSWORD = process.env.KS_E2E_PASSWORD ?? 'dev-e2e-pass';
const CONVERSATION_ID = process.env.KS_E2E_CONVERSATION ?? '00000000-0000-4000-8000-00000000e2e3';

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function realSchedule(fn: () => void, delayMs: number): { cancel(): void } {
  const timer = setTimeout(fn, delayMs);
  return { cancel: () => clearTimeout(timer) };
}

test('desktop real backend: create -> SSE -> cancel -> terminal CANCELLED (never SUCCESS)', async (t) => {
  let token: string;
  try {
    token = (await login(BASE_URL, USERNAME, PASSWORD)).access_token;
  } catch (error) {
    t.skip(`backend unreachable at ${BASE_URL}: ${String(error)}`);
    return;
  }

  const created = await createTask(
    BASE_URL,
    token,
    buildCreateTaskBody(CONVERSATION_ID, 'desktop e2e: cancel path'),
    randomUUID(),
  );
  assert.equal(created.status, 'QUEUED');
  const taskId = created.task_id;

  const updates: TaskEventUpdate[] = [];
  const client = new TaskEventClient(
    taskId,
    createBrowserTaskEventDeps({
      baseUrl: BASE_URL,
      fetch: globalThis.fetch,
      token,
      schedule: realSchedule,
      random: Math.random,
    }),
  );
  client.subscribe((update) => updates.push(update));
  client.connect();

  // 给首个连接建立（TASK_CREATED 投影）留一点时间，再触发取消。
  await sleep(500);
  const cancelled = await cancelTask(BASE_URL, token, taskId);
  assert.equal(cancelled.status, 'CANCEL_REQUESTED');

  // 轮询等待终态：真实 TEMPORAL_SIGNAL → 事件游标 → commit_root_terminal_state → SSE terminal 帧。
  const deadline = Date.now() + 20_000;
  let terminal: TaskEventUpdate | null = null;
  while (Date.now() < deadline) {
    terminal = updates.find((update) => update.type === 'terminal') ?? null;
    if (terminal !== null) break;
    await sleep(250);
  }
  client.disconnect();

  assert.ok(terminal !== null, 'expected a terminal update before timeout');
  assert.equal(terminal.type, 'terminal');
  assert.equal(terminal.display, 'CANCELLED');
  assert.ok(
    updates.every((update) => update.type !== 'terminal' || update.display !== 'SUCCESS'),
    'terminal must never render SUCCESS for a cancelled task',
  );
});
