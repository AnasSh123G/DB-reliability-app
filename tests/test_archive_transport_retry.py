"""Retries honor server pacing and never turn data errors into retry loops."""
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import resume_schedule_corrections as transport


def wrapped(status, headers=None):
    error=RuntimeError('archive failed')
    error.__cause__=HTTPError('https://example.invalid/file',status,'error',headers or {},None)
    return error


def test_retry_after_controls_429_delay():
    assert transport.retry_delay(wrapped(429,{'Retry-After':'900'}),0)==900
    assert transport.retry_delay(wrapped(503),1)==600


def test_fatal_download_and_data_errors_do_not_retry(monkeypatch):
    sleeps=[];monkeypatch.setattr(transport.time,'sleep',sleeps.append)
    for error in (wrapped(404), ValueError('source mismatch')):
        with pytest.raises(type(error)):
            transport.retry_call(lambda: (_ for _ in ()).throw(error))
    assert sleeps==[]


def test_successful_retry_does_not_repeat_completed_request(monkeypatch):
    sleeps=[];monkeypatch.setattr(transport.time,'sleep',sleeps.append)
    calls=[]
    def request():
        calls.append(1)
        if len(calls)==1: raise wrapped(429,{'Retry-After':'800'})
        return 'complete'
    assert transport.retry_call(request)=='complete'
    assert len(calls)==2 and sleeps==[800]
