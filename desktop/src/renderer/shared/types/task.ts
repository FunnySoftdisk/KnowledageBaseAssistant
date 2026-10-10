/**
 * DEV-07 桌面离线切片：与后端 `event_stream.py` 冻结契约对齐的 Task 事件 View Model。
 *
 * 只携带低基数业务进度事实。Prompt / 思维链 / 模型原包 / Secret / 内部 Ref / Digest / 正文
 * 均被后端 `project_stream_event` 失败关闭地剥离，客户端只信任该授权投影，不自行拼接内部表。
 *
 * 注意：后端 `TaskEventProjectionV1` 的 `event_schema_version` 属内部字段，不进入 SSE 帧
 * （SSE 只传 `id:`/`event:`/`data:`，其中 `data:` 即授权负载），故此处不建模该字段。
 */

export const TASK_STATUS_VALUES = [
  'QUEUED',
  'PLANNING',
  'RUNNING',
  'REPLANNING',
  'FINALIZING',
  'WAITING_USER_INPUT',
  'WAITING_APPROVAL',
  'RETRY_WAIT',
  'CANCEL_REQUESTED',
  'COMPLETED',
  'FAILED',
  'CANCELLED',
] as const;

export type TaskStatus = (typeof TASK_STATUS_VALUES)[number];

/** 终态：REST 读取已提交结果后即可渲染，SSE 不再推进。 */
export const TERMINAL_STATUSES: readonly TaskStatus[] = ['COMPLETED', 'FAILED', 'CANCELLED'];

/** 终态在桌面端的展示结论：只有 COMPLETED 显示成功，失败/取消绝不冒充成功。 */
export type TerminalDisplay = 'SUCCESS' | 'FAILED' | 'CANCELLED';

/** 可流式到桌面的任务生命周期事件白名单（与后端 `SSE_STREAMABLE_EVENT_TYPES` 一致）。 */
export const STREAMABLE_EVENT_TYPES = [
  'TASK_CREATED',
  'GOAL_UNDERSTANDING_COMPLETED',
  'PLAN_ACTIVATED',
  'USER_INPUT_REQUESTED',
  'TASK_FAILED',
  'TASK_CANCELLED',
] as const;

export type StreamableEventType = (typeof STREAMABLE_EVENT_TYPES)[number];

/** SSE `data:` 负载：`TaskProgressProjectionV1` 的 JSON 投影（后端 `exclude_none`）。 */
export interface TaskProgressProjection {
  task_id?: string;
  status?: TaskStatus;
  plan_version?: number;
  reason_code?: string;
  display_summary?: string;
}

/** 客户端视角的单条可流式事件：`sequence` 即 SSE `id:`，`event_type` 即 SSE `event:`。 */
export interface TaskEventProjection {
  sequence: number;
  event_type: StreamableEventType;
  payload: TaskProgressProjection;
}

/** SSE `event: terminal` 的 `data:` 负载：终态 + 展示结论。 */
export interface TerminalFrame {
  status: TaskStatus;
  display: TerminalDisplay;
}

/** `POST /api/v1/tasks/{task_id}/cancel` 响应（后端 `TaskCancelResponse`：task_id + 新状态）。 */
export interface TaskCancelResponse {
  task_id: string;
  status: TaskStatus;
}

export function isTaskStatus(value: string): value is TaskStatus {
  return (TASK_STATUS_VALUES as readonly string[]).includes(value);
}

export function isStreamableEventType(value: string): value is StreamableEventType {
  return (STREAMABLE_EVENT_TYPES as readonly string[]).includes(value);
}

export function isTerminal(status: TaskStatus): boolean {
  return (TERMINAL_STATUSES as readonly string[]).includes(status);
}

export function terminalDisplayFor(status: TaskStatus): TerminalDisplay {
  if (status === 'COMPLETED') return 'SUCCESS';
  if (status === 'FAILED') return 'FAILED';
  if (status === 'CANCELLED') return 'CANCELLED';
  throw new Error('SSE_TERMINAL_STATUS_REQUIRED');
}
