import argparse
import datetime as dt
import json
import os
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv


# 兼容你现有脚本的 .env 位置（../.env），同时也支持默认的当前目录 .env
load_dotenv()
load_dotenv("../.env")


DATA_API_BASE = os.getenv("PM_DATA_API_BASE", "https://data-api.polymarket.com").rstrip("/")
GAMMA_API_BASE = os.getenv("PM_GAMMA_API_BASE", "https://gamma-api.polymarket.com").rstrip("/")


def _parse_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _now_iso() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{_now_iso()}] {msg}")


def _format_ts(ts: Optional[int]) -> str:
    if ts is None:
        return "-"
    try:
        return dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def _parse_time_arg(s: str) -> int:
    """
    支持：
    - epoch 秒（纯数字）
    - ISO 日期/时间：YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS 或 YYYY-MM-DDTHH:MM:SS
      （不带时区时按“本机本地时间”解释）
    """
    raw = (s or "").strip()
    if not raw:
        raise ValueError("空时间参数")
    if raw.isdigit():
        return int(raw)
    raw = raw.replace("T", " ")
    # 允许只给日期
    if len(raw) == 10:
        raw = raw + " 00:00:00"
    try:
        d = dt.datetime.fromisoformat(raw)
    except Exception as e:
        raise ValueError(f"无法解析时间：{s}") from e
    return int(d.timestamp())


def filter_trades_by_time(
    trades: List[Dict[str, Any]],
    *,
    start_ts: Optional[int],
    end_ts: Optional[int],
) -> List[Dict[str, Any]]:
    """
    过滤规则：
    - start_ts：包含 >= start_ts
    - end_ts：包含 <= end_ts
    """
    if start_ts is None and end_ts is None:
        return trades
    out: List[Dict[str, Any]] = []
    for t in trades:
        ts = t.get("timestamp")
        try:
            ts_i = int(ts)
        except Exception:
            # 没 timestamp 的记录直接跳过
            continue
        if start_ts is not None and ts_i < start_ts:
            continue
        if end_ts is not None and ts_i > end_ts:
            continue
        out.append(t)
    return out


def fetch_all_trades(
    user: str,
    *,
    limit: int = 200,
    max_trades: Optional[int] = None,
    timeout_s: int = 20,
) -> List[Dict[str, Any]]:
    """
    拉取某个 user 的全部 trades（会用 offset 进行分页）。
    data-api 返回的是数组，不含 nextCursor，因此用 offset/limit 循环直到返回空或不足一页。
    """
    trades: List[Dict[str, Any]] = []
    offset = 0
    while True:
        params = {"user": user, "limit": limit, "offset": offset}
        r = requests.get(f"{DATA_API_BASE}/trades", params=params, timeout=timeout_s)
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list):
            raise RuntimeError(f"意外的 /trades 返回格式（不是数组）：{type(batch)}")

        if not batch:
            break

        trades.extend(batch)
        offset += len(batch)

        if max_trades is not None and len(trades) >= max_trades:
            trades = trades[:max_trades]
            break

        # 最后一页通常会少于 limit
        if len(batch) < limit:
            break

    return trades


def fetch_market_by_condition_id(condition_id: str, *, timeout_s: int = 20) -> Optional[Dict[str, Any]]:
    """
    用 gamma API 根据 condition_id 获取 market 信息。
    注意：gamma 的过滤参数是 condition_ids（下划线），不是 conditionId。
    """
    params = {"condition_ids": condition_id, "limit": 1}
    r = requests.get(f"{GAMMA_API_BASE}/markets", params=params, timeout=timeout_s)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or not data:
        return None
    return data[0]


def _coerce_json_list(x: Any) -> Optional[List[Any]]:
    # gamma API 有时会把数组字段序列化成字符串，例如 '["Up","Down"]'
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        s = x.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                v = json.loads(s)
                return v if isinstance(v, list) else None
            except Exception:
                return None
    return None


def _extract_outcomes_and_prices(market: Dict[str, Any]) -> Tuple[List[str], List[float]]:
    outcomes_any = _coerce_json_list(market.get("outcomes")) or []
    prices_any = _coerce_json_list(market.get("outcomePrices")) or []
    if not outcomes_any or not prices_any or len(outcomes_any) != len(prices_any):
        return [], []
    outcomes = [str(x) for x in outcomes_any]
    prices = [_parse_float(p, default=0.0) for p in prices_any]
    return outcomes, prices


def infer_winning_outcome(market: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    基于 outcomePrices 推断是否“已结算”，以及胜出的 outcome 文本（例如 "Yes"/"No"）。

    经验规则（尽量稳健，不依赖 undocumented 字段）：
    - market.closed == True
    - outcomePrices 中最大值接近 1，最小值接近 0
    """
    if market.get("closed") is not True:
        return False, None

    outcomes, prices = _extract_outcomes_and_prices(market)
    if not outcomes or not prices:
        return False, None

    max_i = max(range(len(prices)), key=lambda i: prices[i])
    max_p = prices[max_i]
    min_p = min(prices)

    # 结算后通常接近 (1, 0)；这里留一点浮动空间
    if max_p >= 0.99 and min_p <= 0.01:
        return True, str(outcomes[max_i])

    return False, None


def summarize(
    trades: List[Dict[str, Any]],
    *,
    win_mode: str = "net_position",
    gamma_timeout_s: int = 20,
    progress_every: int = 100,
    include_unsettled_mtm: bool = False,
) -> Dict[str, Any]:
    """
    输出统计口径：
    - total_trades: 成交记录条数（fills）
    - markets_traded: 参与过的市场数（按 conditionId 去重）
    - settled_markets: 其中已结算的市场数（通过 gamma 推断）
    - wins/losses: 在已结算市场里，你“胜/负”的次数
      - win_mode=net_position（默认）：按你在该市场每个 outcome 的净持仓（BUY-Sell）判断你站哪边
      - win_mode=ever_bought：只要你曾 BUY 过胜方 outcome，就算胜（更宽松）
    """
    if win_mode not in ("net_position", "ever_bought"):
        raise ValueError("win_mode 只能是 net_position 或 ever_bought")

    by_condition: Dict[str, Dict[str, Any]] = {}

    total_buy_cost = 0.0
    total_sell_proceeds = 0.0
    total_shares_bought = 0.0

    for t in trades:
        cid = t.get("conditionId")
        if not cid:
            continue
        cid = str(cid)
        outcome = str(t.get("outcome") or "")
        side = str(t.get("side") or "").upper()
        size = _parse_float(t.get("size"), default=0.0)
        price = _parse_float(t.get("price"), default=0.0)
        notional = size * price
        ts_raw = t.get("timestamp")
        try:
            ts_i = int(ts_raw)
        except Exception:
            ts_i = None

        rec = by_condition.setdefault(
            cid,
            {
                "trades": 0,
                "net_shares_by_outcome": {},  # outcome -> float
                "ever_bought": set(),  # set(outcome)
                "buy_cost": 0.0,
                "sell_proceeds": 0.0,
                "first_ts": None,
                "last_ts": None,
                "sample": {
                    "title": t.get("title"),
                    "slug": t.get("slug"),
                    "eventSlug": t.get("eventSlug"),
                },
            },
        )

        rec["trades"] += 1
        if ts_i is not None:
            if rec["first_ts"] is None or ts_i < rec["first_ts"]:
                rec["first_ts"] = ts_i
            if rec["last_ts"] is None or ts_i > rec["last_ts"]:
                rec["last_ts"] = ts_i

        if side == "BUY":
            rec["buy_cost"] += notional
            total_buy_cost += notional
            total_shares_bought += size
        elif side == "SELL":
            rec["sell_proceeds"] += notional
            total_sell_proceeds += notional

        if outcome:
            net_map: Dict[str, float] = rec["net_shares_by_outcome"]
            net_map.setdefault(outcome, 0.0)
            if side == "BUY":
                net_map[outcome] += size
                rec["ever_bought"].add(outcome)
            elif side == "SELL":
                net_map[outcome] -= size

    total_trades = len(trades)
    markets_traded = len(by_condition)

    wins = 0
    losses = 0
    unsettled_or_unknown = 0
    no_position = 0
    negative_net_position_markets = 0

    settled_value_total = 0.0
    settled_pnl_total = 0.0
    unsettled_mtm_value_total = 0.0

    # 风险回报分析（仅对“已结算且可判定胜负”的市场）
    total_profit_from_wins = 0.0
    total_loss_from_losses = 0.0

    # 时间序列（用于最大回撤、连胜/连败）：以“已结算且可判定胜负”的市场为单位
    settled_events: List[Tuple[int, float, bool]] = []  # (event_ts, pnl, is_win)

    market_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    resolved_cache: Dict[str, Tuple[bool, Optional[str]]] = {}

    items = list(by_condition.items())
    for i, (cid, rec) in enumerate(items, start=1):
        if progress_every > 0 and (i == 1 or i % progress_every == 0 or i == len(items)):
            log(f"结算判定进度: {i}/{len(items)}")
        if cid not in market_cache:
            market_cache[cid] = fetch_market_by_condition_id(cid, timeout_s=gamma_timeout_s)
        market = market_cache[cid]
        if market is None:
            unsettled_or_unknown += 1
            continue

        if cid not in resolved_cache:
            resolved_cache[cid] = infer_winning_outcome(market)
        resolved, winning_outcome = resolved_cache[cid]
        outcomes, prices = _extract_outcomes_and_prices(market)

        cashflow = float(rec["sell_proceeds"]) - float(rec["buy_cost"])
        # 净成本：买入-卖出（>=0 表示投入，<0 表示净回收现金）
        net_cost = float(rec["buy_cost"]) - float(rec["sell_proceeds"])

        if not resolved or not winning_outcome:
            if include_unsettled_mtm and outcomes and prices:
                price_by_outcome = {o: p for o, p in zip(outcomes, prices)}
                mtm_value = 0.0
                for o, shares in rec["net_shares_by_outcome"].items():
                    mtm_value += float(shares) * float(price_by_outcome.get(o, 0.0))
                unsettled_mtm_value_total += mtm_value
            else:
                unsettled_or_unknown += 1
            continue

        net_map = rec["net_shares_by_outcome"]
        winning_shares = float(net_map.get(winning_outcome, 0.0))
        settled_value = winning_shares * 1.0
        pnl = cashflow + settled_value
        settled_value_total += settled_value
        settled_pnl_total += pnl

        # 是否“胜场/负场”的判定：沿用胜率统计的口径
        is_win = False
        is_loss = False
        if win_mode == "ever_bought":
            is_win = winning_outcome in rec["ever_bought"]
            is_loss = not is_win
        else:
            if net_map:
                best_outcome, best_shares = max(net_map.items(), key=lambda kv: kv[1])
                if best_shares > 0:
                    is_win = best_outcome == winning_outcome
                    is_loss = not is_win

        # 你要求的口径：
        # - total_profit_from_wins：所有胜场的 (结算价值 - 成本) 之和
        #   这里用 (settled_value - net_cost)；并且只累计正数部分，避免出现“胜场但交易导致为负”拉低 profit factor
        # - total_loss_from_losses：所有负场的 成本 之和（取绝对值）
        #   这里用 max(net_cost, 0)，避免出现净成本<=0 时把 loss 记成负数
        if is_win:
            profit = settled_value - net_cost
            if profit > 0:
                total_profit_from_wins += profit
            if rec.get("last_ts") is not None:
                settled_events.append((int(rec["last_ts"]), float(pnl), True))
        elif is_loss:
            loss_cost = net_cost if net_cost > 0 else 0.0
            total_loss_from_losses += abs(loss_cost)
            if rec.get("last_ts") is not None:
                settled_events.append((int(rec["last_ts"]), float(pnl), False))

        if win_mode == "ever_bought":
            if winning_outcome in rec["ever_bought"]:
                wins += 1
            else:
                losses += 1
            continue

        # net_position
        if not net_map:
            no_position += 1
            continue

        # 找到净持仓最大的 outcome，作为“你站的方向”
        best_outcome, best_shares = max(net_map.items(), key=lambda kv: kv[1])
        if best_shares <= 0:
            if best_shares < 0:
                negative_net_position_markets += 1
            no_position += 1
            continue

        if best_outcome == winning_outcome:
            wins += 1
        else:
            losses += 1

    settled_markets = wins + losses + no_position

    winrate = None
    denom = wins + losses
    if denom > 0:
        winrate = wins / denom

    profit_factor = None
    if total_loss_from_losses > 0:
        profit_factor = total_profit_from_wins / total_loss_from_losses

    avg_win = None
    if wins > 0:
        avg_win = total_profit_from_wins / wins

    avg_loss = None
    if losses > 0:
        avg_loss = total_loss_from_losses / losses

    win_loss_ratio = None
    if avg_win is not None and avg_loss is not None and avg_loss > 0:
        win_loss_ratio = avg_win / avg_loss

    roi = None
    if total_buy_cost > 0:
        roi = settled_pnl_total / total_buy_cost

    mtm_roi = None
    if include_unsettled_mtm and total_buy_cost > 0:
        mtm_roi = float((total_sell_proceeds - total_buy_cost) + settled_value_total + unsettled_mtm_value_total) / total_buy_cost

    avg_entry_price = None
    if total_shares_bought > 0:
        avg_entry_price = total_buy_cost / total_shares_bought

    # 最大回撤 & 连胜/连败（按 settled_events 时间排序）
    max_drawdown = None
    max_drawdown_pct = None
    max_win_streak = None
    max_loss_streak = None
    if settled_events:
        settled_events.sort(key=lambda x: x[0])
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        peak_at_max = 0.0

        cur_win = 0
        cur_loss = 0
        max_w = 0
        max_l = 0

        for _, pnl, is_win_ev in settled_events:
            cum += pnl
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd
                peak_at_max = peak

            if is_win_ev:
                cur_win += 1
                cur_loss = 0
            else:
                cur_loss += 1
                cur_win = 0
            if cur_win > max_w:
                max_w = cur_win
            if cur_loss > max_l:
                max_l = cur_loss

        max_drawdown = max_dd
        if peak_at_max > 0:
            max_drawdown_pct = max_dd / peak_at_max
        max_win_streak = max_w
        max_loss_streak = max_l

    return {
        "total_trades": total_trades,
        "markets_traded": markets_traded,
        "settled_markets": settled_markets,
        "wins": wins,
        "losses": losses,
        "winrate": winrate,
        "no_position": no_position,
        "unsettled_or_unknown": unsettled_or_unknown,
        "win_mode": win_mode,
        "total_buy_cost": total_buy_cost,
        "total_sell_proceeds": total_sell_proceeds,
        "net_cashflow": total_sell_proceeds - total_buy_cost,
        "settled_value_total": settled_value_total,
        "settled_pnl_total": settled_pnl_total,
        "unsettled_mtm_value_total": unsettled_mtm_value_total if include_unsettled_mtm else None,
        "mtm_pnl_total": (total_sell_proceeds - total_buy_cost) + settled_value_total + unsettled_mtm_value_total
        if include_unsettled_mtm
        else None,
        "negative_net_position_markets": negative_net_position_markets,
        "include_unsettled_mtm": include_unsettled_mtm,
        # 风险回报分析
        "total_profit_from_wins": total_profit_from_wins,
        "total_loss_from_losses": total_loss_from_losses,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "win_loss_ratio": win_loss_ratio,
        # ROI / 入场均价
        "roi": roi,
        "mtm_roi": mtm_roi,
        "total_shares_bought": total_shares_bought,
        "avg_entry_price": avg_entry_price,
        # 回撤 & 连胜连败（以“已结算市场=一笔交易”为单位）
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": max_drawdown_pct,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="统计你在 Polymarket 的交易次数与胜场数（基于 data-api + gamma-api）。")
    parser.add_argument("--user", default=os.getenv("PM_ADDRESS"), help="钱包地址（默认读取环境变量 PM_ADDRESS）")
    parser.add_argument("--limit", type=int, default=200, help="每页拉取 trades 的条数（默认 200）")
    parser.add_argument("--max-trades", type=int, default=None, help="最多拉取多少条 trades（用于快速试跑）")
    parser.add_argument("--start", default=None, help="统计起始时间（epoch 秒或 YYYY-MM-DD[ HH:MM:SS]）")
    parser.add_argument("--end", default=None, help="统计结束时间（epoch 秒或 YYYY-MM-DD[ HH:MM:SS]）")
    parser.add_argument("--since-days", type=float, default=None, help="统计最近 N 天（会覆盖 --start/--end）")
    parser.add_argument(
        "--win-mode",
        choices=["net_position", "ever_bought"],
        default="net_position",
        help="胜负判定口径：net_position（默认）或 ever_bought（更宽松）",
    )
    parser.add_argument("--progress-every", type=int, default=100, help="每处理多少个市场打印一次进度（默认 100；设为 0 关闭）")
    parser.add_argument(
        "--include-unsettled-mtm",
        action="store_true",
        help="可选：对未结算市场按 gamma 当前 outcomePrices 做估值（mark-to-market），并给出包含未结算的总盈亏",
    )
    parser.add_argument("--json", dest="json_path", default=None, help="可选：把统计结果写入 JSON 文件")
    args = parser.parse_args()

    if not args.user:
        raise SystemExit("未提供 --user，且环境变量 PM_ADDRESS 也未配置。")

    user = str(args.user).strip()
    log(f"开始统计 user={user}")
    log(f"data-api: {DATA_API_BASE}")
    log(f"gamma-api: {GAMMA_API_BASE}")

    trades = fetch_all_trades(user, limit=args.limit, max_trades=args.max_trades)
    log(f"已拉取 trades 条数: {len(trades)}")

    start_ts = _parse_time_arg(args.start) if args.start else None
    end_ts = _parse_time_arg(args.end) if args.end else None
    if args.since_days is not None:
        now_ts = int(dt.datetime.now().timestamp())
        start_ts = int(now_ts - float(args.since_days) * 86400.0)
        end_ts = now_ts

    trades_filtered = filter_trades_by_time(trades, start_ts=start_ts, end_ts=end_ts)
    if start_ts is not None or end_ts is not None:
        log(f"时间范围过滤: start={_format_ts(start_ts)} end={_format_ts(end_ts)}")
        log(f"过滤后 trades 条数: {len(trades_filtered)}")

    stats = summarize(
        trades_filtered,
        win_mode=args.win_mode,
        progress_every=args.progress_every,
        include_unsettled_mtm=bool(args.include_unsettled_mtm),
    )

    print("")
    print("======== 统计结果 ========")
    print(f"总成交次数（trades/fills）: {stats['total_trades']}")
    print(f"参与市场数（按 conditionId 去重）: {stats['markets_traded']}")
    print(f"已结算市场数（可判定胜负）: {stats['settled_markets']}")
    print(f"胜场数: {stats['wins']}")
    print(f"负场数: {stats['losses']}")
    if stats.get("winrate") is not None:
        print(f"胜率（wins/(wins+losses)）: {float(stats['winrate'])*100:.2f}%")
    print(f"无法判定（已结算但你无净持仓/或无数据）: {stats['no_position']}")
    print(f"未结算或无法获取（gamma 无法确认）: {stats['unsettled_or_unknown']}")
    print(f"胜负口径（win_mode）: {stats['win_mode']}")
    print("==========================")

    print("")
    print("======== 资金统计（USDC） ========")
    print(f"总买入成本（BUY size*price）: {stats['total_buy_cost']:.6f}")
    print(f"总卖出回款（SELL size*price）: {stats['total_sell_proceeds']:.6f}")
    print(f"净现金流（卖出-买入）: {stats['net_cashflow']:.6f}")
    print(f"已结算市场：结算兑付总价值: {stats['settled_value_total']:.6f}")
    print(f"已结算市场：总盈亏（PnL）: {stats['settled_pnl_total']:.6f}")
    if stats.get("roi") is not None:
        print(f"ROI（PnL/总买入成本）: {float(stats['roi'])*100:.2f}%")
    else:
        print("ROI（PnL/总买入成本）: -")
    if stats.get("avg_entry_price") is not None:
        print(f"平均入场价格（Avg Entry）: {float(stats['avg_entry_price']):.6f}  (总买入成本/总买入份额 {stats['total_shares_bought']:.6f})")
    else:
        print("平均入场价格（Avg Entry）: -")
    if stats.get("include_unsettled_mtm"):
        print(f"未结算市场：估值总价值（MTM）: {float(stats['unsettled_mtm_value_total']):.6f}")
        print(f"包含未结算市场：总盈亏（MTM PnL）: {float(stats['mtm_pnl_total']):.6f}")
        if stats.get("mtm_roi") is not None:
            print(f"包含未结算市场：ROI（MTM PnL/总买入成本）: {float(stats['mtm_roi'])*100:.2f}%")
    if stats.get("negative_net_position_markets", 0):
        print(f"⚠️ 检测到净持仓为负的市场数（可能为异常/或短仓情况）: {stats['negative_net_position_markets']}")
    print("===============================")

    print("")
    print("======== 风险回报分析（已结算市场） ========")
    print(f"总盈利（胜场）：Σ(max(结算价值-成本, 0)) = {stats['total_profit_from_wins']:.6f}")
    print(f"总亏损（负场）：Σ(成本) = {stats['total_loss_from_losses']:.6f}")
    if stats.get("profit_factor") is None:
        print("盈利因子（Profit Factor）: -（没有亏损样本或亏损为 0）")
    else:
        print(f"盈利因子（Profit Factor）: {float(stats['profit_factor']):.4f}")
    if stats.get("avg_win") is not None:
        print(f"平均盈利（avg_win）: {float(stats['avg_win']):.6f}")
    else:
        print("平均盈利（avg_win）: -")
    if stats.get("avg_loss") is not None:
        print(f"平均亏损（avg_loss）: {float(stats['avg_loss']):.6f}")
    else:
        print("平均亏损（avg_loss）: -")
    if stats.get("win_loss_ratio") is not None:
        print(f"盈亏比（avg_win/avg_loss）: {float(stats['win_loss_ratio']):.4f}")
    else:
        print("盈亏比（avg_win/avg_loss）: -")
    print("=========================================")

    print("")
    print("======== 回撤 / 连胜连败（已结算市场按时间排序） ========")
    if stats.get("max_drawdown") is None:
        print("最大回撤（Max Drawdown）: -")
    else:
        s = f"{float(stats['max_drawdown']):.6f}"
        if stats.get("max_drawdown_pct") is not None:
            s += f"  ({float(stats['max_drawdown_pct'])*100:.2f}%)"
        print(f"最大回撤（Max Drawdown）: {s}")
    if stats.get("max_win_streak") is not None:
        print(f"最长连胜（Max Win Streak）: {int(stats['max_win_streak'])}")
    else:
        print("最长连胜（Max Win Streak）: -")
    if stats.get("max_loss_streak") is not None:
        print(f"最长连败（Max Loss Streak）: {int(stats['max_loss_streak'])}")
    else:
        print("最长连败（Max Loss Streak）: -")
    print("==============================================")

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        log(f"已写入: {args.json_path}")


if __name__ == "__main__":
    main()

