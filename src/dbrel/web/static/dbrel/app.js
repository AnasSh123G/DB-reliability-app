// Station autocomplete. No framework, no build step.
(function () {
  const input = document.getElementById('q');
  const list = document.getElementById('results');
  if (!input || !list) return;

  let timer = null;
  let inflight = null;
  let items = [];
  let cursor = -1;

  function close() {
    list.hidden = true;
    list.innerHTML = '';
    input.setAttribute('aria-expanded', 'false');
    items = [];
    cursor = -1;
  }

  function highlight(next) {
    if (!items.length) return;
    cursor = (next + items.length) % items.length;
    [...list.children].forEach((li, i) =>
      li.setAttribute('aria-selected', i === cursor ? 'true' : 'false')
    );
    list.children[cursor].scrollIntoView({ block: 'nearest' });
  }

  function render(results) {
    items = results;
    cursor = -1;
    list.innerHTML = '';

    if (!results.length) {
      const li = document.createElement('li');
      li.className = 'empty';
      li.textContent = 'No station, line or train matches that.';
      list.appendChild(li);
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      return;
    }

    for (const item of results) {
      const li = document.createElement('li');
      li.setAttribute('role', 'option');
      li.setAttribute('aria-selected', 'false');
      // Only an unmonitored station is a dead end; lines and trains always have
      // something to show, even if only the route.
      if (item.kind === 'station' && !item.tracked) li.className = 'untracked';

      const label = document.createElement('span');
      label.className = 'label';

      const badge = document.createElement('span');
      badge.className = 'kind ' + item.kind;
      badge.textContent = item.kind;
      label.appendChild(badge);
      label.appendChild(document.createTextNode(item.name));
      li.appendChild(label);

      const sub = document.createElement('span');
      sub.className = 'sub';
      sub.textContent = item.sub || '';
      li.appendChild(sub);

      li.addEventListener('mousedown', (event) => {
        event.preventDefault();
        window.location = item.url;
      });
      list.appendChild(li);
    }
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
  }

  function search(term) {
    // Abort rather than race: out-of-order replies would otherwise overwrite the
    // results for what the user is currently typing.
    if (inflight) inflight.abort();
    inflight = new AbortController();
    fetch('/api/suggest?q=' + encodeURIComponent(term), { signal: inflight.signal })
      .then((r) => r.json())
      .then((data) => render(data.results || []))
      .catch((err) => {
        if (err.name !== 'AbortError') close();
      });
  }

  input.addEventListener('input', () => {
    const term = input.value.trim();
    clearTimeout(timer);
    if (term.length < 2) {
      if (inflight) inflight.abort();
      close();
      return;
    }
    timer = setTimeout(() => search(term), 120);
  });

  input.addEventListener('keydown', (event) => {
    if (list.hidden) return;
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      highlight(cursor + 1);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      highlight(cursor - 1);
    } else if (event.key === 'Enter' && cursor >= 0) {
      event.preventDefault();
      window.location = items[cursor].url;
    } else if (event.key === 'Escape') {
      close();
    }
  });

  input.addEventListener('blur', () => setTimeout(close, 120));
})();
