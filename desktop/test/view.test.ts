import { test } from 'node:test';
import { strict as assert } from 'node:assert';

import { deriveResultDetail, deriveTaskView, statusLabelFor } from '../src/renderer/view.ts';
import { createInitialState } from '../src/renderer/shared/state/taskReducer.ts';

const TASK_ID = '00000000-0000-0000-0000-000000000001';

test('initial connecting state shows 连接中', () => {
  const view = deriveTaskView(createInitialState(TASK_ID), 'connecting', null);
  assert.equal(view.statusLabel, '连接中…');
  assert.equal(view.phase, 'connecting');
});

test('terminal display shows 成功/失败/取消 and phase done', () => {
  for (const [display, label] of [
    ['SUCCESS', '成功'],
    ['FAILED', '失败'],
    ['CANCELLED', '已取消'],
  ] as const) {
    const state = { ...createInitialState(TASK_ID), terminalDisplay: display };
    const view = deriveTaskView(state, 'open', null);
    assert.equal(view.statusLabel, label);
    assert.equal(view.phase, 'done');
    assert.equal(view.terminalDisplay, display);
  }
});

test('needsSnapshot shows 同步中 and reconciling phase', () => {
  const state = { ...createInitialState(TASK_ID), needsSnapshot: true };
  const view = deriveTaskView(state, 'open', 'RUNNING');
  assert.equal(view.statusLabel, '同步中…');
  assert.equal(view.phase, 'reconciling');
});

test('lastStatus maps to its label while streaming', () => {
  const state = createInitialState(TASK_ID);
  assert.equal(deriveTaskView(state, 'open', 'QUEUED').statusLabel, '排队中');
  assert.equal(deriveTaskView(state, 'open', 'RUNNING').statusLabel, '执行中');
  assert.equal(deriveTaskView(state, 'open', 'PLANNING').statusLabel, '规划中');
});

test('open with no status yet shows 进行中', () => {
  const view = deriveTaskView(createInitialState(TASK_ID), 'open', null);
  assert.equal(view.statusLabel, '进行中…');
  assert.equal(view.phase, 'streaming');
});

test('statusLabelFor maps cancel statuses to labels', () => {
  assert.equal(statusLabelFor('CANCEL_REQUESTED'), '取消请求中');
  assert.equal(statusLabelFor('CANCELLED'), '已取消');
});

test('deriveResultDetail surfaces failure reason then status', () => {
  assert.equal(deriveResultDetail({ error_code: 'E_INPUT' }), '失败原因：E_INPUT');
  assert.equal(deriveResultDetail({ status: 'COMPLETED' }), '结果状态：COMPLETED');
  assert.equal(deriveResultDetail({}), '');
  assert.equal(deriveResultDetail(null), '');
  assert.equal(deriveResultDetail('not-an-object'), '');
});
