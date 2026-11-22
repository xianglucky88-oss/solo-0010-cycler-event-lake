# 电池台架事件时间数据湖

**数据工程 · 数仓与数据湖。** 将离线台架的分块电流采样转成封存区间事实，记录步骤端点的来源，并对过长缺口和迟到采样分别产出质量证据；在此之上，把采样与独立电量计、缺测能力和试验/结算时钟关系联合起来，逐工步给出仍可能的充放范围与签发账单。不控制设备，不依赖消息队列。

在仓库根目录以 Python 3.9+ 运行：

```sh
python3 -B -m cyclerlake --db demo.sqlite ingest examples/initial.json --gap-ms 12
python3 -B -m cyclerlake --db demo.sqlite query bench-larch
python3 -B examples/smoke.py
python3 -B examples/settlement_smoke.py
```

首块只封存到 0 毫秒，尚无可积分区间。离线演练导入第二块乱序采样，得到 `2000 μC`、一个缺口和一行迟到拒收证据。

## 结算

把工步、表窗、缺测能力和时钟装入湖，再逐工步结算：

```sh
python3 -B -m cyclerlake --db demo.sqlite configure examples/settlement.json
python3 -B -m cyclerlake --db demo.sqlite settle            # 全部工步的区间与证据
python3 -B -m cyclerlake --db demo.sqlite settle --step S2  # 单个工步
python3 -B -m cyclerlake --db demo.sqlite sign S2 --mode pre      # 先签确定项
python3 -B -m cyclerlake --db demo.sqlite sign S3 --mode group    # 等整组一起关账
python3 -B -m cyclerlake --db demo.sqlite history S2             # 逐版账单
python3 -B -m cyclerlake --db demo.sqlite query bench-larch      # 运行事实 + 结算分类
```

`settle` 对每个工步给出：

- `observed`：仅来自封存梯形积分的实测充入/放出；
- `possible`：在所有相容解释下仍可能的 `in`/`out`/`net` 区间（精确分数字符串，联合求解，不拼接各账极值）；
- `status`：`observed` / `missing`（确认缺测）/ `meter_ready` / `meter_pending`（有表证但未确定）/ `unsealed` / `contradiction` / `conflict`；
- `witness`：支撑它的采样端点、表读数或缺测能力；
- 若整组无解，`conflict_core` 点名互相冲突的表读数与封存事实。

账单只追加：新证据与旧账冲突时，查询仍显示实测并标出差异与原签账，从不静默重写。详见 [PROJECT_SPEC.md](PROJECT_SPEC.md)。
