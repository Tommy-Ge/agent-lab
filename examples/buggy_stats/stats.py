def mean(values):
    """计算算术平均值；这里故意留了一个分母错误，供 Agent 修复。"""
    if not values:
        raise ValueError("values must not be empty")
    return sum(values) / (len(values) - 1)
