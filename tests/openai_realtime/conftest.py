from queue import Queue
from threading import Event as ThreadingEvent

import pytest
from openai.types.realtime import RealtimeSessionCreateRequest
from openai.types.realtime.realtime_audio_config import RealtimeAudioConfig
from openai.types.realtime.realtime_audio_config_input import RealtimeAudioConfigInput
from openai.types.realtime.realtime_audio_config_output import RealtimeAudioConfigOutput
from openai.types.realtime.realtime_audio_formats import AudioPCM

from chatbot.api.openai_realtime.runtime_config import RuntimeConfig
from chatbot.api.openai_realtime.service import RealtimeService


def _session_pipeline() -> RealtimeSessionCreateRequest:
    """Build a test session: 16 kHz mic input, 24 kHz TTS output (pipeline rates)."""
    in_fmt = AudioPCM.model_construct(rate=16000, type="audio/pcm")
    out_fmt = AudioPCM.model_construct(rate=24000, type="audio/pcm")
    return RealtimeSessionCreateRequest.model_construct(
        type="realtime",
        audio=RealtimeAudioConfig.model_construct(
            input=RealtimeAudioConfigInput.model_construct(format=in_fmt),
            output=RealtimeAudioConfigOutput.model_construct(format=out_fmt),
        ),
    )


@pytest.fixture
def runtime_config():
    cfg = RuntimeConfig(thinker="luna")
    cfg.session = _session_pipeline()
    return cfg


@pytest.fixture
def text_prompt_queue():
    return Queue()


@pytest.fixture
def should_listen():
    ev = ThreadingEvent()
    ev.set()
    return ev


@pytest.fixture
def service(runtime_config, text_prompt_queue, should_listen):
    svc = RealtimeService(
        text_prompt_queue=text_prompt_queue,
        should_listen=should_listen,
    )
    return svc


@pytest.fixture
def conn_id(service, runtime_config):
    cid = service.register()
    service._state(cid).runtime_config = runtime_config
    yield cid
    service.unregister(cid)
