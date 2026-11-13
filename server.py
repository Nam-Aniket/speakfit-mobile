"""Mac side of sync: receives recordings from the phone and runs the pipeline on each one.

  PUT /upload/<sfm_...m4a>  header X-SpeakFit-Token  -> 200 stored (or already had it), 401 bad token, 400 bad name
  GET /ping                                           -> "speakfit"
Advertised on the LAN as _speakfit._tcp via macOS dns-sd, so the phone finds the Mac even if its IP changes.
Run: .venv/bin/python server.py   (install_agent.sh keeps it running in the background)
"""
import hmac, queue, subprocess, threading, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pipeline

PORT = 8770
TOKEN = (pipeline.ROOT / "secrets" / "token").read_text().strip()
MAX_BYTES = 600 * 1024 * 1024   # a 30 min part is ~11 MB; this only stops runaway uploads
jobs = queue.Queue()


def worker():
    con = pipeline.connect()   # one connection, one thread: SQLite writes stay serial
    while True:
        path = jobs.get()
        try:
            pipeline.process(path, con=con)
        except Exception:
            traceback.print_exc()   # file stays in data/audio; restarting the server retries it


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self.reply(200, "speakfit") if self.path == "/ping" else self.reply(404, "no")

    def do_PUT(self):
        name = self.path.removeprefix("/upload/")
        if not hmac.compare_digest(self.headers.get("X-SpeakFit-Token", ""), TOKEN):
            return self.reply(401, "bad token")
        if not self.path.startswith("/upload/") or not pipeline.NAME_RE.match(name):   # also blocks ../ paths
            return self.reply(400, "bad name")
        size = int(self.headers.get("Content-Length", 0))
        if not 0 < size <= MAX_BYTES:
            return self.reply(400, "bad size")
        dest = pipeline.AUDIO / name
        if dest.exists() and dest.stat().st_size == size:
            return self.reply(200, "already have it")
        tmp = dest.with_suffix(".part")
        with open(tmp, "wb") as f:
            left = size
            while left:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    tmp.unlink(missing_ok=True)
                    return self.reply(400, "upload cut short")
                f.write(chunk)
                left -= len(chunk)
        tmp.rename(dest)   # only complete files get the real name
        jobs.put(dest)
        print(f"received {name} ({size} bytes)", flush=True)
        self.reply(200, "stored")


if __name__ == "__main__":
    pipeline.AUDIO.mkdir(parents=True, exist_ok=True)
    con = pipeline.connect()
    done = {r[0] for r in con.execute("SELECT file FROM recordings")}
    for p in sorted(pipeline.AUDIO.glob("sfm_*.m4a")):   # anything received but not processed before a restart
        if p.name not in done:
            jobs.put(p)
    threading.Thread(target=worker, daemon=True).start()
    adv = subprocess.Popen(["dns-sd", "-R", "SpeakFit Mac", "_speakfit._tcp", "local", str(PORT)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        print(f"SpeakFit sync listening on :{PORT}", flush=True)
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    finally:
        adv.terminate()
