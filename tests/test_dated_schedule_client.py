"""Live client receipt timestamps belong to the response completion time."""
import httpx
import pytest

from dbrel.ingest import client as live


@pytest.mark.parametrize('status', [200, 404])
def test_response_keeps_body_headers_and_completion_timestamp(tmp_path, monkeypatch, status):
    monkeypatch.setattr(live, 'TT_RATELIMIT_STATE', tmp_path / 'rate-limit.json')
    clock = {'now': 100}
    monkeypatch.setattr(live.time, 'time', lambda: clock['now'])

    def receive(request):
        clock['now'] = 123
        return httpx.Response(status, content=b'original response', headers={'test-header': 'value'})

    with live.TimetablesClient(client_id='fixture', api_key='fixture',
                               base_url='https://example.invalid') as source:
        source.http.close()
        source.http = httpx.Client(transport=httpx.MockTransport(receive))
        response = source.plan(8000123, '260901', 12)
        assert response.recorded_at == 123
        assert response.status == status and response.body == b'original response'
        assert response.headers['test-header'] == 'value'
        assert response.plan_history == ()
        assert source.n_requests == 1
