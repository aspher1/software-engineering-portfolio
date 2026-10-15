#!/usr/bin/env python3
"""Phone dashboard for the FOMO trader - the bot's remote control.

Runs on the VPS next to the bot (started by deploy.sh). Open on your phone:
    http://<vps-ip>:8080/?token=<dashboard_token>

Shows: bot status, open positions with live P&L, recent activity.
Buttons: Start / Stop / Kill switch. Auth is the dashboard_token in config.json -
keep the URL private, it is the only login.

Stdlib only. No extra deps.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

CONFIG = {}
CONFIG_PATH = ""
PIDFILE = os.path.join(BASE, "bot.pid")
HALTFILE = os.path.join(BASE, "halt.flag")
BOTLOG = os.path.join(BASE, "bot.log")
_TRADER = None


def trader():
    global _TRADER
    if _TRADER is None:
        from fomo_trader import Trader
        state_path = os.path.join(BASE, "state.json")
        _TRADER = Trader(CONFIG, state_path)
    return _TRADER


def bot_running():
    if not os.path.exists(PIDFILE):
        return False
    try:
        pid = int(open(PIDFILE).read().strip())
        os.kill(pid, 0)
    except Exception:
        return False
    # a zombie (reaped-by-nobody dead child) still answers kill(pid, 0) -
    # check /proc so we don't report a dead bot as running
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().split()[2] != "Z"
    except Exception:
        return True


def _reap(pid):
    try:
        os.waitpid(pid, os.WNOHANG)
    except Exception:
        pass


def has_systemd():
    return (os.path.exists("/etc/systemd/system/fomo-trader.service")
            and shutil.which("systemctl"))


def start_bot(force=False):
    if os.path.exists(HALTFILE) and not force:
        return {"ok": False,
                "msg": "kill switch is ON - confirm restart to override it"}
    if os.path.exists(HALTFILE):
        os.remove(HALTFILE)
    if bot_running():
        return {"ok": True, "msg": "already running"}
    if has_systemd():
        r = subprocess.run(["systemctl", "start", "fomo-trader"],
                           capture_output=True, text=True, timeout=30)
        ok = r.returncode == 0 and bot_running()
        return {"ok": ok, "msg": r.stderr.strip() or "started via systemd"}
    py = os.path.join(BASE, ".venv", "bin", "python")
    if not os.path.exists(py):
        py = sys.executable
    logf = open(BOTLOG, "a")
    p = subprocess.Popen(
        [py, os.path.join(BASE, "fomo_trader.py"), "--config", CONFIG_PATH],
        stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    open(PIDFILE, "w").write(str(p.pid))
    return {"ok": True, "msg": "started pid %d" % p.pid}


def stop_bot():
    if has_systemd():
        r = subprocess.run(["systemctl", "stop", "fomo-trader"],
                           capture_output=True, text=True, timeout=30)
        return {"ok": r.returncode == 0, "msg": r.stderr.strip() or "stopped"}
    if not os.path.exists(PIDFILE):
        return {"ok": True, "msg": "not running"}
    try:
        pid = int(open(PIDFILE).read().strip())
        os.kill(pid, signal.SIGTERM)
        for _ in range(15):
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(1)
        _reap(pid)  # collect the zombie so it doesn't haunt bot_running()
        if not bot_running():
            try:
                os.remove(PIDFILE)
            except OSError:
                pass
            return {"ok": True, "msg": "stopped pid %d" % pid}
        return {"ok": False, "msg": "pid %d would not die" % pid}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def kill_switch():
    r = stop_bot()
    open(HALTFILE, "w").write("killed %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    return {"ok": r["ok"], "msg": "KILL SWITCH engaged - bot stopped and blocked from restarting"}


def status():
    t = trader()
    positions = []
    for mint, pos in t.state["positions"].items():
        try:
            now = t.price_sol(mint)
        except Exception:
            now = None
        entry = pos.get("entry") or 0
        chg = round((now - entry) / entry * 100, 1) if now and entry else None
        positions.append({
            "name": pos.get("name"), "entry": entry,
            "peak": pos.get("peak"), "now": now, "chg_pct": chg,
            "rungs": len(pos.get("rungs_fired", [])),
            "buy_sol": pos.get("buy_sol"),
        })
    log_lines = []
    try:
        with open(BOTLOG) as f:
            lines = f.readlines()[-120:]
        for ln in lines:
            if "scan: 0 FOMO signal(s)" in ln:
                continue
            log_lines.append(ln.rstrip())
        log_lines = log_lines[-25:]
    except Exception:
        pass
    return {
        "running": bot_running(),
        "halt": os.path.exists(HALTFILE),
        "dry_run": CONFIG.get("dry_run", True),
        "positions": positions,
        "trades_today": len(t.state.get("trades_today", [])),
        "max_trades": t.cfg.get("risk", {}).get("max_trades_per_day"),
        "realized_sol": t.state.get("realized_sol", 0.0),
        "log": log_lines,
    }


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1">
<title>FOMO Trader</title>
<style>
*{box-sizing:border-box}body{background:#0d1117;color:#e6edf3;font-family:-apple-system,system-ui,sans-serif;margin:0;padding:14px;max-width:640px}
h1{font-size:22px;margin:4px 0 2px}#dot{display:inline-block;width:12px;height:12px;border-radius:50%;background:#555;margin-left:8px}
#dot.on{background:#3fb950;box-shadow:0 0 8px #3fb950}#dot.off{background:#f85149}
.sub{color:#8b949e;font-size:13px;margin-bottom:12px}
.btns{display:flex;gap:10px;margin:12px 0}
button{flex:1;padding:14px 0;font-size:16px;border-radius:12px;border:0;color:#fff;background:#1f6feb}
button.stop{background:#6e3b00}button.kill{background:#a40e26}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:12px;margin:10px 0}
.card .nm{font-weight:700;font-size:16px}.card .row{display:flex;justify-content:space-between;font-size:14px;color:#8b949e;margin-top:4px}
.up{color:#3fb950;font-weight:700}.dn{color:#f85149;font-weight:700}
pre{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:10px;font-size:11px;overflow-x:auto;white-space:pre-wrap;max-height:300px;overflow-y:auto}
h2{font-size:15px;color:#8b949e;margin:18px 0 4px}
.empty{color:#8b949e;font-size:14px;padding:8px 0}
</style></head><body>
<h1>FOMO Trader<span id=dot></span></h1>
<div class=sub id=meta>loading…</div>
<div class=btns>
<button onclick="act('start')">▶ Start</button>
<button class=stop onclick="act('stop')">⏸ Stop</button>
<button class=kill onclick="if(confirm('KILL SWITCH? Stops the bot and blocks it from restarting.'))act('halt')">🛑 Kill</button>
</div>
<h2>POSITIONS</h2><div id=pos></div>
<h2>ACTIVITY</h2><pre id=log>loading…</pre>
<script>
const token=new URLSearchParams(location.search).get('token')||'';
async function api(p,m){const r=await fetch('/api/'+p+'?token='+encodeURIComponent(token),{method:m||'GET'});return r.json();}
function fmt(n,d){return n==null?'—':Number(n).toFixed(d==null?9:d);}
async function refresh(){
 try{const s=await api('status');
  const d=document.getElementById('dot');d.className=s.running?'on':'off';
  document.getElementById('meta').textContent=
   (s.running?'● running':'○ stopped')+(s.halt?' · KILL SWITCH ON':'')+
   ' · '+(s.dry_run?'PAPER TRADING':'LIVE MONEY')+
   ' · trades today '+s.trades_today+'/'+s.max_trades;
  const pe=document.getElementById('pos');
  pe.innerHTML=s.positions.length?s.positions.map(p=>{
   const c=p.chg_pct==null?'':(p.chg_pct>=0?'<span class=up>+'+p.chg_pct+'%</span>':'<span class=dn>'+p.chg_pct+'%</span>');
   return '<div class=card><div class=nm>'+p.name+' '+c+'</div>'+
   '<div class=row><span>entry</span><span>'+fmt(p.entry)+'</span></div>'+
   '<div class=row><span>now</span><span>'+fmt(p.now)+'</span></div>'+
   '<div class=row><span>size</span><span>'+p.buy_sol+' SOL · TP rungs '+p.rungs+'</span></div></div>';
  }).join(''):'<div class=empty>No open positions.</div>';
  document.getElementById('log').textContent=s.log.join('\\n')||'no activity yet';
 }catch(e){document.getElementById('meta').textContent='connection error — is the dashboard running?';}
}
async function act(a){
 if(a==='stop'&&!confirm('Stop the bot? Open positions will no longer be managed.'))return;
 let r=await api(a,'POST');
 if(a==='start'&&!r.ok&&/kill switch/i.test(r.msg||'')){
  if(!confirm('Kill switch is ON. Restart the bot anyway?'))return;
  r=await (await fetch('/api/start?force=1&token='+encodeURIComponent(token),{method:'POST'})).json();
 }
 alert(r.msg||'done');refresh();
}
setInterval(refresh,10000);refresh();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def authorized(self):
        tok = CONFIG.get("dashboard_token")
        if not tok:
            return False
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return q.get("token", [""])[0] == tok

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if not CONFIG.get("dashboard_token"):
            self.send_json({"error": "dashboard_token not set in config.json"}, 500)
            return
        if not self.authorized():
            self.send_json({"error": "bad token"}, 401)
            return
        if path == "/" or path == "/index.html":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            try:
                self.send_json(status())
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not CONFIG.get("dashboard_token") or not self.authorized():
            self.send_json({"error": "unauthorized"}, 401)
            return
        try:
            if path == "/api/start":
                q = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query)
                self.send_json(start_bot(force=q.get("force", [""])[0] == "1"))
            elif path == "/api/stop":
                self.send_json(stop_bot())
            elif path == "/api/halt":
                self.send_json(kill_switch())
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as e:
            self.send_json({"ok": False, "msg": str(e)}, 500)


def main():
    global CONFIG, CONFIG_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    CONFIG_PATH = os.path.abspath(os.path.expanduser(args.config))
    with open(CONFIG_PATH) as f:
        CONFIG = json.load(f)
    if not CONFIG.get("dashboard_token"):
        print("ERROR: set dashboard_token in config.json (deploy.sh generates one)")
        sys.exit(1)
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print("dashboard on port %d (token required)" % args.port, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
