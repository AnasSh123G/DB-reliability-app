// Line -> direction -> stop -> time. Each field only exists once its parent is
// chosen, and choosing a field clears everything after it -- there is no valid
// state where a station is picked but the direction that produced its list has
// changed underneath it.
(function () {
  const lineInput = document.getElementById('browse-line');
  const lineBox = document.getElementById('browse-line-typeahead');
  const directionSelect = document.getElementById('browse-direction');
  const stationSelect = document.getElementById('browse-station');
  const dateInput = document.getElementById('browse-date');
  const timeInput = document.getElementById('browse-time');
  const results = document.getElementById('browse-results');
  if (!lineInput || !results) return;

  // Today, in the viewer's own calendar -- a plain default, not a claim about
  // which day type it is. The date field itself is what decides that.
  const today = new Date();
  dateInput.value = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-` +
    `${String(today.getDate()).padStart(2, '0')}`;

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
    dateInput.disabled = true;
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
    dateInput.disabled = true;
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
    dateInput.disabled = true;
    timeInput.disabled = true;
    results.innerHTML = '';
    const direction = directionSelect.value;
    if (lineId === null || direction === '') return;

    fetch(`/api/line/${lineId}/stations?direction=${direction}`)
      .then((r) => r.json())
      .then((data) => {
        fillSelect(stationSelect, data.stations, 'name', 'id');
        dateInput.disabled = false;
        timeInput.disabled = false;
      })
      .catch(() => resetSelect(stationSelect, 'Could not load stops'));
  });

  function forecast(pred) {
    if (!pred) return '<span class="pred none">&mdash; no forecast for this call</span>';
    return `<span class="pred" title="model forecast, run for this call just now, 50% conformal interval">` +
      `<span class="pred-tag">forecast</span> ${pred.p50_min} min` +
      `<span class="pred-range-label">Range of expected delay:</span>` +
      `<span class="pred-range">${pred.lo_min} to ${pred.hi_min} min</span></span>`;
  }

  function renderTrips(data) {
    const poolLabel = data.day_type === 'SUNDAY_HOLIDAY'
      ? 'conditioned on Sundays &amp; holidays' : 'conditioned on weekdays';
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
          ${forecast(trip.dep_pred)}
        </div>
      </li>`).join('');
    results.innerHTML = `
      <h3 class="direction-head">${data.station.name} &middot; ${data.line.name}
        <span class="n">${poolLabel}</span>
      </h3>
      <ol class="btrips">${items}</ol>`;
  }

  function lookup() {
    const direction = directionSelect.value;
    const station = stationSelect.value;
    const date = dateInput.value;
    const time = timeInput.value;
    if (lineId === null || direction === '' || station === '' || !date || !time) return;

    fetch(`/api/line/${lineId}/around?direction=${direction}&station=${station}` +
      `&date=${date}&time=${time}`)
      .then((r) => r.json())
      .then(renderTrips)
      .catch(() => {
        results.innerHTML = '<p class="empty">Could not load trips for that stop, date and time.</p>';
      });
  }

  stationSelect.addEventListener('change', lookup);
  dateInput.addEventListener('change', lookup);
  timeInput.addEventListener('change', lookup);
})();
