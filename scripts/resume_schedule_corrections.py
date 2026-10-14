"""Run the unchanged correction producer with paced, resumable HTTP retries."""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import build_trip_schedule_corrections as producer

ROOT = Path(__file__).resolve().parents[1]
POLICY = {'scope':'transport only; label producer and parser unchanged',
          'attempts':8,'initial_backoff_seconds':300,'maximum_backoff_seconds':3600,
          'initial_resume_pause_seconds':300,
          'minimum_download_interval_seconds':3,'honor_retry_after':True}


def retry_delay(error, attempt):
    cause = error.__cause__ or error
    status = getattr(cause,'code',None)
    if status is not None and status != 429 and not 500 <= status < 600:
        raise error
    if status is None and not isinstance(cause,(urllib.error.URLError,TimeoutError)):
        raise error
    delay = min(POLICY['maximum_backoff_seconds'],POLICY['initial_backoff_seconds']*2**attempt)
    value = getattr(cause,'headers',{}).get('Retry-After') if getattr(cause,'headers',None) else None
    if value:
        try: server_delay = max(0,float(value))
        except ValueError:
            try: server_delay = max(0,(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())
            except (ValueError,TypeError): server_delay = 0
        delay = max(delay,server_delay)
    return delay


def retry_call(function,*args):
    for attempt in range(POLICY['attempts']):
        try: return function(*args)
        except Exception as error:
            # Classify the actual transport cause; parsing/data errors fail
            # immediately instead of being mistaken for a transient download.
            if attempt+1 == POLICY['attempts']: raise
            delay = retry_delay(error,attempt)
            print(f'Archive transport retry {attempt+1}: waiting {delay:.0f}s after {error}',flush=True)
            time.sleep(delay)


def main():
    record = {'policy':POLICY,'launcher_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    path = producer.OUT/'transport-policy.json'
    if path.exists() and json.loads(path.read_text()) != record:
        raise ValueError('transport launcher changed; preserve its recorded identity')
    path.write_text(json.dumps(record,indent=2))
    original_list, original_download = producer.list_day, producer.download
    producer.list_day = lambda day: retry_call(original_list,day)
    last_download = [0.0]
    def download(remote,store):
        pause = POLICY['minimum_download_interval_seconds']-(time.monotonic()-last_download[0])
        if pause>0: time.sleep(pause)
        result = retry_call(original_download,remote,store)
        last_download[0] = time.monotonic()
        store.save()  # Keep successful files even if the next request fails.
        return result
    producer.download = download
    print('Resuming committed corrections after a 300-second archive cooldown',flush=True)
    time.sleep(POLICY['initial_resume_pause_seconds'])
    producer.main()


if __name__=='__main__': main()
