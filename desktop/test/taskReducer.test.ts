import { test } from 'node:test';
import { strict as assert } from 'node:assert';

import {
  createInitialState,
  reduceTaskStream,
  type TaskStreamState,
} from '../src/renderer/shared/state/taskReducer.ts';
import type { TaskEventProjection, TaskStatus } from '../src/renderer/shared/types/task.ts';

const TASK_ID = '00000000-0000-0000-0000-000000000001';

function evt(sequence: number, status?: TaskStatus): TaskEventProjection {
  return {
    sequence,
    event_type: 'TASK_CREATED',
    payload: status === undefined ? {} : { status },
  };
}

test('accepts consecutive sequences and advances lastSequence', () => {
  let state = createInitialState(TASK_ID);
  state = reduceTaskStream(state, { type: 'event', event: evt(1) });
  state = reduceTaskStream(state, { type: 'event', event: evt(2) });
  state = reduceTaskStream(state, { type: 'event', event: evt(3) });
  assert.equal(state.lastSequence, 3);
  assert.equal(state.needsSnapshot, false);
  assert.equal(state.terminalDisplay, null);
});

test('ignores duplicate sequence', () => {
  let state = createInitialState(TASK_ID);
  state = reduceTaskStream(state, { type: 'event', event: evt(1) });
  const before = state;
  state = reduceTaskStream(state, { type: 'event', event: evt(1) });
  assert.equal(state, before); // 返回同一引用即未推进。
  assert.equal(state.lastSequence, 1);
});

test('ignores out-of-order (sequence <= lastSequence)', () => {
  let state = createInitialState(TASK_ID);
  state = reduceTaskStream(state, { type: 'event', event: evt(3) });
  state = reduceTaskStream(state, { type: 'event', event: evt(2) });
  state = reduceTaskStream(state, { type: 'event', event: evt(5) });
  state = reduceTaskStream(state, { type: 'event', event: evt(4) });
  // 首个 3 相对 0 是间隙，故 lastSequence 仍为 0；后续乱序亦被忽略。
  assert.equal(state.lastSequence, 0);
  assert.equal(state.needsSnapshot, true);
});

test('gap marks needsSnapshot and does not advance', () => {
  let state = createInitialState(TASK_ID);
  state = reduceTaskStream(state, { type: 'event', event: evt(1) });
  state = reduceTaskStream(state, { type: 'event', event: evt(4) });
  assert.equal(state.lastSequence, 1);
  assert.equal(state.needsSnapshot, true);
});

test('snapshot reconciles lastSequence and clears needsSnapshot', () => {
  let state = createInitialState(TASK_ID);
  state = reduceTaskStream(state, { type: 'event', event: evt(1) });
  state = reduceTaskStream(state, { type: 'event', event: evt(4) });
  assert.equal(state.needsSnapshot, true);
  state = reduceTaskStream(state, { type: 'snapshot', lastSequence: 6 });
  assert.equal(state.lastSequence, 6);
  assert.equal(state.needsSnapshot, false);
});

test('snapshot never regresses lastSequence', () => {
  let state = createInitialState(TASK_ID);
  state = reduceTaskStream(state, { type: 'event', event: evt(1) });
  state = reduceTaskStream(state, { type: 'event', event: evt(2) });
  state = reduceTaskStream(state, { type: 'snapshot', lastSequence: 1 });
  assert.equal(state.lastSequence, 2);
});

test('terminal action sets display and clears needsSnapshot', () => {
  let state = createInitialState(TASK_ID);
  state = reduceTaskStream(state, { type: 'event', event: evt(1) });
  state = reduceTaskStream(state, { type: 'event', event: evt(3) }); // gap
  state = reduceTaskStream(state, { type: 'terminal', display: 'SUCCESS' });
  assert.equal(state.terminalDisplay, 'SUCCESS');
  assert.equal(state.needsSnapshot, false);
});

test('terminal does not regress: open event after terminal is ignored', () => {
  let state = createInitialState(TASK_ID);
  state = reduceTaskStream(state, { type: 'terminal', display: 'SUCCESS' });
  state = reduceTaskStream(state, { type: 'event', event: evt(1, 'RUNNING') });
  assert.equal(state.terminalDisplay, 'SUCCESS');
  assert.equal(state.lastSequence, 0);
});

test('terminal status inside event payload freezes state', () => {
  let state = createInitialState(TASK_ID);
  state = reduceTaskStream(state, { type: 'event', event: evt(1, 'FAILED') });
  assert.equal(state.terminalDisplay, 'FAILED');
  state = reduceTaskStream(state, { type: 'event', event: evt(2, 'RUNNING') });
  assert.equal(state.terminalDisplay, 'FAILED'); // 不回退。
  assert.equal(state.lastSequence, 1);
});

test('failed and cancelled never display success', () => {
  for (const [status, display] of [
    ['FAILED', 'FAILED'],
    ['CANCELLED', 'CANCELLED'],
  ] as const) {
    let state = createInitialState(TASK_ID);
    state = reduceTaskStream(state, { type: 'event', event: evt(1, status) });
    assert.equal(state.terminalDisplay, display);
    assert.notEqual(state.terminalDisplay, 'SUCCESS');
  }
});

test('snapshot after terminal keeps terminalDisplay', () => {
  let state = createInitialState(TASK_ID);
  state = reduceTaskStream(state, { type: 'terminal', display: 'FAILED' });
  state = reduceTaskStream(state, { type: 'snapshot', lastSequence: 10 });
  assert.equal(state.terminalDisplay, 'FAILED');
  assert.equal(state.lastSequence, 10);
});

test('state shape is stable across actions', () => {
  const state: TaskStreamState = createInitialState(TASK_ID);
  assert.deepEqual(Object.keys(state).sort(), [
    'lastSequence',
    'needsSnapshot',
    'taskId',
    'terminalDisplay',
  ]);
});
