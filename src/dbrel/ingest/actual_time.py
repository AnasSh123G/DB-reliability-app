"""Conservative handling of IRIS actual timestamps without a UTC offset."""
from datetime import datetime
from functools import lru_cache

from dbrel.config import TZ


@lru_cache(maxsize=2048)
def ambiguous_actual(epoch: int) -> bool:
    """Whether this wall clock has two valid UTC interpretations in Berlin.

    A scheduled time or a late receipt does not prove which occurrence the
    train used. Until independent chronology is available, neither occurrence
    can be certified as an actual from this payload alone.
    """
    local = datetime.fromtimestamp(epoch, TZ)
    alternative = int(local.replace(fold=1 - local.fold).timestamp())
    if alternative == epoch:
        return False
    other = datetime.fromtimestamp(alternative, TZ)
    return other.replace(tzinfo=None) == local.replace(tzinfo=None)
