/**
 * DEV-07 桌面真实接线：纯函数视图模型。
 *
 * 把 `TaskStreamState` + 连接态 + 最近非终态 status 折叠为可渲染的展示事实：
 * 终态只显示成功/失败/取消（绝不回退），间隙只显示「同步中」，否则显示最近状态或连接提示。
 */

import type { TaskStreamState } from './shared/state/taskReducer.ts';
import type { TaskStatus, TerminalDisplay } from './shared/types/task.ts';

export type ConnectionState = 'connecting' | 'open' | 'retrying';

export interface TaskView {
  statusLabel: string;
  phase: 'connecting' | 'streaming' | 'reconciling' | 'done';
  terminalDisplay: TerminalDisplay | null;
  lastSequence: number;
  connection: ConnectionState;
}

const STATUS_LABELS: Record<TaskStatus, string> = {
  QUEUED: '排队中',
  PLANNING: '规划中',
  RUNNING: '执行中',
  REPLANNING: '重新规划',
  FINALIZING: '收尾中',
  WAITING_USER_INPUT: '等待输入',
  WAITING_APPROVAL: '等待审批',
  RETRY_WAIT: '重试等待',
  CANCEL_REQUESTED: '取消请求中',
  COMPLETED: '已完成',
  FAILED: '失败',
  CANCELLED: '已取消',
};

const DISPLAY_LABELS: Record<TerminalDisplay, string> = {
  SUCCESS: '成功',
  FAILED: '失败',
  CANCELLED: '已取消',
};

/** 单一状态 → 展示文案（取消按钮成功后即时回显，无需等下一帧 SSE）。 */
export function statusLabelFor(status: TaskStatus): string {
  return STATUS_LABELS[status];
}

export function deriveTaskView(
  state: TaskStreamState,
  connection: ConnectionState,
  lastStatus: TaskStatus | null,
): TaskView {
  if (state.terminalDisplay !== null) {
    return {
      statusLabel: DISPLAY_LABELS[state.terminalDisplay],
      phase: 'done',
      terminalDisplay: state.terminalDisplay,
      lastSequence: state.lastSequence,
      connection,
    };
  }
  if (state.needsSnapshot) {
    return {
      statusLabel: '同步中…',
      phase: 'reconciling',
      terminalDisplay: null,
      lastSequence: state.lastSequence,
      connection,
    };
  }
  return {
    statusLabel:
      lastStatus !== null
        ? STATUS_LABELS[lastStatus]
        : connection === 'connecting'
          ? '连接中…'
          : '进行中…',
    phase: connection === 'connecting' ? 'connecting' : 'streaming',
    terminalDisplay: null,
    lastSequence: state.lastSequence,
    connection,
  };
}

/**
 * 结果恢复：把 `GET /result` 的 `TaskResultView` 折叠成一行可读结论。
 * 最小页只展示状态与失败原因（error_code），答案正文在 result_artifact_id 里，需另行取回，不属于本切片。
 */
export function deriveResultDetail(result: unknown): string {
  if (typeof result !== 'object' || result === null) return '';
  const record = result as Record<string, unknown>;
  const errorCode = record.error_code;
  if (typeof errorCode === 'string' && errorCode !== '') return `失败原因：${errorCode}`;
  const status = record.status;
  if (typeof status === 'string' && status !== '') return `结果状态：${status}`;
  return '';
}
