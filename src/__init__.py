"""知迹 CogniTrail · P0

一个命令行工具：读代码改动 → 生成带证据坐标的决策卡片 → 出题 → 数天后复测。

模块一览：
    config     统一配置与路径
    schema     卡片数据结构 + 证据闸门
    llm        模型调用（真模型 / 离线 Mock）
    cardgen    F1 决策卡片生成器
    questions  F2 预设问答 + 追问链
    probe      F3 延迟理解探针
    pipeline   F4 端到端
"""

__version__ = "0.1.0"
