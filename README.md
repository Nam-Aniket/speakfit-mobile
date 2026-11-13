# SpeakFit Mobile

Driving practice tracker, built from the doc "Speak Fit Mobile: Driving Practice Tracker Build Plan" (revised scope, option B).
Separate from the Mac SpeakFit app, which stays frozen.

## Pieces

| Piece | File | What it does |
|---|---|---|
| Phone app | `android/src/Main.kt`, `make_apk.sh` | One tap START, records with screen off (foreground service), new file every 30 min, tags date and practice mode in the file name |
| Sync | `Sync` in `Main.kt`, `server.py` | On Wi-Fi the phone finds the Mac (DNS-SD `_speakfit._tcp`), uploads finished files with a shared token |
| Pipeline | `pipeline.py` | faster-whisper `small.en` with word timestamps, Parselmouth pitch and loudness, SQLite in `data/speakfit.db` |
| Dashboard | `dashboard.py` | Weekly focus metric, drive and desk as separate lines, transcripts and audio |

## Commands

```bash
.venv/bin/python pipeline.py some_take.m4a          # process a desk file by hand
.venv/bin/python pipeline.py --demo                 # metric self-check
.venv/bin/streamlit run dashboard.py                # dashboard on http://127.0.0.1:8501
.venv/bin/python server.py                          # sync server in the foreground
./install_agent.sh                                  # keep the sync server always on
./make_apk.sh                                       # build build/SpeakFitMobile.apk
```

## Metric definitions

- Pause: gap of 0.3 s to 3 s between words. Gaps of 3 s or more are breaks (not talking) and count toward neither pauses nor speaking time.
- Mid-phrase pause: the word before the pause has no punctuation. Whisper punctuates sparsely in long run-on speech, so treat this as a rough signal.
- Low-confidence word: Whisper word probability below 0.5.
- Pitch variation: standard deviation of pitch in semitones around the median, measured only inside words (ignores car noise between words).
- Fillers: um, uh, er, erm, ah, hmm, mm, "you know", "i mean", and "like" only when set off by a comma.

## Known limits

- Whisper still misses some fillers (measured against SpeakFit's Apple engine: 5 vs 6 and 13 vs 9 on two takes).
- Car audio is noisier than desk audio, so drive numbers are not comparable to desk numbers; the dashboard keeps them apart.
- A phone call or another app using the mic can silence the recording.
