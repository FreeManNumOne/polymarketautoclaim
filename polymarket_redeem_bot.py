import time
import requests
import datetime
from web3 import Web3
from eth_account import Account
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

# 检查间隔（秒）(15 分钟 = 900 秒)
CHECK_INTERVAL = 5 * 60

# 单次执行的最大允许运行时长（秒）；用于 cron 防卡死
RUN_TIMEOUT_SECONDS = int(os.getenv("RUN_TIMEOUT_SECONDS", "180"))

# ================= 常量与 ABI =================

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

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


def get_redeemable_markets(proxy_address: str):
    log("🔍 通过 API 检查可领取（redeemable）的仓位...")
    url = "https://data-api.polymarket.com/positions"
    params = {"user": proxy_address, "redeemable": "true", "limit": 50}

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        conditions = set()
        skipped_not_won = 0
        for item in data:
            # 只领取“结算后为 1”的获胜仓位：
            # 在 positions API 中，获胜 outcome 结算后 curPrice 会为 1。
            try:
                cur_price = float(item.get("curPrice", 0) or 0)
            except Exception:
                cur_price = 0.0
            won = cur_price >= 0.999
            if not won:
                skipped_not_won += 1
                continue

            if float(item.get("size", 0)) > 0:
                conditions.add(item.get("conditionId"))
        if skipped_not_won:
            log(f"🧹 已过滤未获胜/无价值仓位数量: {skipped_not_won}")
        return list(conditions)
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


def redeem_via_proxy(w3: Web3, account, condition_id: str) -> None:
    proxy = w3.eth.contract(address=PROXY_ADDRESS, abi=SAFE_ABI)
    ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)

    log(f"⚙️ 准备领取 conditionId: {condition_id}")

    try:
        cond_id_bytes = bytes.fromhex(condition_id.replace("0x", ""))

        # 1) 生成对 CTF.redeemPositions 的 calldata（仅用于拿到 data）
        ctf_tx_dummy = ctf.functions.redeemPositions(
            USDC_ADDRESS,
            b"\x00" * 32,
            cond_id_bytes,
            [1, 2],
        ).build_transaction(
            {
                "chainId": 137,
                "gas": 0,
                "gasPrice": 0,
                "from": "0x0000000000000000000000000000000000000000",
            }
        )
        ctf_data = ctf_tx_dummy["data"]

        # 2) 生成 Safe 所需 signatures（此处按原脚本方式构造）
        owner_int = int(account.address, 16)
        signature = owner_int.to_bytes(32, "big") + (0).to_bytes(32, "big") + (1).to_bytes(1, "big")

        # 3) Proxy(Safe).execTransaction 调用
        tx_call = proxy.functions.execTransaction(
            CTF_ADDRESS,
            0,
            ctf_data,
            0,
            0,
            0,
            0,
            "0x0000000000000000000000000000000000000000",
            "0x0000000000000000000000000000000000000000",
            signature,
        )

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

    conditions = get_redeemable_markets(PROXY_ADDRESS)
    if not conditions:
        log("未发现可领取仓位。")
        return

    log(f"🔥 发现可领取 markets 数量: {len(conditions)}")
    for cond in conditions:
        redeem_via_proxy(w3, account, cond)
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

