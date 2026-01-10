import time
import requests
import datetime
from web3 import Web3
from eth_account import Account
from typing import Optional
from dotenv import load_dotenv
import os
import multiprocessing as mp


load_dotenv("../.env")

# ================= 配置 =================

# 1. 你的钱包私钥（Proxy 的 Owner）
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")

# 2. Proxy 钱包地址（Gnosis Safe）
PROXY_ADDRESS = os.getenv("PM_ADDRESS")

# 3. Polygon RPC
# 建议使用 Alchemy/Infura 等更稳定的 RPC
RPC_URL = "https://polygon-rpc.com"

# 是否自动领取“输掉的仓位”（curPrice≈0）：
# - 默认 False：避免花 gas 去领取 0
# - 如果你希望把输仓也一并 redeem（有些情况下用于清理仓位/解锁状态），设为 true
REDEEM_LOSING_POSITIONS = os.getenv("REDEEM_LOSING_POSITIONS", "false").strip().lower() in ("1", "true", "yes", "y")

# 检查间隔（秒）(15 分钟 = 900 秒)
CHECK_INTERVAL = 5 * 60

# 单次执行的最大允许运行时长（秒）；用于 cron 防卡死
RUN_TIMEOUT_SECONDS = int(os.getenv("RUN_TIMEOUT_SECONDS", "180"))

# ================= 常量与 ABI =================

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Polymarket email/Builder 合约钱包（当前观察到的实现）会把“owner 合约地址”存到一个固定 slot。
# 该 slot 来自钱包实现合约中的 PUSH32 常量（链上可验证）。
WALLET_OWNER_SLOT = int(
    "0x734a2a5caf82146a5ddd5263d9af379f9f72724959f0567ddc9df2c40cf2cc20",
    16,
)

CTF_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

SAFE_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "value", "type": "uint256"},
            {"internalType": "bytes", "name": "data", "type": "bytes"},
            {"internalType": "enum Enum.Operation", "name": "operation", "type": "uint8"},
            {"internalType": "uint256", "name": "safeTxGas", "type": "uint256"},
            {"internalType": "uint256", "name": "baseGas", "type": "uint256"},
            {"internalType": "uint256", "name": "gasPrice", "type": "uint256"},
            {"internalType": "address", "name": "gasToken", "type": "address"},
            {"internalType": "address", "name": "refundReceiver", "type": "address"},
            {"internalType": "bytes", "name": "signatures", "type": "bytes"},
        ],
        "name": "execTransaction",
        "outputs": [{"internalType": "bool", "name": "success", "type": "bool"}],
        "stateMutability": "payable",
        "type": "function",
    }
    ,
    {
        "inputs": [],
        "name": "nonce",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getOwners",
        "outputs": [{"internalType": "address[]", "name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getThreshold",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "value", "type": "uint256"},
            {"internalType": "bytes", "name": "data", "type": "bytes"},
            {"internalType": "enum Enum.Operation", "name": "operation", "type": "uint8"},
            {"internalType": "uint256", "name": "safeTxGas", "type": "uint256"},
            {"internalType": "uint256", "name": "baseGas", "type": "uint256"},
            {"internalType": "uint256", "name": "gasPrice", "type": "uint256"},
            {"internalType": "address", "name": "gasToken", "type": "address"},
            {"internalType": "address", "name": "refundReceiver", "type": "address"},
            {"internalType": "uint256", "name": "_nonce", "type": "uint256"},
        ],
        "name": "getTransactionHash",
        "outputs": [{"internalType": "bytes32", "name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function",
    },
]

WALLET_PROXY_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "uint8", "name": "operation", "type": "uint8"},
                    {"internalType": "address", "name": "to", "type": "address"},
                    {"internalType": "uint256", "name": "value", "type": "uint256"},
                    {"internalType": "bytes", "name": "data", "type": "bytes"},
                ],
                "internalType": "struct Call[]",
                "name": "calls",
                "type": "tuple[]",
            }
        ],
        "name": "proxy",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


def log(message: str) -> None:
    """输出带时间戳的日志。"""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}")


def get_raw_tx_bytes(signed_tx):
    """兼容不同 Web3.py 版本的 rawTransaction 字段名。"""
    if hasattr(signed_tx, "raw_transaction"):
        return signed_tx.raw_transaction
    if hasattr(signed_tx, "rawTransaction"):
        return signed_tx.rawTransaction
    if isinstance(signed_tx, dict) and "rawTransaction" in signed_tx:
        return signed_tx["rawTransaction"]
    return signed_tx[0] if isinstance(signed_tx, (tuple, list)) else signed_tx


def get_redeemable_positions(proxy_address: str):
    """
    返回可领取（redeemable=true）的仓位列表（包含赢/输两种情况）。
    同时根据 outcomeIndex 推导 indexSet，避免对没持仓的 indexSet 调用 redeemPositions 触发 revert。
    """
    log("🔍 通过 API 检查可领取（redeemable）的仓位...")
    url = "https://data-api.polymarket.com/positions"
    params = {"user": proxy_address, "redeemable": "true", "limit": 50}

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        out = []
        skipped_bad = 0

        for item in data:
            try:
                cur_price = float(item.get("curPrice", 0) or 0)
            except Exception:
                cur_price = 0.0

            try:
                size = float(item.get("size", 0) or 0)
            except Exception:
                size = 0.0
            if size <= 0:
                continue

            condition_id = item.get("conditionId")
            outcome_index = item.get("outcomeIndex")
            if not condition_id or outcome_index is None:
                skipped_bad += 1
                continue

            try:
                outcome_index_int = int(outcome_index)
                index_set = 1 << outcome_index_int
            except Exception:
                skipped_bad += 1
                continue

            out.append(
                {
                    "conditionId": condition_id,
                    "outcomeIndex": outcome_index_int,
                    "indexSet": index_set,
                    "size": size,
                    "curPrice": cur_price,
                    "title": item.get("title"),
                    "outcome": item.get("outcome"),
                }
            )

        if skipped_bad:
            log(f"🧹 已跳过缺少字段/无法解析的仓位数量: {skipped_bad}")

        # 按 conditionId 合并（同一市场可能出现多条）
        merged = {}
        for p in out:
            cid = p["conditionId"]
            rec = merged.setdefault(
                cid,
                {
                    "conditionId": cid,
                    "indexSets": set(),
                    "titles": set(),
                    "outcomes": set(),
                },
            )
            rec["indexSets"].add(int(p["indexSet"]))
            if p.get("title"):
                rec["titles"].add(p["title"])
            if p.get("outcome"):
                rec["outcomes"].add(p["outcome"])

        result = []
        for cid, rec in merged.items():
            result.append(
                {
                    "conditionId": cid,
                    "indexSets": sorted(list(rec["indexSets"])),
                    "title": next(iter(rec["titles"]), None),
                    "outcome": next(iter(rec["outcomes"]), None),
                }
            )

        return result
    except Exception as e:
        log(f"⚠️ Polymarket API 报错（稍后再试即可）：{e}")
        return []

def rpc_healthcheck(rpc_url: str, timeout_s: int = 10) -> bool:
    """
    用最简单的 JSON-RPC 调用检查 RPC 是否可用。
    这样我们可以强制 requests 的超时，避免 Web3 内部调用在某些网络环境里卡死。
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}
    try:
        r = requests.post(rpc_url, json=payload, timeout=timeout_s)
        r.raise_for_status()
        data = r.json()
        # Polygon 主网 chainId = 137 (0x89)
        return "result" in data
    except Exception as e:
        log(f"⚠️ RPC 健康检查失败：{e}")
        return False


def _is_contract(w3: Web3, addr: str) -> bool:
    code = w3.eth.get_code(Web3.to_checksum_address(addr))
    return bool(code and len(code) > 0)


def _get_wallet_owner_contract(w3: Web3, wallet_addr: str) -> Optional[str]:
    """
    对 Polymarket email/Builder 合约钱包：读取固定 slot 得到 owner 合约地址。
    若 slot 为 0，返回 None。
    """
    v = w3.eth.get_storage_at(Web3.to_checksum_address(wallet_addr), WALLET_OWNER_SLOT)
    if not v or int.from_bytes(v, "big") == 0:
        return None
    return Web3.to_checksum_address("0x" + v.hex()[-40:])


def redeem_via_proxy(w3: Web3, account, condition_id: str, index_sets: list[int]) -> None:
    # web3.py v7 默认只接受 checksum address；为了兼容你在 .env 里配置小写地址，这里统一转换
    try:
        proxy_addr = Web3.to_checksum_address(PROXY_ADDRESS)
    except Exception as e:
        raise ValueError(f"PM_ADDRESS 不是合法地址或无法转换为 checksum：{PROXY_ADDRESS}") from e

    try:
        ctf_addr = Web3.to_checksum_address(CTF_ADDRESS)
        usdc_addr = Web3.to_checksum_address(USDC_ADDRESS)
    except Exception as e:
        raise ValueError("脚本内置合约地址无法转换为 checksum（异常情况）") from e

    ctf = w3.eth.contract(address=ctf_addr, abi=CTF_ABI)

    log(f"⚙️ 准备领取 conditionId: {condition_id} indexSets={index_sets}")

    try:
        cond_id_bytes = bytes.fromhex(condition_id.replace("0x", ""))

        # 1) 生成对 CTF.redeemPositions 的 calldata
        ctf_tx_dummy = ctf.functions.redeemPositions(
            usdc_addr,
            b"\x00" * 32,
            cond_id_bytes,
            index_sets,
        ).build_transaction(
            {
                "chainId": 137,
                "gas": 0,
                "gasPrice": 0,
                "from": "0x0000000000000000000000000000000000000000",
            }
        )
        ctf_data = ctf_tx_dummy["data"]

        # 2) 根据 PM_ADDRESS 类型选择执行路径：
        # - EOA：直接从 EOA 调用 CTF.redeemPositions
        # - 合约钱包（email/builder）：EOA -> ownerContract.proxy([ wallet.proxy([ CTF.call ]) ])
        if not _is_contract(w3, proxy_addr):
            log("🧾 PM_ADDRESS 为 EOA，直接发起 redeemPositions。")
            tx_call = ctf.functions.redeemPositions(
                usdc_addr,
                b"\x00" * 32,
                cond_id_bytes,
                index_sets,
            )
        else:
            owner_contract_addr = _get_wallet_owner_contract(w3, proxy_addr)
            if not owner_contract_addr:
                raise RuntimeError(
                    "检测到 PM_ADDRESS 为合约钱包，但无法从预期 slot 读取 owner 合约地址。"
                    "这可能意味着 Polymarket 钱包实现已升级，需要更新脚本的解析逻辑。"
                )

            log(f"🔐 合约钱包 owner 合约: {owner_contract_addr}")

            owner_contract = w3.eth.contract(address=owner_contract_addr, abi=WALLET_PROXY_ABI)
            wallet = w3.eth.contract(address=proxy_addr, abi=WALLET_PROXY_ABI)

            # wallet.proxy([ (0, CTF, 0, ctf_data) ])
            wallet_proxy_data = wallet.functions.proxy([(0, ctf_addr, 0, bytes.fromhex(ctf_data[2:]))])._encode_transaction_data()

            # owner.proxy([ (0, wallet, 0, wallet_proxy_data) ])
            tx_call = owner_contract.functions.proxy([(0, proxy_addr, 0, bytes.fromhex(wallet_proxy_data[2:]))])

        # 4) build + 估算 gas + 签名 + 发送
        tx = tx_call.build_transaction(
            {
                "from": account.address,
                "chainId": 137,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gasPrice": w3.eth.gas_price,
            }
        )

        try:
            est_gas = w3.eth.estimate_gas(tx)
            tx["gas"] = int(est_gas * 1.3)
        except Exception:
            tx["gas"] = 500000

        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        raw_tx = get_raw_tx_bytes(signed_tx)
        tx_hash = w3.eth.send_raw_transaction(raw_tx)

        log(f"🚀 已发送交易: https://polygonscan.com/tx/{w3.to_hex(tx_hash)}")

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status == 1:
            log("✅ 领取成功！")
        else:
            log("❌ 交易执行失败（revert）。")

    except Exception as e:
        log(f"❌ 领取过程出错: {e}")


def run_cycle() -> None:
    """执行一次完整检查周期。"""
    if not PRIVATE_KEY:
        log("⚠️ 未配置环境变量 POLYMARKET_PRIVATE_KEY，本轮跳过。")
        return
    if not PROXY_ADDRESS:
        log("⚠️ 未配置环境变量 PM_ADDRESS（Proxy/Safe 地址），本轮跳过。")
        return

    if not rpc_healthcheck(RPC_URL, timeout_s=10):
        log("⚠️ RPC 不可用，本轮跳过。")
        return

    # 给 Web3 的 HTTPProvider 配置超时（用于后续所有链上调用）
    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 10}))

    try:
        account = Account.from_key(PRIVATE_KEY)
    except Exception:
        log("⚠️ 私钥无效或未配置（POLYMARKET_PRIVATE_KEY）。")
        return

    # 兼容 email/托管钱包：PM_ADDRESS 可能是合约钱包地址。
    # Polymarket email/Builder 钱包常见结构：
    # - PM_ADDRESS 为合约钱包
    # - 该钱包的 owner 是一个“owner 合约”（而不是你的 EOA）
    # - 你的 EOA 可以调用 owner 合约的 proxy(...)，由 owner 合约再去调用钱包执行
    try:
        proxy_addr = Web3.to_checksum_address(PROXY_ADDRESS)
        if _is_contract(w3, proxy_addr):
            owner_contract_addr = _get_wallet_owner_contract(w3, proxy_addr)
            if owner_contract_addr:
                log(f"🔎 检测到合约钱包 owner 合约: {owner_contract_addr}")
                owner_contract = w3.eth.contract(address=owner_contract_addr, abi=WALLET_PROXY_ABI)
                try:
                    # 验证：当前 EOA 是否被允许调用 owner 合约的 proxy(...)
                    owner_contract.functions.proxy([]).call({"from": account.address})
                    log("✅ 当前 EOA 可调用 owner 合约（可继续尝试自动领取）。")
                except Exception as e:
                    log("❌ 当前 EOA 无法调用 owner 合约的 proxy(...)。")
                    log(f"   你的 EOA(from private key): {account.address}")
                    log(f"   合约钱包(PM_ADDRESS): {proxy_addr}")
                    log(f"   owner 合约: {owner_contract_addr}")
                    log(f"   具体错误: {e}")
                    return
            else:
                log("⚠️ PM_ADDRESS 是合约钱包，但无法识别 owner 合约地址（可能是钱包实现已升级）。")
                log("   仍会继续尝试领取；若失败请提供最新报错。")
    except Exception:
        pass

    redeemables = get_redeemable_positions(PROXY_ADDRESS)
    if not redeemables:
        log("未发现可领取仓位。")
        return

    # 重新拉取一遍明细用于判断输赢（merged 结果缺少 curPrice）
    raw = requests.get(
        "https://data-api.polymarket.com/positions",
        params={"user": PROXY_ADDRESS, "redeemable": "true", "limit": 50},
        timeout=10,
    ).json()
    won_cids = set()
    lost_cids = set()
    for it in raw:
        cid = it.get("conditionId")
        if not cid:
            continue
        try:
            cp = float(it.get("curPrice", 0) or 0)
        except Exception:
            cp = 0.0
        if cp >= 0.999:
            won_cids.add(cid)
        elif cp <= 0.001:
            lost_cids.add(cid)

    won = [x for x in redeemables if x["conditionId"] in won_cids]
    lost = [x for x in redeemables if x["conditionId"] in lost_cids and x["conditionId"] not in won_cids]
    other = [x for x in redeemables if x["conditionId"] not in won_cids and x["conditionId"] not in lost_cids]

    if lost and not REDEEM_LOSING_POSITIONS:
        log(f"ℹ️ 发现可领取但为输仓（curPrice≈0）的 markets 数量: {len(lost)}，默认跳过以避免消耗 gas。")
        log("   如需连输仓也一起 redeem，请在 .env 设置 REDEEM_LOSING_POSITIONS=true")

    targets = won + (lost if REDEEM_LOSING_POSITIONS else []) + other
    if not targets:
        log("未发现可自动领取的仓位（赢仓=0 且已按配置跳过输仓）。")
        return

    log(f"🔥 将尝试领取 markets 数量: {len(targets)}（赢仓: {len(won)}，输仓: {len(lost)}，其他: {len(other)}）")
    for item in targets:
        cond = item["conditionId"]
        idx_sets = item.get("indexSets") or [1, 2]
        redeem_via_proxy(w3, account, cond, idx_sets)
        time.sleep(3)  # 避免 nonce/网络延迟导致冲突


def main() -> None:
    """
    单次执行入口：跑完一轮检查/领取就退出。
    适合配合 cron/计划任务，由外部调度决定频率。
    """
    log("🤖 单次执行开始。")
    log(f"👤 Proxy Address: {PROXY_ADDRESS}")
    # 进程级 watchdog：防止 DNS/RPC/网络卡死导致 cron 堆积
    def _worker():
        try:
            run_cycle()
        except Exception as e:
            log(f"💥 单次执行出现未捕获异常: {e}")

    p = mp.Process(target=_worker, daemon=True)
    p.start()
    p.join(timeout=RUN_TIMEOUT_SECONDS)
    if p.is_alive():
        log(f"⏱️ 单次执行超时（>{RUN_TIMEOUT_SECONDS}s），已强制结束。")
        p.terminate()
        p.join(timeout=5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 脚本被用户中断。")

