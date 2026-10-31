# 修 bug 练习

`stats.py` 的 `mean` 有意包含一个分母错误。三个测试覆盖普通输入、单元素和空输入；修复前应有失败，不属于 Agent Lab 自身的回归失败。

先复制此目录，再让 Agent 操作副本。运行命令：

```bash
python3 -m unittest -v
```

验收目标：修正 `stats.py`，保持三个测试不变，让它们全部通过。
