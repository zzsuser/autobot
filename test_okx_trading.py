#!/usr/bin/env python3
"""
OKX 开平仓完整测试脚本

测试流程:
    Step 1: 验证 API 连接（获取账户余额）
    Step 2: 获取当前 ETH 价格
    Step 3: 设置杠杆倍数
    Step 4: 开多仓（市价）
    Step 5: 查询持仓确认
    Step 6: 平多仓（市价）
    Step 7: 确认多仓已平
    Step 8: 开空仓（市价）
    Step 9: 查询持仓确认
    Step 10: 平空仓（市价）
    Step 11: 确认空仓已平
    Step 12: 最终账户状态

用法:
    python test_okx_trading.py              # 默认使用 .passwd 中的配置
    python test_okx_trading.py --dry-run    # 仅检查连接，不执行交易
    python test_okx_trading.py --inst ETH-USDT-SWAP  # 指定合约

注意:
    ⚠️  强烈建议先用模拟盘测试（.passwd 中设置 OKX_FLAG=1）
    ⚠️  测试使用极小仓位（1张合约），但仍涉及真实/模拟资金
"""
import sys
import time
import json
import argparse
import traceback
from typing import List, Tuple, Optional

# 确保能导入 autobot
sys.path.insert(0, ".")


# ============================================================
# 工具函数
# ============================================================

class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    BOLD = "\033[1m"
    END = "\033[0m"


results: List[Tuple[str, bool, str]] = []


def print_header(title: str):
    print(f"\n{'=' * 65}")
    print(f"  {Colors.BOLD}{Colors.CYAN}{title}{Colors.END}")
    print(f"{'=' * 65}")


def print_step(step: int, msg: str):
    print(f"\n  {Colors.BOLD}[Step {step}]{Colors.END} {Colors.MAGENTA}{msg}{Colors.END}")
    print(f"  {'-' * 55}")


def print_pass(msg: str):
    print(f"    {Colors.GREEN}✓ PASS{Colors.END}  {msg}")


def print_fail(msg: str, error: str = ""):
    print(f"    {Colors.RED}✗ FAIL{Colors.END}  {msg}")
    if error:
        print(f"             {Colors.RED}{error}{Colors.END}")


def print_warn(msg: str):
    print(f"    {Colors.YELLOW}⚠ WARN{Colors.END}  {msg}")


def print_info(msg: str):
    print(f"           {msg}")


def record(name: str, passed: bool, detail: str = ""):
    results.append((name, passed, detail))
    if passed:
        print_pass(f"{name} {detail}")
    else:
        print_fail(name, detail)


def wait(seconds: float, reason: str = ""):
    """等待一段时间，让交易所处理订单"""
    if reason:
        print_info(f"⏳ 等待 {seconds}s ({reason})...")
    time.sleep(seconds)


# ============================================================
# OKX API 封装（直接用 SDK，比 OKXTrader 更底层，便于测试）
# ============================================================

class OKXTester:
    """OKX 交易测试器"""

    def __init__(self, inst_id: str = "ETH-USDT-SWAP"):
        from autobot.config import OKXConfig, TradingConfig

        self.inst_id = inst_id
        self.config = OKXConfig
        self.trading_config = TradingConfig

        # 检查配置
        if not OKXConfig.is_configured():
            raise RuntimeError(
                "OKX API 未配置！请在 .passwd 文件中设置:\n"
                "  OKX_API_KEY=xxx\n"
                "  OKX_SECRET_KEY=xxx\n"
                "  OKX_PASSPHRASE=xxx\n"
                "  OKX_FLAG=1  (模拟盘)"
            )

        self.flag = OKXConfig.FLAG
        self.is_demo = self.flag == "1"

        # 初始化 SDK
        import okx.Trade as Trade
        import okx.Account as Account
        import okx.MarketData as MarketData

        self.trade_api = Trade.TradeAPI(
            OKXConfig.API_KEY, OKXConfig.SECRET_KEY, OKXConfig.PASSPHRASE,
            False, self.flag
        )
        self.account_api = Account.AccountAPI(
            OKXConfig.API_KEY, OKXConfig.SECRET_KEY, OKXConfig.PASSPHRASE,
            False, self.flag
        )
        self.market_api = MarketData.MarketAPI(
            OKXConfig.API_KEY, OKXConfig.SECRET_KEY, OKXConfig.PASSPHRASE,
            False, self.flag
        )

    def get_min_size(self) -> str:

        """查询合约最小下单量"""
        import okx.PublicData as PublicData
        public_api = PublicData.PublicAPI(flag=self.flag)
        result = public_api.get_instruments(instType="SWAP", instId=self.inst_id)
        if result["code"] == "0" and result["data"]:
            return result["data"][0].get("minSz", "1")

        return "1"

    def get_balance(self) -> dict:
        """获取账户余额"""
        result = self.account_api.get_account_balance()
        if result["code"] != "0":
            raise RuntimeError(f"获取余额失败: {result.get('msg', result)}")
        return result

    def get_usdt_balance(self) -> float:
        """获取 USDT 可用余额"""
        result = self.get_balance()
        for detail in result["data"][0]["details"]:
            if detail["ccy"] == "USDT":
                return float(detail["availBal"])
        return 0.0

    def get_price(self) -> float:
        """获取当前价格"""
        # 使用 OKXTrader 中的公开 API 方式
        import requests
        headers = {"User-Agent": "Mozilla/5.0"}
        url = f"https://www.okx.com/api/v5/market/candles?instId={self.inst_id}&bar=1m&limit=1"
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        if data.get("data"):
            return float(data["data"][0][4])
        raise RuntimeError(f"获取价格失败: {data}")

    def set_leverage(self, leverage: int = 1) -> dict:
        """设置杠杆"""
        # 双向持仓模式下需要分别设置
        results_list = []
        for pos_side in ["long", "short"]:
            result = self.account_api.set_leverage(
                instId=self.inst_id,
                lever=str(leverage),
                mgnMode="cross",
                posSide=pos_side,
            )
            results_list.append(result)
            if result["code"] != "0":
                # 可能是单向持仓模式，尝试不指定 posSide
                result2 = self.account_api.set_leverage(
                    instId=self.inst_id,
                    lever=str(leverage),
                    mgnMode="isolated",
                )
                return result2
        return results_list[0]

    def get_positions(self) -> list:
        """获取当前持仓"""
        result = self.account_api.get_positions(instId=self.inst_id)
        if result["code"] != "0":
            raise RuntimeError(f"获取持仓失败: {result}")
        return result["data"]

    def open_position(self, side: str, pos_side: str, size: str = "0.01") -> dict:
        """
        开仓

        Args:
            side: 'buy' (做多) 或 'sell' (做空)
            pos_side: 'long' 或 'short'
            size: 合约张数
        """
        result = self.trade_api.place_order(
            instId=self.inst_id,
            tdMode="cross",
            side=side,
            posSide=pos_side,
            ordType="market",
            sz=size,
        )
        return result

    def close_position(self, pos_side: str) -> dict:
        """平仓"""
        result = self.trade_api.close_positions(
            instId=self.inst_id,
            mgnMode="cross",
            posSide=pos_side,
        )
        return result

    def get_order_detail(self, order_id: str) -> dict:
        """查询订单详情"""
        result = self.trade_api.get_order(instId=self.inst_id, ordId=order_id)
        return result

    def get_account_config(self) -> dict:
        """获取账户配置（持仓模式等）"""
        return self.account_api.get_account_config()

    def set_position_mode(self, pos_mode: str = "long_short_mode") -> dict:
        """
        设置持仓模式
        pos_mode: 'long_short_mode' (双向持仓) 或 'net_mode' (单向持仓)
        """
        return self.account_api.set_position_mode(posMode=pos_mode)


# ============================================================
# 测试流程
# ============================================================

def run_trading_test(inst_id: str = "ETH-USDT-SWAP", dry_run: bool = False):
    """运行完整的开平仓测试"""

    print_header("OKX 开平仓完整测试")

    # ---- 初始化 ----
    try:
        tester = OKXTester(inst_id=inst_id)
        mode_str = "模拟盘" if tester.is_demo else "实盘"
        print_info(f"模式: {mode_str}")
        print_info(f"合约: {inst_id}")

        if not tester.is_demo:
            print(f"\n    {Colors.RED}{Colors.BOLD}⚠️  警告: 当前为实盘模式！测试将使用真实资金！{Colors.END}")
            print(f"    {Colors.RED}建议: 在 .passwd 中设置 OKX_FLAG=1 切换到模拟盘{Colors.END}")
            confirm = input(f"\n    确认继续？(输入 YES 继续): ")
            if confirm.strip() != "YES":
                print("\n    已取消测试。")
                return

        record("OKX API 初始化", True, mode_str)
    except Exception as e:
        record("OKX API 初始化", False, str(e))
        traceback.print_exc()
        return

    # ---- Step 1: 验证连接 / 获取余额 ----
    print_step(1, "验证 API 连接 & 获取账户余额")
    try:
        balance = tester.get_usdt_balance()
        record("获取 USDT 余额", balance >= 0, f"可用: ${balance:.4f}")

        if balance < 1:
            print_warn(f"余额较低 (${balance:.4f})，可能无法完成开仓测试")
            if balance == 0:
                print_warn("余额为0，请先向交易账户转入 USDT")
                if not tester.is_demo:
                    return
    except Exception as e:
        record("获取余额", False, str(e))
        traceback.print_exc()
        return

    # ---- Step 1.5: 检查并设置持仓模式 ----
    print_info("检查账户持仓模式...")
    try:
        config = tester.get_account_config()
        if config["code"] == "0":
            pos_mode = config["data"][0].get("posMode", "unknown")
            print_info(f"当前持仓模式: {pos_mode}")
            if pos_mode != "long_short_mode":
                print_info("切换到双向持仓模式...")
                set_result = tester.set_position_mode("long_short_mode")
                if set_result.get("code") == "0":
                    print_info("✓ 已切换到双向持仓模式")
                else:
                    print_warn(f"切换失败: {set_result.get('msg', '')}")
                    print_info("将尝试继续测试...")
    except Exception as e:
        print_warn(f"检查持仓模式失败: {e}，将尝试继续")

    # ---- Step 2: 获取当前价格 ----
    print_step(2, f"获取 {inst_id} 当前价格")
    try:
        price = tester.get_price()
        record("获取当前价格", price > 0, f"${price:.2f}")
    except Exception as e:
        record("获取价格", False, str(e))
        return

    # ---- Step 3: 设置杠杆 ----
    print_step(3, "设置杠杆倍数")
    try:
        leverage = tester.trading_config.DEFAULT_LEVERAGE
        result = tester.set_leverage(leverage)
        is_ok = result.get("code") == "0" if isinstance(result, dict) else True
        record("设置杠杆", is_ok, f"{leverage}x")
    except Exception as e:
        record("设置杠杆", False, str(e))
        print_warn("杠杆设置失败，但可能之前已设置过，继续测试...")

    if dry_run:
        print(f"\n    {Colors.YELLOW} Dry-run 模式: 跳过实际交易，仅验证连接{Colors.END}")
        record("Dry-run 连接测试", True, "API连接正常，跳过交易")
        _print_summary()
        return

    # 查询最小下单张数，后续开仓复用
    min_size = tester.get_min_size()
    print_info(f"最小下单张数: {min_size}")

    # ---- Step 4: 开多仓 ----
    print_step(4, "开多仓（市价，最小张数）")
    long_order_id = None
    try:
        result = tester.open_position(side="buy", pos_side="long", size=min_size)
        success = result.get("code") == "0"
        if success:
            long_order_id = result["data"][0].get("ordId", "")
            record("开多仓", True, f"orderId={long_order_id}")
        else:
            error_msg = result.get("data", [{}])[0].get("sMsg", result.get("msg", ""))
            record("开多仓", False, f"错误: {error_msg}")
            # 如果开仓失败，尝试记录详细信息
            print_info(f"完整响应: {json.dumps(result, ensure_ascii=False)}")
    except Exception as e:
        record("开多仓", False, str(e))
        traceback.print_exc()

    wait(2, "等待订单成交")

    # ---- Step 5: 确认多仓持仓 ----
    print_step(5, "查询持仓 - 确认多仓")
    try:
        positions = tester.get_positions()
        long_found = False
        for pos in positions:
            if pos.get("posSide") == "long" and float(pos.get("pos", "0")) > 0:
                long_found = True
                avg_price = pos.get("avgPx", "?")
                upl = pos.get("upl", "0")
                print_info(f"多仓: 数量={pos['pos']}, 均价={avg_price}, 浮盈={upl}")
        record("确认多仓存在", long_found,
               "持仓确认" if long_found else "未找到多仓（可能未成交）")
    except Exception as e:
        record("查询多仓", False, str(e))

    # ---- 查看订单详情 ----
    if long_order_id:
        try:
            order = tester.get_order_detail(long_order_id)
            if order["code"] == "0" and order["data"]:
                od = order["data"][0]
                print_info(f"订单状态: {od.get('state', '?')}, "
                           f"成交均价: {od.get('avgPx', '?')}, "
                           f"成交量: {od.get('accFillSz', '?')}")
        except Exception:
            pass

    wait(1, "准备平仓")

    # ---- Step 6: 平多仓 ----
    print_step(6, "平多仓（市价）")
    try:
        result = tester.close_position(pos_side="long")
        success = result.get("code") == "0"
        record("平多仓", success,
               "平仓成功" if success else f"错误: {result.get('msg', result)}")
        if not success:
            print_info(f"完整响应: {json.dumps(result, ensure_ascii=False)}")
    except Exception as e:
        record("平多仓", False, str(e))

    wait(2, "等待平仓完成")

    # ---- Step 7: 确认多仓已平 ----
    print_step(7, "确认多仓已平")
    try:
        positions = tester.get_positions()
        long_found = False
        for pos in positions:
            if pos.get("posSide") == "long" and float(pos.get("pos", "0")) > 0:
                long_found = True
        record("多仓已平", not long_found,
               "已确认无多仓" if not long_found else "⚠️ 仍有多仓！")
    except Exception as e:
        record("确认多仓已平", False, str(e))

    wait(1, "准备开空仓")

    # ---- Step 8: 开空仓 ----
    print_step(8, "开空仓（市价，最小张数）")
    short_order_id = None
    try:
        result = tester.open_position(side="sell", pos_side="short", size=min_size)
        success = result.get("code") == "0"
        if success:
            short_order_id = result["data"][0].get("ordId", "")
            record("开空仓", True, f"orderId={short_order_id}")
        else:
            error_msg = result.get("data", [{}])[0].get("sMsg", result.get("msg", ""))
            record("开空仓", False, f"错误: {error_msg}")
            print_info(f"完整响应: {json.dumps(result, ensure_ascii=False)}")
    except Exception as e:
        record("开空仓", False, str(e))
        traceback.print_exc()

    wait(2, "等待订单成交")

    # ---- Step 9: 确认空仓持仓 ----
    print_step(9, "查询持仓 - 确认空仓")
    try:
        positions = tester.get_positions()
        short_found = False
        for pos in positions:
            if pos.get("posSide") == "short" and float(pos.get("pos", "0")) > 0:
                short_found = True
                avg_price = pos.get("avgPx", "?")
                upl = pos.get("upl", "0")
                print_info(f"空仓: 数量={pos['pos']}, 均价={avg_price}, 浮盈={upl}")
        record("确认空仓存在", short_found,
               "持仓确认" if short_found else "未找到空仓（可能未成交）")
    except Exception as e:
        record("查询空仓", False, str(e))

    # ---- 查看订单详情 ----
    if short_order_id:
        try:
            order = tester.get_order_detail(short_order_id)
            if order["code"] == "0" and order["data"]:
                od = order["data"][0]
                print_info(f"订单状态: {od.get('state', '?')}, "
                           f"成交均价: {od.get('avgPx', '?')}, "
                           f"成交量: {od.get('accFillSz', '?')}")
        except Exception:
            pass

    wait(1, "准备平仓")

    # ---- Step 10: 平空仓 ----
    print_step(10, "平空仓（市价）")
    try:
        result = tester.close_position(pos_side="short")
        success = result.get("code") == "0"
        record("平空仓", success,
               "平仓成功" if success else f"错误: {result.get('msg', result)}")
        if not success:
            print_info(f"完整响应: {json.dumps(result, ensure_ascii=False)}")
    except Exception as e:
        record("平空仓", False, str(e))

    wait(2, "等待平仓完成")

    # ---- Step 11: 确认空仓已平 ----
    print_step(11, "确认空仓已平")
    try:
        positions = tester.get_positions()
        short_found = False
        for pos in positions:
            if pos.get("posSide") == "short" and float(pos.get("pos", "0")) > 0:
                short_found = True
        record("空仓已平", not short_found,
               "已确认无空仓" if not short_found else "⚠️ 仍有空仓！")
    except Exception as e:
        record("确认空仓已平", False, str(e))

    # ---- Step 12: 最终状态 ----
    print_step(12, "最终账户状态")
    try:
        final_balance = tester.get_usdt_balance()
        record("最终余额", True, f"${final_balance:.4f}")

        # 确认无遗留仓位
        positions = tester.get_positions()
        has_any = any(float(p.get("pos", "0")) > 0 for p in positions)
        record("无遗留仓位", not has_any,
               "全部仓位已清理" if not has_any else "⚠️ 仍有遗留仓位！")

        if has_any:
            print_warn("存在遗留仓位，详情:")
            for pos in positions:
                if float(pos.get("pos", "0")) > 0:
                    print_info(f"  {pos.get('posSide')}: {pos.get('pos')}张 @ {pos.get('avgPx')}")
    except Exception as e:
        record("最终状态", False, str(e))

    # ---- 汇总 ----
    _print_summary()


def _print_summary():
    """打印测试汇总"""
    print(f"\n{'=' * 65}")
    print(f"  {Colors.BOLD}测试汇总{Colors.END}")
    print(f"{'=' * 65}")

    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    total = len(results)

    for name, ok, detail in results:
        status = f"{Colors.GREEN}PASS{Colors.END}" if ok else f"{Colors.RED}FAIL{Colors.END}"
        print(f"  [{status}] {name}")
        if not ok and detail:
            print(f"          {Colors.RED}{detail}{Colors.END}")

    print(f"\n  总计: {total}  通过: {Colors.GREEN}{passed}{Colors.END}  失败: {Colors.RED}{failed}{Colors.END}")

    if failed == 0:
        print(f"\n  {Colors.GREEN}{Colors.BOLD}全部测试通过！OKX 开平仓功能正常{Colors.END}\n")
    else:
        print(f"\n  {Colors.YELLOW}请检查失败项，常见原因:{Colors.END}")
        print(f"    1. API 密钥配置错误或权限不足")
        print(f"    2. 模拟盘账户无 USDT 余额（需手动申请模拟资金）")
        print(f"    3. 合约不存在或已下架")
        print(f"    4. 网络无法访问 OKX API（需代理）")
        print(f"    5. 持仓模式不匹配（单向/双向）\n")

    sys.exit(0 if failed == 0 else 1)


# ============================================================
# 附加测试：测试 OKXTrader 封装类
# ============================================================

def test_okx_trader_wrapper():
    """测试项目中 OKXTrader 封装类的开平仓"""
    print_header("附加测试: OKXTrader 封装类")

    try:
        from autobot.exchange.okx_trader import OKXTrader
        from autobot.config import OKXConfig, TradingConfig

        if not OKXConfig.is_configured():
            record("OKXTrader 测试", False, "API 未配置")
            return

        trader = OKXTrader()

        # 获取价格
        price = trader.get_current_price(TradingConfig.DEFAULT_INST_ID)
        record("OKXTrader.get_current_price", price > 0, f"${price:.2f}")

        # 获取持仓
        positions = trader.get_positions()
        is_ok = "error" not in positions
        record("OKXTrader.get_positions", is_ok,
               f"持仓数: {len(positions.get('data', []))}" if is_ok else str(positions))

        # 获取余额
        balance = trader.get_balance()
        is_ok = "error" not in balance
        record("OKXTrader.get_balance", is_ok, "查询成功" if is_ok else str(balance))

        # 测试开仓（1张，最小量）
        print_info("测试 OKXTrader.open_position (开多1张)...")
        open_result = trader.open_position(
            side="buy",
            pos_side="long",
            current_price=price,
            balance=TradingConfig.DEFAULT_BALANCE,
            leverage=TradingConfig.DEFAULT_LEVERAGE,
            ratio=TradingConfig.DEFAULT_RATIO,
        )
        record("OKXTrader.open_position",
               open_result.get("success", False),
               open_result.get("message", ""))

        if open_result.get("success"):
            wait(2, "等待成交")

            # 平仓
            print_info("测试 OKXTrader.close_position...")
            close_result = trader.close_position(pos_side="long")
            record("OKXTrader.close_position",
                   close_result.get("success", False),
                   "平仓成功" if close_result.get("success") else str(close_result))

    except Exception as e:
        record("OKXTrader 封装测试", False, str(e))
        traceback.print_exc()


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="OKX 开平仓测试")
    parser.add_argument("--dry-run", action="store_true", help="仅测试连接，不执行交易")
    parser.add_argument("--inst", default="ETH-USDT-SWAP", help="合约ID (默认: ETH-USDT-SWAP)")
    parser.add_argument("--wrapper", action="store_true", help="附加测试 OKXTrader 封装类")
    args = parser.parse_args()

    print(f"\n{Colors.BOLD}{'=' * 65}")
    print(f"  Autobot - OKX 交易功能测试")
    print(f"{'=' * 65}{Colors.END}")

    # 加载配置
    try:
        from autobot.config import OKXConfig
        mode = "模拟盘" if OKXConfig.FLAG == "1" else "实盘"
        print(f"  交易模式: {mode}")
        print(f"  合约: {args.inst}")
        print(f"  Dry-run: {'是' if args.dry_run else '否'}")
        if OKXConfig.is_configured():
            print(f"  API: {OKXConfig.display_safe()}")
        else:
            print(f"  {Colors.RED}API 未配置！{Colors.END}")
            sys.exit(1)
    except Exception as e:
        print(f"  配置加载失败: {e}")
        sys.exit(1)

    # 执行测试
    run_trading_test(inst_id=args.inst, dry_run=args.dry_run)

    # 附加测试
    if args.wrapper:
        test_okx_trader_wrapper()
        _print_summary()


if __name__ == "__main__":
    main()
