/**
 * DEV-07 桌面真实接线：preload 通过 contextBridge 暴露给渲染进程的最小只读配置。
 *
 * 渲染进程只拿到 taskId / baseUrl / platform，绝不暴露 Node 能力；任务页所需的全部网络
 * I/O 由渲染进程自身发起（fetch SSE + REST），后端按主体做所有权隔离。
 */

export interface DesktopBridge {
  /** 要观察的任务 ID（由启动环境变量 KS_TASK_ID 注入）。 */
  readonly taskId: string;
  /** 后端基址（默认 http://127.0.0.1:8000）。 */
  readonly baseUrl: string;
  /** 运行平台（process.platform），仅用于展示。 */
  readonly platform: string;
}
