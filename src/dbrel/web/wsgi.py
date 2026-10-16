from __future__ import annotations

import os
import threading

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dbrel.web.settings")

application = get_wsgi_application()


def _warm() -> None:
    """Load the current model's notice rates, logging (not raising) any failure."""
    from dbrel.db import get_engine
    from dbrel.web import queries

    try:
        queries.warm_model(get_engine())
    except Exception:  # the first request will hit the same problem and report it
        import logging

        logging.getLogger("dbrel.web").exception("warming the delay model failed")

    try:
        from dbrel.db import read_scope
        from dbrel.planner.timetable import get_timetable

        with read_scope() as session:
            get_timetable(session)
    except Exception:
        import logging

        logging.getLogger("dbrel.web").exception("warming the planner timetable failed")


# Off the startup path: the server answers at once, and the rates (about 25 s) load in the background.
threading.Thread(target=_warm, name="warm-model", daemon=True).start()
