"""{% versioned 'dbrel/app.css' %} — {% static %} plus the file's own mtime.

Django's static server sends Last-Modified and no Cache-Control, which makes the
response *heuristically* cacheable: a browser may reuse it without asking,
typically for a tenth of its age. That cost real debugging time once already —
a cached browse.js kept rendering "no observations yet" out of a field the API
had stopped sending, which reads exactly like a backend bug and is not one.

A Cache-Control header would be the obvious fix and does not work here:
runserver's StaticFilesHandler short-circuits static requests in __call__,
before the middleware chain ever runs, so no middleware can stamp them. Putting
the version in the URL instead works whatever serves the file, because a changed
file is simply a different URL.

mtime, not a hand-bumped constant: the version that has to be remembered is the
one that will be forgotten, and it will be forgotten on exactly the edit that
needed it.
"""

from __future__ import annotations

import os

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@register.simple_tag
def versioned(path: str) -> str:
    url = static(path)
    located = finders.find(path)
    if not located:
        # Missing files are the static machinery's problem to report, not this
        # tag's to hide behind a broken-looking URL.
        return url
    return f"{url}?v={int(os.path.getmtime(located))}"
