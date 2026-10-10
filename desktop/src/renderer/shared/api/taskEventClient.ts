/**
 * DEV-07 桌面离线切片：Task 事件流重连客户端（对应桌面设计 §5.3）。
 *
 * 只依赖注入端口（EventSource 工厂、REST 快照/结果读取、调度器、随机源），零真实 I/O。
 * 规则：
 * - 重连携带 `Last-Event-ID`，按持久化 `sequence` 续读；
 * - 序号间隙暂停增量并回快照；终态帧回结果；服务端 `retry:` 作为正常循环节奏，
 *   网络断开则按 1/2/5/10/30 秒 + 抖动退避；
 * - 断开（disconnect）不等于取消：只停止重连，不派生取消语义。
 */

import {
  createInitialState,
  reduceTaskStream,
  type TaskStreamState,
} from '../state/taskReducer.ts';
import { reconnectDelayMs } from './backoff.ts';
import { decodeSseFrame, type SseFrame } from './sse.ts';
import {
  isTerminal,
  terminalDisplayFor,
  type TaskEventProjection,
  type TaskStatus,
  type TerminalDisplay,
} from '../types/task.ts';

export interface TaskSnapshot {
  status: TaskStatus;
  lastEventSequence: number;
}

export interface EventSourceHandlers {
  onOpen: () => void;
  onMessage: (frame: SseFrame) => void;
  onClose: () => void;
}

export interface TaskEventClientDeps {
  baseUrl?: string;
  openEventSource: (
    url: string,
    lastEventId: string | null,
    handlers: EventSourceHandlers,
  ) => { close(): void };
  fetchSnapshot: (taskId: string) => Promise<TaskSnapshot>;
  fetchResult: (taskId: string) => Promise<unknown>;
  schedule: (fn: () => void, delayMs: number) => { cancel(): void };
  random?: () => number;
  jitterRatio?: number;
}

export type TaskEventUpdate =
  | { type: 'event'; event: TaskEventProjection; state: TaskStreamState }
  | { type: 'terminal'; display: TerminalDisplay; state: TaskStreamState }
  | { type: 'result'; result: unknown; state: TaskStreamState }
  | { type: 'snapshot-requested'; state: TaskStreamState }
  | { type: 'state'; state: TaskStreamState }
  | { type: 'connection'; state: 'open' | 'retrying' };

export type Unsubscribe = () => void;

export class TaskEventClient {
  private readonly _taskId: string;
  private readonly _baseUrl: string;
  private readonly _openEventSource: TaskEventClientDeps['openEventSource'];
  private readonly _fetchSnapshot: TaskEventClientDeps['fetchSnapshot'];
  private readonly _fetchResult: TaskEventClientDeps['fetchResult'];
  private readonly _schedule: TaskEventClientDeps['schedule'];
  private readonly _random: () => number;
  private readonly _jitterRatio: number;

  private _state: TaskStreamState;
  private _lastEventId: string | null = null;
  private _attempt = 0;
  private _connection: { close(): void } | null = null;
  private _timer: { cancel(): void } | null = null;
  private _disposed = false;
  private readonly _listeners = new Set<(update: TaskEventUpdate) => void>();

  constructor(taskId: string, deps: TaskEventClientDeps) {
    this._taskId = taskId;
    this._baseUrl = deps.baseUrl ?? '';
    this._openEventSource = deps.openEventSource;
    this._fetchSnapshot = deps.fetchSnapshot;
    this._fetchResult = deps.fetchResult;
    this._schedule = deps.schedule;
    this._random = deps.random ?? Math.random;
    this._jitterRatio = deps.jitterRatio ?? 0.2;
    this._state = createInitialState(taskId);
  }

  getState(): TaskStreamState {
    return this._state;
  }

  subscribe(listener: (update: TaskEventUpdate) => void): Unsubscribe {
    this._listeners.add(listener);
    return () => {
      this._listeners.delete(listener);
    };
  }

  /** 建立（或恢复）连接；`lastEventId` 用于断线/刷新后按序号续读。 */
  connect(lastEventId?: string): void {
    this._disposed = false;
    if (lastEventId !== undefined) this._lastEventId = lastEventId;
    this._open();
  }

  /** 停止重连与当前连接；不派生任何取消语义（客户端断开 ≠ 取消任务）。 */
  disconnect(): void {
    this._disposed = true;
    this._timer?.cancel();
    this._timer = null;
    this._connection?.close();
    this._connection = null;
  }

  private _open(): void {
    if (this._disposed) return;
    const url = `${this._baseUrl}/api/v1/tasks/${encodeURIComponent(this._taskId)}/events`;
    let retryMs: number | null = null;
    this._connection = this._openEventSource(url, this._lastEventId, {
      onOpen: () => {
        this._attempt = 0; // 连接成功即重置退避。
        this._emit({ type: 'connection', state: 'open' });
      },
      onMessage: (frame) => {
        const decoded = decodeSseFrame(frame);
        if (decoded.kind === 'retry') {
          retryMs = decoded.ms;
        } else if (decoded.kind === 'projection') {
          this._applyEvent(decoded.event);
        } else if (decoded.kind === 'terminal') {
          this._applyTerminal(decoded.display);
          void this._loadResult();
        }
        // heartbeat / ignore：无状态推进。
      },
      onClose: () => {
        this._connection = null;
        if (this._disposed) return;
        this._emit({ type: 'connection', state: 'retrying' });
        const delay =
          retryMs !== null
            ? retryMs
            : reconnectDelayMs(this._attempt++, {
                jitterRatio: this._jitterRatio,
                random: this._random,
              });
        this._timer = this._schedule(() => {
          this._timer = null;
          this._open();
        }, delay);
      },
    });
  }

  private _applyEvent(event: TaskEventProjection): void {
    const next = reduceTaskStream(this._state, { type: 'event', event });
    if (next === this._state) return; // 去重 / 乱序 / 终态后忽略。
    this._state = next;
    if (next.needsSnapshot) {
      this._emit({ type: 'snapshot-requested', state: next });
      void this._reconcileSnapshot();
      return;
    }
    this._lastEventId = String(event.sequence);
    this._emit({ type: 'event', event, state: next });
  }

  private _applyTerminal(display: TerminalDisplay): void {
    if (this._state.terminalDisplay === display) return;
    const next = reduceTaskStream(this._state, { type: 'terminal', display });
    this._state = next;
    this._emit({ type: 'terminal', display, state: next });
  }

  private async _reconcileSnapshot(): Promise<void> {
    let snapshot: TaskSnapshot;
    try {
      snapshot = await this._fetchSnapshot(this._taskId);
    } catch {
      return;
    }
    if (this._disposed) return;
    const next = reduceTaskStream(this._state, {
      type: 'snapshot',
      lastSequence: snapshot.lastEventSequence,
    });
    this._state = next;
    this._lastEventId = String(
      Math.max(this._lastEventId === null ? 0 : Number(this._lastEventId), snapshot.lastEventSequence),
    );
    this._emit({ type: 'state', state: next });
    if (isTerminal(snapshot.status)) {
      this._applyTerminal(terminalDisplayFor(snapshot.status));
      void this._loadResult();
    }
  }

  private async _loadResult(): Promise<void> {
    let result: unknown;
    try {
      result = await this._fetchResult(this._taskId);
    } catch {
      return;
    }
    if (this._disposed) return;
    this._emit({ type: 'result', result, state: this._state });
  }

  private _emit(update: TaskEventUpdate): void {
    for (const listener of this._listeners) listener(update);
  }
}
