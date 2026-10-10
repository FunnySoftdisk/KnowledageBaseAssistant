import { test } from 'node:test';
import { strict as assert } from 'node:assert';

import { buildCreateTaskBody } from '../src/renderer/shared/types/createTask.ts';

test('buildCreateTaskBody fills query with empty/default controls', () => {
  const body = buildCreateTaskBody('00000000-0000-0000-0000-000000000001', '查一下');
  assert.deepEqual(body, {
    conversation_id: '00000000-0000-0000-0000-000000000001',
    query: '查一下',
    attachments: [],
    knowledge_scope: null,
    output_contract: null,
    network_policy: null,
    selected_resource_refs: [],
    explicit_constraints: [],
  });
});
