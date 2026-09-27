import logging
import threading
from threading import Event

import uvicorn

from chatbot.api.openai_realtime.pipeline_unit import PipelineUnit
from chatbot.api.openai_realtime.websocket_router import create_app

logger = logging.getLogger(__name__)


class RealtimeServer:
    """
    Pipeline handler for the OpenAI Realtime API mode.

    Owns the single browser pipeline and its uvicorn WebSocket server.
    """

    def __init__(
        self,
        stop_event: Event,
        unit: PipelineUnit,
        host: str = "0.0.0.0",
        port: int = 8766,
    ) -> None:
        self.stop_event = stop_event
        self.unit = unit
        self.host = host
        self.port = port

    def run(self) -> None:
        """Start the FastAPI/uvicorn server (called from a ThreadManager thread)."""
        app = create_app(unit=self.unit, stop_event=self.stop_event)

        logger.info(f"OpenAI Realtime API starting on ws://{self.host}:{self.port}/v1/realtime")

        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="info",
        )
        server = uvicorn.Server(config)

        server.install_signal_handlers = lambda: None  # type: ignore[attr-defined]

        def _watch_stop() -> None:
            self.stop_event.wait()
            server.should_exit = True

        watcher = threading.Thread(target=_watch_stop, daemon=True)
        watcher.start()

        try:
            server.run()
        finally:
            # A bind/startup failure must also stop the model threads. During a
            # normal shutdown the event is already set by ThreadManager.
            self.stop_event.set()
