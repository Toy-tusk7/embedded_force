"""LangSmith tracing turns itself off when LangSmith refuses traces (over the monthly quota, a bad key) instead of
posting, failing and retrying on every agent step for the rest of the run."""

from embedforge.core.telemetry import LangSmithTracer


class RefusingClient:
    def __init__(self, error):
        self.error, self.calls = error, 0

    def create_run(self, **_):
        self.calls += 1
        raise RuntimeError(self.error)

    update_run = create_run


def _tracer(client):
    t = LangSmithTracer()
    t.tracing_enabled, t._client = True, client
    return t


def test_over_quota_turns_tracing_off():
    client = RefusingClient("HTTPError('429 Client Error: Too Many Requests', 'Monthly unique traces usage limit exceeded')")
    t = _tracer(client)
    t.start_span("a", "step")
    t.end_span("a")
    t.start_span("b", "step")
    assert not t.tracing_enabled and client.calls == 1


def test_a_passing_network_error_keeps_tracing_on():
    client = RefusingClient("ConnectionResetError: connection reset by peer")
    t = _tracer(client)
    t.start_span("a", "step")
    assert t.tracing_enabled and client.calls == 1
