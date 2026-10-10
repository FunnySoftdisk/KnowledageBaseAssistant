/**
 * DEV-07 桌面真实接线：登录响应的 View Model（与后端 `LoginResponse` 对齐）。
 *
 * 只承载低基数认证事实；access_token 只在渲染进程内存中短存，不写入 localStorage / 文档。
 */

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in_seconds: number;
  user_id: string;
  organization_id: string;
  display_name: string;
  must_change_password: boolean;
}
