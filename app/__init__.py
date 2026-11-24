"""声级档案迁移核验 API。

模块划分：
- csvio:   CSV 解析、整行多重集（频次）与内容哈希
- db:      SQLite 连接、表结构与事务
- errors:  统一错误类型
- services:核心业务（旧卷提交/签发、分片替换保护、查询）
- demo:    “两卷争用同一份读数”的一键演示
- main:    FastAPI 路由与 /docs
"""

__version__ = "1.1.0"
