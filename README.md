# 电池台架事件时间数据湖

**数据工程 · 数仓与数据湖。** 将离线台架的分块电流采样转成封存区间事实，记录步骤端点的来源，并对过长缺口和迟到采样分别产出质量证据。不控制设备，不依赖消息队列。

在仓库根目录以 Python 3.9+ 运行：

```sh
python3 -B -m cyclerlake --db demo.sqlite ingest examples/initial.json --gap-ms 12
python3 -B -m cyclerlake --db demo.sqlite query bench-larch
python3 -B examples/smoke.py
```

首块只封存到 0 毫秒，尚无可积分区间。离线演练导入第二块乱序采样，得到 `2000 μC`、一个缺口和一行迟到拒收证据。详见 [PROJECT_SPEC.md](PROJECT_SPEC.md)。
