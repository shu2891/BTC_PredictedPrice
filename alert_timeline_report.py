#!/usr/bin/env python3
import argparse
import json
import sqlite3
from pathlib import Path

from market_alert_daemon import ensure_state_db


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export alert event timeline from alert_state.db.")
    p.add_argument("--db-path", default="alert_state.db")
    p.add_argument("--report-md", default="提醒事件時間軸.md")
    p.add_argument("--limit", type=int, default=200)
    return p.parse_args()


def load_rows(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT symbol, event_type, sent_at, last_price, level, stop_loss, take_profit_1, take_profit_2
        FROM alert_events
        ORDER BY sent_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def load_states(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT symbol, state_json FROM symbol_states ORDER BY symbol").fetchall()
    states: dict[str, dict] = {}
    for row in rows:
        try:
            states[row["symbol"]] = json.loads(row["state_json"])
        except Exception:
            states[row["symbol"]] = {}
    return states


def main() -> None:
    args = parse_args()
    conn = ensure_state_db(Path(args.db_path))
    rows = load_rows(conn, args.limit)
    states = load_states(conn)

    lines = ["# 提醒事件時間軸", ""]
    lines.append(f"- 事件數: `{len(rows)}`")
    lines.append(f"- 追蹤中標的數: `{len(states)}`")
    lines.append("")

    if states:
        lines.append("## 目前狀態")
        for symbol, state in states.items():
            lines.append(
                f"- {symbol}: long_stage=`{state.get('long_stage', 'idle')}` / "
                f"short_stage=`{state.get('short_stage', 'idle')}` / "
                f"long_retest=`{state.get('long_retest_seen_at')}` / "
                f"short_retest=`{state.get('short_retest_seen_at')}`"
            )
        lines.append("")

    lines.append("## 最近事件")
    if not rows:
        lines.append("- 無資料")
    else:
        for row in rows:
            lines.append(
                f"- `{row['sent_at']}` {row['symbol']} `{row['event_type']}` "
                f"價位=`{row['level']}` 現價=`{row['last_price']}` "
                f"止損=`{row['stop_loss']}` TP1=`{row['take_profit_1']}` TP2=`{row['take_profit_2']}`"
            )

    Path(args.report_md).write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {args.report_md}")


if __name__ == "__main__":
    main()
