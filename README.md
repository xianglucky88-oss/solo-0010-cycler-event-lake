# 电池台架事件时间数据湖

**数据工程 · 数仓与数据湖。** 将离线台架的分块电流采样转成封存区间事实。采样入湖认出块、设备、开机段与样本修订；按来源优先级、设备进度、可用时段证据和校时锚点选择每个时段的来源；接缝缺证据不跨接、不积分，也不卡住可独立封存的尾段；签收与更正分层结算，新覆盖与旧回执更正分列。不控制设备，不依赖消息队列。

在仓库根目录以 Python 3.9+ 运行：

```sh
python3 -B -m cyclerlake --db demo.sqlite ingest examples/initial.json --gap-ms 12
python3 -B -m cyclerlake --db demo.sqlite query bench-larch
python3 -B examples/smoke.py
```

首块只封存到 0 毫秒，尚无可积分区间。离线演练导入第二块乱序采样，得到 `2000 μC`、一个缺口和一行迟到拒收证据。详见 [PROJECT_SPEC.md](PROJECT_SPEC.md)。

## 主备切换演练

`examples/handover/` 给出主采集器切到后备的完整材料：来源优先级、两台设备的校时锚点（各自把本地毫秒平移到规范时间轴）、可用与不可用时段证据、三块采样、接缝证据和一份更正。

```sh
# 声明材料：优先级、锚点、可用时段
python3 -B -m cyclerlake --db demo.sqlite priority examples/handover/priority.json
python3 -B -m cyclerlake --db demo.sqlite anchor examples/handover/anchor-primary.json
python3 -B -m cyclerlake --db demo.sqlite anchor examples/handover/anchor-backup.json
python3 -B -m cyclerlake --db demo.sqlite availability examples/handover/availability-primary.json
python3 -B -m cyclerlake --db demo.sqlite availability examples/handover/availability-primary-down.json
python3 -B -m cyclerlake --db demo.sqlite availability examples/handover/availability-backup.json

# 入湖三块采样；后备区间独立封存，接缝保持待补证窗口
python3 -B -m cyclerlake --db demo.sqlite ingest examples/handover/chunk-primary-1.json --gap-ms 12
python3 -B -m cyclerlake --db demo.sqlite ingest examples/handover/chunk-backup-1.json --gap-ms 12
python3 -B -m cyclerlake --db demo.sqlite ingest examples/handover/chunk-backup-2.json --gap-ms 12
python3 -B -m cyclerlake --db demo.sqlite query bench-switch

# 旧批先签收；后备区间可单独取证，先单独签出并保留断点
python3 -B -m cyclerlake --db demo.sqlite sign bench-switch 1000 1030
python3 -B -m cyclerlake --db demo.sqlite sign bench-switch 1030 1070
python3 -B -m cyclerlake --db demo.sqlite sign bench-switch 1070 1100

# 更正只调整它所针对的回执；重复申请不重复抵扣
python3 -B -m cyclerlake --db demo.sqlite correct examples/handover/correction-handover.json

# 接缝补证后，接缝区间封存为新覆盖并补签
python3 -B -m cyclerlake --db demo.sqlite seam examples/handover/seam-primary-backup.json
python3 -B -m cyclerlake --db demo.sqlite sign bench-switch 1030 1035
python3 -B -m cyclerlake --db demo.sqlite settle bench-switch
```

`query` 核出各时段选中的来源、各来源进度与待补证窗口；`settle` 分列新覆盖电荷与旧回执的正负更正，并按回执保留原值。也可以不先签后备区间，留待接缝证据到达后整段一致再签（`examples/smoke.py` 中 `bench-switch-b` 演练该路径），代价是这一段暂不可结算；两种选择都不拖住无关尾段。
