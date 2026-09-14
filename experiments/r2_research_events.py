"""研究引擎事件回放：找出 DB 真实行情下 9-01 起所有 R2 入场事件(研究口径)，
用于与实盘引擎行为对账（解释"回测有信号/开仓但实盘未动"的分歧）。只读，无副作用。"""
import pandas as pd
import pymysql
from autobot.strategies.eth_spike_r2_strategy import (
    raw_shape_candidates, frozen_shape_configs,
    _standard_confirmation, _custom_f3_confirmation,
)

CFG = frozen_shape_configs()

def load(sym_db):
    cfg = {}
    for l in open("/home/ecs-user/.passwd"):
        l = l.strip()
        if "=" in l and not l.startswith("#"):
            k, v = l.split("=", 1); cfg[k] = v
    conn = pymysql.connect(host=cfg["DB_HOST"], port=int(cfg["DB_PORT"]), user=cfg["DB_USER"],
                           password=cfg["DB_PASSWORD"], database=cfg["DB_NAME"], connect_timeout=5)
    cur = conn.cursor()
    cur.execute("SELECT id FROM currencies WHERE symbol=%s", (sym_db,))
    cid = cur.fetchone()[0]
    cur.execute("""SELECT open_time, open, high, low, close FROM kline_data
                   WHERE currency_id=%s AND interval_id=3 AND confirm=1 ORDER BY open_time ASC""", (cid,))
    rows = cur.fetchall()
    conn.close()
    df = pd.DataFrame([r[1:] for r in rows], columns=["open", "high", "low", "close"]).astype(float)
    df.index = pd.to_datetime([r[0] for r in rows], unit="ms")
    return df

def research_events(full: pd.DataFrame, since):
    x = raw_shape_candidates(full)
    n = len(x)
    events = []  # (event_bar_ts, shape, dir, signal_i, conf_j, ref_price)
    for shape, cfg in CFG.items():
        cand = np_flat = None
        import numpy as np
        cand = np.flatnonzero(x[f"{shape}_candidate"].to_numpy(bool))
        for i in cand:
            i = int(i)
            if x.index[i] < since:
                continue
            if cfg.custom_structure:
                j, price = _custom_f3_confirmation(x, int(i), cfg)
            else:
                j, price = _standard_confirmation(x, int(i), cfg)
            if j is None:
                continue
            j = int(j)
            d = "long" if cfg.side == 1 else "short"
            events.append((x.index[j], shape, d, i, j, price))
    # 去重：同确认bar 同方向 只保留研究序(F1->F4)第一个(对齐 add_fourshape_signals)
    events.sort(key=lambda e: (e[0], {"F1":0,"F2":1,"F3":2,"F4":3}[e[1]]))
    seen = set()
    out = []
    for e in events:
        key = (e[0], e[2])
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out

def main():
    for sym, lab in (("ETHUSDT","ETH-USDT-SWAP"), ("BTCUSDT","BTC-USDT-SWAP")):
        full = load(sym)
        since = pd.Timestamp("2026-09-04 00:00:00")
        ev = research_events(full, since)
        print(f"\n===== {lab} 研究口径入场事件 (signal bar >= {since}) 共 {len(ev)} 个 =====")
        for (jb, shape, d, si, j, price) in ev:
            sig_ts = full.index[si]
            entry_txt = ("确认收盘bar触发(j+1开盘入)" if shape=="F3"
                         else f"价格触及确认于 bar#{j}")
            print(f"  {shape}/{d:5s} sig={sig_ts} 确认bar={jb} ref={price:.2f}  [{entry_txt}]")

if __name__ == "__main__":
    main()
