"""项目回收站相关的共享常量。

单独成一个零依赖的小文件，是因为 ``PROJECT_RECYCLE_BIN_RETENTION_S`` /
``ACCOUNT_DELETE_RETENTION_S`` 被 :mod:`app.domain.projects` 包内多个关注点
模块（``listing``、``lifecycle``）以及包外的 :mod:`app.domain.account_deletion`
共用；放进任何一个业务模块都会造成另一侧对着「业务逻辑」文件做纯常量导入。
"""
from __future__ import annotations

# 软删除项目在回收站保留的时长；到期由 sweep_expired_deleted_projects 彻底清理。
# 判据是 deleted_at 时间戳（见 app/db.py MIGRATIONS 的列注释），这个常量只用来
# 算「到期时间」，不是驱动清理的计时器本身。
PROJECT_RECYCLE_BIN_RETENTION_S = 24 * 3600

# 账号级联软删除（管理员删账号）带出的项目改用这个更长的保留期，见
# sweep_expired_deleted_projects() 与 app.domain.account_deletion。
ACCOUNT_DELETE_RETENTION_S = 30 * 24 * 3600
