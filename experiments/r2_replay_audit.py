"""R2 离线重放审计：对 9-03 19:00(本地) 起每根已收盘 15m bar，
模拟引擎 _r2_new_bar 调用的 pending/next_open 意图生成，对比实际引擎行为。

数据源: MySQL 15min OHLC（与 db_reader 同口径）
不依赖网络/Redis；只读。
"""
import sys, datetime
import pandas as pd
import pymysql
from autobot.strategies.eth_spike_r2_strategy import (
    new_pending_entry_intents, new_next_open_entry_intents,
    frozen_shape_configs,
)

LOCAL_OFFSET = datetime.timedelta(hours=3, minutes=30)  # +0330
START_LOCAL = datetime.datetime(2026, 9, 3, 19, 0)  # 引擎重启时刻
END_LOCAL = datetime.datetime(2026, 9, 4, 17, 45)

def load_ohlc(sym_db: str):
    cfg = {}
    for l in open("/home/ecs-user/.passwd"):
        l = l.strip()
        if "=" in l and not l.startswith("#"):
            k, v = l.split("=", 1)
            cfg[k] = v
    conn = pymysql.connect(host=cfg["DB_HOST"], port=int(cfg["DB_PORT"]),
                           user=cfg["DB_USER"], password=cfg["DB_PASSWORD"],
                           database=cfg["DB_NAME"], connect_timeout=5)
    cur = conn.cursor()
    cur.execute("SELECT id FROM currencies WHERE symbol=%s", (sym_db,))
    cid = cur.fetchone()[0]
    # 8-25 起足够 warmup（>128 + 上下文窗口）
    cur.execute("""SELECT open_time, open, high, low, close FROM kline_data
                   WHERE currency_id=%s AND interval_id=3
                     AND open_time >= 1785456000000 ORDER BY open_time ASC""", (cid,))
    rows = cur.fetchall()
    conn.close()
    df = pd.DataFrame([r[1:] for r in rows], columns=["open", "high", "low", "close"]).astype(float)
    df.index = pd.to_datetime([r[0] for r in rows], unit="ms")  # UTC naive
    return df

def utc_of_local(dt: datetime.datetime) -> datetime.datetime:
    return dt - LOCAL_OFFSET

def run(sym_db: str, label: str):
    full = load_ohlc(sym_db)
    print(f"\n===== {label} 15m bars: {full.index[0]} .. {full.index[-1]} (UTC) total={len(full)} =====")
    start_utc, end_utc = utc_of_local(START_LOCAL), utc_of_local(END_LOCAL)
    events = []
    # 逐根"已收盘 bar"滚动：bar_t 作为窗口最后一根（模拟引擎剔除未收盘后 df[-1]）
    closes = full[full.index <= end_utc]
    for t in closes.index:
        if t < start_utc:
            continue
        win = full.loc[:t].tail(200)
        pend = new_pending_entry_intents(win)
        nxt = new_next_open_entry_intents(win)
        for it in pend:
            d = (it.expires_at - it.created_at).total_seconds() / 60
            events.append(f"PEND {it.shape} side={it.side} sig={it.signal_bar_start} trigger={it.trigger_price:.2f} "
                          f"inv={it.invalidation_price} close={it.signal_close:.2f} "
                          f"dist={abs(it.trigger_price-it.signal_close)/it.signal_close*100:.3f}%>{it.max_distance_pct}% "
                          f"life={d:.0f}min")
        for it in nxt:
            events.append(f"NEXT {it.shape} side={it.side} sig={it.signal_bar_start} conf={it.confirmation_bar_start} "
                          f"exec={it.execute_at} close={it.signal_close:.2f} maxd={it.max_distance_pct}%")
    if events:
        print(f"UTC window {start_utc}..{end_utc} 理论意图 {len(events)} 条:")
        for e in events:
            print("  ", e)
    else:
        print(f"UTC window {start_utc}..{end_utc} 理论意图 0 条")

if __name__ == "__main__":
    run("ETHUSDT", "ETH-USDT-SWAP")
    run("BTCUSDT", "BTC-USDT-SWAP")
