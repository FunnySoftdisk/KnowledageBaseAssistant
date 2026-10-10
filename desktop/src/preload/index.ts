/**
 * DEV-07 桌面真实接线：Electron preload。
 *
 * 通过 contextBridge 只暴露只读 taskId/baseUrl/platform，不暴露任何 Node 能力；
 * 渲染进程自行以 fetch 连后端，后端按主体做所有权隔离。
 */

import { contextBridge } from 'electron';
import type { DesktopBridge } from '../renderer/shared/types/bridge.ts';

const bridge: DesktopBridge = {
  taskId: process.env.KS_TASK_ID ?? '',
  baseUrl: process.env.KS_BASE_URL ?? 'http://127.0.0.1:8000',
  platform: process.platform,
};

contextBridge.exposeInMainWorld('desktopTask', bridge);
