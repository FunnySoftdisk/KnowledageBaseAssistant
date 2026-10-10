/**
 * DEV-07 桌面离线切片：网络断开退避（对应桌面设计 §5.3）。
 *
 * 1/2/5/10/30 秒退避，加入 ±20% 确定性抖动（`random` 可注入以便测试）。
 */

export const RECONNECT_BACKOFF_MS = [1000, 2000, 5000, 10000, 30000] as const;

/**
 * 计算下一次重连延迟。`attempt` 从 0 起（首次重连为 1 秒），
 * 超过表长后封顶 30 秒；`random` 在 [0,1) 取值，0 取 -抖动下界、1 取 +抖动上界。
 */
export function reconnectDelayMs(
  attempt: number,
  options: { jitterRatio?: number; random?: () => number } = {},
): number {
  const jitterRatio = options.jitterRatio ?? 0.2;
  const random = options.random ?? Math.random;
  const index = Math.min(Math.max(Math.floor(attempt), 0), RECONNECT_BACKOFF_MS.length - 1);
  const base = RECONNECT_BACKOFF_MS[index] ?? 30000;
  const jitter = base * jitterRatio;
  return Math.round(base + (random() * 2 - 1) * jitter);
}
