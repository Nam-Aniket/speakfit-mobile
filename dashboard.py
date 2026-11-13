"""Streamlit dashboard. Run: .venv/bin/streamlit run dashboard.py"""
import datetime as dt, re, sqlite3
from pathlib import Path
import pandas as pd
import streamlit as st

DB = Path(__file__).resolve().parent / "data" / "speakfit.db"
WORKBOOK = Path.home() / "Documents" / "speaking-workbook.md"  # SpeakFit's weekly plan, read only

# Each focus: (label, column, lower_is_better). Doc's curriculum: voice, articulation, pausing, tonality.
FOCUS = {
    "Voice": [("Loudness spread (dB, steadier = lower)", "loud_sd_db", True), ("Speaking minutes", "speaking_min", False)],
    "Articulation": [("Low-confidence words %", "lowconf_pct", True), ("Unclear sentence endings %", "end_lowconf_pct", True)],
    "Pausing": [("Mid-phrase pauses %", "mid_pause_pct", True), ("Fillers per minute", "fillers_pm", True)],
    "Tonality": [("Pitch variation (semitones)", "pitch_sd_st", False)],
}
# Workbook week title -> focus. Only "voice" maps one to one; other weeks you pick in the sidebar.
WEEK_TO_FOCUS = {"voice": "Voice"}

st.set_page_config(page_title="SpeakFit Mobile", layout="wide")


def workbook_week(today):
    """Return the workbook week title covering today, e.g. 'structure', or None."""
    if not WORKBOOK.exists():
        return None
    for m in re.finditer(r"^## Week (\d+) \(\w+ (\d+ \w+) to \w+ (\d+ \w+)\): (.+)$", WORKBOOK.read_text(), re.M):
        a, b = (dt.datetime.strptime(f"{x} {today.year}", "%d %b %Y").date() for x in m.group(2, 3))
        if a <= today <= b:
            return f"Week {m[1]}: {m[4].strip()}", m[4].strip().lower()
    return None


def rates(g):
    """Aggregate raw counts into rates so several recordings combine exactly (not an average of averages)."""
    mins = g.speaking_s.sum() / 60
    w = g.speaking_s.sum() or 1
    return pd.Series({
        "speaking_min": mins,
        "wpm": g.n_words.sum() / mins if mins else None,
        "fillers_pm": g.n_fillers.sum() / mins if mins else None,
        "pauses_pm": g.n_pauses.sum() / mins if mins else None,
        "mid_pause_pct": 100 * g.n_pauses_mid.sum() / g.n_pauses.sum() if g.n_pauses.sum() else None,
        "avg_pause_s": g.pause_total_s.sum() / g.n_pauses.sum() if g.n_pauses.sum() else None,
        "lowconf_pct": 100 * g.n_lowconf.sum() / g.n_words.sum() if g.n_words.sum() else None,
        "end_lowconf_pct": 100 * g.n_sentence_end_lowconf.sum() / g.n_sentence_end.sum() if g.n_sentence_end.sum() else None,
        "pitch_sd_st": (g.pitch_sd_st * g.speaking_s).sum() / w,   # ponytail: speaking-time-weighted mean of per-file spreads
        "loud_sd_db": (g.loud_sd_db * g.speaking_s).sum() / w,
        "unique_words": g.n_unique.sum(),
        "new_words": g.n_new_words.sum(),
    })


if not DB.exists():
    st.info("No recordings processed yet. Run pipeline.py on an audio file or start server.py.")
    st.stop()
df = pd.read_sql("SELECT * FROM recordings", sqlite3.connect(DB), parse_dates=["recorded_at"])
if df.empty:
    st.info("No recordings processed yet.")
    st.stop()
df["day"] = df.recorded_at.dt.date
df["week"] = df.recorded_at.dt.to_period("W-SUN").dt.start_time.dt.date

today = dt.date.today()
wk = workbook_week(today)
st.sidebar.markdown(f"**SpeakFit workbook:** {wk[0] if wk else 'no week found'}")
default = WEEK_TO_FOCUS.get(wk[1]) if wk else None
focus = st.sidebar.selectbox("This week's focus", list(FOCUS), index=list(FOCUS).index(default) if default else 0)
if not default:
    st.sidebar.caption("This workbook week has no matching metric, so pick one.")
grain = st.sidebar.radio("Trend by", ["day", "week"], horizontal=True)

st.title("SpeakFit Mobile")
st.caption("Drive (car, phone) and desk recordings are kept as separate lines: car noise changes the numbers.")

# Headline: focus metrics, this week vs last week, per source
this_wk = today - dt.timedelta(days=today.weekday())
last_wk = this_wk - dt.timedelta(days=7)
st.subheader(f"Focus: {focus}")
for source in sorted(df.source.unique()):
    s = df[df.source == source]
    cur, prev = s[s.week == this_wk], s[s.week == last_wk]
    cols = st.columns(len(FOCUS[focus]) + 1)
    cols[0].markdown(f"**{source}**  \n{len(cur)} recordings this week")
    for c, (label, col, lower_better) in zip(cols[1:], FOCUS[focus]):
        v = rates(cur)[col] if len(cur) else None
        p = rates(prev)[col] if len(prev) else None
        c.metric(label, "no data" if v is None or pd.isna(v) else f"{v:.1f}",
                 None if v is None or p is None or pd.isna(v) or pd.isna(p) else f"{v - p:+.1f} vs last week",
                 delta_color="inverse" if lower_better else "normal")

agg = df.groupby([grain, "source"]).apply(rates, include_groups=False).reset_index()
st.subheader("Trends")
names = {"wpm": "Words per minute", "fillers_pm": "Fillers per minute", "mid_pause_pct": "Mid-phrase pauses %",
         "avg_pause_s": "Average pause (s)", "lowconf_pct": "Low-confidence words %", "pitch_sd_st": "Pitch variation (st)",
         "loud_sd_db": "Loudness spread (dB)", "unique_words": "Unique words", "new_words": "New words", "speaking_min": "Speaking minutes"}
first = [c for _, c, _ in FOCUS[focus]]
cols = st.columns(2)
for i, col in enumerate(first + [c for c in names if c not in first]):
    cols[i % 2].markdown(f"**{names.get(col, col)}**")
    cols[i % 2].line_chart(agg.pivot(index=grain, columns="source", values=col))

st.subheader("Recordings")
show = df.sort_values("recorded_at", ascending=False)
for _, r in show.iterrows():
    mins = r.speaking_s / 60 or 1
    with st.expander(f"{r.recorded_at:%a %d %b %H:%M} · {r.source}/{r['mode']} · {r.n_words} words · "
                     f"{r.n_words / mins:.0f} wpm · {r.n_fillers / mins:.1f} fillers/min"):
        st.audio(str(DB.parent / "audio" / r.file))
        st.write(r.transcript)
