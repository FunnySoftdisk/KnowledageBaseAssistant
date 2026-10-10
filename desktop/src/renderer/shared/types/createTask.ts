/**
 * DEV-07 桌面真实接线：创建任务请求/响应的 View Model（与后端
 * `CreateTaskRequestV1` / `TaskCreateResponse` 对齐）。
 *
 * 最小任务页只发起「输入 query」：conversation_id 由客户端生成，其余控件一律空/默认，
 * 服务端按已冻结规则做规范与准入；本客户端不自行拼装知识范围/输出契约/资源引用。
 */

export interface CreateTaskRequestBody {
  conversation_id: string;
  query: string;
  attachments: readonly { artifact_id: string; sha256: string; ordinal: number }[];
  knowledge_scope: null;
  output_contract: null;
  network_policy: 'DENY' | 'ASK' | 'ALLOW' | null;
  selected_resource_refs: readonly unknown[];
  explicit_constraints: readonly string[];
}

export interface TaskCreateResponse {
  task_id: string;
  input_id: string;
  status: string;
  deadline_at: string;
  active_plan_version: number | null;
}

export function buildCreateTaskBody(conversationId: string, query: string): CreateTaskRequestBody {
  return {
    conversation_id: conversationId,
    query,
    attachments: [],
    knowledge_scope: null,
    output_contract: null,
    network_policy: null,
    selected_resource_refs: [],
    explicit_constraints: [],
  };
}
