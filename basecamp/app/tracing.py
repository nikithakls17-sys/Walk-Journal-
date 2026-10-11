"""Sentry agent tracing for every Gemma call (gen_ai spans). A no-op unless SENTRY_DSN is set.

Each call becomes a `gen_ai.invoke_agent` span (the Base Camp "agent" doing one job, e.g. verify_photo)
containing a `gen_ai.chat` span with model, latency, token counts and the outcome
(quest generated / photo match / no match / couldn't tell). Prompts, photos and responses are NOT sent:
only sizes and outcomes, so private data stays on the laptop.
"""
from contextlib import contextmanager

from . import config

_enabled = False


def init():
    global _enabled
    if not config.SENTRY_DSN:
        return
    import sentry_sdk
    sentry_sdk.init(
        dsn=config.SENTRY_DSN,
        traces_sample_rate=1.0,
        send_default_pii=False,
        environment="basecamp",
        release="walk-journal@0.1.0",
    )
    _enabled = True


class _Span:
    def __init__(self, agent=None, chat=None):
        self.agent, self.chat = agent, chat

    def _set(self, key, value):
        for s in (self.chat, self.agent):
            if s is not None and value is not None:
                s.set_data(key, value)

    def fail(self, reason: str):
        self._set("walkjournal.outcome", "error")
        self._set("error.type", reason[:120])
        if self.chat is not None:
            self.chat.set_status("internal_error")

    def finish(self, latency_s=None, input_tokens=None, output_tokens=None, response=None):
        self._set("gen_ai.usage.input_tokens", input_tokens)
        self._set("gen_ai.usage.output_tokens", output_tokens)
        if input_tokens is not None and output_tokens is not None:
            self._set("gen_ai.usage.total_tokens", input_tokens + output_tokens)
        self._set("walkjournal.latency_ms", round(latency_s * 1000) if latency_s is not None else None)
        self._set("walkjournal.response_chars", len(response) if response else None)

    def outcome(self, value: str):
        self._set("walkjournal.outcome", value)
        if self.agent is not None:
            self.agent.set_tag("walkjournal.outcome", value.split(":")[0])


@contextmanager
def gen_ai_span(operation: str, model: str, prompt: str):
    if not _enabled:
        yield _Span()
        return
    import sentry_sdk
    parent = sentry_sdk.get_current_span()
    opener = (parent.start_child if parent is not None else sentry_sdk.start_transaction)
    with opener(op="gen_ai.invoke_agent", name=f"invoke_agent walk-journal {operation}") as agent:
        agent.set_data("gen_ai.operation.name", "invoke_agent")
        agent.set_data("gen_ai.agent.name", f"walk-journal.{operation}")
        agent.set_data("gen_ai.system", "ollama")
        agent.set_data("gen_ai.request.model", model)
        with agent.start_child(op="gen_ai.chat", name=f"chat {model}") as chat:
            chat.set_data("gen_ai.operation.name", "chat")
            chat.set_data("gen_ai.system", "ollama")
            chat.set_data("gen_ai.request.model", model)
            chat.set_data("gen_ai.response.model", model)
            chat.set_data("walkjournal.task", operation)
            chat.set_data("walkjournal.prompt_chars", len(prompt))
            yield _Span(agent, chat)
