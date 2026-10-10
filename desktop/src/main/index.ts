/**
 * DEV-07 桌面真实接线：Electron 主进程。
 *
 * 只创建一个 BrowserWindow 加载最小任务页；渲染进程无 Node 能力（contextIsolation:true /
 * nodeIntegration:false / sandbox:true），preload 只暴露只读 taskId/baseUrl/platform。
 * taskId/baseUrl 由启动环境变量 KS_TASK_ID / KS_BASE_URL 注入（默认 127.0.0.1:8000）。
 */

import { app, BrowserWindow } from 'electron';
import * as path from 'node:path';

function createWindow(): void {
  const appPath = app.getAppPath();
  const window = new BrowserWindow({
    width: 900,
    height: 640,
    webPreferences: {
      preload: path.join(appPath, 'dist', 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  void window.loadFile(path.join(appPath, 'dist', 'renderer', 'index.html'));
}

void app.whenReady().then(() => {
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
