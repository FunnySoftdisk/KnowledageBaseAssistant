# 核心契约：任务入口、Goal与首轮Plan Draft

CORE-01～CORE-40已批准。当前22份Schema来自[基础引用](../../server/src/knowledge_system/modules/tasking/domain/core_types.py)、[任务入口](../../server/src/knowledge_system/modules/tasking/domain/input_contracts.py)、[Goal契约](../../server/src/knowledge_system/modules/tasking/domain/goal_contracts.py)、[Plan Draft契约](../../server/src/knowledge_system/modules/tasking/domain/planning_fragments.py)和已有TokenAccountingV1。已实现严格DTO、关系校验与Plan Item稳定ID；不是完整CompiledPlan/CompiledCriterion/Task Runtime/Final或正式发行包。

[导出工具](../../tools/core_contract_exports.py)以确定JSON字节导出；manifest记录22个实际Schema的长度/SHA，并明确Draft已构建，但Compiled Criterion、完整首轮Plan、ORM、PG迁移、发行签名和Runtime均未完成。[字段矩阵](core-field-matrix-v1.json)记录已批准数据库投影及三项仍待闭合依赖；它不是ORM或DDL。

仓库根目录只读复现：

```bash
PYTHONPATH=server/src server/.venv/bin/python tools/core_contract_exports.py
```

`mismatched_files`为空才表示保存字节与源码一致。缺失、篡改或单文件符号链接退出1，不联网/写文件/调用模型；`--dump`只将生成字节输出stdout。Pydantic frozen禁止字段重新赋值，容器采用tuple；授权、摘要语义和PG不可变性仍由Builder/Compiler/Repository负责。
