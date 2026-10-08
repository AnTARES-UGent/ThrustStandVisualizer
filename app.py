"""Thrust Stand Game - local web app.

Run:   python app.py
Open:  http://localhost:5000

Routes:  /  (home)   /recorder   /leaderboard

Reuses resolve_port() / parse_packet() / BAUD from read_data.py.
The serial port is set on the home page (saved in config.json).
"""
import csv
import os
import re
import threading
import time
from collections import deque
from datetime import datetime

import matplotlib

matplotlib.use("Agg")  # no GUI window, just write PNG files
import matplotlib.pyplot as plt
from flask import Flask, abort, jsonify, render_template_string, request, send_from_directory

import read_data  # your existing script (its __main__ block does not run on import)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = BASE_DIR                          # CSVs live next to the script, like before
PLOT_DIR = os.path.join(BASE_DIR, "plots")   # generated PNGs
os.makedirs(PLOT_DIR, exist_ok=True)

# Set THRUST_FAKE=1 to test the website without an Arduino.
FAKE = os.environ.get("THRUST_FAKE") == "1"

app = Flask(__name__)


# --------------------------------------------------------------------------
# Recording (serial reading happens in a background thread)
# --------------------------------------------------------------------------
class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.recording = False
        self.name = ""
        self.csv_path = None
        self.rows = 0
        self.error = ""
        self.last_finished = ""   # name of the last completed recording
        self.lines = deque(maxlen=3000)   # (id, text) for the terminal box
        self.next_id = 1
        self._thread = None
        self._stop = threading.Event()

    def log(self, text):
        with self.lock:
            self.lines.append((self.next_id, text))
            self.next_id += 1

    def start(self, name):
        with self.lock:
            if self.recording:
                return False, "Already recording."
        clean = clean_name(name)
        if not clean:
            return False, "Please enter a filename."
        path = unique_path(clean)

        ser = None
        if not FAKE:
            try:
                import serial
                port = read_data.resolve_port(read_data.get_port())
                if port is None:
                    return False, "No serial port found. Is the Arduino plugged in?"
                ser = serial.Serial(port, read_data.BAUD, timeout=1)
                self.log(f"Using serial port: {port}")
            except Exception as e:  # noqa: BLE001
                return False, f"Could not open serial port: {e}"

        with self.lock:
            self.name = os.path.splitext(os.path.basename(path))[0]
            self.csv_path = path
            self.rows = 0
            self.error = ""
            self.recording = True
            self._stop.clear()
        self.log(f"--- Recording to {os.path.basename(path)} ---")
        self._thread = threading.Thread(target=self._run, args=(ser, path), daemon=True)
        self._thread.start()
        return True, self.name

    def stop(self):
        with self.lock:
            if not self.recording:
                return False, "Not recording."
        self._stop.set()
        self._thread.join(timeout=5)
        return True, self.last_finished

    def _lines_from_source(self, ser):
        """Yield raw text lines from the Arduino (or a fake generator)."""
        if FAKE:
            t0 = time.time()
            while not self._stop.is_set():
                t = time.time() - t0
                f = 10 + (40 * max(0, 1 - abs(t - 3) / 2) if t > 1 else 0)
                yield f"{t:.3f};{f:.4f}"
                time.sleep(0.1)
            return
        ser.reset_input_buffer()
        while not self._stop.is_set():
            line = ser.readline().decode("utf-8", errors="ignore").strip()
            if line:
                yield line

    def _run(self, ser, path):
        f = None
        try:
            f = open(path, "w", newline="")
            w = csv.writer(f)
            w.writerow(["time_since_start_s", "force_N"])
            for line in self._lines_from_source(ser):
                packet = read_data.parse_packet(line)
                if packet is None:
                    self.log(f"Could not parse: {line}")
                    continue
                t, force = packet
                w.writerow([f"{t:.3f}", f"{force:.4f}"])
                f.flush()
                with self.lock:
                    self.rows += 1
                self.log(f"t = {t:.3f} s    F = {force:.4f} N")
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            self.log(f"ERROR: {e}")
        finally:
            if f:
                f.close()
            if ser:
                try:
                    ser.close()
                except Exception:  # noqa: BLE001
                    pass
            self._finish(path)

    def _finish(self, path):
        name = os.path.splitext(os.path.basename(path))[0]
        if self.rows == 0:
            try:
                os.remove(path)
            except OSError:
                pass
            self.log("--- Stopped. No data received, nothing saved. ---")
            name = ""
        else:
            try:
                make_plot(path)
                self.log(f"--- Stopped. Saved {self.rows} samples and graph for '{name}'. ---")
            except Exception as e:  # noqa: BLE001
                self.log(f"--- Stopped. CSV saved, but graph failed: {e} ---")
        with self.lock:
            self.last_finished = name
            self.recording = False
            self.csv_path = None


rec = Recorder()


def clean_name(name):
    name = (name or "").strip()
    if name.lower().endswith(".csv"):
        name = name[:-4]
    name = re.sub(r"[^A-Za-z0-9 _.\-]", "_", name).strip(" .")
    return name[:60]


def unique_path(name):
    path = os.path.join(DATA_DIR, name + ".csv")
    i = 2
    while os.path.exists(path):
        path = os.path.join(DATA_DIR, f"{name}_{i}.csv")
        i += 1
    return path


# --------------------------------------------------------------------------
# CSV analysis + plots
# --------------------------------------------------------------------------
def read_csv(path):
    ts, fs = [], []
    with open(path, newline="") as f:
        for row in csv.reader(f):
            try:
                ts.append(float(row[0]))
                fs.append(float(row[1]))
            except (ValueError, IndexError):
                continue  # header or bad row
    return ts, fs


def analyse(path):
    ts, fs = read_csv(path)
    if len(ts) < 2:
        return None
    peak = max(fs)
    impulse = sum((ts[i] - ts[i - 1]) * (fs[i] + fs[i - 1]) / 2 for i in range(1, len(ts)))
    return peak, impulse


def make_plot(csv_path):
    ts, fs = read_csv(csv_path)
    name = os.path.splitext(os.path.basename(csv_path))[0]
    result = analyse(csv_path)
    bg, fg, yellow = "#0b1d3a", "#ffffff", "#ffd400"
    fig, ax = plt.subplots(figsize=(11, 6), dpi=110)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)
    ax.plot(ts, fs, color=yellow, linewidth=2)
    ax.margins(y=0.15)
    ax.grid(True, color="#335", alpha=0.8)
    for s in ax.spines.values():
        s.set_color(fg)
    ax.tick_params(colors=fg, labelsize=13)
    ax.set_xlabel("Time (s)", color=fg, fontsize=14)
    ax.set_ylabel("Thrust (N)", color=fg, fontsize=14)
    title = name
    if result:
        peak, impulse = result
        i = fs.index(peak)
        ax.plot([ts[i]], [peak], "o", color="#ff5a5a", markersize=8)
        ax.annotate(f"{peak:.2f} N", (ts[i], peak), textcoords="offset points",
                    xytext=(10, 8), color=fg, fontsize=13)
        title += f"\nPeak {peak:.2f} N   |   Impulse {impulse:.2f} N·s"
    ax.set_title(title, color=yellow, fontsize=16)
    fig.tight_layout()
    fig.savefig(os.path.join(PLOT_DIR, name + ".png"), facecolor=bg)
    plt.close(fig)


_stats_cache = {}  # path -> (mtime, size, stats)


def leaderboard():
    active = rec.csv_path if rec.recording else None
    out = []
    for fn in os.listdir(DATA_DIR):
        if not fn.lower().endswith(".csv"):
            continue
        path = os.path.join(DATA_DIR, fn)
        if path == active:
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        key = (st.st_mtime, st.st_size)
        cached = _stats_cache.get(path)
        if cached and cached[0] == key:
            stats = cached[1]
        else:
            stats = analyse(path)
            _stats_cache[path] = (key, stats)
        if stats is None:
            continue
        out.append({
            "name": fn[:-4],
            "date": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "peak": round(stats[0], 3),
            "impulse": round(stats[1], 3),
        })
    out.sort(key=lambda r: r["peak"], reverse=True)
    return out


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
@app.post("/api/start")
def api_start():
    ok, msg = rec.start((request.get_json(silent=True) or {}).get("name", ""))
    return jsonify(ok=ok, message=msg), (200 if ok else 400)


@app.post("/api/stop")
def api_stop():
    ok, msg = rec.stop()
    return jsonify(ok=ok, message=msg), (200 if ok else 400)


@app.get("/api/config")
def api_config_get():
    ports = []
    try:
        import serial.tools.list_ports
        ports = [p.device for p in serial.tools.list_ports.comports()]
    except Exception:  # noqa: BLE001
        pass
    return jsonify(port=read_data.load_config().get("port", ""), default=read_data.PORT, ports=ports)


@app.post("/api/config")
def api_config_set():
    if rec.recording:
        return jsonify(ok=False, message="Stop the recording first."), 400
    port = ((request.get_json(silent=True) or {}).get("port") or "").strip()
    if len(port) > 200:
        return jsonify(ok=False, message="Port name too long."), 400
    read_data.save_port(port)
    return jsonify(ok=True, message=f"Saved: {port}" if port else "Cleared. Falling back to PORT in read_data.py.")


@app.get("/api/lines")
def api_lines():
    after = int(request.args.get("after", 0))
    with rec.lock:
        new = [{"id": i, "text": t} for i, t in rec.lines if i > after]
        return jsonify(lines=new, recording=rec.recording, rows=rec.rows,
                       name=rec.name, finished=rec.last_finished, error=rec.error)


@app.get("/api/leaderboard")
def api_leaderboard():
    return jsonify(leaderboard())


@app.get("/plot/<name>.png")
def plot(name):
    csv_path = os.path.join(DATA_DIR, os.path.basename(name) + ".csv")
    png = os.path.join(PLOT_DIR, os.path.basename(name) + ".png")
    if not os.path.exists(csv_path):
        abort(404)
    # (re)generate if missing or older than the csv
    if not os.path.exists(png) or os.path.getmtime(png) < os.path.getmtime(csv_path):
        make_plot(csv_path)
    return send_from_directory(PLOT_DIR, os.path.basename(png), max_age=0)


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
LAYOUT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Thrust Stand Game</title>
<style>
  :root { --bg:#0b1d3a; --panel:#13294f; --line:#2a4577; --yellow:#ffd400; --text:#ffffff; }
  html { font-size: clamp(16px, 1.25vw + 8px, 34px); }  /* everything uses rem, so it scales */
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font-family: Arial, Helvetica, sans-serif; line-height:1.4; }
  nav { display:flex; gap:1.5rem; padding:0.8rem 2rem; background:#081428; border-bottom:2px solid var(--line); }
  nav a { color:var(--text); text-decoration:none; padding:0.2rem 0.1rem; border-bottom:3px solid transparent; }
  nav a:hover { color:var(--yellow); }
  nav a.active { color:var(--yellow); border-bottom-color:var(--yellow); }
  main { padding:2rem; max-width:80rem; margin:0 auto; }
  h1 { color:var(--yellow); font-size:2.4rem; margin:0 0 1rem; }
  button { font:inherit; padding:0.5rem 1.2rem; border:0; border-radius:0.3rem; cursor:pointer; }
  button:disabled { opacity:0.4; cursor:not-allowed; }
  input { font:inherit; padding:0.5rem 0.7rem; border-radius:0.3rem; border:2px solid var(--line);
          background:#081428; color:var(--text); }
  {% block css %}{% endblock %}
</style></head><body>
<nav>
  <a href="/" class="{{ 'active' if page=='home' }}">Home</a>
  <a href="/recorder" class="{{ 'active' if page=='recorder' }}">Recorder</a>
  <a href="/leaderboard" class="{{ 'active' if page=='leaderboard' }}">Leaderboard</a>
</nav>
<main>{% block body %}{% endblock %}</main>
</body></html>"""

HOME = LAYOUT.replace("{% block css %}{% endblock %}", """
  .center { text-align:center; padding-top:8vh; }
  .center h1 { font-size:3.2rem; }
  .warn { font-size:1.5rem; max-width:40rem; margin:2rem auto; }
  .port { margin:2rem auto; max-width:40rem; }
  .port label { display:block; margin-bottom:0.5rem; font-size:1.1rem; }
  .port .row { display:flex; gap:0.8rem; justify-content:center; flex-wrap:wrap; }
  .port input { flex:1 1 14rem; max-width:22rem; }
  #save { background:var(--yellow); color:#241d00; }
  #msg { margin-top:0.8rem; min-height:1.5rem; }
""").replace("{% block body %}{% endblock %}", """
<div class="center">
  <h1>Welcome to the Thrust Stand Game</h1>
  <p class="warn">⚠ Warning: make sure the Arduino setup is still wired correctly and
  connected to your laptop before you start recording.</p>
  <div class="port">
    <label for="port">Arduino USB port</label>
    <div class="row">
      <input id="port" list="ports" placeholder="e.g. /dev/ttyACM0 or COM3 (empty = default from read_data.py)" autocomplete="off">
      <datalist id="ports"></datalist>
      <button id="save">Save</button>
    </div>
    <div id="msg"></div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
fetch('/api/config').then(r => r.json()).then(c => {
  $('port').value = c.port;
  $('ports').innerHTML = c.ports.map(p => '<option value="' + p + '">').join('');
  if (!c.port) $('msg').textContent = 'No port saved. Using ' + (c.default || 'auto-detect') + '.';
});
$('save').onclick = async () => {
  const r = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
                                        body: JSON.stringify({port: $('port').value})});
  $('msg').textContent = (await r.json()).message;
};
$('port').addEventListener('keydown', e => { if (e.key === 'Enter') $('save').click(); });
</script>""")

RECORDER = LAYOUT.replace("{% block css %}{% endblock %}", """
  .row { display:flex; flex-wrap:wrap; gap:0.8rem; align-items:center; margin-bottom:1rem; }
  .row input { flex:1 1 15rem; }
  #start { background:#2ecc71; color:#06240f; }
  #stop  { background:#ff5a5a; color:#2b0505; }
  #status { margin-bottom:1rem; min-height:1.5rem; }
  #term { background:#000; color:#7CFC8A; font-family: monospace; font-size:1rem;
          height:55vh; overflow-y:auto; padding:0.8rem; border:2px solid var(--line);
          border-radius:0.3rem; white-space:pre-wrap; }
  a { color:var(--yellow); }
""").replace("{% block body %}{% endblock %}", """
<h1>Recorder</h1>
<div class="row">
  <input id="name" placeholder="Thrust stand filename (e.g. run1)" maxlength="60" autofocus>
  <button id="start">Start</button>
  <button id="stop" disabled>Stop</button>
</div>
<div id="status">Ready.</div>
<div id="term">Waiting for data...</div>
<script>
const $ = id => document.getElementById(id);
let lastId = 0, wasRecording = false, cleared = false;
function status(msg){ $('status').innerHTML = msg; }
function setUI(rec){
  $('start').disabled = rec; $('name').disabled = rec; $('stop').disabled = !rec;
}
$('start').onclick = async () => {
  const r = await fetch('/api/start', {method:'POST', headers:{'Content-Type':'application/json'},
                                       body: JSON.stringify({name: $('name').value})});
  const j = await r.json();
  if (!j.ok) status('⚠ ' + j.message);
  else { cleared = false; status('Starting...'); }
};
$('stop').onclick = async () => {
  $('stop').disabled = true; status('Stopping and building graph...');
  await fetch('/api/stop', {method:'POST'});
};
async function poll(){
  try {
    const j = await (await fetch('/api/lines?after=' + lastId)).json();
    const term = $('term');
    if (j.lines.length){
      if (!cleared){ term.textContent = ''; cleared = true; }
      const atBottom = term.scrollHeight - term.scrollTop - term.clientHeight < 40;
      term.textContent += j.lines.map(l => l.text).join('\\n') + '\\n';
      lastId = j.lines[j.lines.length-1].id;
      if (atBottom) term.scrollTop = term.scrollHeight;
    }
    setUI(j.recording);
    if (j.recording) status('● Recording <b>' + j.name + '</b> — ' + j.rows + ' samples');
    else if (wasRecording){
      status(j.finished
        ? 'Saved <b>' + j.finished + '</b>. <a href="/leaderboard">See it on the leaderboard</a>'
        : 'Stopped. No data was received, so nothing was saved.');
    }
    wasRecording = j.recording;
  } catch(e){ status('⚠ Lost connection to the server.'); }
}
setInterval(poll, 200); poll();
</script>""")

LEADERBOARD = LAYOUT.replace("{% block css %}{% endblock %}", """
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:0.6rem 0.8rem; border-bottom:1px solid var(--line); }
  th { color:var(--yellow); cursor:pointer; user-select:none; background:var(--panel); }
  td.num, th.num { text-align:right; }
  td.name { color:var(--yellow); cursor:pointer; text-decoration:underline; }
  tr:hover td { background:var(--panel); }
  #empty { margin-top:1rem; }
  #modal { position:fixed; inset:0; background:rgba(0,0,0,0.8); display:none;
           align-items:center; justify-content:center; padding:2rem; }
  #modal.open { display:flex; }
  #modal img { max-width:100%; max-height:100%; border:3px solid var(--line); background:var(--bg); }
  #hint { opacity:0.7; margin-top:1rem; }
""").replace("{% block body %}{% endblock %}", """
<h1>Leaderboard</h1>
<table>
  <thead><tr>
    <th data-k="rank" class="num">#</th>
    <th data-k="name">Name</th>
    <th data-k="date">Date</th>
    <th data-k="peak" class="num">Peak thrust (N)</th>
    <th data-k="impulse" class="num">Impulse (N·s)</th>
  </tr></thead>
  <tbody id="rows"></tbody>
</table>
<div id="empty"></div>
<div id="hint">Click a name to see its thrust graph. Click a column header to sort.</div>
<div id="modal"><img id="img" alt="Thrust graph"></div>
<script>
let data = [], lastJson = '', sortKey = 'peak', sortDir = -1;
const esc = s => s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function render(){
  const sorted = [...data].sort((a,b) => {
    const k = sortKey === 'rank' ? 'peak' : sortKey, d = sortKey === 'rank' ? -1 : sortDir;
    return (a[k] < b[k] ? -1 : a[k] > b[k] ? 1 : 0) * d;
  });
  const rankOf = [...data].sort((a,b) => b.peak - a.peak).map(r => r.name);
  document.getElementById('rows').innerHTML = sorted.map(r =>
    '<tr><td class="num">' + (rankOf.indexOf(r.name)+1) + '</td>' +
    '<td class="name" data-n="' + esc(r.name) + '">' + esc(r.name) + '</td>' +
    '<td>' + r.date + '</td>' +
    '<td class="num">' + r.peak.toFixed(2) + '</td>' +
    '<td class="num">' + r.impulse.toFixed(2) + '</td></tr>').join('');
  document.getElementById('empty').textContent = data.length ? '' : 'No recordings yet.';
}
async function poll(){
  try {
    const txt = await (await fetch('/api/leaderboard')).text();
    if (txt !== lastJson){ lastJson = txt; data = JSON.parse(txt); render(); }
  } catch(e){}
}
document.querySelectorAll('th').forEach(th => th.onclick = () => {
  const k = th.dataset.k;
  if (k === sortKey) sortDir = -sortDir; else { sortKey = k; sortDir = (k === 'name' || k === 'date') ? 1 : -1; }
  render();
});
document.getElementById('rows').onclick = e => {
  const n = e.target.dataset.n; if (!n) return;
  document.getElementById('img').src = '/plot/' + encodeURIComponent(n) + '.png?t=' + Date.now();
  document.getElementById('modal').classList.add('open');
};
document.getElementById('modal').onclick = () => document.getElementById('modal').classList.remove('open');
document.addEventListener('keydown', e => { if (e.key === 'Escape') document.getElementById('modal').classList.remove('open'); });
setInterval(poll, 500); poll();
</script>""")


@app.get("/")
def home():
    return render_template_string(HOME, page="home")


@app.get("/recorder")
def recorder_page():
    return render_template_string(RECORDER, page="recorder")


@app.get("/leaderboard")
def leaderboard_page():
    return render_template_string(LEADERBOARD, page="leaderboard")


if __name__ == "__main__":
    print("Thrust Stand Game running at http://localhost:5000")
    app.run(host="127.0.0.1", port=5000, threaded=True)
