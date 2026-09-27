"""Ready-gate and deferred handler setup, without models or network."""

from queue import Queue
from threading import Event

from chatbot.baseHandler import BaseHandler
from chatbot.pipeline.messages import PIPELINE_END
from chatbot.pipeline.ready import PipelineReady


def test_empty_gate_is_ready():
    gate = PipelineReady()
    assert gate.ready
    assert not gate.failed


def test_gate_ready_after_all_marks():
    gate = PipelineReady(2)
    assert not gate.ready
    gate.mark(True)
    assert not gate.ready
    gate.mark(True)
    assert gate.ready
    assert not gate.failed


def test_gate_failure_stays_unready():
    gate = PipelineReady(2)
    gate.mark(False)
    gate.mark(True)
    assert not gate.ready
    assert gate.failed


class _MarkerHandler(BaseHandler):
    def setup(self, **kwargs):
        self.called = True

    def process(self, item):
        return
        yield


def test_handler_defers_setup_until_run():
    handler = _MarkerHandler(Event(), Queue(), Queue(), defer_setup=True)
    assert handler._setup_done is False
    assert not hasattr(handler, "called")
    handler.queue_in.put(PIPELINE_END)
    handler.run()
    assert handler.called is True
    assert handler._setup_done is True


def test_handler_setup_runs_in_init_by_default():
    handler = _MarkerHandler(Event(), Queue(), Queue())
    assert handler.called is True
    assert handler._setup_done is True


def test_handler_setup_failure_notifies_ready_gate():
    class Boom(BaseHandler):
        def setup(self, **kwargs):
            raise RuntimeError("load failed")

        def process(self, item):
            return
            yield

    gate = PipelineReady(1)
    handler = Boom(Event(), Queue(), Queue(), defer_setup=True)
    handler.ready_callback = gate.mark
    handler.run()
    assert gate.failed
    assert not gate.ready
    assert handler.queue_out.get_nowait() == PIPELINE_END
