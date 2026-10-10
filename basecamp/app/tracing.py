"""Tracing hooks around Gemma calls. No-op until Sentry is configured (Phase 7)."""
from contextlib import contextmanager


class _Span:
    def fail(self, reason: str):
        pass

    def finish(self, **kw):
        pass

    def outcome(self, value: str):
        pass


@contextmanager
def gen_ai_span(operation: str, model: str, prompt: str):
    yield _Span()


def init():
    pass
