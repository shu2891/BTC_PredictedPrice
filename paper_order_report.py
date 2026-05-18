#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from paper_order_engine import build_paper_report, ensure_paper_db


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate markdown report for simulated orders.")
    parser.add_argument("--paper-db", default="paper_trading.db")
    parser.add_argument("--output-md", default="paper_trading_report.md")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    conn = ensure_paper_db(Path(args.paper_db))
    try:
        build_paper_report(conn, Path(args.output_md))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
