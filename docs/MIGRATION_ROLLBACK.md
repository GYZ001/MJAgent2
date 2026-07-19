# Harness 迁移与回滚

数据库迁移采用只增列/增表方式，由 `app.db` 启动时幂等执行。发现历史孤儿或重复行时，启动流程会先把 SQLite 快照写入 `data/integrity_backups/`，再把修复前后计数、受影响 ID、备份路径写入 `data/integrity_reports/`。回滚应用版本时保留新增表列，旧代码会忽略它们，避免破坏证据与账务历史；需要还原完整性修复时，应先停服务并使用报告所指向的备份。

项目级 `harness_engine_enabled` 是灰度开关，新项目默认开启。需要隔离某项目时只关闭该项目开关，不删除 Run、Artifact、Evaluation、budget reservation 或 gate decision。恢复时重新开启，并从运行中心受控重试。

禁止通过删除 job、provider task id 或 budget reservation“修复”卡住状态；这可能造成重复付费。应先查看 lease、`next_retry_at`、provider task id 和 RunEvent，再按运行手册恢复。

T5 交付包和客户反馈是审计快照，不参与原地回滚。需要修改时创建新的上游 Artifact 和修订 Run，旧交付包保持可复验。
