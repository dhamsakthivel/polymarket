#!/usr/bin/env python3
"""Read-only local dashboard for BTC 5-minute bot JSONL logs."""

from __future__ import annotations

import argparse
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from datetime import datetime
from zoneinfo import ZoneInfo


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BTC 5m Bot Dashboard</title>
<style>
body{font:14px system-ui,sans-serif;margin:24px;background:#10151f;color:#e7edf7}h1{margin-bottom:4px}
.muted{color:#aab7ca}.cards{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}.card{background:#192231;border-radius:8px;padding:14px;min-width:150px}.value{font-size:24px;font-weight:700}
table{border-collapse:collapse;width:100%;background:#192231}th,td{text-align:left;padding:9px;border-bottom:1px solid #2d3a4e;vertical-align:top}th{color:#aab7ca}code{white-space:pre-wrap;word-break:break-word}.good{color:#61d69b}.bad{color:#ff8b8b}
button{border:0;border-radius:6px;padding:9px 12px;font-weight:700;cursor:pointer}.on{background:#61d69b;color:#102018}.off{background:#ff8b8b;color:#290d0d}
</style></head><body>
<h1>BTC Up/Down 5m bot</h1><div class="muted" id="updated">Loading local log files…</div>
<div class="cards"><div class="card">±$200 contra entries<div class="value" id="contra-status">Loading…</div><button id="contra-toggle" disabled>Loading…</button></div><div class="card">Accepted fills<div class="value" id="fills">0</div></div><div class="card">Unavailable entries<div class="value" id="unavailable">0</div></div><div class="card">Resolved P/L (USDC)<div class="value" id="pnl">$0.00</div></div><div class="card">Resolved P/L (INR)<div class="value" id="pnl-inr">₹0.00</div></div><div class="card">Open positions<div class="value" id="open">0</div></div></div>
<h2>Day-wise summary</h2><table><thead><tr><th>Date (ADT/AST)</th><th>Trades</th><th>Total lot size</th><th>Wins</th><th>Losses</th><th>Settled P/L (USDC)</th><th>Settled P/L (INR)</th></tr></thead><tbody id="daily"></tbody></table>
<h2>Trades and results</h2><table><thead><tr><th>Time (ADT/AST)</th><th>Entry buy</th><th>Price</th><th>BTC difference (signed)</th><th>Lot size</th><th>Final outcome</th><th>P/L (USDC) / status</th><th>P/L (INR)</th></tr></thead><tbody id="trades"></tbody></table>
<h2>Price checkpoints</h2><div class="muted">First observed leading-outcome price at each level for every monitored 5-minute market.</div><table><thead><tr><th>Time (ADT/AST)</th><th>Level</th><th>Outcome at level</th><th>Up price</th><th>Down price</th><th>BTC difference (signed)</th><th>Since market start</th><th>Time remaining</th></tr></thead><tbody id="milestones"></tbody></table>
<h2>Unavailable entries and operational events</h2><table><thead><tr><th>Time (ADT/AST)</th><th>Event</th><th>Price / time left</th><th>Reason</th></tr></thead><tbody id="events"></tbody></table>
<script>
const dollar=v=>'$'+Number(v||0).toFixed(2), rupee=v=>'₹'+Number(v||0).toFixed(2), text=v=>v==null?'':String(v);
const adt=v=>v?new Intl.DateTimeFormat('en-CA',{timeZone:'America/Halifax',year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit',timeZoneName:'short'}).format(new Date(v)):'';
const duration=v=>v==null?'—':Math.floor(v/60)+'m '+Math.floor(v%60)+'s';
function finalOutcome(trade){
  if(!trade.settlement)return 'Awaiting resolution';
  if(trade.settlement.final_outcome)return trade.settlement.final_outcome;
  if(trade.settlement.final_price===1)return trade.outcome;
  if(trade.settlement.final_price===0)return trade.outcome==='Up'?'Down':'Up';
  return 'Awaiting resolution';
}
function cell(row, value, cls=''){let td=document.createElement('td');td.textContent=text(value);td.className=cls;row.appendChild(td)}
function setContraControl(enabled){
  const status=document.querySelector('#contra-status'), button=document.querySelector('#contra-toggle');
  status.textContent=enabled?'ON':'OFF'; status.className=enabled?'good':'bad';
  button.textContent=enabled?'Turn off contra':'Turn on contra'; button.className=enabled?'off':'on'; button.disabled=false;
}
document.querySelector('#contra-toggle').addEventListener('click',async()=>{
  const button=document.querySelector('#contra-toggle'), enabled=button.className!=='off';
  button.disabled=true;
  try {const r=await fetch('/api/contra-trading-enabled',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});if(!r.ok)throw new Error('Unable to save control');setContraControl((await r.json()).contra_trading_enabled)}
  catch(e){document.querySelector('#updated').textContent=e.message;button.disabled=false}
});
async function refresh(){
  const r=await fetch('/api/data',{cache:'no-store'}); if(!r.ok)throw new Error('Unable to read logs'); const d=await r.json();
  setContraControl(d.contra_trading_enabled);
  document.querySelector('#fills').textContent=d.summary.fills; document.querySelector('#unavailable').textContent=d.summary.unavailable;
  document.querySelector('#pnl').textContent=dollar(d.summary.realized_pnl); document.querySelector('#pnl').className=d.summary.realized_pnl<0?'bad':'good';
  document.querySelector('#pnl-inr').textContent=rupee(d.summary.realized_pnl_inr); document.querySelector('#pnl-inr').className=d.summary.realized_pnl_inr<0?'bad':'good';
  document.querySelector('#open').textContent=d.summary.open_positions; document.querySelector('#updated').textContent='Reading '+d.directory+' • refreshed '+new Date().toLocaleTimeString();
  const daily=document.querySelector('#daily'); daily.replaceChildren();
  for(const x of d.daily_summary){const row=document.createElement('tr'); cell(row,x.date); cell(row,x.trades); cell(row,dollar(x.lot_size_usdc)); cell(row,x.wins); cell(row,x.losses); cell(row,dollar(x.realized_pnl),x.realized_pnl<0?'bad':'good'); cell(row,rupee(x.realized_pnl_inr),x.realized_pnl_inr<0?'bad':'good'); daily.appendChild(row)}
  const trades=document.querySelector('#trades'); trades.replaceChildren();
  for(const x of d.trades){const row=document.createElement('tr'); cell(row,adt(x.timestamp)); cell(row,(x.outcome||'')+' buy • '+(x.token_id||'')); cell(row,dollar(x.filled_price||x.price)); cell(row,x.actual_price_difference_usdc==null?(x.price_difference_usdc==null?'—':dollar(x.price_difference_usdc)):dollar(x.actual_price_difference_usdc)); cell(row,dollar(x.size_usdc)); cell(row,finalOutcome(x)); const result=x.settlement?dollar(x.settlement.pnl_usdc):(x.status||'accepted'); cell(row,result,x.settlement?.pnl_usdc<0?'bad':'good'); cell(row,x.settlement?rupee(x.settlement.pnl_usdc*d.inr_per_usdc):'—',x.settlement?.pnl_usdc<0?'bad':'good'); trades.appendChild(row)}
  const milestones=document.querySelector('#milestones'); milestones.replaceChildren();
  for(const x of d.price_milestones){const row=document.createElement('tr'); cell(row,adt(x.timestamp)); cell(row,x.milestone_cents+'¢'); cell(row,x.milestone_outcome); cell(row,dollar(x.up_price)); cell(row,dollar(x.down_price)); cell(row,x.actual_price_difference_usdc==null?'—':dollar(x.actual_price_difference_usdc)); cell(row,duration(x.elapsed_seconds)); cell(row,duration(x.seconds_remaining)); milestones.appendChild(row)}
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


def contra_trading_enabled(control_file: Path) -> bool:
    """Missing flag means contra entries are enabled; a malformed flag is safe-off."""
    if not control_file.exists():
        return True
    try:
        control = json.loads(control_file.read_text(encoding="utf-8"))
        return control.get("contra_trading_enabled", True) is True
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def save_contra_trading_control(control_file: Path, enabled: bool) -> None:
    temporary = control_file.with_suffix(".tmp")
    temporary.write_text(json.dumps({"contra_trading_enabled": enabled}, indent=2) + "\n", encoding="utf-8")
    temporary.replace(control_file)


def dashboard_data(directory: Path, inr_per_usdc: float = 83.0,
                   control_file: Path | None = None) -> dict[str, Any]:
    control_file = control_file or directory / "bot_control.json"
    paper = read_jsonl(directory / "paper_trades.jsonl")
    live = read_jsonl(directory / "live_trades.jsonl")
    events = read_jsonl(directory / "bot_events.jsonl")
    state = {}
    try:
        state = json.loads((directory / "bot_state.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    settlements = dict(state.get("settlements", {})) if isinstance(state, dict) else {}
    # State can be recreated after a crash; event logs remain the durable result history.
    for event in events:
        if event.get("event") == "market_settled" and event.get("market_id"):
            settlements[event["market_id"]] = {
                "settled_at": event.get("settled_at"),
                "pnl_usdc": event.get("pnl_usdc", 0),
                "final_price": event.get("final_price"),
                "final_outcome": event.get("final_outcome"),
            }
    trades = sorted(paper + live, key=lambda item: str(item.get("timestamp", "")), reverse=True)
    for trade in trades:
        if market_id := trade.get("market_id"):
            trade["settlement"] = settlements.get(market_id)
    atlantic = ZoneInfo("America/Halifax")
    daily: dict[str, dict[str, Any]] = {}
    for trade in trades:
        try:
            date = datetime.fromisoformat(str(trade["timestamp"]).replace("Z", "+00:00")).astimezone(atlantic).date().isoformat()
        except (KeyError, ValueError):
            continue
        row = daily.setdefault(date, {"date": date, "trades": 0, "lot_size_usdc": 0.0,
                                      "wins": 0, "losses": 0, "realized_pnl": 0.0})
        row["trades"] += 1
        row["lot_size_usdc"] += float(trade.get("size_usdc", 0))
        if settlement := trade.get("settlement"):
            pnl = float(settlement.get("pnl_usdc", 0))
            row["realized_pnl"] += pnl
            row["wins"] += pnl > 0
            row["losses"] += pnl < 0
    for row in daily.values():
        row["realized_pnl_inr"] = row["realized_pnl"] * inr_per_usdc
    relevant_events = [
        event for event in events
        if event.get("event") in {"entry_unavailable", "skipped_opportunity", "operational_error", "critical_stop"}
    ]
    price_milestones = [
        event for event in events if event.get("event") == "price_milestone_reached"
    ]
    return {
        "directory": str(directory),
        "inr_per_usdc": inr_per_usdc,
        "contra_trading_enabled": contra_trading_enabled(control_file),
        "summary": {
            "fills": len(trades),
            "unavailable": sum(event.get("event") == "entry_unavailable" for event in events),
            "realized_pnl": sum(float(item.get("pnl_usdc", 0)) for item in settlements.values()),
            "realized_pnl_inr": sum(float(item.get("pnl_usdc", 0)) for item in settlements.values()) * inr_per_usdc,
            "open_positions": len(state.get("open_trades", {})) if isinstance(state, dict) else 0,
        },
        "daily_summary": sorted(daily.values(), key=lambda item: item["date"], reverse=True),
        "trades": trades[:200],
        "price_milestones": sorted(
            price_milestones, key=lambda item: str(item.get("timestamp", "")), reverse=True
        )[:500],
        "events": sorted(relevant_events, key=lambda item: str(item.get("timestamp", "")), reverse=True)[:300],
    }


def make_handler(directory: Path, inr_per_usdc: float, control_file: Path) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                self._respond(HTTPStatus.OK, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/data":
                self._respond(HTTPStatus.OK, json.dumps(
                    dashboard_data(directory, inr_per_usdc, control_file)).encode(), "application/json")
            else:
                self._respond(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/contra-trading-enabled":
                self._respond(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 1024:
                    raise ValueError("request is too large")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload.get("enabled"), bool):
                    raise ValueError("enabled must be a boolean")
                save_contra_trading_control(control_file, payload["enabled"])
                body = json.dumps({"contra_trading_enabled": payload["enabled"]}).encode()
                self._respond(HTTPStatus.OK, body, "application/json")
            except (OSError, ValueError, json.JSONDecodeError):
                self._respond(HTTPStatus.BAD_REQUEST, b"Invalid trading control request",
                              "text/plain; charset=utf-8")

        def _respond(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                # A browser refresh or closed local tab can abort its request; keep serving.
                return

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return DashboardHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the local BTC bot trade dashboard.")
    parser.add_argument("--directory", type=Path, default=Path.cwd(), help="Directory containing the bot JSONL files")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--inr-rate", type=float, default=float(os.getenv("INR_PER_USDC", "83.00")),
                        help="INR value for one USDC (default: INR_PER_USDC or 83.00)")
    parser.add_argument("--control-file", type=Path, default=None,
                        help="Shared bot control file (default: bot_control.json in --directory)")
    args = parser.parse_args()
    directory = args.directory.resolve()
    control_file = (args.control_file or directory / "bot_control.json").resolve()
    server = ThreadingHTTPServer(("127.0.0.1", args.port),
                                 make_handler(directory, args.inr_rate, control_file))
    print(f"Dashboard: http://127.0.0.1:{args.port} (reading {directory}; ₹{args.inr_rate:.2f}/USDC)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
