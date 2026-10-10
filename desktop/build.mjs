/**
 * DEV-07 桌面真实接线：esbuild 打包脚本。
 *
 * - main / preload → CJS（Electron 主/预加载进程，electron 走 external）；
 * - renderer → ESM 浏览器 bundle（把 shared 核心 + fetch 适配 + 视图打进单文件）；
 * - index.html 原样拷贝到 dist/renderer/。
 */

import * as esbuild from 'esbuild';
import { copyFile, mkdir } from 'node:fs/promises';

await mkdir('dist/renderer', { recursive: true });

await Promise.all([
  esbuild.build({
    entryPoints: ['src/main/index.ts'],
    outfile: 'dist/main.cjs',
    bundle: true,
    platform: 'node',
    format: 'cjs',
    external: ['electron'],
    target: 'node20',
  }),
  esbuild.build({
    entryPoints: ['src/preload/index.ts'],
    outfile: 'dist/preload.cjs',
    bundle: true,
    platform: 'node',
    format: 'cjs',
    external: ['electron'],
    target: 'node20',
  }),
  esbuild.build({
    entryPoints: ['src/renderer/index.ts'],
    outfile: 'dist/renderer/index.js',
    bundle: true,
    platform: 'browser',
    format: 'esm',
    target: 'es2022',
  }),
  copyFile('src/renderer/index.html', 'dist/renderer/index.html'),
]);
