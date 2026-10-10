/**
 * DEV-07 桌面离线切片：SSE 文本解析与帧解码（与后端 `tasks.py` 的帧格式对齐）。
 *
 * 后端帧格式：
 * - 投影事件：`id: {sequence}\nevent: {event_type}\ndata: {payload}\n\n`
 * - 终态帧：`event: terminal\ndata: {"status":...,"display":...}\n\n`
 * - 心跳注释：`: ping\n\n`
 * - 重连提示：`retry: 1000\n\n`
 */

import {
  isStreamableEventType,
  isTaskStatus,
  type StreamableEventType,
  type TaskEventProjection,
  type TaskProgressProjection,
  type TaskStatus,
  type TerminalDisplay,
} from '../types/task.ts';

export type SseFrame =
  | { kind: 'event'; id: string | null; event: string | null; data: string }
  | { kind: 'comment'; text: string }
  | { kind: 'retry'; ms: number };

export type DecodedFrame =
  | { kind: 'projection'; event: TaskEventProjection }
  | { kind: 'terminal'; display: TerminalDisplay }
  | { kind: 'heartbeat' }
  | { kind: 'retry'; ms: number }
  | { kind: 'ignore' };

/** 把一段 SSE 文本切分为结构化帧（`\r\n` 归一化为 `\n`，多行 `data:` 以 `\n` 连接）。 */
export function parseSseStream(text: string): SseFrame[] {
  const frames: SseFrame[] = [];
  const lines = text.replace(/\r\n/g, '\n').split('\n');
  let dataLines: string[] = [];
  let id: string | null = null;
  let eventType: string | null = null;
  let retryMs: number | null = null;

  const flushEvent = () => {
    if (dataLines.length === 0 && id === null && eventType === null) return;
    frames.push({ kind: 'event', id, event: eventType, data: dataLines.join('\n') });
    dataLines = [];
    id = null;
    eventType = null;
  };

  for (const line of lines) {
    if (line === '') {
      if (retryMs !== null) {
        frames.push({ kind: 'retry', ms: retryMs });
        retryMs = null;
      } else {
        flushEvent();
      }
      continue;
    }
    if (line.startsWith(':')) {
      frames.push({ kind: 'comment', text: line.slice(1) });
      continue;
    }
    const colon = line.indexOf(':');
    if (colon < 0) continue;
    const field = line.slice(0, colon);
    let value = line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'id') id = value;
    else if (field === 'event') eventType = value;
    else if (field === 'data') dataLines.push(value);
    else if (field === 'retry') {
      const parsed = Number(value);
      if (Number.isInteger(parsed) && parsed >= 0) retryMs = parsed;
    }
  }
  if (retryMs !== null) frames.push({ kind: 'retry', ms: retryMs });
  else flushEvent();

  return frames;
}

/** 把结构化帧解码为领域语义；非白名单事件、畸形 JSON 或非法序号静默忽略（不推进状态）。 */
export function decodeSseFrame(frame: SseFrame): DecodedFrame {
  if (frame.kind === 'retry') return { kind: 'retry', ms: frame.ms };
  if (frame.kind === 'comment') {
    return frame.text.trim() === 'ping' ? { kind: 'heartbeat' } : { kind: 'ignore' };
  }
  const eventType = frame.event ?? '';
  const id = frame.id ?? '';
  if (eventType === 'terminal') {
    const display = decodeTerminalDisplay(frame.data);
    return display === null ? { kind: 'ignore' } : { kind: 'terminal', display };
  }
  if (isStreamableEventType(eventType)) {
    const event = decodeProjection(id, eventType, frame.data);
    return event === null ? { kind: 'ignore' } : { kind: 'projection', event };
  }
  return { kind: 'ignore' };
}

function decodeTerminalDisplay(data: string): TerminalDisplay | null {
  let raw: unknown;
  try {
    raw = JSON.parse(data);
  } catch {
    return null;
  }
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null;
  const display = (raw as Record<string, unknown>).display;
  return display === 'SUCCESS' || display === 'FAILED' || display === 'CANCELLED' ? display : null;
}

/** 投影帧只保留白名单字段，未知字段防御性丢弃；非法序号/类型返回 null。 */
function decodeProjection(
  id: string,
  eventType: StreamableEventType,
  data: string,
): TaskEventProjection | null {
  const sequence = Number(id);
  if (!Number.isInteger(sequence) || sequence < 1) return null;
  let raw: unknown;
  try {
    raw = JSON.parse(data);
  } catch {
    return null;
  }
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null;
  const obj = raw as Record<string, unknown>;
  const payload: TaskProgressProjection = {};
  const taskId = obj.task_id;
  const status = obj.status;
  const planVersion = obj.plan_version;
  const reasonCode = obj.reason_code;
  const displaySummary = obj.display_summary;
  if (typeof taskId === 'string') payload.task_id = taskId;
  if (typeof status === 'string' && isTaskStatus(status)) payload.status = status as TaskStatus;
  if (typeof planVersion === 'number') payload.plan_version = planVersion;
  if (typeof reasonCode === 'string') payload.reason_code = reasonCode;
  if (typeof displaySummary === 'string') payload.display_summary = displaySummary;
  return { sequence, event_type: eventType, payload };
}
