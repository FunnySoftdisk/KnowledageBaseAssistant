/**
 * DEV-07 桌面真实接线：浏览器 `fetch` 版 SSE + REST（登录 / 快照 / 结果 / 创建任务）。
 *
 * 不用原生 `EventSource`，因为：
 * - 需要显式设置 `Last-Event-ID` 请求头（刷新/断线后按持久化游标续读，EventSource 首连无法自定义）；
 * - 可用 `AbortController` 干净关闭连接。
 * SSE 帧解析复用共享 `parseSseStream`/`decodeSseFrame`，不重写协议。
 *
 * 说明：本工程后端以 `\n` 作为帧/行分隔符（非 `\r\n`），按 `\n\n` 切帧即正确；
 * 这里仍做一次 `\r\n`→`\n` 归一以防御性兼容。
 *
 * 认证：登录后拿到的 `access_token` 只在本模块内作为参数在内存中传递，经
 * `Authorization: Bearer` 请求头带上；不落 localStorage、不出现在 URL。
 */

import { parseSseStream, type SseFrame } from './shared/api/sse.ts';
import type {
  EventSourceHandlers,
  TaskEventClientDeps,
  TaskSnapshot,
} from './shared/api/taskEventClient.ts';
import type { LoginResponse } from './shared/types/auth.ts';
import type { CreateTaskRequestBody, TaskCreateResponse } from './shared/types/createTask.ts';
import { isTaskStatus, type TaskCancelResponse } from './shared/types/task.ts';

export interface FetchEventSourceOptions {
  fetch?: typeof globalThis.fetch;
  token?: string | null;
}

function authHeaders(token: string | null): Record<string, string> {
  return token === null || token === undefined ? {} : { Authorization: `Bearer ${token}` };
}

/** 把增量文本块聚合成完整 SSE 帧（按空行切帧），复用 `parseSseStream` 做字段解析。 */
export class SseStreamDecoder {
  private _buffer = '';

  push(chunk: string): SseFrame[] {
    this._buffer += chunk;
    this._buffer = this._buffer.replace(/\r\n/g, '\n');
    const frames: SseFrame[] = [];
    let index: number;
    while ((index = this._buffer.indexOf('\n\n')) >= 0) {
      const frame = this._buffer.slice(0, index + 1);
      this._buffer = this._buffer.slice(index + 2);
      frames.push(...parseSseStream(`${frame}\n`));
    }
    return frames;
  }

  /** 流结束时清空残尾（无终止空行的最后一帧）。 */
  flush(): SseFrame[] {
    if (this._buffer.length === 0) return [];
    const rest = this._buffer;
    this._buffer = '';
    return parseSseStream(rest.endsWith('\n') ? `${rest}\n` : `${rest}\n\n`);
  }
}

export function fetchEventSource(
  url: string,
  lastEventId: string | null,
  handlers: EventSourceHandlers,
  options: FetchEventSourceOptions = {},
): { close(): void } {
  const fetcher = options.fetch ?? globalThis.fetch;
  const controller = new AbortController();
  const decoder = new SseStreamDecoder();
  let closed = false;

  void (async () => {
    try {
      const headers: Record<string, string> = {
        Accept: 'text/event-stream',
        ...authHeaders(options.token ?? null),
      };
      if (lastEventId !== null) headers['Last-Event-ID'] = lastEventId;
      const response = await fetcher(url, { headers, signal: controller.signal });
      if (!response.ok) throw new Error(`SSE_HTTP_${response.status}`);
      if (response.body === null) throw new Error('SSE_NO_BODY');
      handlers.onOpen();
      const reader = response.body.getReader();
      const textDecoder = new TextDecoder();
      for (;;) {
        const result = await reader.read();
        if (result.done || result.value === undefined) break;
        const chunk = textDecoder.decode(result.value, { stream: true });
        for (const frame of decoder.push(chunk)) handlers.onMessage(frame);
      }
      for (const frame of decoder.flush()) handlers.onMessage(frame);
    } catch {
      // 主动 close() 中止：signal.aborted 为真，静默返回（不触发重连）。
      if (controller.signal.aborted) return;
    }
    if (!closed) {
      closed = true;
      handlers.onClose();
    }
  })();

  return {
    close() {
      closed = true;
      controller.abort();
    },
  };
}

export function createBrowserSnapshotFetcher(
  baseUrl: string,
  fetcher: typeof globalThis.fetch = globalThis.fetch,
  token: string | null = null,
): (taskId: string) => Promise<TaskSnapshot> {
  return async (taskId) => {
    const response = await fetcher(`${baseUrl}/api/v1/tasks/${encodeURIComponent(taskId)}`, {
      headers: authHeaders(token),
    });
    if (!response.ok) throw new Error(`TASK_HTTP_${response.status}`);
    const detail = (await response.json()) as { status?: unknown; last_event_sequence?: unknown };
    if (typeof detail.status !== 'string' || !isTaskStatus(detail.status)) {
      throw new Error('TASK_SNAPSHOT_STATUS_INVALID');
    }
    const lastEventSequence = Number(detail.last_event_sequence);
    if (!Number.isInteger(lastEventSequence) || lastEventSequence < 0) {
      throw new Error('TASK_SNAPSHOT_SEQUENCE_INVALID');
    }
    return { status: detail.status, lastEventSequence };
  };
}

export function createBrowserResultFetcher(
  baseUrl: string,
  fetcher: typeof globalThis.fetch = globalThis.fetch,
  token: string | null = null,
): (taskId: string) => Promise<unknown> {
  return async (taskId) => {
    const response = await fetcher(`${baseUrl}/api/v1/tasks/${encodeURIComponent(taskId)}/result`, {
      headers: authHeaders(token),
    });
    if (!response.ok) throw new Error(`RESULT_HTTP_${response.status}`);
    return response.json();
  };
}

/** `POST /api/v1/auth/login`：用户名/密码 → 登录响应（access_token 只在内存中短存）。 */
export async function login(
  baseUrl: string,
  username: string,
  password: string,
  fetcher: typeof globalThis.fetch = globalThis.fetch,
): Promise<LoginResponse> {
  const response = await fetcher(`${baseUrl}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) throw new Error(`LOGIN_HTTP_${response.status}`);
  return (await response.json()) as LoginResponse;
}

/** `POST /api/v1/tasks`：带 `Authorization` 与 `Idempotency-Key` 头创建任务。 */
export async function createTask(
  baseUrl: string,
  token: string,
  body: CreateTaskRequestBody,
  idempotencyKey: string,
  fetcher: typeof globalThis.fetch = globalThis.fetch,
): Promise<TaskCreateResponse> {
  const response = await fetcher(`${baseUrl}/api/v1/tasks`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
      'Idempotency-Key': idempotencyKey,
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`CREATE_TASK_HTTP_${response.status}`);
  return (await response.json()) as TaskCreateResponse;
}

/** `POST /api/v1/tasks/{task_id}/cancel`：请求取消开放态任务，返回新状态。 */
export async function cancelTask(
  baseUrl: string,
  token: string,
  taskId: string,
  fetcher: typeof globalThis.fetch = globalThis.fetch,
): Promise<TaskCancelResponse> {
  const response = await fetcher(`${baseUrl}/api/v1/tasks/${encodeURIComponent(taskId)}/cancel`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new Error(`CANCEL_TASK_HTTP_${response.status}`);
  const body = (await response.json()) as { task_id?: unknown; status?: unknown };
  if (typeof body.status !== 'string' || !isTaskStatus(body.status)) {
    throw new Error('CANCEL_TASK_STATUS_INVALID');
  }
  return { task_id: String(body.task_id), status: body.status };
}

export interface BrowserTaskEventDepsOptions {
  baseUrl: string;
  fetch?: typeof globalThis.fetch;
  token?: string | null;
  schedule: (fn: () => void, delayMs: number) => { cancel(): void };
  random?: () => number;
  jitterRatio?: number;
}

export function createBrowserTaskEventDeps(options: BrowserTaskEventDepsOptions): TaskEventClientDeps {
  const fetcher = options.fetch ?? globalThis.fetch;
  const token = options.token ?? null;
  return {
    baseUrl: options.baseUrl,
    openEventSource: (url, lastEventId, handlers) =>
      fetchEventSource(url, lastEventId, handlers, { fetch: fetcher, token }),
    fetchSnapshot: createBrowserSnapshotFetcher(options.baseUrl, fetcher, token),
    fetchResult: createBrowserResultFetcher(options.baseUrl, fetcher, token),
    schedule: options.schedule,
    random: options.random,
    jitterRatio: options.jitterRatio,
  };
}
