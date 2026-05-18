#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests


USER_AGENT = "shadow-backfill-dashboard/1.0"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="回填 24h/72h 訊號結果，並產生命中率與簡易 Dashboard。"
    )
    p.add_argument("--db-path", default="shadow_mode.db")
    p.add_argument("--horizons", default="1,4,24,72", help="回填小時，以逗號分隔")
    p.add_argument("--report-md", default="shadow_dashboard_report.md")
    p.add_argument("--dashboard-html", default="shadow_dashboard.html")
    p.add_argument("--timeout", type=int, default=15)
    p.add_argument("--binance-base", default="https://api.binance.com")
    p.add_argument("--loop-minutes", type=int, default=0, help=">0 時常駐執行，每 N 分鐘刷新一次")
    return p.parse_args()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_iso(ts: str) -> dt.datetime:
    d = dt.datetime.fromisoformat(ts)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER NOT NULL,
            horizon_hours INTEGER NOT NULL,
            target_timestamp TEXT NOT NULL,
            eval_timestamp TEXT NOT NULL,
            eval_price REAL NOT NULL,
            return_pct REAL NOT NULL,
            decision_hit INTEGER NOT NULL,
            direction_bias TEXT,
            direction_hit INTEGER,
            trigger_touched INTEGER,
            trigger_hit INTEGER,
            max_up_pct REAL,
            max_down_pct REAL,
            outcome TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(signal_id, horizon_hours),
            FOREIGN KEY(signal_id) REFERENCES signals(id)
        )
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(evaluations)")}
    migrations = {
        "direction_bias": "ALTER TABLE evaluations ADD COLUMN direction_bias TEXT",
        "direction_hit": "ALTER TABLE evaluations ADD COLUMN direction_hit INTEGER",
        "trigger_touched": "ALTER TABLE evaluations ADD COLUMN trigger_touched INTEGER",
        "trigger_hit": "ALTER TABLE evaluations ADD COLUMN trigger_hit INTEGER",
        "max_up_pct": "ALTER TABLE evaluations ADD COLUMN max_up_pct REAL",
        "max_down_pct": "ALTER TABLE evaluations ADD COLUMN max_down_pct REAL",
    }
    for col, sql in migrations.items():
        if col not in existing:
            conn.execute(sql)
    conn.commit()


def get_binance_path(
    symbol: str, start_time: dt.datetime, target_time: dt.datetime, timeout: int, base_url: str
) -> tuple[str, float, float, float, list[list[Any]]]:
    start_ms = int(start_time.timestamp() * 1000)
    end_ms = int(target_time.timestamp() * 1000)
    url = f"{base_url}/api/v3/klines"
    params = {"symbol": symbol, "interval": "5m", "startTime": start_ms, "endTime": end_ms, "limit": 1000}
    r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    data = r.json()
    if not data:
        params = {"symbol": symbol, "interval": "1m", "startTime": start_ms, "endTime": end_ms, "limit": 1000}
        r2 = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": USER_AGENT})
        r2.raise_for_status()
        data = r2.json()
    if not data:
        raise RuntimeError(f"binance 查無 K 線: {symbol} @ {start_time.isoformat()} -> {target_time.isoformat()}")
    last = data[-1]
    ts = dt.datetime.fromtimestamp(int(last[0]) / 1000, tz=dt.timezone.utc).isoformat()
    close_price = float(last[4])
    max_high = max(float(c[2]) for c in data)
    min_low = min(float(c[3]) for c in data)
    return ts, close_price, max_high, min_low, data


def evaluate_decision(decision: str, return_pct: float) -> tuple[int, str]:
    d = decision.lower()
    if d == "scale_in_test":
        hit = 1 if return_pct > 0 else 0
    elif d == "watch":
        hit = 1 if (return_pct <= 0 or abs(return_pct) <= 2.0) else 0
    elif d == "avoid":
        hit = 1 if return_pct <= 0 else 0
    else:
        hit = 1 if return_pct <= 0 else 0
    if return_pct > 0.15:
        outcome = "up"
    elif return_pct < -0.15:
        outcome = "down"
    else:
        outcome = "flat"
    return hit, outcome


def evaluate_direction(direction_bias: str | None, return_pct: float) -> int | None:
    if direction_bias == "long":
        return 1 if return_pct > 0 else 0
    if direction_bias == "short":
        return 1 if return_pct < 0 else 0
    return None


def volume_ratio_from_rows(rows: list[list[Any]], idx: int, window: int = 20) -> float:
    start = max(0, idx - window)
    hist = [float(r[5]) for r in rows[start:idx]]
    if not hist:
        return 0.0
    avg = sum(hist) / len(hist)
    if avg == 0:
        return 0.0
    return float(rows[idx][5]) / avg


def detect_false_breakout_after(rows: list[list[Any]], idx: int, trigger: float, side: str, window: int = 2) -> bool:
    future = rows[idx + 1 : idx + 1 + window]
    if side == "long":
        return any(float(r[4]) < trigger for r in future)
    return any(float(r[4]) > trigger for r in future)


def evaluate_trigger_signal(
    rows: list[list[Any]],
    direction_bias: str | None,
    trigger_price: float,
) -> tuple[int, int | None]:
    if direction_bias not in {"long", "short"} or trigger_price <= 0:
        return 0, None
    for idx, row in enumerate(rows):
        close_price = float(row[4])
        vol_ratio = volume_ratio_from_rows(rows, idx)
        if direction_bias == "long":
            passed = close_price >= trigger_price and vol_ratio > 1.2
        else:
            passed = close_price <= trigger_price and vol_ratio > 1.2
        if not passed:
            continue
        false_break = detect_false_breakout_after(rows, idx, trigger_price, direction_bias)
        return 1, 0 if false_break else 1
    return 0, 0


def backfill(conn: sqlite3.Connection, horizons: list[int], timeout: int, base_url: str) -> dict[str, int]:
    now = utc_now()
    inserted = 0
    pending = 0
    skipped = 0
    errors = 0
    cur = conn.cursor()
    for hz in horizons:
        cur.execute(
            """
            SELECT s.id, s.timestamp, s.symbol, s.current_price, s.decision,
                   s.direction_bias, s.long_short_plan_json,
                   e.id, e.direction_hit, e.max_up_pct
            FROM signals s
            LEFT JOIN evaluations e
              ON e.signal_id = s.id AND e.horizon_hours = ?
            ORDER BY s.id ASC
            """,
            (hz,),
        )
        rows = cur.fetchall()
        for signal_id, sig_ts, symbol, current_price, decision, direction_bias, long_short_plan_json, eval_id, direction_hit_existing, max_up_existing in rows:
            actionable_direction = direction_bias in {"long", "short"}
            needs_refresh = eval_id is None or max_up_existing is None or (actionable_direction and direction_hit_existing is None)
            if not needs_refresh:
                continue
            sig_dt = parse_iso(sig_ts)
            target = sig_dt + dt.timedelta(hours=hz)
            if now < target:
                pending += 1
                continue
            try:
                if eval_id is not None:
                    conn.execute("DELETE FROM evaluations WHERE id = ?", (eval_id,))
                eval_ts, eval_price, max_high, min_low, path_rows = get_binance_path(symbol, sig_dt, target, timeout, base_url)
                ret = (eval_price / float(current_price) - 1.0) * 100.0
                hit, outcome = evaluate_decision(decision, ret)
                direction_hit = evaluate_direction(direction_bias, ret)
                long_short_plan = json.loads(long_short_plan_json) if long_short_plan_json else {}
                long_setup = long_short_plan.get("long_setup", {})
                short_setup = long_short_plan.get("short_setup", {})
                long_trigger = float(long_setup.get("trigger_price", 0) or 0)
                short_trigger = float(short_setup.get("trigger_price", 0) or 0)
                trigger_touched = 0
                trigger_hit = None
                if direction_bias == "long":
                    trigger_touched, effective = evaluate_trigger_signal(path_rows, direction_bias, long_trigger)
                    trigger_hit = 1 if (effective == 1 and ret > 0) else 0 if effective is not None else None
                elif direction_bias == "short":
                    trigger_touched, effective = evaluate_trigger_signal(path_rows, direction_bias, short_trigger)
                    trigger_hit = 1 if (effective == 1 and ret < 0) else 0 if effective is not None else None
                max_up_pct = (max_high / float(current_price) - 1.0) * 100.0
                max_down_pct = (min_low / float(current_price) - 1.0) * 100.0
                conn.execute(
                    """
                    INSERT INTO evaluations (
                        signal_id, horizon_hours, target_timestamp, eval_timestamp, eval_price,
                        return_pct, decision_hit, direction_bias, direction_hit,
                        trigger_touched, trigger_hit, max_up_pct, max_down_pct,
                        outcome, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal_id,
                        hz,
                        target.isoformat(),
                        eval_ts,
                        eval_price,
                        ret,
                        hit,
                        direction_bias,
                        direction_hit,
                        trigger_touched,
                        trigger_hit,
                        max_up_pct,
                        max_down_pct,
                        outcome,
                        utc_now().isoformat(),
                    ),
                )
                inserted += 1
            except Exception:
                errors += 1
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM signals")
    total_signals = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM evaluations")
    total_eval = int(cur.fetchone()[0])
    skipped = max(0, total_signals * len(horizons) - total_eval - pending)
    return {
        "inserted": inserted,
        "pending": pending,
        "skipped_or_existing": skipped,
        "errors": errors,
        "total_signals": total_signals,
        "total_evaluations": total_eval,
    }


def load_joined(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
          s.id, s.run_id, s.timestamp, s.symbol, s.current_price, s.risk_score, s.risk_level,
          s.decision, s.confidence, s.trend_view, s.direction_bias,
          e.horizon_hours, e.eval_timestamp, e.eval_price, e.return_pct, e.decision_hit,
          e.direction_hit, e.trigger_touched, e.trigger_hit, e.max_up_pct, e.max_down_pct, e.outcome
        FROM signals s
        LEFT JOIN evaluations e ON e.signal_id = s.id
        ORDER BY s.id ASC, e.horizon_hours ASC
        """
    )
    cols = [d[0] for d in cur.description]
    out: list[dict[str, Any]] = []
    for row in cur.fetchall():
        out.append(dict(zip(cols, row)))
    return out


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def bar(count: int, total: int, width: int = 24) -> str:
    if total <= 0:
        return "░" * width
    n = int(round(count / total * width))
    n = max(0, min(width, n))
    return "█" * n + "░" * (width - n)


def build_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    signals = {}
    for r in rows:
        sid = r["id"]
        if sid not in signals:
            signals[sid] = r
    signal_rows = list(signals.values())
    eval_rows = [r for r in rows if r["horizon_hours"] is not None]

    decision_dist = Counter(r["decision"] for r in signal_rows)
    risk_dist = Counter(r["risk_level"] for r in signal_rows)
    symbol_dist = Counter(r["symbol"] for r in signal_rows)
    direction_dist = Counter((r["direction_bias"] or "neutral") for r in signal_rows)

    by_horizon = defaultdict(list)
    for r in eval_rows:
        by_horizon[int(r["horizon_hours"])].append(r)

    horizon_stats = {}
    for hz, items in sorted(by_horizon.items()):
        hit_rate = (
            sum(int(i["decision_hit"]) for i in items) / len(items) if items else 0.0
        )
        avg_ret = sum(float(i["return_pct"]) for i in items) / len(items) if items else 0.0
        dir_items = [i for i in items if i["direction_hit"] is not None]
        trigger_items = [i for i in items if i["trigger_hit"] is not None]
        horizon_stats[hz] = {
            "count": len(items),
            "hit_rate": hit_rate,
            "avg_return_pct": avg_ret,
            "direction_count": len(dir_items),
            "direction_hit_rate": (sum(int(i["direction_hit"]) for i in dir_items) / len(dir_items)) if dir_items else None,
            "trigger_count": len(trigger_items),
            "trigger_hit_rate": (sum(int(i["trigger_hit"]) for i in trigger_items) / len(trigger_items)) if trigger_items else None,
        }

    decision_stats = {}
    for d in sorted(decision_dist.keys()):
        items = [r for r in eval_rows if r["decision"] == d]
        if not items:
            continue
        decision_stats[d] = {
            "count": len(items),
            "hit_rate": sum(int(i["decision_hit"]) for i in items) / len(items),
            "avg_return_pct": sum(float(i["return_pct"]) for i in items) / len(items),
        }

    direction_stats = {}
    for d in sorted(direction_dist.keys()):
        items = [r for r in eval_rows if (r["direction_bias"] or "neutral") == d and r["direction_hit"] is not None]
        if not items:
            continue
        direction_stats[d] = {
            "count": len(items),
            "hit_rate": sum(int(i["direction_hit"]) for i in items) / len(items),
            "trigger_hit_rate": (
                sum(int(i["trigger_hit"]) for i in items if i["trigger_hit"] is not None) / len([i for i in items if i["trigger_hit"] is not None])
                if [i for i in items if i["trigger_hit"] is not None] else None
            ),
            "avg_return_pct": sum(float(i["return_pct"]) for i in items) / len(items),
        }

    daily = defaultdict(list)
    for r in eval_rows:
        day = parse_iso(str(r["timestamp"])).date().isoformat()
        key = f"{day}_h{int(r['horizon_hours'])}"
        daily[key].append(r)
    daily_stats = []
    for key, items in sorted(daily.items()):
        day, hz = key.split("_h")
        daily_stats.append(
            {
                "day": day,
                "horizon": int(hz),
                "count": len(items),
                "hit_rate": sum(int(i["decision_hit"]) for i in items) / len(items),
                "avg_return_pct": sum(float(i["return_pct"]) for i in items) / len(items),
            }
        )

    return {
        "total_signals": len(signal_rows),
        "total_evaluations": len(eval_rows),
        "decision_distribution": decision_dist,
        "risk_distribution": risk_dist,
        "symbol_distribution": symbol_dist,
        "direction_distribution": direction_dist,
        "horizon_stats": horizon_stats,
        "decision_stats": decision_stats,
        "direction_stats": direction_stats,
        "daily_stats": daily_stats,
    }


def write_md_report(path: Path, backfill_result: dict[str, int], metrics: dict[str, Any]) -> None:
    total_signals = metrics["total_signals"]
    total_eval = metrics["total_evaluations"]
    decision_dist: Counter = metrics["decision_distribution"]
    risk_dist: Counter = metrics["risk_distribution"]
    symbol_dist: Counter = metrics["symbol_distribution"]
    direction_dist: Counter = metrics["direction_distribution"]
    horizon_stats: dict[int, dict[str, Any]] = metrics["horizon_stats"]
    decision_stats: dict[str, dict[str, Any]] = metrics["decision_stats"]
    direction_stats: dict[str, dict[str, Any]] = metrics["direction_stats"]
    daily_stats: list[dict[str, Any]] = metrics["daily_stats"]

    lines: list[str] = []
    lines.append("# Shadow Mode 監控報告（Binance）")
    lines.append("")
    lines.append(f"- 產生時間(UTC): `{utc_now().isoformat()}`")
    lines.append(f"- 總訊號數: `{total_signals}`")
    lines.append(f"- 已回填筆數: `{total_eval}`")
    lines.append("")
    lines.append("## 回填狀態")
    lines.append(f"- 新增回填: `{backfill_result['inserted']}`")
    lines.append(f"- 尚未到期(24h/72h): `{backfill_result['pending']}`")
    lines.append(f"- 既有或略過: `{backfill_result['skipped_or_existing']}`")
    lines.append(f"- 錯誤: `{backfill_result['errors']}`")
    lines.append("")
    lines.append("## Horizon 命中率")
    if not horizon_stats:
        lines.append("- 目前尚無到期資料可計算命中率。")
    else:
        for hz, st in horizon_stats.items():
            base = f"- `{hz}h`: count `{st['count']}`, hit-rate `{pct(st['hit_rate'])}`, avg-return `{st['avg_return_pct']:.3f}%`"
            if st["direction_hit_rate"] is not None:
                base += f", direction-hit `{pct(st['direction_hit_rate'])}`"
            if st["trigger_hit_rate"] is not None:
                base += f", trigger-hit `{pct(st['trigger_hit_rate'])}`"
            lines.append(base)
    lines.append("")
    lines.append("## 每日績效")
    if not daily_stats:
        lines.append("- 目前尚無到期資料。")
    else:
        lines.append("| 日期 | Horizon | 筆數 | 命中率 | 平均報酬 |")
        lines.append("|---|---:|---:|---:|---:|")
        for d in daily_stats:
            lines.append(
                f"| {d['day']} | {d['horizon']}h | {d['count']} | {pct(d['hit_rate'])} | {d['avg_return_pct']:.3f}% |"
            )
    lines.append("")
    lines.append("## 風險分布")
    if total_signals == 0:
        lines.append("- 無資料")
    else:
        for k, c in sorted(risk_dist.items(), key=lambda x: x[0]):
            lines.append(f"- `{k}` {bar(c, total_signals)} `{c}`")
    lines.append("")
    lines.append("## 決策分布")
    if total_signals == 0:
        lines.append("- 無資料")
    else:
        for k, c in sorted(decision_dist.items(), key=lambda x: x[0]):
            lines.append(f"- `{k}` {bar(c, total_signals)} `{c}`")
    lines.append("")
    lines.append("## 方向分布")
    if total_signals == 0:
        lines.append("- 無資料")
    else:
        for k, c in sorted(direction_dist.items(), key=lambda x: x[0]):
            lines.append(f"- `{k}` {bar(c, total_signals)} `{c}`")
    lines.append("")
    lines.append("## 幣種分布")
    if total_signals == 0:
        lines.append("- 無資料")
    else:
        for k, c in sorted(symbol_dist.items(), key=lambda x: x[0]):
            lines.append(f"- `{k}` {bar(c, total_signals)} `{c}`")
    lines.append("")
    lines.append("## 依決策命中率")
    if not decision_stats:
        lines.append("- 目前尚無到期資料。")
    else:
        lines.append("| 決策 | 筆數 | 命中率 | 平均報酬 |")
        lines.append("|---|---:|---:|---:|")
        for d, st in sorted(decision_stats.items(), key=lambda x: x[0]):
            lines.append(
                f"| {d} | {st['count']} | {pct(st['hit_rate'])} | {st['avg_return_pct']:.3f}% |"
            )
    lines.append("")
    lines.append("## 依方向命中率")
    if not direction_stats:
        lines.append("- 目前尚無可交易方向訊號。")
    else:
        lines.append("| 方向 | 筆數 | 方向命中率 | 觸發後命中率 | 平均報酬 |")
        lines.append("|---|---:|---:|---:|---:|")
        for d, st in sorted(direction_stats.items(), key=lambda x: x[0]):
            trigger_text = pct(st["trigger_hit_rate"]) if st["trigger_hit_rate"] is not None else "-"
            lines.append(
                f"| {d} | {st['count']} | {pct(st['hit_rate'])} | {trigger_text} | {st['avg_return_pct']:.3f}% |"
            )
    lines.append("")
    lines.append("## 命中率定義")
    lines.append("- `scale_in_test`: 回填報酬率 > 0 視為命中。")
    lines.append("- `watch`: 回填報酬率 <= 0 或振幅 <= 2% 視為命中（避免追價風險）。")
    lines.append("- `avoid`: 回填報酬率 <= 0 視為命中。")
    lines.append("- `direction-hit`: `long` 對應報酬率 > 0，`short` 對應報酬率 < 0。")
    lines.append("- `trigger-hit`: 必須先出現有效突破（5m 收線突破 + volume_ratio > 1.2），且不能在接下來 2 根 5m K 內假突破回落，再在 horizon 結束時方向仍正確。")
    lines.append("")
    lines.append("## Ollama 狀態")
    lines.append("- 影子模式可在 `--llama auto` 下自動回退規則引擎，不會阻塞分析流程。")
    lines.append("- 若模型名稱不存在會回 404；目前程式已支援自動偵測已安裝模型。")

    path.write_text("\n".join(lines), encoding="utf-8")


def write_html_dashboard(path: Path, metrics: dict[str, Any]) -> None:
    decision_dist = dict(metrics["decision_distribution"])
    direction_dist = dict(metrics["direction_distribution"])
    risk_dist = dict(metrics["risk_distribution"])
    horizon_stats = metrics["horizon_stats"]
    daily_stats = metrics["daily_stats"]
    direction_stats = metrics["direction_stats"]
    payload = {
        "total_signals": metrics["total_signals"],
        "total_evaluations": metrics["total_evaluations"],
        "decision_dist": decision_dist,
        "direction_dist": direction_dist,
        "risk_dist": risk_dist,
        "horizon_stats": horizon_stats,
        "daily_stats": daily_stats,
        "direction_stats": direction_stats,
    }
    html = f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Shadow Dashboard</title>
  <style>
    :root {{
      --bg:#f7f9fc; --card:#ffffff; --text:#1f2937; --muted:#6b7280; --line:#e5e7eb;
      --a:#0ea5e9; --b:#22c55e; --c:#f59e0b; --d:#ef4444;
    }}
    body {{ margin:0; font-family: "Segoe UI", "Noto Sans TC", sans-serif; background:var(--bg); color:var(--text); }}
    .wrap {{ max-width: 1100px; margin: 24px auto; padding: 0 16px 28px; }}
    .grid {{ display:grid; grid-template-columns: repeat(auto-fit,minmax(220px,1fr)); gap:12px; }}
    .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; }}
    h1,h2 {{ margin:0 0 10px; }}
    .num {{ font-size: 28px; font-weight: 700; }}
    .muted {{ color:var(--muted); font-size: 13px; }}
    .bar {{ height: 10px; border-radius: 999px; background: #e5e7eb; overflow: hidden; margin-top: 6px; }}
    .bar > span {{ display:block; height:100%; background: linear-gradient(90deg, var(--a), var(--b)); }}
    table {{ width:100%; border-collapse: collapse; }}
    th,td {{ text-align:left; padding:8px; border-bottom:1px solid var(--line); font-size:14px; }}
    .row {{ display:flex; gap:8px; flex-wrap:wrap; }}
    input,select,button {{ padding:9px 10px; border:1px solid var(--line); border-radius:8px; }}
    button {{ background: linear-gradient(90deg, var(--a), var(--b)); border:none; color:#fff; cursor:pointer; }}
    .result {{ border:1px solid var(--line); border-radius:10px; padding:10px; margin-top:10px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Shadow Mode Dashboard</h1>
    <p class="muted">更新時間(UTC): {utc_now().isoformat()}</p>
    <div class="grid">
      <div class="card"><div class="muted">總訊號</div><div class="num" id="totalSignals"></div></div>
      <div class="card"><div class="muted">已回填</div><div class="num" id="totalEvals"></div></div>
      <div class="card"><div class="muted">決策種類</div><div class="num" id="decisionKinds"></div></div>
      <div class="card"><div class="muted">風險層級</div><div class="num" id="riskKinds"></div></div>
    </div>

    <div class="card" style="margin-top:12px;">
      <h2>決策分布</h2>
      <div id="decisionDist"></div>
    </div>

    <div class="card" style="margin-top:12px;">
      <h2>風險分布</h2>
      <div id="riskDist"></div>
    </div>

    <div class="card" style="margin-top:12px;">
      <h2>方向分布</h2>
      <div id="directionDist"></div>
    </div>

    <div class="card" style="margin-top:12px;">
      <h2>Horizon 命中率</h2>
      <table>
        <thead><tr><th>Horizon</th><th>筆數</th><th>命中率</th><th>平均報酬</th></tr></thead>
        <tbody id="horizonRows"></tbody>
      </table>
    </div>

    <div class="card" style="margin-top:12px;">
      <h2>每日績效</h2>
      <table>
        <thead><tr><th>日期</th><th>Horizon</th><th>筆數</th><th>命中率</th><th>平均報酬</th></tr></thead>
        <tbody id="dailyRows"></tbody>
      </table>
    </div>

    <div class="card" style="margin-top:12px;">
      <h2>方向命中率</h2>
      <table>
        <thead><tr><th>方向</th><th>筆數</th><th>方向命中率</th><th>觸發後命中率</th><th>平均報酬</th></tr></thead>
        <tbody id="directionRows"></tbody>
      </table>
    </div>

    <div class="card" style="margin-top:12px;">
      <h2>即時新手查詢</h2>
      <p class="muted">先啟動：<code>python advisor_api_server.py</code>，再在這裡輸入 BTC/ETH 查白話摘要。</p>
      <div class="row">
        <input id="askSymbol" value="ETH" placeholder="輸入幣種，例如 ETH 或 BTC ETH" />
        <select id="askProfile">
          <option value="conservative">conservative</option>
          <option value="balanced">balanced</option>
          <option value="aggressive">aggressive</option>
        </select>
        <select id="askLlama">
          <option value="auto">auto</option>
          <option value="off">off</option>
          <option value="on">on</option>
        </select>
        <button id="askBtn">查詢摘要</button>
      </div>
      <div id="askStatus" class="muted">尚未查詢</div>
      <div id="askResult"></div>
    </div>
  </div>
  <script>
    const data = {json.dumps(payload, ensure_ascii=False)};
    const totalSignals = data.total_signals || 0;
    document.getElementById('totalSignals').textContent = totalSignals;
    document.getElementById('totalEvals').textContent = data.total_evaluations || 0;
    document.getElementById('decisionKinds').textContent = Object.keys(data.decision_dist || {{}}).length;
    document.getElementById('riskKinds').textContent = Object.keys(data.risk_dist || {{}}).length;

    function renderDist(elId, dist) {{
      const el = document.getElementById(elId);
      const keys = Object.keys(dist || {{}}).sort();
      if (!keys.length) {{
        el.innerHTML = '<p class="muted">無資料</p>';
        return;
      }}
      el.innerHTML = keys.map(k => {{
        const c = dist[k];
        const pct = totalSignals ? (c / totalSignals * 100).toFixed(1) : '0.0';
        return `
          <div style="margin:8px 0;">
            <div><b>${{k}}</b> - ${{c}} (${{pct}}%)</div>
            <div class="bar"><span style="width:${{pct}}%"></span></div>
          </div>
        `;
      }}).join('');
    }}
    renderDist('decisionDist', data.decision_dist);
    renderDist('riskDist', data.risk_dist);
    renderDist('directionDist', data.direction_dist);

    const hz = data.horizon_stats || {{}};
    const hzRows = Object.keys(hz).sort((a,b)=>Number(a)-Number(b)).map(k => {{
      const r = hz[k];
      const hr = ((r.hit_rate || 0) * 100).toFixed(1) + '%';
      const ar = (r.avg_return_pct || 0).toFixed(3) + '%';
      const dr = r.direction_hit_rate == null ? '-' : ((r.direction_hit_rate || 0) * 100).toFixed(1) + '%';
      const tr = r.trigger_hit_rate == null ? '-' : ((r.trigger_hit_rate || 0) * 100).toFixed(1) + '%';
      return `<tr><td>${{k}}h</td><td>${{r.count || 0}}</td><td>${{hr}} / dir ${{dr}} / trg ${{tr}}</td><td>${{ar}}</td></tr>`;
    }}).join('');
    document.getElementById('horizonRows').innerHTML = hzRows || '<tr><td colspan="4" class="muted">尚無到期資料</td></tr>';

    const dailyRows = (data.daily_stats || []).map(r => {{
      const hr = ((r.hit_rate || 0) * 100).toFixed(1) + '%';
      const ar = (r.avg_return_pct || 0).toFixed(3) + '%';
      return `<tr><td>${{r.day}}</td><td>${{r.horizon}}h</td><td>${{r.count}}</td><td>${{hr}}</td><td>${{ar}}</td></tr>`;
    }}).join('');
    document.getElementById('dailyRows').innerHTML = dailyRows || '<tr><td colspan="5" class="muted">尚無到期資料</td></tr>';

    const dirRows = Object.keys(data.direction_stats || {{}}).sort().map(k => {{
      const r = data.direction_stats[k];
      const hr = ((r.hit_rate || 0) * 100).toFixed(1) + '%';
      const tr = r.trigger_hit_rate == null ? '-' : ((r.trigger_hit_rate || 0) * 100).toFixed(1) + '%';
      const ar = (r.avg_return_pct || 0).toFixed(3) + '%';
      return `<tr><td>${{k}}</td><td>${{r.count || 0}}</td><td>${{hr}}</td><td>${{tr}}</td><td>${{ar}}</td></tr>`;
    }}).join('');
    document.getElementById('directionRows').innerHTML = dirRows || '<tr><td colspan="5" class="muted">尚無可交易方向訊號</td></tr>';

    async function askNow() {{
      const symbols = document.getElementById('askSymbol').value.trim();
      const profile = document.getElementById('askProfile').value;
      const llama = document.getElementById('askLlama').value;
      const status = document.getElementById('askStatus');
      const result = document.getElementById('askResult');
      if (!symbols) {{
        status.textContent = '請輸入幣種';
        return;
      }}
      status.textContent = '查詢中...';
      result.innerHTML = '';
      try {{
        const q = new URLSearchParams({{symbols, profile, llama}});
        const resp = await fetch('http://127.0.0.1:8765/api/analyze?' + q.toString());
        const data = await resp.json();
        if (!resp.ok) {{
          throw new Error(data.error || '查詢失敗');
        }}
        status.textContent = `完成：${{data.results.length}} 個幣種`;
        result.innerHTML = data.results.map(r => {{
          const b = r.beginner_summary || {{}};
          const lv = r.actionable_levels || {{}};
          const ls = r.long_short_plan || {{}};
          const longSetup = ls.long_setup || {{}};
          const shortSetup = ls.short_setup || {{}};
          const avoid = (b.avoid || []).map(x => `<li>${{x}}</li>`).join('');
          return `
            <div class="result">
              <div><b>${{r.symbol}}</b> - ${{b.headline || r.decision}}</div>
              <div>為什麼：${{b.core_reason || ''}}</div>
              <div>現在怎麼做：${{b.now_action || ''}}</div>
              <div>提醒價位：${{b.reminder || ''}}</div>
              <div>區間/時間：${{b.range_hint || ''}}</div>
              <div>做多做空：${{b.long_short_hint || ''}}</div>
              <div class="muted">上破=${{lv.breakout_up ?? '-'}} / 下破=${{lv.breakout_down ?? '-'}} / 區間=${{lv.range_low ?? '-'}}~${{lv.range_high ?? '-'}}</div>
              <div class="muted">做多觸發=${{longSetup.trigger_price ?? '-'}} / 做空觸發=${{shortSetup.trigger_price ?? '-'}} / 建議=${{ls.recommendation ?? '-'}}</div>
              <ul>${{avoid}}</ul>
            </div>
          `;
        }}).join('');
      }} catch (err) {{
        status.textContent = '查詢失敗：' + err.message;
      }}
    }}
    document.getElementById('askBtn').addEventListener('click', askNow);
  </script>
</body>
</html>"""
    path.write_text(html, encoding="utf-8")


def main() -> None:
    args = parse_args()
    db_path = Path(args.db_path)
    horizons = [int(x.strip()) for x in args.horizons.split(",") if x.strip()]
    md_path = Path(args.report_md)
    html_path = Path(args.dashboard_html)

    def run_once() -> None:
        conn = sqlite3.connect(db_path)
        try:
            ensure_tables(conn)
            backfill_result = backfill(conn, horizons, args.timeout, args.binance_base)
            rows = load_joined(conn)
        finally:
            conn.close()
        metrics = build_metrics(rows)
        write_md_report(md_path, backfill_result, metrics)
        write_html_dashboard(html_path, metrics)
        print(f"[{utc_now().isoformat()}] Saved markdown report: {md_path}")
        print(f"[{utc_now().isoformat()}] Saved dashboard html: {html_path}")
        print(json.dumps(backfill_result, ensure_ascii=False))

    if args.loop_minutes and args.loop_minutes > 0:
        while True:
            run_once()
            time.sleep(args.loop_minutes * 60)
    else:
        run_once()


if __name__ == "__main__":
    main()
