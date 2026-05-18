#!/usr/bin/env python3
import argparse
import json
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from shadow_mode import (
    analysis_to_dict,
    build_symbol_analysis,
    fetch_rss_items,
    normalize_symbol,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="本地交易顧問 API + 簡易網頁")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--risk-profile", choices=["conservative", "balanced", "aggressive"], default="conservative")
    p.add_argument("--llama", choices=["auto", "on", "off"], default="auto")
    p.add_argument("--llama-model", default="llama3.1:8b")
    p.add_argument("--quote", default="USDT")
    p.add_argument("--timeout", type=int, default=15)
    return p.parse_args()


def html_page(default_profile: str, default_llama: str) -> str:
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>本地交易顧問 API</title>
  <style>
    :root {{
      --bg:#f4f7fb;
      --card:#fff;
      --text:#111827;
      --line:#e5e7eb;
      --muted:#6b7280;
      --a:#0284c7;
      --b:#059669;
    }}
    body {{
      margin:0;
      font-family:"Segoe UI","Noto Sans TC",sans-serif;
      background:var(--bg);
      color:var(--text);
    }}
    .wrap {{
      max-width:980px;
      margin:20px auto;
      padding:0 14px 20px;
    }}
    .card {{
      background:var(--card);
      border:1px solid var(--line);
      border-radius:12px;
      padding:14px;
      margin-bottom:12px;
    }}
    h1,h2 {{
      margin:0 0 10px;
    }}
    .row {{
      display:flex;
      gap:8px;
      flex-wrap:wrap;
    }}
    input,select,button {{
      padding:10px 12px;
      border:1px solid var(--line);
      border-radius:10px;
      font-size:15px;
    }}
    input {{
      min-width:260px;
      flex:1;
    }}
    button {{
      background:linear-gradient(90deg,var(--a),var(--b));
      color:#fff;
      border:none;
      cursor:pointer;
    }}
    .muted {{
      color:var(--muted);
      font-size:13px;
    }}
    .item {{
      border:1px solid var(--line);
      border-radius:10px;
      padding:12px;
      margin-top:10px;
    }}
    code {{
      background:#f3f4f6;
      padding:2px 6px;
      border-radius:6px;
    }}
    ul {{
      padding-left:20px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>本地交易顧問 API</h1>
      <p class="muted">輸入幣種代號，例如 BTC ETH，快速查看目前報告與白話摘要。</p>
      <div class="row">
        <input id="symbols" value="ETH" placeholder="輸入 BTC ETH SOL" />
        <select id="profile">
          <option value="conservative">conservative</option>
          <option value="balanced">balanced</option>
          <option value="aggressive">aggressive</option>
        </select>
        <select id="llama">
          <option value="auto">auto</option>
          <option value="off">off</option>
          <option value="on">on</option>
        </select>
        <button id="runBtn">開始查詢</button>
      </div>
      <p class="muted">預設 profile=<code>{default_profile}</code>，llama=<code>{default_llama}</code></p>
    </div>
    <div class="card">
      <h2>查詢結果</h2>
      <div id="status" class="muted">等待查詢</div>
      <div id="results"></div>
    </div>
  </div>
  <script>
    document.getElementById('profile').value = '{default_profile}';
    document.getElementById('llama').value = '{default_llama}';

    function fmtValue(value, fallback = '-') {{
      if (value === undefined || value === null || value === '') {{
        return fallback;
      }}
      if (typeof value === 'number' && Number.isFinite(value)) {{
        return value.toFixed(4).replace(/\\.?0+$/, '');
      }}
      if (typeof value === 'string') {{
        const maybeNumber = Number(value);
        if (value.trim() !== '' && Number.isFinite(maybeNumber)) {{
          return maybeNumber.toFixed(4).replace(/\\.?0+$/, '');
        }}
      }}
      return String(value);
    }}

    function fmtRange(range) {{
      return `${{fmtValue(range?.[0])}} ~ ${{fmtValue(range?.[1])}}`;
    }}

    function appendParagraph(parent, text, className = '') {{
      const p = document.createElement('p');
      if (className) {{
        p.className = className;
      }}
      p.textContent = text;
      parent.appendChild(p);
      return p;
    }}

    function buildResultItem(result) {{
      const item = document.createElement('div');
      item.className = 'item';

      const heading = document.createElement('h3');
      heading.textContent = fmtValue(result.symbol);
      item.appendChild(heading);

      const beginner = result.beginner_summary || {{}};
      const levels = result.actionable_levels || {{}};
      const longShort = result.long_short_plan || {{}};
      const longSetup = longShort.long_setup || {{}};
      const shortSetup = longShort.short_setup || {{}};
      const shortSignal = result.short_term_signal || {{}};

      const headline = document.createElement('p');
      const bold = document.createElement('b');
      bold.textContent = fmtValue(beginner.headline, '');
      headline.appendChild(bold);
      item.appendChild(headline);

      appendParagraph(item, `為什麼：${{fmtValue(beginner.core_reason, '')}}`);
      appendParagraph(item, `現在怎麼做：${{fmtValue(beginner.now_action, '')}}`);
      appendParagraph(item, `提醒：${{fmtValue(beginner.reminder, '')}}`);
      appendParagraph(item, `區間提示：${{fmtValue(beginner.range_hint, '')}}`);
      appendParagraph(item, `盤面階段：${{fmtValue(beginner.phase_hint, '')}}`);
      appendParagraph(item, `支撐壓力地圖：${{fmtValue(beginner.path_map_hint, '')}}`);
      appendParagraph(item, `破位後路徑：${{fmtValue(beginner.break_path_hint, '')}}`);
      appendParagraph(item, `建議止損：${{fmtValue(beginner.stop_hint, '')}}`);
      appendParagraph(item, `槓桿提示：${{fmtValue(beginner.leverage_hint, '')}}`);

      const longShortHint = document.createElement('p');
      const longShortBold = document.createElement('b');
      longShortBold.textContent = '多空提示：';
      longShortHint.appendChild(longShortBold);
      longShortHint.appendChild(document.createTextNode(fmtValue(beginner.long_short_hint, '')));
      item.appendChild(longShortHint);

      appendParagraph(
        item,
        `突破價：上破 ${{fmtValue(levels.breakout_up)}} / 下破 ${{fmtValue(levels.breakout_down)}} / 大區間 ${{fmtValue(levels.range_low)}} ~ ${{fmtValue(levels.range_high)}}`,
        'muted'
      );
      appendParagraph(
        item,
        `執行區：做多 ${{fmtRange(levels.long_ready_zone)}} / 做空 ${{fmtRange(levels.short_ready_zone)}} / 噪音區 ${{fmtRange(levels.noise_zone)}}`,
        'muted'
      );
      appendParagraph(
        item,
        `短線訊號：${{fmtValue(shortSignal.bias)}} / 強度 ${{fmtValue(shortSignal.strength)}} / 做多觸發 ${{fmtValue(longSetup.trigger_price)}} / 做空觸發 ${{fmtValue(shortSetup.trigger_price)}} / 建議 ${{fmtValue(longShort.recommendation)}}`,
        'muted'
      );

      const avoid = beginner.avoid || [];
      if (avoid.length) {{
        const ul = document.createElement('ul');
        avoid.forEach((entry) => {{
          const li = document.createElement('li');
          li.textContent = fmtValue(entry, '');
          ul.appendChild(li);
        }});
        item.appendChild(ul);
      }}

      appendParagraph(
        item,
        `現價=${{fmtValue(result.price)}} / 決策=${{fmtValue(result.decision)}} / 風險=${{fmtValue(result.risk_level)}}(${{fmtValue(result.risk_score)}})`,
        'muted'
      );

      return item;
    }}

    async function runAnalyze() {{
      const symbols = document.getElementById('symbols').value.trim();
      const profile = document.getElementById('profile').value;
      const llama = document.getElementById('llama').value;
      const status = document.getElementById('status');
      const results = document.getElementById('results');

      if (!symbols) {{
        status.textContent = '請先輸入幣種';
        return;
      }}

      status.textContent = '查詢中...';
      results.replaceChildren();

      try {{
        const q = new URLSearchParams({{ symbols, profile, llama }});
        const resp = await fetch('/api/analyze?' + q.toString());
        const data = await resp.json();
        if (!resp.ok) {{
          throw new Error(data.error || 'API 錯誤');
        }}

        status.textContent = `完成：${{data.results.length}} 個幣種`;
        data.results.forEach((result) => {{
          results.appendChild(buildResultItem(result));
        }});
      }} catch (err) {{
        status.textContent = '錯誤：' + err.message;
      }}
    }}

    document.getElementById('runBtn').addEventListener('click', runAnalyze);
  </script>
</body>
</html>"""


def build_handler(args: argparse.Namespace):
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, payload: dict[str, Any], code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str, code: int = 200) -> None:
            body = html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self._send_html(html_page(args.risk_profile, args.llama))
                return
            if parsed.path == "/health":
                self._send_json({"ok": True})
                return
            if parsed.path != "/api/analyze":
                self._send_json({"error": "Not Found"}, 404)
                return

            try:
                qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                symbols_raw = qs.get("symbols", ["ETH"])[0]
                profile = qs.get("profile", [args.risk_profile])[0]
                llama_mode = qs.get("llama", [args.llama])[0]
                if profile not in {"conservative", "balanced", "aggressive"}:
                    profile = args.risk_profile
                if llama_mode not in {"auto", "on", "off"}:
                    llama_mode = args.llama

                raw_tokens = symbols_raw.replace(",", " ").split()
                if not raw_tokens:
                    self._send_json({"error": "請提供 symbols，例如 BTC ETH"}, 400)
                    return
                symbols = [normalize_symbol(s, args.quote) for s in raw_tokens[:5]]
                rss_items = fetch_rss_items(args.timeout)
                results: list[dict[str, Any]] = []
                errors: list[dict[str, str]] = []
                for symbol in symbols:
                    try:
                        analysis = build_symbol_analysis(
                            symbol=symbol,
                            profile=profile,
                            timeout=args.timeout,
                            llama_mode=llama_mode,
                            llama_model=args.llama_model,
                            rss_items=rss_items,
                        )
                        results.append(analysis_to_dict(analysis))
                    except Exception as exc:  # noqa: BLE001
                        errors.append({"symbol": symbol, "error": str(exc)})
                self._send_json(
                    {
                        "profile": profile,
                        "llama": llama_mode,
                        "results": results,
                        "errors": errors,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc), "trace": traceback.format_exc(limit=1)}, 500)

    return Handler


def main() -> None:
    args = parse_args()
    handler = build_handler(args)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Server running on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
