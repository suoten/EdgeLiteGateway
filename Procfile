# 注: 采集调度器/规则引擎随 web 进程内运行（无独立 worker 入口模块），
# 原 worker 行指向不存在的 edgelite.scheduler 模块，启动即崩，已移除
web: python -m edgelite --host 0.0.0.0 --port ${PORT:-8080}
