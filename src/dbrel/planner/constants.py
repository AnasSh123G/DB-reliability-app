"""Bounds shared by the journey planner."""

NO_TIME = -2147483648  # NULL time in int32 arrays.
MIN_TRANSFER_S = 300  # Minimum time to change trains.
MAX_WAIT_START_S = 21600  # Six-hour departure window at the start.
MAX_WAIT_TRANSFER_S = 10800  # Three-hour departure window at a change.
MAX_JOURNEY_S = 57600  # Sixteen-hour journey horizon.
MAX_DEP_DELAY_S = 1800  # Carried departure delay cap and departure-window lookback.
MAX_LEGS = 5  # Maximum trains per journey.
MAX_STEP_S = 21600  # Maximum valid interval between schedule steps.
PROFILE_DAYS = 28  # Number of service dates in the running profile.
PROFILE_MIN_RUNS = 2  # Minimum runs on a weekday to qualify.
ROUTES_K = 3  # Maximum distinct routes one search returns.
CONNECTIONS = 4  # Connections a plan lists, each leaving after the one before.
CHAIN_MAX = 12  # Most connections walked through to find the last ones before a time.
MAX_SEARCHES = 10  # Maximum A* searches per plan.
MAX_RUNS_PER_SEARCH = 25000  # Maximum forecast runs per A* search.
SEARCH_SECONDS = 8.0  # Time budget per A* search.
REQUEST_SECONDS = 20.0  # Time budget per plan or timings request.
FORECAST_BATCH_WINDOW_S = 900  # Lookahead interval for forecast batching.
FORECAST_BATCH_MAX_RUNS = 256  # Maximum runs per forecast batch.
FORECAST_CACHE_RUNS = 50000  # Maximum runs retained in the forecast LRU.
TIMINGS_HALF_WINDOW_S = 7200  # Two hours on either side of a timings request.
TIMINGS_MAX = 24  # Maximum instances in a timings response.
PLAN_CONCURRENCY = 2  # Simultaneous plan or timings requests.
PLAN_QUEUE_SECONDS = 30  # Maximum wait for a planner slot.
UNREACHABLE_S = 10**9  # Travel floor of a station no scheduled ride leads from to the target.
BEFORE_WINDOWS_S = (3600, 14400, 21600)  # How far back a search for earlier connections reaches.
