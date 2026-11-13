"""SpeakFit Mobile pipeline: audio file -> faster-whisper words -> metrics -> SQLite.

Usage:  .venv/bin/python pipeline.py FILE [FILE ...] [--source desk|drive] [--mode MODE]
Phone files are named sfm_<yyyyMMdd-HHmmss>_<mode>_p<NN>.m4a and are tagged source=drive automatically.
"""
import argparse, datetime as dt, os, re, shutil, sqlite3, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB = DATA / "speakfit.db"
AUDIO = DATA / "audio"
MODEL = os.environ.get("SFM_MODEL", "small.en")  # ponytail: one model; base.en if speed matters more than accuracy

PAUSE_MIN = 0.3      # gap (s) between words that counts as a pause
BREAK_MIN = 3.0      # gap (s) at or above this is "not talking" (listening, ordering food): excluded from pauses and speaking time
LOWCONF = 0.5        # word probability below this = low confidence (articulation proxy)
SINGLE_FILLERS = {"um", "uh", "er", "erm", "ah", "hmm", "mm", "uhm"}
PAIR_FILLERS = {("you", "know"), ("i", "mean")}
# Whisper tends to drop fillers; a prompt that contains them makes it keep them.
PROMPT = "Um, so, uh, I was like, you know, thinking about it. Hmm, I mean, er, it was fine."
# initial_prompt is dropped after the first 30 s window when condition_on_previous_text=False
# (faster_whisper/transcribe.py ~L1383); hotwords are re-sent every window. Measured: fillers 1->5 and 3->13 on two desk takes.
HOTWORDS = "Um, uh, er, like, you know"
NAME_RE = re.compile(r"^sfm_(\d{8}-\d{6})_([a-z0-9-]+)_p(\d+)\.(m4a|wav)$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings(
  id INTEGER PRIMARY KEY, file TEXT UNIQUE, session TEXT, source TEXT, mode TEXT, recorded_at TEXT,
  duration_s REAL, speaking_s REAL, n_words INT, n_fillers INT, n_pauses INT, n_pauses_mid INT, pause_total_s REAL,
  n_lowconf INT, n_sentence_end INT, n_sentence_end_lowconf INT, pitch_sd_st REAL, loud_sd_db REAL,
  n_unique INT, n_new_words INT, transcript TEXT, model TEXT, processed_at TEXT);
CREATE TABLE IF NOT EXISTS words(recording_id INT, i INT, word TEXT, start REAL, end REAL, prob REAL);
CREATE INDEX IF NOT EXISTS words_rec ON words(recording_id);
"""


def norm(w):
    return re.sub(r"[^a-z']", "", w.lower())


def text_metrics(words):
    """words: list of (word, start, end, prob). Returns counts; rates are derived in the dashboard so they aggregate exactly."""
    toks = [norm(w[0]) for w in words]
    fillers = sum(t in SINGLE_FILLERS for t in toks)
    fillers += sum((a, b) in PAIR_FILLERS for a, b in zip(toks, toks[1:]))
    # ponytail: "like" counts only when set off by a comma ("like," or ", like"); a real classifier if this misfires
    fillers += sum(t == "like" and (words[i][0].rstrip().endswith(",") or (i and words[i - 1][0].rstrip().endswith(",")))
                   for i, t in enumerate(toks))
    speaking = sum(e - s for _, s, e, _ in words)
    n_pauses = n_mid = 0
    pause_total = 0.0
    for (w, _, e, _), (_, s2, _, _) in zip(words, words[1:]):
        gap = s2 - e
        if gap < BREAK_MIN:
            speaking += max(gap, 0)
        if PAUSE_MIN <= gap < BREAK_MIN:
            n_pauses += 1
            pause_total += gap
            n_mid += not re.search(r"[.,?!;:]$", w.strip())
    ends = [w for w in words if re.search(r"[.?!]$", w[0].strip())]
    content = {t for t in toks if t and t not in SINGLE_FILLERS}
    return dict(speaking_s=speaking, n_words=len(words), n_fillers=fillers, n_pauses=n_pauses, n_pauses_mid=n_mid,
                pause_total_s=pause_total, n_lowconf=sum(p < LOWCONF for *_, p in words),
                n_sentence_end=len(ends), n_sentence_end_lowconf=sum(p < LOWCONF for *_, p in ends),
                n_unique=len(content)), content


def prosody(wav, words):
    """Pitch spread (semitones) and loudness spread (dB), measured only inside word spans so car noise between words is ignored."""
    import numpy as np, parselmouth
    snd = parselmouth.Sound(str(wav))
    pitch = snd.to_pitch(time_step=0.01, pitch_floor=75, pitch_ceiling=400)
    inten = snd.to_intensity(time_step=0.01)
    spans = np.array([(s, e) for _, s, e, _ in words]) if words else np.zeros((0, 2))

    def inside(t):
        if not len(spans):
            return np.zeros(len(t), bool)
        idx = np.searchsorted(spans[:, 0], t, side="right") - 1
        return (idx >= 0) & (t <= spans[np.clip(idx, 0, None), 1])

    f0 = pitch.selected_array["frequency"]
    keep = (f0 > 0) & inside(pitch.xs())
    pitch_sd = float(np.std(12 * np.log2(f0[keep] / np.median(f0[keep])))) if keep.sum() > 20 else None
    db = inten.values[0]
    keep = inside(inten.xs())
    loud_sd = float(np.std(db[keep])) if keep.sum() > 20 else None
    return pitch_sd, loud_sd, snd.duration


_model = None


def transcribe(wav):
    global _model
    from faster_whisper import WhisperModel
    if _model is None:
        _model = WhisperModel(MODEL, device=os.environ.get("SFM_DEVICE", "cpu"), compute_type="int8")
    import wave, numpy as np
    with wave.open(str(wav)) as f:  # ffmpeg already made 16 kHz mono; skips faster-whisper's PyAV decoder (version clash)
        audio = np.frombuffer(f.readframes(f.getnframes()), np.int16).astype(np.float32) / 32768
    segs, _ = _model.transcribe(audio, language="en", word_timestamps=True, vad_filter=True,
                                initial_prompt=PROMPT, hotwords=HOTWORDS, condition_on_previous_text=False)
    return [(w.word.strip(), float(w.start), float(w.end), float(w.probability)) for seg in segs for w in seg.words]


def connect():
    DATA.mkdir(exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    return con


def tags_for(path, source=None, mode=None):
    m = NAME_RE.match(path.name)
    if m:
        start = dt.datetime.strptime(m[1], "%Y%m%d-%H%M%S")
        return m[1], source or "drive", mode or m[2], start.isoformat()
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime)
    return path.stem, source or "desk", mode or "desk", mtime.isoformat(timespec="seconds")


def process(path, source=None, mode=None, con=None):
    path = Path(path)
    con = con or connect()
    if con.execute("SELECT 1 FROM recordings WHERE file=?", (path.name,)).fetchone():
        print(f"skip (already processed): {path.name}")
        return False
    session, source, mode, recorded_at = tags_for(path, source, mode)
    AUDIO.mkdir(parents=True, exist_ok=True)
    stored = AUDIO / path.name
    if path.resolve() != stored.resolve():
        shutil.copy2(path, stored)
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(stored), "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
        words = transcribe(wav)
        pitch_sd, loud_sd, duration = prosody(wav, words)
    m, vocab = text_metrics(words)
    seen = {r[0] for r in con.execute(
        "SELECT DISTINCT w.word FROM words w JOIN recordings r ON r.id=w.recording_id WHERE r.recorded_at < ?", (recorded_at,))}
    seen = {norm(w) for w in seen}
    with con:
        cur = con.execute(
            """INSERT INTO recordings(file, session, source, mode, recorded_at, duration_s, pitch_sd_st, loud_sd_db,
               n_new_words, transcript, model, processed_at, speaking_s, n_words, n_fillers, n_pauses, n_pauses_mid,
               pause_total_s, n_lowconf, n_sentence_end, n_sentence_end_lowconf, n_unique)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (path.name, session, source, mode, recorded_at, duration, pitch_sd, loud_sd, len(vocab - seen),
             " ".join(w[0] for w in words), MODEL, dt.datetime.now().isoformat(timespec="seconds"),
             m["speaking_s"], m["n_words"], m["n_fillers"], m["n_pauses"], m["n_pauses_mid"], m["pause_total_s"],
             m["n_lowconf"], m["n_sentence_end"], m["n_sentence_end_lowconf"], m["n_unique"]))
        con.executemany("INSERT INTO words VALUES (?,?,?,?,?,?)",
                        [(cur.lastrowid, i, *w) for i, w in enumerate(words)])
    mins = m["speaking_s"] / 60 or 1
    print(f"done: {path.name} [{source}/{mode}] {m['n_words']} words, {m['n_words']/mins:.0f} wpm, "
          f"{m['n_fillers']/mins:.1f} fillers/min, {m['n_pauses']} pauses ({m['n_pauses_mid']} mid-phrase), "
          f"pitch sd {pitch_sd and round(pitch_sd, 2)} st")
    return True


def demo():
    w = [("So,", 0.0, 0.3, .9), ("um", 0.8, 1.0, .9), ("I", 1.0, 1.1, .3), ("like,", 1.1, 1.4, .9),
         ("went", 2.0, 2.3, .9), ("home.", 2.7, 3.0, .4), ("You", 10.0, 10.2, .9), ("know", 10.2, 10.4, .9)]
    m, vocab = text_metrics(w)
    assert m["n_fillers"] == 3, m                                  # um, "like,", you know
    assert m["n_pauses"] == 3 and m["n_pauses_mid"] == 1, m        # after "So," and "like," = boundary; went|home = mid; 7 s gap = break
    assert m["n_lowconf"] == 2 and m["n_sentence_end"] == 1 and m["n_sentence_end_lowconf"] == 1, m
    assert abs(m["speaking_s"] - 3.4) < 1e-9, m                    # 1.9 s of words + 1.5 s of short gaps
    assert "um" not in vocab and "like" in vocab, vocab
    print("demo ok", m)


if __name__ == "__main__":
    if sys.argv[1:] == ["--demo"]:
        demo(); sys.exit()
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--source", choices=["desk", "drive"])
    ap.add_argument("--mode")
    a = ap.parse_args()
    con = connect()
    for f in a.files:
        process(f, a.source, a.mode, con)
