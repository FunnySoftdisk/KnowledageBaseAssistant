/**
 * DEV-07 桌面真实接线：渲染进程入口（DOM）。
 *
 * 完整最小任务页：登录 → 输入任务 → 创建 → 观察进度 → 结果恢复。
 * - access_token 只在内存中短存，不写入 localStorage / 文档 / URL；
 * - 任务创建用客户端生成的 conversation_id + Idempotency-Key（每次创建幂等）；
 * - 观察沿用 fetch 版 SSE 客户端：刷新/断线按 localStorage 游标续读，终态回结果。
 */

import { TaskEventClient } from './shared/api/taskEventClient.ts';
import type { TaskEventUpdate } from './shared/api/taskEventClient.ts';
import { cancelTask, createBrowserTaskEventDeps, createTask, login } from './browserAdapter.ts';
import { buildCreateTaskBody } from './shared/types/createTask.ts';
import { deriveResultDetail, deriveTaskView, statusLabelFor } from './view.ts';
import type { ConnectionState, TaskView } from './view.ts';
import type { DesktopBridge } from './shared/types/bridge.ts';
import type { TaskStatus } from './shared/types/task.ts';

function readBridge(): DesktopBridge | null {
  return (window as unknown as { desktopTask?: DesktopBridge }).desktopTask ?? null;
}

function cursorKey(taskId: string): string {
  return `ks.task.${taskId}.cursor`;
}

function connectionLabel(view: TaskView): string {
  if (view.phase === 'done') return '已结束';
  if (view.phase === 'reconciling') return '正在同步';
  if (view.connection === 'retrying') return '重连中';
  if (view.connection === 'connecting') return '连接中';
  return '已连接';
}

/** UUIDv4：`crypto.getRandomValues` 在浏览器 / Electron 渲染进程始终可用。 */
function randomUuid(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function boot(): void {
  const authPanel = document.getElementById('auth');
  const taskPanel = document.getElementById('task-form');
  const progressPanel = document.getElementById('progress');
  const usernameEl = document.getElementById('username') as HTMLInputElement | null;
  const passwordEl = document.getElementById('password') as HTMLInputElement | null;
  const loginBtn = document.getElementById('login-btn') as HTMLButtonElement | null;
  const loginError = document.getElementById('login-error');
  const queryEl = document.getElementById('query') as HTMLTextAreaElement | null;
  const createBtn = document.getElementById('create-btn') as HTMLButtonElement | null;
  const createError = document.getElementById('create-error');
  const statusEl = document.getElementById('status');
  const phaseEl = document.getElementById('phase');
  const seqEl = document.getElementById('seq');
  const resultEl = document.getElementById('result');
  const cancelBtn = document.getElementById('cancel-btn') as HTMLButtonElement | null;
  const cancelError = document.getElementById('cancel-error');

  const bridge = readBridge();
  if (
    authPanel === null ||
    taskPanel === null ||
    progressPanel === null ||
    usernameEl === null ||
    passwordEl === null ||
    loginBtn === null ||
    loginError === null ||
    queryEl === null ||
    createBtn === null ||
    createError === null ||
    statusEl === null ||
    phaseEl === null ||
    seqEl === null ||
    resultEl === null ||
    cancelBtn === null ||
    cancelError === null ||
    bridge === null
  ) {
    document.body.textContent = '桌面端配置缺失：请以 KS_BASE_URL 启动。';
    return;
  }
  const baseUrl = bridge.baseUrl;

  let token: string | null = null;
  let currentClient: TaskEventClient | null = null;
  let currentTaskId: string | null = null;
  let lastStatus: TaskStatus | null = null;
  let connection: ConnectionState = 'connecting';

  const schedule = (fn: () => void, delayMs: number): { cancel(): void } => {
    const id = setTimeout(fn, delayMs);
    return { cancel: () => clearTimeout(id) };
  };

  const show = (panel: HTMLElement): void => {
    authPanel.hidden = panel !== authPanel;
    taskPanel.hidden = panel !== taskPanel;
    progressPanel.hidden = panel !== progressPanel;
  };

  const observe = (taskId: string): void => {
    currentClient?.disconnect();
    currentTaskId = taskId;
    lastStatus = null;
    connection = 'connecting';
    resultEl.textContent = '';
    cancelError.textContent = '';

    const client = new TaskEventClient(
      taskId,
      createBrowserTaskEventDeps({ baseUrl, schedule, token }),
    );
    currentClient = client;

    const persistCursor = (): void => {
      const sequence = client.getState().lastSequence;
      if (sequence > 0) localStorage.setItem(cursorKey(taskId), String(sequence));
    };

    const render = (): void => {
      const view = deriveTaskView(client.getState(), connection, lastStatus);
      statusEl.textContent = view.statusLabel;
      phaseEl.textContent = connectionLabel(view);
      seqEl.textContent = `已同步事件序号 ${view.lastSequence}`;
    };

    client.subscribe((update: TaskEventUpdate) => {
      if (update.type === 'connection') {
        connection = update.state;
      } else if (update.type === 'event') {
        lastStatus = update.event.payload.status ?? lastStatus;
        persistCursor();
      } else if (update.type === 'state') {
        persistCursor();
      } else if (update.type === 'terminal') {
        persistCursor();
      } else if (update.type === 'result') {
        resultEl.textContent = deriveResultDetail(update.result);
      }
      render();
    });

    const saved = localStorage.getItem(cursorKey(taskId));
    client.connect(saved ?? undefined);
    show(progressPanel);
    render();
  };

  loginBtn.addEventListener('click', () => {
    void (async () => {
      loginError.textContent = '';
      loginBtn.disabled = true;
      try {
        const response = await login(baseUrl, usernameEl.value, passwordEl.value);
        token = response.access_token;
        // 启动时已注入 KS_TASK_ID：登录后直接观察既有任务；否则进入任务输入。
        if (bridge.taskId !== '') observe(bridge.taskId);
        else show(taskPanel);
      } catch (err) {
        loginError.textContent = `登录失败：${err instanceof Error ? err.message : String(err)}`;
      } finally {
        loginBtn.disabled = false;
      }
    })();
  });

  createBtn.addEventListener('click', () => {
    void (async () => {
      const query = queryEl.value.trim();
      if (query === '') {
        createError.textContent = '请输入任务描述。';
        return;
      }
      if (token === null) {
        createError.textContent = '尚未登录。';
        return;
      }
      createError.textContent = '';
      createBtn.disabled = true;
      try {
        const body = buildCreateTaskBody(randomUuid(), query);
        const created = await createTask(baseUrl, token, body, randomUuid());
        observe(created.task_id);
      } catch (err) {
        createError.textContent = `创建失败：${err instanceof Error ? err.message : String(err)}`;
      } finally {
        createBtn.disabled = false;
      }
    })();
  });

  cancelBtn.addEventListener('click', () => {
    void (async () => {
      if (token === null) {
        cancelError.textContent = '尚未登录。';
        return;
      }
      if (currentTaskId === null) {
        cancelError.textContent = '当前没有可取消的任务。';
        return;
      }
      cancelError.textContent = '';
      cancelBtn.disabled = true;
      try {
        const result = await cancelTask(baseUrl, token, currentTaskId);
        // 立即回显新状态；后续 TASK_CANCELLED 事件会经 SSE 再刷新并推进到终态。
        lastStatus = result.status;
        statusEl.textContent = statusLabelFor(result.status);
      } catch (err) {
        cancelError.textContent = `取消失败：${err instanceof Error ? err.message : String(err)}`;
      } finally {
        cancelBtn.disabled = false;
      }
    })();
  });

  // 入口始终从登录开始：读任务事件需要有效 token 做所有权隔离。
  show(authPanel);
}

boot();
