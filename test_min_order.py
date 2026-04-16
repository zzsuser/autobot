#!/usr/bin/env python3
"""
OKX 永续合约最小开仓信息查询

功能：查询 BTC 和 ETH 永续合约的合约参数与最小开仓金额（只读，不交易）

用法:
    python test_min_order.py
"""
import sys
import requests


# 查询的合约列表
CONTRACTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]

# OKX 公开 API 地址
BASE_URL = "https://www.okx.com"


def get_instrument_info(inst_id: str) -> dict:
    """查询合约基本信息（ctVal、minSz 等）"""
    url = f"{BASE_URL}/api/v5/public/instruments?instType=SWAP&instId={inst_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=10)
    data = resp.json()
    if data.get("code") == "0" and data.get("data"):
        return data["data"][0]
    raise RuntimeError(f"查询合约信息失败 ({inst_id}): {data}")


def get_current_price(inst_id: str) -> float:
    """获取合约当前价格（最新1分钟K线收盘价）"""
    url = f"{BASE_URL}/api/v5/market/candles?instId={inst_id}&bar=1m&limit=1"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=10)
    data = resp.json()
    if data.get("data"):
        return float(data["data"][0][4])  # 收盘价
    raise RuntimeError(f"获取价格失败 ({inst_id}): {data}")


def print_contract_info(inst_id: str):
    """打印单个合约的最小开仓信息"""
    print(f"\n  合约: {inst_id}")
    print("  " + "─" * 33)

    # 获取合约信息
    info = get_instrument_info(inst_id)
    ct_val = float(info.get("ctVal", "0"))
    min_sz = float(info.get("minSz", "1"))
    ct_ccy = info.get("ctValCcy", "")

    # 获取当前价格
    price = get_current_price(inst_id)

    # 计算 1 张合约的价值（USDT）
    value_per_contract = price * ct_val

    # 不同杠杆下的最小保证金
    margin_100x = value_per_contract * min_sz / 100
    margin_10x = value_per_contract * min_sz / 10
    margin_5x = value_per_contract * min_sz / 5

    print(f"  当前价格:        ${price:>12,.2f}")
    print(f"  合约面值(ctVal):  {ct_val} {ct_ccy}")
    print(f"  最小张数(minSz):  {int(min_sz)}")
    print(f"  1张价值:          ${value_per_contract:>10,.2f}")
    print(f"  最小保证金(100x): ${margin_100x:>10,.2f}")
    print(f"  最小保证金(10x):  ${margin_10x:>10,.2f}")
    print(f"  最小保证金(5x):   ${margin_5x:>10,.2f}")


def main():
    print("\n" + "=" * 50)
    print("  OKX 永续合约最小开仓信息查询")
    print("=" * 50)

    errors = []
    for inst_id in CONTRACTS:
        try:
            print_contract_info(inst_id)
        except Exception as e:
            errors.append((inst_id, str(e)))
            print(f"\n  合约: {inst_id}")
            print(f"  ✗ 查询失败: {e}")

    print()

    if errors:
        print("  以下合约查询失败:")
        for inst_id, err in errors:
            print(f"    - {inst_id}: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
