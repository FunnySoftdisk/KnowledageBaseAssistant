/**
 * DEV-07 桌面离线切片：Task 事件流的纯函数 Reducer（对应桌面设计 §5.4）。
 *
 * 不变量：
 * - 只接受 `sequence > lastSequence` 的事件，去重与乱序被忽略；
 * - 出现序号间隙时**暂停增量**、标记 `needsSnapshot`，由客户端以 REST 快照重读事实，不猜测状态；
 * - 终态（COMPLETED/FAILED/CANCELLED）一旦置位不回退到运行态；状态倒退事件被忽略。
 */

import {
  isTerminal,
  terminalDisplayFor,
  type TaskEventProjection,
  type TerminalDisplay,
} from '../types/task.ts';

export interface TaskStreamState {
  taskId: string;
  /** 已接受的最大事件序号；0 表示尚无先验事件。 */
  lastSequence: number;
  /** 发现序号间隙，需以 REST 快照重读后回填事实水位。 */
  needsSnapshot: boolean;
  /** 终态展示结论；置位后不回退。 */
  terminalDisplay: TerminalDisplay | null;
}

export type TaskStreamAction =
  | { type: 'event'; event: TaskEventProjection }
  | { type: 'terminal'; display: TerminalDisplay }
  | { type: 'snapshot'; lastSequence: number };

export function createInitialState(taskId: string): TaskStreamState {
  return { taskId, lastSequence: 0, needsSnapshot: false, terminalDisplay: null };
}

export function reduceTaskStream(
  state: TaskStreamState,
  action: TaskStreamAction,
): TaskStreamState {
  switch (action.type) {
    case 'terminal':
      // 终态只进不退：覆盖为给定结论并清空快照标记。
      return { ...state, terminalDisplay: action.display, needsSnapshot: false };

    case 'snapshot':
      // 快照重读：以服务端事实水位回填（只前进不后退），清空间隙标记；不触碰终态。
      return {
        ...state,
        lastSequence: Math.max(state.lastSequence, action.lastSequence),
        needsSnapshot: false,
      };

    case 'event': {
      if (state.terminalDisplay !== null) return state; // 终态后忽略一切事件，不回退。
      const sequence = action.event.sequence;
      if (sequence <= state.lastSequence) return state; // 去重 / 乱序。
      if (sequence !== state.lastSequence + 1) {
        return { ...state, needsSnapshot: true }; // 间隙：暂停增量，等快照。
      }
      const status = action.event.payload.status;
      const terminalDisplay =
        status !== undefined && isTerminal(status) ? terminalDisplayFor(status) : null;
      return { ...state, lastSequence: sequence, terminalDisplay, needsSnapshot: false };
    }
  }
}
