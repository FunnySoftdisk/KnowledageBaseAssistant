import { test } from 'node:test';
import { strict as assert } from 'node:assert';

import { RECONNECT_BACKOFF_MS, reconnectDelayMs } from '../src/renderer/shared/api/backoff.ts';

const CENTER = () => 0.5; // 无抖动。

test('backoff sequence is 1/2/5/10/30 seconds', () => {
  const delays = [0, 1, 2, 3, 4].map((attempt) =>
    reconnectDelayMs(attempt, { random: CENTER }),
  );
  assert.deepEqual(delays, [1000, 2000, 5000, 10000, 30000]);
});

test('caps at 30 seconds beyond the table', () => {
  assert.equal(reconnectDelayMs(5, { random: CENTER }), 30000);
  assert.equal(reconnectDelayMs(99, { random: CENTER }), 30000);
});

test('negative attempt clamps to first step', () => {
  assert.equal(reconnectDelayMs(-3, { random: CENTER }), 1000);
});

test('jitter spans ±20% around the base', () => {
  const base = RECONNECT_BACKOFF_MS[0] ?? 1000;
  const low = reconnectDelayMs(0, { random: () => 0 }); // 1 - 0.2
  const high = reconnectDelayMs(0, { random: () => 1 }); // 1 + 0.2
  assert.equal(low, Math.round(base * 0.8));
  assert.equal(high, Math.round(base * 1.2));
});

test('jitterRatio is configurable', () => {
  const base = RECONNECT_BACKOFF_MS[0] ?? 1000;
  assert.equal(reconnectDelayMs(0, { jitterRatio: 0, random: CENTER }), base);
  assert.equal(reconnectDelayMs(0, { jitterRatio: 0.5, random: () => 1 }), Math.round(base * 1.5));
});
