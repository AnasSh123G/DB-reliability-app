---
name: live-accuracy-study
description: Run, check and report the live accuracy study -- fetch real arrivals/departures from the DB Timetables API for German stations, compare TabM's prediction and the stored average against what really happened, draw the histogram, and compare runs taken at different times. Use whenever the user asks how accurate TabM or the averages are on real/live/current trains, asks to "run the study/test again", "check the timings", "compare against real departures", or wants the error histogram or a comparison between two runs.
---

# Live accuracy study — step-by-step instructions

Follow these steps **in order**. Do not skip a step, do not invent a shortcut, and do not
improvise a different method. Every rule below exists because a previous run went wrong without
it. When a step says "stop and tell the user", stop.

---

## 0. What this study is (read this first)

The user wants to know: *if someone had looked up a train in the app earlier today, how far off
would the prediction have been from what actually happened?*

For each station:
1. Ask the DB Timetables API which trains passed through in the **last 2 hours**, with their
   **planned** time and their **real** recorded time.
2. For every stop that has **already happened**, compute:
   - **The deployed model's prediction** — from the `CURRENT` artifact, exactly as the app does.
     Which figure that is depends on the artifact kind (check `manifest.json` → `kind`):
     - `tabm` (v2, two-sided): TabM's median (p50) for arrivals **and** departures.
     - `arrival-net`: an **arrival-only** estimate — the centre of the most likely 6-minute window
       ("window rule"), **not** a median. It makes **no departure predictions**, so the report
       says departures are not predicted and scores arrivals only. Never call this figure "TabM"
       or "p50"; the report and CSV label it "Model estimate (window rule)" / `model_point`.
   - **The stored average** (the "Ø" the app shows) — exactly as the station board does.
3. Compare both against the **real delay** DB recorded.

A prediction only depends on the timetable (train, station, date, scheduled time, holiday), never
on anything that happens later. So computing it now, after the train ran, gives exactly the same
number the app would have shown hours earlier. That is why a single after-the-fact run is a fair
test.

Everything is already implemented. **Your job is to run it, check it, and report it — not to
rewrite it.** The code lives in:

| File | What it does |
|---|---|
| `src/dbrel/study/live_accuracy.py` | sampling, fetching, matching, predicting, scoring, report |
| `src/dbrel/study/histogram.py` + `histogram_template.html` | the histogram page |
| `src/dbrel/study/cli.py` | the `dbrel study live-accuracy` command |
| `tests/test_live_accuracy.py` | tests |
| `openspec/changes/add-live-accuracy-study/` | the plan (proposal, spec, design, tasks) |

---

## 1. Hard rules (never break these)

1. **Ask before running.** A full run makes ~22,000 live API requests and takes ~9–10 minutes.
   Only start it when the user asked for a run in this conversation. "Run it again" counts.
2. **Always launch through `dbrel ml launch`**, never directly in the terminal and never with
   `nohup`/`&`/`setsid`. The launcher puts the job in its own capped systemd unit, so it survives
   the editor/terminal closing and cannot eat the desktop's memory.
3. **Never kill processes with `pkill -f <pattern>`.** The pattern matches your own shell and kills
   it (exit code 144). Stop the job with `systemctl --user stop dbrel-ml-live-accuracy.service`.
4. **Never explain a strange result with an outside cause** ("there must be a disruption today",
   "the network is having a bad day") unless you have proof. A strange result is almost always a
   bug. Check it first — see step 6.
5. **Do not check on the job every few seconds.** Use the single background wait command in
   step 4 and let the harness notify you. Before you say how much time is left, run `date` —
   do not guess from memory.
6. **Keep statistics simple** (step 7). No pinball loss, no "band coverage", no quantile jargon.
   The user explicitly rejected those.
7. **Do not modify the study code to "fix" numbers you don't like.** If a number looks wrong,
   find out why (step 6), tell the user, and only change code once you know the cause.
8. **The study is read-only.** It must never write to `trip_stop`, the averages tables or the
   model artifacts. Do not add anything that does.

---

## 2. How the DB API works (so you don't repeat old mistakes)

The DB Timetables API (IRIS-TTS) only has **station-based** endpoints:

| Endpoint | Returns | Notes |
|---|---|---|
| `/plan/{eva}/{YYMMDD}/{HH}` | the trains **planned** at that station in that hour: train category + number, planned times, stop id | Only serves **today and tomorrow**. |
| `/fchg/{eva}` | **changes** for that station: changed (real or estimated) times, cancellations | A **pure delta**: for ~99% of stops it has **no train number and no planned time**. |
| `/station/{pattern}` | station lookup | not used here |

There is **no per-train endpoint**. You cannot "pick random trains and ask the API about them".
Sampling is always per station.

The four facts that broke earlier runs:

1. **`/fchg` alone is useless for this study.** Measured on Bochum Hbf: 280 stops, only 1 had a
   train number. The first run used `/fchg` alone, kept only the rare special trains that do carry
   a number (mostly disrupted replacement trains), reported 79% of stations as "empty" and
   ~33-minute errors. The fix, which is implemented: per station, fetch `/plan` for the current
   hour and the 2 hours before, fetch `/fchg` once, and **match the two by stop id**
   (`merge_plan_and_changes`).
2. **A changed time (`ct`) is not proof a train has run.** Before a train arrives, `ct` is DB's
   live *estimate*. A side only counts as "happened" when its changed time is more than
   `GRACE_S` (120 seconds) in the past (`realized_sides`).
3. **Cancelled stops are skipped.** There is no delay to compare.
4. **Right after midnight** the look-back hours belong to yesterday, which `/plan` no longer
   serves. A run between 00:00 and ~02:00 will find far fewer trains. Tell the user before
   running at that time.

The stop's route position (stop number, number of stops) is not in either response; it comes
from the local static timetable (`station_stop`) via `dbrel.ml.timetable.calls_at`. That costs no
extra API calls. A train that isn't in the local timetable is skipped and counted.

---

## 3. Before running: checklist

Run each command and compare with the expected result. If anything differs, **stop and tell the
user** what you saw.

```bash
cd /home/anas/dev/DB-reliability-app
```

**3.1 Credentials work.**
```bash
.venv/bin/dbrel tt-check
```
Expected: the line `DB-Client-Id + DB-Api-Key    200` and `authenticates with: DB-Client-Id + DB-Api-Key`.

**3.2 A model exists.**
```bash
cat data/models/v2/CURRENT && ls data/models/v2/$(cat data/models/v2/CURRENT)
```
Expected: a timestamp, and a folder containing `manifest.json` and `model.onnx`.

**3.3 No other study is running.**
```bash
.venv/bin/dbrel ml status live-accuracy | head -2
```
Expected: `finished successfully`, `not found`, or `failed` — **not** `running`. If it is running,
do not start another one; tell the user.

**3.4 Enough free memory** (a run peaks at ~1.5 GiB; the cap is 4 GiB).
```bash
grep MemAvailable /proc/meminfo
```
Expected: more than 5,000,000 kB. If less, tell the user and do not start.

**3.5 The tests pass** (only needed if anyone changed code under `src/dbrel/study/`).
```bash
.venv/bin/python -m pytest -q tests/test_live_accuracy.py
```
Expected: `17 passed` (or more), no failures.

**3.6 Note the time.**
```bash
date +%H:%M
```
Write it down. The user compares runs by time of day.

---

## 4. Running it

### 4.1 Choose the size

| What the user asked for | `--sample-size` | Requests | Expected duration |
|---|---|---|---|
| "all stations" / "more than 3000" / nothing specific after the first full run | `10000` (caps at all 5,422 stations) | ~22,000 | **9–10 minutes** |
| "3000 stations" | `3000` | ~12,000 | ~5–6 minutes |
| a quick check | `300` | ~1,200 | ~40 seconds |

Duration is roughly: **fetching ≈ 1 minute per 600 stations**, plus **scoring ≈ 1 minute per
30,000 stops**. Tell the user the estimate *before* starting, and the clock time it should end.

For reproducible sampling, add `--seed 1` (any number). Without it, each run samples differently
(irrelevant when running all stations).

### 4.2 Launch

```bash
.venv/bin/dbrel ml launch live-accuracy --memory 4G -- .venv/bin/dbrel study live-accuracy --sample-size 10000
```

Expected output:
```
started dbrel-ml-live-accuracy.service (MemoryMax=4G, no swap)
  log:    /home/anas/dev/DB-reliability-app/data/ml/logs/live-accuracy-<STAMP>.log
```
Copy the exact log path. You need it below.

If it says the unit is already running, see rule 1.3 and step 3.3.

### 4.3 Wait (one background command, then stop talking)

Run this **once**, with the Bash tool's `run_in_background: true`, replacing `<LOG>` with the log
path:

```bash
cd /home/anas/dev/DB-reliability-app && until [ "$(systemctl --user show dbrel-ml-live-accuracy.service -p SubState --value)" != "running" ]; do sleep 15; done; .venv/bin/dbrel ml status live-accuracy | head -5; cat <LOG>
```

Then tell the user in one or two sentences: what started, and when it should finish. **Do not
keep checking.** The harness notifies you when it finishes.

### 4.4 If the user asks "how much left?"

```bash
date +%H:%M:%S; .venv/bin/dbrel ml status live-accuracy | sed -n 1,5p; tail -8 <LOG>
```

Read the log lines (they appear in this order):

| Log line | Meaning | Time after launch (all stations) |
|---|---|---|
| `sampled 5422 stations` | fetching started | ~0 s |
| `5135 stations had realized stops; 95360 realized sides fetched` | fetching done | ~7–8 min |
| `92399 sides resolved to a local timetable match; 2961 had none` | matching done | a few seconds later |
| `running TabM on 92,399 stops` | model running | seconds |
| `looking up the stored averages` then `10,000 / 92,399`, `20,000 / ...` | scoring | ~1 min per 30k |
| `report: data/ml/studies/<STAMP>/report.md` | done | — |

Answer with the actual phase and a remaining-time estimate based on the table. If a phase takes
more than **3×** its expected time, say so honestly and offer to stop and investigate.

---

## 5. What the run produces

Everything goes into `data/ml/studies/<STAMP>/` (the stamp is the **UTC** finish time, e.g.
`20260925T185229Z` = 20:52 in Berlin summer time):

| File | Content |
|---|---|
| `report.md` | plain-language summary per side (departures, arrivals) |
| `result.json` | the same numbers as JSON |
| `comparisons.csv` | **one row per stop**: side, station, train, date, segment, holiday, real delay, the model's p25 / point / p75 (`tabm_p25/p50/p75` for a v2 artifact, `model_p25/model_point/model_p75` for an arrival-net one), stored average |
| `histogram.html` | the histogram page (written automatically) |

If `histogram.html` is missing (the job ran older code), create it:
```bash
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); from pathlib import Path; from dbrel.study.histogram import write_histogram; print(write_histogram(Path('data/ml/studies/<STAMP>')))"
```

---

## 6. Sanity checks — do these BEFORE telling the user anything

Open the report:
```bash
cat data/ml/studies/<STAMP>/report.md
```

Compare with these normal ranges (from the real runs on 2026-09-25, **v2 TabM artifact only**).
With an `arrival-net` artifact the model rows below do **not** apply: its window rule trades
within-1-minute hits for within-3-minute hits by design, so its "within 1 min" and "minutes off"
are expected to differ from the average's. Normal ranges for it must be re-established from its
first real runs — do not judge it against the TabM numbers, and do not invent a range. The
non-model rows (stations with no realized stops, stops skipped, stops compared, real delays) still
apply.

| Check | Normal | If outside the normal range |
|---|---|---|
| Stations with no realized stops | ~5% (e.g. 287 of 5,422) | **> 20% means a bug** (the `/fchg`-only bug gave 79%). Or it's just after midnight (fact 2.4). |
| Stops skipped (not in local timetable) | ~3–4% of realized sides | > 15%: the local timetable is stale or identity matching broke |
| Average minutes off, both methods | **~3 minutes** (2.9–3.4 seen) | > 6: **assume a bug first**. The broken first run showed ~33. |
| TabM vs average difference | within ~0.5 min of each other | a large gap means one side is broken |
| Within 1 min | ~40% TabM, ~44–48% average | |
| Stops compared | ~85k–115k for all stations | a few thousand = the fetch is broken |
| Real delay, share > 5 min late | ~20–25% | |

**If anything is outside the normal range:**
1. Do **not** report the numbers as findings.
2. Look at real rows:
   ```bash
   head -20 data/ml/studies/<STAMP>/comparisons.csv
   ```
   Are real delays mostly 0–3 minutes (normal) or all 30+ (bug)? Are TabM and the average
   plausible (single-digit minutes)?
3. Check one busy station live:
   ```bash
   .venv/bin/python - <<'EOF'
   import sys; sys.path.insert(0, "src")
   from collections import Counter
   from dbrel.db import read_scope
   from dbrel.ingest.client import TimetablesClient
   from dbrel.ingest.timeutil import read_clock
   from dbrel.models import Station
   from dbrel.study.live_accuracy import fetch_station
   c = TimetablesClient(); now = read_clock(None)
   with read_scope() as s:
       st = s.query(Station).filter_by(eva=8000041).one()   # Bochum Hbf
       r = fetch_station(c, st, now)
       print(len(r.realized), "realized sides;", r.skip_reason)
       print(sorted(Counter(round(x.real_delay_min) for x in r.realized).items()))
   EOF
   ```
   Normal: dozens of realized sides, delays mostly 0–3 minutes.
4. Tell the user what is wrong and what you found. Fix code only after you know the cause, and
   run the tests (step 3.5) after any change.

---

## 7. Reporting to the user

Keep it short and plain. Use **only** these numbers:

- **average minutes off** for each method
- **% within 1 min, within 3 min, within 5 min**
- **how often each method was closer** (TabM closer / average closer)
- how many stops were compared, and how many stations had no trains
- how late trains really were: counts over 5, 15, 30, 60 minutes, and the latest train

Always mention these two facts, because they explain the numbers:
1. **The stored average is a whole number of minutes, and DB records delays in whole minutes**,
   so the average can match exactly; TabM gives fractions (e.g. 0.9) and never ties. This helps
   the average on "within 1 min". (With an `arrival-net` artifact, also say that its estimate is
   chosen to be within 3 minutes as often as possible, not to be exactly right, and that
   departures are not predicted.)
2. **Stops with no stored average** are listed separately — TabM is the only prediction there,
   and those stops are harder (TabM was ~12–13 min off on them).

Template (fill in the numbers; departures and arrivals separately, or say they were nearly
identical):

```markdown
The <HH:MM> run finished in <N> minutes: <S> stations, <K> had trains that had already run,
<C> stops compared.

| | Minutes off (avg) | Within 1 min | Within 3 min | Within 5 min |
|---|---|---|---|---|
| TabM (or "Model estimate (window rule)" for an arrival-net artifact) | … | … | … | … |
| Stored average | … | … | … | … |

- Closer, stop by stop: stored average <A> times, model <T> times.
- How late trains really were: <x> (…%) more than 5 min late, <y> more than 30, latest <z> min.
- <one sentence on the whole-minute rounding effect>
- <one sentence on stops without a stored average>

Saved in `data/ml/studies/<STAMP>/`.
```

Do **not** add pinball loss, quantiles, band coverage, p-values, or confidence intervals.

---

## 8. The histogram

The histogram shows, for each range of **minutes off** (0–1, 1–3, 3–5, 5–10, 10–20, 20+), one
bar for TabM and one for the stored average, as a share of the stops that have both. It has
buttons for all stops / departures / arrivals and tiles showing how late trains really were.

To show it to the user, publish the file with the Artifact tool:
- `file_path`: `data/ml/studies/<STAMP>/histogram.html`
- `icon`: `chart` (first publish only)
- `description`: one sentence, e.g. "How many minutes TabM and the stored average were off on
  113,957 real stops, fetched live from every German station at 20:43."

Every histogram page has the same `<title>`, "Live Prediction Misses". If you publish several,
tell the user they share that tab title; the time is in the page's first paragraph.

To read the numbers behind it (for your chat summary):
```bash
.venv/bin/python -c "import sys, json; sys.path.insert(0,'src'); from pathlib import Path; from dbrel.study.histogram import histogram_data; d = histogram_data(Path('data/ml/studies/<STAMP>')); print(json.dumps({k: d[k] for k in ('both','real','meta')}))"
```
`both.tabm` / `both.average` are counts per range in the order above; divide by `both.n` for
percentages.

---

## 9. Comparing two runs (e.g. afternoon vs evening)

List the runs:
```bash
ls data/ml/studies/
```
Print both side by side (replace the two stamps):
```bash
.venv/bin/python - <<'EOF'
import sys, json; sys.path.insert(0, "src")
from pathlib import Path
from dbrel.study.histogram import histogram_data
RUNS = ["<STAMP_A>", "<STAMP_B>"]
bins = ["0-1", "1-3", "3-5", "5-10", "10-20", "20+"]
for stamp in RUNS:
    d = histogram_data(Path("data/ml/studies") / stamp)
    r = json.loads((Path("data/ml/studies") / stamp / "result.json").read_text())
    b, real = d["both"], d["real"]
    print(f"\n{d['meta']['when']}: {b['n']:,} stops compared")
    for side in ("dep", "arr"):
        s = r["scores"][side]
        print(f"  {side}: minutes off TabM {s['tabm']['minutes_off']:.1f}, "
              f"average {s['average']['minutes_off']:.1f}; closer: TabM {s['tabm_closer']:,}, "
              f"average {s['average_closer']:,}")
    for i, name in enumerate(bins):
        print(f"  {name:>5} min off: TabM {100*b['tabm'][i]/b['n']:4.1f}%   "
              f"average {100*b['average'][i]/b['n']:4.1f}%")
    print(f"  real: >5 min {100*real['over5']/real['n']:.0f}%, >30 min {real['over30']:,}, "
          f"latest {real['max']:.0f} min")
EOF
```
Report it as two small tables: headline numbers (stops, late trains, minutes off) and the
histogram ranges written as "16:00 → 20:43" (e.g. `40% → 40%`). Say in one or two sentences
what changed, and only what the numbers show.

---

## 10. Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| `not running under dbrel ml launch` | started directly | use step 4.2 |
| `no trained delay model ... export the TabM model` | no `CURRENT` artifact | tell the user; do not train or export anything yourself |
| `MissingCredentials` / `tt-check` shows 401 | `.env` credentials missing or wrong | tell the user; never print the keys |
| Unit already `running` | a previous run is still going | wait for it, or ask the user before stopping it |
| Scoring phase far slower than 1 min per 30k stops | direction cache not used | check `score_stops` passes `direction=model_direction.get` to `predict_calls` |
| Most stations "no realized stops" | `/fchg`-only fetching came back, or run just after midnight | step 6 |
| ~30-minute errors for both methods | same `/fchg`-only bug (only special trains kept) | step 6; `merge_plan_and_changes` must be used |
| `histogram.html` missing | the job ran older code | step 5 command |
| Tests fail after a code change | you broke something | fix before running for real |
| Artifact publish refused | wrong path or not HTML | publish `histogram.html` from the study folder |

---

## 11. Done means

- [ ] The run finished (`finished successfully`).
- [ ] The sanity checks in step 6 were all normal (or the user was told what was not).
- [ ] The user got the simple summary from step 7.
- [ ] If they asked for a chart: the histogram was published and the link given.
- [ ] If there is an earlier run: offer (one line) to compare, or compare if asked.
