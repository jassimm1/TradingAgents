"""Small authenticated web interface for running TradingAgents from a phone."""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import threading
import uuid
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv


_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env", override=False)

_LOG = logging.getLogger("tradingagents.web")
_ALLOWED_ANALYSTS = {"market", "news", "fundamentals", "sentiment"}
_GRAPH_ANALYST_KEYS = {"sentiment": "social"}
_TICKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.^=-]{0,19}$")
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_ACTIVE_JOB: str | None = None
_USERNAME = os.environ.get("APP_USERNAME", "").strip()
_PASSWORD = os.environ.get("APP_PASSWORD", "")


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#101827"><title>TradingAgents</title>
<style>
:root{color-scheme:dark;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#101827;color:#e8edf5}
*{box-sizing:border-box}body{margin:0;padding:20px;display:flex;justify-content:center}
main{width:min(100%,700px)}h1{font-size:1.7rem;margin:8px 0}p{color:#aab8ca;line-height:1.45}
form,.card{background:#182336;border:1px solid #2a3a52;border-radius:16px;padding:18px;margin:16px 0}
label{display:block;font-weight:600;margin:12px 0 6px}input,button{font:inherit;border-radius:10px;width:100%;padding:12px;border:1px solid #3b4c65;background:#111b2a;color:#f4f7fb}
input[type=checkbox]{width:auto;accent-color:#54d6a0}.checks{display:grid;grid-template-columns:1fr 1fr;gap:8px 12px}.checks label{font-weight:400;margin:4px 0}
button{margin-top:18px;border:0;background:#36bd8b;color:#071a14;font-weight:750;cursor:pointer}button:disabled{opacity:.55;cursor:wait}
#status{font-weight:700}#output{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.5;font:14px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace}
.muted{font-size:.9rem;color:#9caec2}.badge{display:inline-block;border-radius:999px;background:#263750;padding:4px 9px;font-size:.82rem}
@media(max-width:480px){body{padding:12px}.checks{grid-template-columns:1fr 1fr}form,.card{padding:14px}}
</style></head>
<body><main>
<h1>TradingAgents</h1><p>Run an AI-assisted market analysis and read the report here on your phone.</p>
<form id="form">
<label for="ticker">Ticker symbol</label><input id="ticker" name="ticker" value="NVDA" maxlength="20" required autocomplete="off">
<label for="date">Analysis date</label><input id="date" name="date" type="date" required>
<label>Analysts</label><div class="checks">
<label><input type="checkbox" name="analysts" value="market" checked> Market</label>
<label><input type="checkbox" name="analysts" value="news" checked> News</label>
<label><input type="checkbox" name="analysts" value="fundamentals" checked> Fundamentals</label>
<label><input type="checkbox" name="analysts" value="sentiment"> Sentiment</label>
</div><button id="run">Run analysis</button>
</form>
<section class="card" aria-live="polite"><div id="status">Ready</div><p class="muted">Analysis can take several minutes and uses your configured model API.</p><div id="output"></div></section>
</main>
<script>
const dateInput=document.querySelector('#date');dateInput.value=new Date().toISOString().slice(0,10);
const form=document.querySelector('#form'),button=document.querySelector('#run'),status=document.querySelector('#status'),output=document.querySelector('#output');
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
form.addEventListener('submit',async event=>{
 event.preventDefault();button.disabled=true;output.textContent='';status.textContent='Submitting…';
 try{
  const analysts=[...form.querySelectorAll('input[name="analysts"]:checked')].map(item=>item.value);
  const response=await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticker:form.ticker.value.trim(),date:dateInput.value,analysts})});
  const started=await response.json();if(!response.ok)throw Error(started.error||'Could not start the analysis');
  status.textContent='Analysis running…';
  while(true){await wait(2500);const result=await fetch('/api/status?id='+encodeURIComponent(started.id));const job=await result.json();
   if(!result.ok)throw Error(job.error||'Could not read analysis status');
   if(job.status==='succeeded'){status.textContent=job.ticker+' · '+job.date+' · '+(job.rating||'Complete');output.textContent=job.report||'Analysis complete.';break}
   if(job.status==='failed'){status.textContent='Analysis failed';output.textContent=job.error||'Check the service logs.';break}
  }
 }catch(error){status.textContent='Could not run analysis';output.textContent=error.message}
 finally{button.disabled=false}
});
</script></body></html>"""


def _json(handler: BaseHTTPRequestHandler, status: int, data: dict) -> None:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _graph_analyst_keys(analysts: list[str]) -> list[str]:
    return [_GRAPH_ANALYST_KEYS.get(key, key) for key in analysts]


def _run_job(job_id: str, ticker: str, trade_date: str, analysts: list[str]) -> None:
    global _ACTIVE_JOB
    try:
        from tradingagents.agents.rating import run_rating
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph(
            _graph_analyst_keys(analysts), config=DEFAULT_CONFIG.copy(), debug=False
        )
        state, signal = graph.propagate(ticker, trade_date)
        rating = run_rating(state) or signal
        report_dir = graph.save_reports(state, ticker, html=False)
        sections = []
        for report_file in sorted(Path(report_dir).glob("*.md")):
            content = report_file.read_text(encoding="utf-8").strip()
            if content:
                sections.append(f"## {report_file.stem.replace('_', ' ').title()}\n\n{content}")
        report = "\n\n---\n\n".join(sections) or str(state.get("final_trade_decision", signal))
        with _JOBS_LOCK:
            _JOBS[job_id].update(status="succeeded", rating=str(rating), report=report[:200_000])
    except Exception:
        _LOG.exception("Analysis job %s failed", job_id)
        with _JOBS_LOCK:
            _JOBS[job_id].update(
                status="failed",
                error="The analysis failed. Check the private service logs for details.",
            )
    finally:
        with _JOBS_LOCK:
            _ACTIVE_JOB = None


class Handler(BaseHTTPRequestHandler):
    server_version = "TradingAgentsWeb/1.0"

    def log_message(self, format: str, *args) -> None:
        _LOG.info("%s - %s", self.address_string(), format % args)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        try:
            userpass = base64.b64decode(token, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            userpass = ""
        username, separator, password = userpass.partition(":")
        valid = (
            scheme.lower() == "basic"
            and bool(separator)
            and hmac.compare_digest(username, _USERNAME)
            and hmac.compare_digest(password, _PASSWORD)
        )
        if not valid:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="TradingAgents", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
        return valid

    def do_GET(self) -> None:
        path = urlparse(self.path)
        if path.path == "/health":
            _json(self, 200, {"ok": True})
            return
        if not self._authorized():
            return
        if path.path == "/":
            body = _PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path.path == "/api/status":
            job_id = parse_qs(path.query).get("id", [""])[0]
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                response = dict(job) if job else None
            if response is None:
                _json(self, 404, {"error": "Analysis not found. Start a new run."})
            else:
                _json(self, 200, response)
        else:
            _json(self, 404, {"error": "Not found"})

    def do_POST(self) -> None:
        global _ACTIVE_JOB
        if not self._authorized():
            return
        if urlparse(self.path).path != "/api/analyze":
            _json(self, 404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 10_000:
                raise ValueError("Request must contain JSON under 10 KB.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object.")
            ticker = str(payload.get("ticker", "")).strip().upper()
            trade_date = str(payload.get("date", ""))
            analysts = payload.get("analysts", [])
            if not _TICKER_RE.fullmatch(ticker):
                raise ValueError("Enter a valid ticker symbol.")
            parsed_date = date.fromisoformat(trade_date)
            if parsed_date > date.today():
                raise ValueError("Choose today or an earlier date.")
            if not isinstance(analysts, list) or not analysts or not set(analysts) <= _ALLOWED_ANALYSTS:
                raise ValueError("Select at least one valid analyst.")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            _json(self, 400, {"error": str(exc) or "Invalid request."})
            return

        with _JOBS_LOCK:
            if _ACTIVE_JOB is not None:
                _json(self, 409, {"error": "An analysis is already running. Wait for it to finish."})
                return
            job_id = uuid.uuid4().hex
            _ACTIVE_JOB = job_id
            _JOBS.clear()
            _JOBS[job_id] = {"id": job_id, "ticker": ticker, "date": trade_date, "status": "running"}
        threading.Thread(
            target=_run_job,
            args=(job_id, ticker, trade_date, analysts),
            name=f"analysis-{job_id[:8]}",
            daemon=True,
        ).start()
        _json(self, 202, {"id": job_id, "status": "running"})


def main() -> None:
    if not _USERNAME or not _PASSWORD:
        raise SystemExit("Set APP_USERNAME and APP_PASSWORD before starting the web app.")
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", "10000"))
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    server = ThreadingHTTPServer((host, port), Handler)
    _LOG.info("TradingAgents phone app listening on %s:%s", host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
