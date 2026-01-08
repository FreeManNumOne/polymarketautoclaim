# polymarketautoclaim

这个仓库目前包含两类脚本：

- `polymarket_redeem_bot.py`：自动领取（redeem）已结算的获胜仓位（链上执行）。
- `polymarket_trade_stats.py`：统计你在 Polymarket 的**成交次数**、参与市场数，以及（可判定的）**胜/负场数**（纯 API 读取，不发链上交易）。

## 环境变量

你可以在 `.env`（或上层目录 `../.env`）里配置：

- `PM_ADDRESS`：你的钱包地址（脚本默认用它当作 `--user`）。
- `POLYMARKET_PRIVATE_KEY`：仅 `polymarket_redeem_bot.py` 需要。

## 交易统计脚本用法

默认读取 `PM_ADDRESS`：

```bash
python3 polymarket_trade_stats.py
```

手动指定地址：

```bash
python3 polymarket_trade_stats.py --user 0xYourAddressHere
```

胜负判定口径（默认 `net_position`；更宽松的 `ever_bought` 也可用）：

```bash
python3 polymarket_trade_stats.py --win-mode net_position
python3 polymarket_trade_stats.py --win-mode ever_bought
```

可选：对未结算市场做估值（mark-to-market），输出包含未结算的总盈亏：

```bash
python3 polymarket_trade_stats.py --include-unsettled-mtm
```

可选：输出 JSON：

```bash
python3 polymarket_trade_stats.py --json stats.json
```