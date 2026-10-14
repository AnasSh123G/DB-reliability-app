"""Build a reference-only report from completed miss diagnostics; no inference."""
from pathlib import Path
import csv
import json
import os

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data/ml/delay_miss_diagnostics_20261013'
os.environ['MPLCONFIGDIR'] = str(OUT / 'tmp/matplotlib')
(OUT / 'tmp/matplotlib').mkdir(parents=True, exist_ok=True)
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


def table(rows, fields):
    return '\n'.join(['| ' + ' | '.join(title for title, fn in fields) + ' |',
                       '| ' + ' | '.join('---' for _ in fields) + ' |'] +
                      ['| ' + ' | '.join(str(fn(r)) for _, fn in fields) + ' |' for r in rows])


def main():
    result = json.loads((OUT / 'result.json').read_text())
    reference_export = {**result, 'results': {k:v for k,v in result['results'].items() if k.startswith('reference_')}}
    reference_export['notes'] = [*result['notes'], 'Candidate breakdowns excluded: ordered row identity not independently verified']
    (OUT / 'reference-only.json').write_text(json.dumps(reference_export, indent=2))
    b = result['results']['reference_display']
    total = b['total'][0]
    percent = lambda n: f'{100*n:.2f}%'
    basic = [('Group', lambda r:r['label']), ('Arrivals', lambda r:f"{r['arrivals']:,}"),
             ('Misses', lambda r:f"{r['misses']:,}"), ('Miss rate', lambda r:percent(r['miss_rate']))]
    days = b['day']
    rates = {x['label']:x['miss_rate'] for x in b['segment']}
    adjusted = []
    for day in days:
        groups = [x for x in b['day_segment'] if x['label'].startswith(day['label'] + ' / ')]
        expected = sum(x['arrivals'] * rates[x['label'].split(' / ')[1]] for x in groups)
        adjusted.append({**day, 'mix_excess':(day['misses']-expected)/day['arrivals']})
    stations = list(csv.DictReader((OUT / 'reference_display_stations.csv').open()))
    names = {int(x['key']): x['label'].split(' [id ')[0] for x in stations}
    top = b['stations']['by_count'][:10]
    selected = list(dict.fromkeys([x['key'] for x in top] + [x['key'] for x in b['stations']['by_rate'][:10]]))
    matrix = np.full((len(selected), len(days)), np.nan)
    daypos = {x['label']:i for i,x in enumerate(days)}
    stationpos = {x:i for i,x in enumerate(selected)}
    with (OUT / 'reference_display_station_day.csv').open() as f:
        for x in csv.DictReader(f):
            slot = int(x['station_slot'])
            if slot in stationpos and int(x['arrivals']) >= 50:
                matrix[stationpos[slot], daypos[x['service_date']]] = 100*float(x['miss_rate'])
    fig, axes = plt.subplots(2,1,figsize=(13,11), gridspec_kw={'height_ratios':[1,2.6]}, layout='constrained')
    axes[0].plot(range(len(days)), [x['miss_rate']*100 for x in days], marker='o', label='Observed')
    axes[0].plot(range(len(days)), [(x['miss_rate']-x['mix_excess'])*100 for x in adjusted], linestyle='--', label='Expected from service-type mix')
    axes[0].axhline(total['miss_rate']*100, color='gray', alpha=.5)
    axes[0].set(xticks=range(len(days)), xticklabels=[x['label'][5:] for x in days], ylabel='Miss rate (%)', title='Arrival errors over five minutes — displayed baseline predictions')
    axes[0].legend(loc='upper left'); axes[0].grid(alpha=.2)
    im=axes[1].imshow(np.ma.masked_invalid(matrix), aspect='auto', vmin=0, vmax=65, cmap='YlOrRd')
    axes[1].set(xticks=range(len(days)), xticklabels=[x['label'][5:] for x in days], yticks=range(len(selected)), yticklabels=[names[x] for x in selected], title='Stations with highest total misses or highest supported miss rates (cells need ≥50 arrivals)')
    fig.colorbar(im, ax=axes[1], label='Miss rate (%)', shrink=.8)
    fig.savefig(OUT/'miss-patterns.svg');fig.savefig(OUT/'miss-patterns.png',dpi=160);plt.close(fig)
    concentration = sum(x['misses'] for x in top)/total['misses']
    states = [x for x in b['state'] if x['label'] in ('DE-NW','DE-BY','DE-BW')]
    state_share = sum(x['misses'] for x in states)/total['misses']
    state_traffic = sum(x['arrivals'] for x in states)/total['arrivals']
    spikes = result['station_day_spikes'][:10]
    spike_fields = [('Service date',lambda r:r['day']),('Station',lambda r:r['station'].split(' [id ')[0]),('Arrivals / misses',lambda r:f"{r['arrivals']:,} / {r['misses']:,}"),('Miss rate',lambda r:percent(r['miss_rate'])),('Other 13 days',lambda r:percent(r['other_days_miss_rate']))]
    lines = ['---','kind: spec','title: "Delay misses by date and place — before recency retraining"','---','',
      '# Delay misses before retraining','',
      f"Across **{total['arrivals']:,} recorded stop-arrivals**, **{total['misses']:,} miss by more than five minutes ({percent(total['miss_rate'])}; accuracy {percent(1-total['miss_rate'])}). **{percent(total['underestimated']/total['misses'])} of misses underestimate delay**.",'',
      'This uses saved, training-only baseline predictions for **service dates September 6–19, 2026**, after the unchanged app clamp/round rule. The baseline was trained through August 22. These are reused exploratory validation dates, not a fresh independent test, and this is not an in-sample evaluation of the deployed final-refit model. Counts are stop-arrivals, not independent trips; many errors can belong to the same journey. A miss means absolute error strictly greater than five minutes.','',
      '![Daily and station miss rates](../data/ml/delay_miss_diagnostics_20261013/miss-patterns.png)','',
      '## When misses increase','',
      table(adjusted,basic+[('Excess over service-mix expectation',lambda r:f"{100*r['mix_excess']:+.2f} percentage points")]),'',
      'The dashed chart line uses each day’s S-Bahn/regional/long-distance proportions and the corresponding full-period miss rates. The residual removes differences in those three proportions only; it does not control for geography, weekday, route or disruptions. Dates are service dates; overnight arrivals may occur on the following calendar day.','',
      '## Where misses concentrate','',
      f"North Rhine-Westphalia, Bavaria and Baden-Württemberg together account for **{percent(state_share)} of misses**, compared with **{percent(state_traffic)} of arrivals**. However, the ten stations with most misses account for only **{percent(concentration)} of all misses**. The problem is distributed across the network, with local spikes on top.",'',
      table(sorted(b['state'],key=lambda r:-r['misses']),basic),'',
      '### Stations with most misses','',table(top,basic),'',
      '### Stations with highest miss rates (at least 1,000 arrivals)','',table(b['stations']['by_rate'][:10],basic),'',
      'Station rates reflect service mix as well as place; long-distance hubs are not directly comparable with S-Bahn-only stops. UNKNOWN station categories merge unseen identities rather than identifying a physical station.','',
      '### Date-specific station spikes','',table(spikes,spike_fields),'',
      'Several Munich stops spike together on September 15. Berlin Sonnenallee on September 19 and Stuttgart Feuersee on September 16 also stand out. These patterns are consistent with shared local events or systematic feed effects, but the statistics do not establish their causes. Each spike has at least 100 arrivals that day and at least 1,000 on other dates. Comparisons with the other thirteen days are unadjusted for weekday and service mix.','',
      '## Which cases are missed','',table(b['segment'],basic+[('Share of all misses',lambda r:percent(r['share_of_misses']))]),'',
      table(b['actual_delay'],basic+[('Share of all misses',lambda r:percent(r['share_of_misses']))]),'',
      'Regional trains produce most misses, while long-distance trains have the highest rate. Late arrivals of 5 minutes or more account for most misses. This supports investigating delay propagation and disruption information; increasing recent training weights alone cannot supply an incident signal missing from the inputs.','',
      '**Early-arrival labels need an audit before drawing training conclusions.** There are 43,415 targets earlier than −5 minutes; the display’s nonnegative clamp necessarily misses them. Their mean absolute displayed error is 33.56 minutes. Several named regional services have 250–252 arrivals and a 100% miss rate dominated by overestimation, with average errors around an hour. This is a signal to inspect source timestamps, schedule versions and label construction, not evidence of a particular bug. No labels have been changed or removed.','',
      table(b['trains']['by_rate'][:8],basic+[('MAE (minutes)',lambda r:f"{r['mae']:.2f}")]),'',
      '### Scheduled local hour','',table(b['hour'],basic),'',
      '### Known and unseen identities','',table(b['novelty'],basic),'',
      'Unseen train identities account for only about 3.68% of misses; they are not the main source of error. Line names can cover services in several regions, so line labels alone should not be interpreted as a geographic corridor.','',
      '## Reliability of the attribution','',
      'The reference prediction array was aligned to prepared stop IDs in full, aggregate scores were reconciled, and sampled station IDs were cross-checked against the read-only database. The independent review confirmed thresholds, grouping denominators and vocabulary decoding. Candidate arrays lack an ordered stop-ID companion: aggregate/day/segment checks cannot detect every within-group reordering. **This report and chart therefore use only the verified reference for all attribution.** Candidate location CSVs in the raw output directory are exploratory and must not be used for conclusions until identity binding is added.','',
      f"Diagnostics peak memory: **{result['resources']['cgroup_peak_bytes']/2**20:.1f} MiB**, under a private 2 GiB limit with job swap disabled. No models were retrained and no app endpoints, deployment pointer or commits were changed.",'',
      '## Next phase, after these findings are reviewed','',
      'Use weight 1 for older training trips, with a linear ramp from 1 to 1.5 over the last calendar month of the training split. For the unchanged split this is July 22–August 22; validation dates cannot define or enter the training ramp. Normalize by the sum of weights and leave evaluation unweighted.','',
      'Limit the comparison to fine-tuned MLP, discrete-quantile classifier output, and the ±3-minute-window classifier output. The two classifier outputs can share one trained network, yielding three candidates from two fits. Report raw and displayed metrics, the same date/place breakdown, and artifact size; preserve browser output and endpoint contracts. Add ordered stop-ID companions and prediction hashes for future candidate attribution.','',
      'The next investigation should also audit the systematically early labels and separate recurrent weaknesses from isolated date-specific spikes. No recency implementation or retraining has begun: this report is the requested checkpoint.','',
      '[Complete verified baseline station CSV](../data/ml/delay_miss_diagnostics_20261013/reference_display_stations.csv) · [Baseline station/date CSV](../data/ml/delay_miss_diagnostics_20261013/reference_display_station_day.csv) · [Verified reference diagnostic JSON](../data/ml/delay_miss_diagnostics_20261013/reference-only.json) · [Independent review](delay-miss-diagnostics-review.md)','']
    (ROOT/'docs/delay-miss-diagnostics.md').write_text('\n'.join(lines))
    print(json.dumps({'report':'docs/delay-miss-diagnostics.md','top10_station_miss_share':concentration,'three_state_miss_share':state_share,'three_state_arrival_share':state_traffic,'day_mix_residuals':[(x['label'],x['mix_excess']) for x in adjusted]}))

if __name__=='__main__':main()
