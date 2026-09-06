#!/usr/bin/env python3
"""Read-only local dashboard for BTC 5-minute bot JSONL logs."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BTC 5m Bot Dashboard</title>
<style>
body{font:14px system-ui,sans-serif;margin:24px;background:#10151f;color:#e7edf7}h1{margin-bottom:4px}
.muted{color:#aab7ca}.cards{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}.card{background:#192231;border-radius:8px;padding:14px;min-width:150px}.value{font-size:24px;font-weight:700}
table{border-collapse:collapse;width:100%;background:#192231}th,td{text-align:left;padding:9px;border-bottom:1px solid #2d3a4e;vertical-align:top}th{color:#aab7ca}code{white-space:pre-wrap;word-break:break-word}.good{color:#61d69b}.bad{color:#ff8b8b}
</style></head><body>
<h1>BTC Up/Down 5m bot</h1><div class="muted" id="updated">Loading local log files…</div>
<div class="cards"><div class="card">Accepted fills<div class="value" id="fills">0</div></div><div class="card">Unavailable entries<div class="value" id="unavailable">0</div></div><div class="card">Resolved P/L<div class="value" id="pnl">$0.00</div></div><div class="card">Open positions<div class="value" id="open">0</div></div></div>
<h2>Trades and results</h2><table><thead><tr><th>Time (ADT/AST)</th><th>Entry buy</th><th>Price</th><th>Size</th><th>Final outcome</th><th>P/L / status</th></tr></thead><tbody id="trades"></tbody></table>
<h2>Unavailable entries and operational events</h2><table><thead><tr><th>Time (ADT/AST)</th><th>Event</th><th>Price / time left</th><th>Reason</th></tr></thead><tbody id="events"></tbody></table>
<script>
const dollar=v=>'$'+Number(v||0).toFixed(2), text=v=>v==null?'':String(v);
const adt=v=>v?new Intl.DateTimeFormat('en-CA',{timeZone:'America/Halifax',year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit',timeZoneName:'short'}).format(new Date(v)):'';
function finalOutcome(trade){
  if(!trade.settlement)return 'Awaiting resolution';
  if(trade.settlement.final_outcome)return trade.settlement.final_outcome;
  if(trade.settlement.final_price===1)return trade.outcome;
  if(trade.settlement.final_price===0)return trade.outcome==='Up'?'Down':'Up';
  return 'Awaiting resolution';
}
function cell(row, value, cls=''){let td=document.createElement('td');td.textContent=text(value);td.className=cls;row.appendChild(td)}
async function refresh(){
  const r=await fetch('/api/data',{cache:'no-store'}); if(!r.ok)throw new Error('Unable to read logs'); const d=await r.json();
  document.querySelector('#fills').textContent=d.summary.fills; document.querySelector('#unavailable').textContent=d.summary.unavailable;
  document.querySelector('#pnl').textContent=dollar(d.summary.realized_pnl); document.querySelector('#pnl').className=d.summary.realized_pnl<0?'bad':'good';
  document.querySelector('#open').textContent=d.summary.open_positions; document.querySelector('#updated').textContent='Reading '+d.directory+' • refreshed '+new Date().toLocaleTimeString();
  const trades=document.querySelector('#trades'); trades.replaceChildren();
  for(const x of d.trades){const row=document.createElement('tr'); cell(row,adt(x.timestamp)); cell(row,(x.outcome||'')+' buy • '+(x.token_id||'')); cell(row,dollar(x.filled_price||x.price)); cell(row,dollar(x.size_usdc)); cell(row,finalOutcome(x)); const result=x.settlement?dollar(x.settlement.pnl_usdc):(x.status||'accepted'); cell(row,result,x.settlement?.pnl_usdc<0?'bad':'good'); trades.appendChild(row)}
  const events=document.querySelector('#events'); events.replaceChildren();
  for(const x of d.events){const row=document.createElement('tr'); cell(row,adt(x.timestamp)); cell(row,x.event); cell(row,(x.price==null?'':dollar(x.price))+(x.seconds_remaining==null?'':' • '+Math.round(x.seconds_remaining)+'s')); cell(row,x.reason||x.error||''); events.appendChild(row)}
}
refresh().catch(e=>document.querySelector('#updated').textContent=e.message); setInterval(()=>refresh().catch(()=>{}),5000);
</script></body></html>"""


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read complete JSONL records only; an in-progress append never breaks the UI."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
        except json.JSONDecodeError:
            continue
    return records


def dashboard_data(directory: Path) -> dict[str, Any]:
    paper = read_jsonl(directory / "paper_trades.jsonl")
    live = read_jsonl(directory / "live_trades.jsonl")
    events = read_jsonl(directory / "bot_events.jsonl")
    state = {}
    try:
        state = json.loads((directory / "bot_state.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    settlements = state.get("settlements", {}) if isinstance(state, dict) else {}
    trades = sorted(paper + live, key=lambda item: str(item.get("timestamp", "")), reverse=True)
    for trade in trades:
        if market_id := trade.get("market_id"):
            trade["settlement"] = settlements.get(market_id)
    relevant_events = [
        event for event in events
        if event.get("event") in {"entry_unavailable", "skipped_opportunity", "operational_error", "critical_stop"}
    ]
    return {
        "directory": str(directory),
        "summary": {
            "fills": len(trades),
            "unavailable": sum(event.get("event") == "entry_unavailable" for event in events),
            "realized_pnl": sum(float(item.get("pnl_usdc", 0)) for item in settlements.values()),
            "open_positions": len(state.get("open_trades", {})) if isinstance(state, dict) else 0,
        },
        "trades": trades[:200],
        "events": sorted(relevant_events, key=lambda item: str(item.get("timestamp", "")), reverse=True)[:300],
    }


def make_handler(directory: Path) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                self._respond(HTTPStatus.OK, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/data":
                self._respond(HTTPStatus.OK, json.dumps(dashboard_data(directory)).encode(), "application/json")
            else:
                self._respond(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")

        def _respond(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return DashboardHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the local BTC bot trade dashboard.")
    parser.add_argument("--directory", type=Path, default=Path.cwd(), help="Directory containing the bot JSONL files")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    directory = args.directory.resolve()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(directory))
    print(f"Dashboard: http://127.0.0.1:{args.port} (reading {directory})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
