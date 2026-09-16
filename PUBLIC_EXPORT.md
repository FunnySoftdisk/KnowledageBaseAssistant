# Public export policy

本GitHub仓库是从私有工作目录生成的公开工程镜像，不是内部设计仓库的完整副本。

## 允许公开

- `.gitattributes`与`.gitignore`；
- `server/pyproject.toml`、`server/uv.lock`与`server/alembic.ini`；
- `server/src/`下的工程源码；
- `server/tests/`下与公开源码对应的测试；
- `server/migrations/`下的数据库迁移链；
- `contracts/core/`下由公开源码生成的核心机器契约；
- `tools/core_contract_exports.py`及其测试；
- `tools/build_public_snapshot.py`及其测试；
- 本公开范围说明。

## 明确排除

- 内部产品需求、调研、概要和模块详细设计；
- `docs/`中的审批、决策、进度和个人开发计划；
- 账户、Workspace、API Host、令牌、密钥和Secret引用实例；
- `deploy/`中的内部环境、发行探针和运行观察；
- 尚未接入工程源码的外部研究Prompt、Profile、fixture及大批设计Schema；
- 缓存、虚拟环境、运行数据、模型权重和构建产物；
- 私有Git提交历史。

## 发布原则

公开快照只能由`tools/build_public_snapshot.py`生成。工具从Git已跟踪文件中按硬编码白名单复制内容，拒绝符号链接、非文件、非空输出目录和常见凭据模式。开发阶段不发布README；发布前必须在快照目录重新运行测试与秘密扫描。

公开仓库使用独立的无父提交历史。内部仓库不得直接push到公开远程，避免后续提交再次携带被排除材料。
