// Line -> direction -> stop -> time. Each field only exists once its parent is
// chosen, and choosing a field clears everything after it -- there is no valid
// state where a station is picked but the direction that produced its list has
// changed underneath it.
(function () {
  const lineInput = document.getElementById('browse-line');
  const lineBox = document.getElementById('browse-line-typeahead');
  const directionSelect = document.getElementById('browse-direction');
  const stationSelect = document.getElementById('browse-station');
  const timeInput = document.getElementById('browse-time');
  const results = document.getElementById('browse-results');
  if (!lineInput || !results) return;

  let lineId = null;
  let lineList = null;
  let inflight = null;

  function resetSelect(select, placeholder) {
    select.innerHTML = '';
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = placeholder;
    select.appendChild(opt);
    select.disabled = true;
  }

  function fillSelect(select, options, labelKey, valueKey) {
    select.innerHTML = '';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = `Choose (${options.length})`;
    select.appendChild(placeholder);
    for (const item of options) {
      const opt = document.createElement('option');
      opt.value = item[valueKey];
      opt.textContent = item[labelKey];
      select.appendChild(opt);
    }
    select.disabled = false;
  }

  function onLineChosen(id, name) {
    lineId = id;
    lineInput.value = name;
    lineBox.querySelector('.typeahead-results').hidden = true;
    resetSelect(stationSelect, 'Choose a direction first');
    timeInput.disabled = true;
    results.innerHTML = '';

    fetch(`/api/line/${id}/directions`)
      .then((r) => r.json())
      .then((data) => fillSelect(directionSelect, data.directions, 'label', 'direction'))
      .catch(() => resetSelect(directionSelect, 'Could not load directions'));
  }

  // --- line typeahead: same shape as the header search, scoped to lines only.
  lineInput.addEventListener('input', () => {
    lineId = null;
    resetSelect(directionSelect, 'Choose a line first');
    resetSelect(stationSelect, 'Choose a direction first');
    timeInput.disabled = true;
    results.innerHTML = '';

    const term = lineInput.value.trim();
    const list = lineBox.querySelector('.typeahead-results');
    if (inflight) inflight.abort();
    if (term.length < 2) {
      list.hidden = true;
      list.innerHTML = '';
      return;
    }
    inflight = new AbortController();
    fetch(`/api/suggest?q=${encodeURIComponent(term)}&only=line`, { signal: inflight.signal })
      .then((r) => r.json())
      .then((data) => {
        lineList = data.results || [];
        list.innerHTML = '';
        if (!lineList.length) {
          const li = document.createElement('li');
          li.className = 'empty';
          li.textContent = 'No line matches that.';
          list.appendChild(li);
          list.hidden = false;
          return;
        }
        for (const line of lineList) {
          const li = document.createElement('li');
          li.textContent = line.name + (line.sub ? ` — ${line.sub}` : '');
          li.addEventListener('mousedown', (event) => {
            event.preventDefault();
            onLineChosen(line.id, line.name);
          });
          list.appendChild(li);
        }
        list.hidden = false;
      })
      .catch((err) => {
        if (err.name !== 'AbortError') list.hidden = true;
      });
  });

  lineInput.addEventListener('blur', () => {
    setTimeout(() => {
      lineBox.querySelector('.typeahead-results').hidden = true;
    }, 120);
  });

  directionSelect.addEventListener('change', () => {
    resetSelect(stationSelect, 'Choose a direction first');
    timeInput.disabled = true;
    results.innerHTML = '';
    const direction = directionSelect.value;
    if (lineId === null || direction === '') return;

    fetch(`/api/line/${lineId}/stations?direction=${direction}`)
      .then((r) => r.json())
      .then((data) => {
        fillSelect(stationSelect, data.stations, 'name', 'id');
        timeInput.disabled = false;
      })
      .catch(() => resetSelect(stationSelect, 'Could not load stops'));
  });

  function figure(avg, label) {
    if (!avg) return `<span class="bavg none">${label}: no observations yet</span>`;
    const provisional = avg.provisional
      ? '<span class="provisional-tag">provisional</span>' : '';
    return `<span class="bavg">${label}: <span class="figure">&#8709; ${avg.avg_min} min</span>` +
      ` · ${avg.punctual_pct}% under 6 min · n=${avg.n} ${provisional}</span>`;
  }

  function renderTrips(data) {
    if (!data.trips.length) {
      results.innerHTML = '<p class="empty">This stop has no scheduled calls in that direction.</p>';
      return;
    }
    const items = data.trips.map((trip) => `
      <li class="btrip ${trip.relation}">
        <div class="btrip-when">${trip.relation === 'after' ? 'next' : 'previous'}</div>
        <div class="btrip-body">
          <a class="train" href="${trip.url}">${trip.name}</a>
          <div class="btimes">
            ${trip.sched_arr ? `<span class="t"><abbr title="scheduled arrival">arr</abbr> ${trip.sched_arr}</span>` : ''}
            ${trip.sched_dep ? `<span class="t dep"><abbr title="scheduled departure">dep</abbr> ${trip.sched_dep}</span>` : ''}
          </div>
          <div class="bavgs">
            ${figure(trip.arr_avg, 'arr')}
            ${figure(trip.dep_avg, 'dep')}
          </div>
        </div>
      </li>`).join('');
    results.innerHTML = `
      <h3 class="direction-head">${data.station.name} &middot; ${data.line.name}</h3>
      <ol class="btrips">${items}</ol>`;
  }

  function lookup() {
    const direction = directionSelect.value;
    const station = stationSelect.value;
    const time = timeInput.value;
    if (lineId === null || direction === '' || station === '' || !time) return;

    fetch(`/api/line/${lineId}/around?direction=${direction}&station=${station}&time=${time}`)
      .then((r) => r.json())
      .then(renderTrips)
      .catch(() => {
        results.innerHTML = '<p class="empty">Could not load trips for that stop and time.</p>';
      });
  }

  stationSelect.addEventListener('change', lookup);
  timeInput.addEventListener('change', lookup);
})();
