import { test } from 'node:test';
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';

/**
 * DEV-07 桌面安全姿态守卫：主进程必须禁用渲染进程 Node 集成并开启隔离与沙箱
 * （执行计划第160行）。以源码级契约锁定三项开关，防止后续改动回退。
 */

const MAIN_SOURCE = readFileSync(new URL('../src/main/index.ts', import.meta.url), 'utf8');

test('renderer webPreferences harden contextIsolation/nodeIntegration/sandbox', () => {
  assert.match(MAIN_SOURCE, /contextIsolation:\s*true/);
  assert.match(MAIN_SOURCE, /nodeIntegration:\s*false/);
  assert.match(MAIN_SOURCE, /sandbox:\s*true/);
  assert.doesNotMatch(MAIN_SOURCE, /sandbox:\s*false/);
});
