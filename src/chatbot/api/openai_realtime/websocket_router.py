import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from queue import Empty
from threading import Event as ThreadingEvent
from typing import Any, Callable

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from openai.types.realtime import (
    ConversationItemCreateEvent,
    InputAudioBufferAppendEvent,
    InputAudioBufferCommitEvent,
    ResponseCancelEvent,
    ResponseCreateEvent,
    SessionUpdateEvent,
)

from chatbot.api.openai_realtime.pipeline_unit import PipelineUnit, SessionState
from chatbot.api.openai_realtime.queue_flush import (
    audio_payload,
    flush_queue,
    is_audio_done,
    keep_audio_sentinel,
    keep_session_end,
    keep_user_text_event,
)
from chatbot.api.openai_realtime.service import (
    ResponseSpeakEvent,
    build_error_event,
)
from chatbot.api.openai_realtime.transports import (
    SessionTransport,
    WebSocketTransport,
    send_ws_event,
)
from chatbot.api.openai_realtime.turn_interruption import PipelineTurnInterrupter
from chatbot.build_info import BACKEND_SOURCES, SourceSnapshot
from chatbot.pipeline.control import SESSION_END, PipelineControlMessage, is_control_message
from chatbot.pipeline.events import (
    AssistantTextEvent,
    PipelineEvent,
    TokenUsageEvent,
    ToolActivityEvent,
)
from chatbot.pipeline.log_context import pipeline_log_ctx
from chatbot.pipeline.messages import PIPELINE_END, AudioOutput

logger = logging.getLogger(__name__)
MAX_AUDIO_BATCH_BYTES = 6400
# How long the release path waits for SESSION_END to propagate through the
# handler chain back to output_queue before warning that the unit is stuck.
# Tests monkeypatch this to a small value since their fixtures usually skip
# the real handler chain.
SESSION_END_DRAIN_TIMEOUT_S = 10.0
# Past this, the unit is quarantined: its service session is unregistered
# (closing the chat so late handler output can't mutate or bill it), but the
# unit stays unclaimable. Releasing it instead would let a new client claim a
# unit whose handlers may still emit the previous session's output (e.g. a
# transcript, which carries no session identity and would be appended to the
# new session's conversation) — a cross-session leak. If SESSION_END does
# eventually drain, the chain has proven itself clean and the unit returns to
# the pool; a dead handler keeps it quarantined forever (logged as an error).
SESSION_END_QUARANTINE_TIMEOUT_S = 180.0


def _audio_generation(item: Any) -> int | None:
    return item.cancel_generation if isinstance(item, AudioOutput) else None


async def _drain_pending_response_events(
    transport: SessionTransport | None,
    unit: PipelineUnit,
    session_id: str | None,
) -> None:
    if session_id is None:
        return

    preserved: list[Any] = []
    drained_assistant = 0
    drained_usage = 0
    drain_assistant_events = True
    try:
        while True:
            try:
                item = unit.text_output_queue.get_nowait()
            except Empty:
                break
            # Usage is accounting-only, so keep the old whole-queue drain behavior.
            # Assistant events are client-visible response output and stop at the
            # first non-response boundary to preserve normal text-event ordering.
            if isinstance(item, TokenUsageEvent):
                unit.service.dispatch_pipeline_event(session_id, item)
                drained_usage += 1
            elif drain_assistant_events and isinstance(item, AssistantTextEvent):
                drained_assistant += 1
                if _generation_is_discardable(unit, item.cancel_generation):
                    continue
                events = unit.service.dispatch_pipeline_event(session_id, item)
                if transport is not None and events:
                    await transport.send_events(events)
            else:
                preserved.append(item)
                drain_assistant_events = False
    finally:
        if preserved:
            with unit.text_output_queue.mutex:
                for item in reversed(preserved):
                    unit.text_output_queue.queue.appendleft(item)
                unit.text_output_queue.not_empty.notify(len(preserved))

    if drained_assistant or drained_usage:
        logger.debug(
            "Pipeline %d: drained %d assistant event(s) and %d token usage event(s) before response completion",
            unit.index,
            drained_assistant,
            drained_usage,
        )


def _clean_unit(unit: PipelineUnit, preserve: Callable[[Any], bool] | None = None) -> None:
    """Cancel in-flight work and flush queues for a single pipeline unit.

    All four pipeline queues are drained — input audio, transcript-to-LM,
    LM-to-TTS output, and the text-event side channel — so pending work from
    a released session cannot be picked up by handlers and leak into the next
    session that claims this unit. SESSION_END is enqueued by the route
    handler *after* this returns to serve as the soft reset signal for
    stateful handlers.
    """
    unit.cancel_scope.cancel()
    flush_queue(unit.input_queue)
    flush_queue(unit.text_prompt_queue)
    flush_queue(unit.output_queue, preserve=preserve)
    flush_queue(unit.text_output_queue, preserve=preserve)
    unit.response_playing.clear()
    unit.cancel_scope.reset()
    unit.should_listen.set()


def _to_audio_bytes(chunk: Any) -> bytes:
    chunk = audio_payload(chunk)
    if isinstance(chunk, PipelineControlMessage):
        raise TypeError(f"unexpected control message on audio output queue: {chunk!r}")
    if isinstance(chunk, np.ndarray) or hasattr(chunk, "tobytes"):
        return chunk.tobytes()
    return chunk


def _is_pipeline_end(item: Any) -> bool:
    payload = audio_payload(item)
    return isinstance(payload, bytes) and payload == PIPELINE_END


def _generation_is_discardable(unit: PipelineUnit, generation: int | None) -> bool:
    """Whether output tagged with *generation* should be dropped.

    A generation is discardable if it has been superseded (``is_stale``) or if the
    cancel scope is in its post-cancel discard window and this is not the current
    live generation. Shared by audio and assistant-text so the two paths stay in
    lockstep: dropping text whenever ``discarding`` is set (without this generation
    check) silently swallows the transcript of a fresh response when ``discarding``
    lingers — e.g. a superseded speculative turn whose TTS never emitted an
    AUDIO_RESPONSE_DONE sentinel, so response_done() never cleared the flag.
    """
    if generation is not None and unit.cancel_scope.is_stale(generation):
        return True
    if unit.cancel_scope.discarding and generation != unit.cancel_scope.generation:
        return True
    return False


def _should_discard_audio(unit: PipelineUnit, item: Any) -> bool:
    return _generation_is_discardable(unit, _audio_generation(item))


def _safe_unregister(unit: PipelineUnit, session_id: str) -> None:
    try:
        unit.service.unregister(session_id)
    except Exception:
        logger.exception(f"Pipeline {unit.index}: unregister failed for session {session_id}")


async def _release_unit_after_drain(unit: PipelineUnit, session: Any, session_id: str) -> None:
    """Wait for SESSION_END to propagate, then release the unit.

    Runs in its own asyncio task so the route handler's finally block can return
    immediately. The unit stays unavailable for new claims (unit.session != None)
    until SESSION_END travels all the way through the handler chain back to
    output_queue — observed by the send loop, which sets session.drained.

    Past SESSION_END_QUARANTINE_TIMEOUT_S (a wedged or dead handler thread) the
    unit is quarantined, NOT released: still-running handlers could emit the old
    session's output (transcripts carry no session identity) into whichever
    session claimed the unit next, and a dead handler would make the unit accept
    clients it can never serve. The session is unregistered right away so late
    output can't mutate or bill the closed conversation; the unit itself only
    returns to the pool if SESSION_END eventually drains, proving the chain is
    clean. A quarantine is logged as an error when it starts.
    """
    elapsed = 0.0
    warned = False
    try:
        while not session.drained.is_set():
            await asyncio.sleep(0.05)
            elapsed += 0.05
            if not warned and elapsed >= SESSION_END_DRAIN_TIMEOUT_S:
                logger.warning(
                    f"Pipeline {unit.index}: SESSION_END not drained after {elapsed:.1f}s — "
                    f"unit will remain unavailable until handlers finish (session {session_id})"
                )
                warned = True
            if session.quarantined_at is None and elapsed >= SESSION_END_QUARANTINE_TIMEOUT_S:
                session.quarantined_at = time.monotonic()
                _safe_unregister(unit, session_id)
                logger.error(
                    f"Pipeline {unit.index}: SESSION_END still not drained after {elapsed:.0f}s — "
                    f"quarantining unit until the handler chain drains (session {session_id})"
                )
    finally:
        # Runs when the drain completed (chain proven clean) or the task is
        # cancelled at shutdown. Release unconditionally: even if unregister
        # raises, the unit must not stay claimed forever.
        try:
            _safe_unregister(unit, session_id)
        finally:
            # Don't clobber a session that stole the unit after we started draining.
            if unit.session is session:
                unit.session = None
        recovered = " after quarantine" if session.quarantined_at is not None else ""
        logger.info(f"Pipeline {unit.index} released{recovered} (session {session_id} ended)")


# Strong references to in-flight drain-and-release tasks (asyncio only
# holds tasks weakly); each task removes itself on completion.
_release_tasks: set[asyncio.Task[None]] = set()


def _release_session(unit: PipelineUnit, session_id: str) -> None:
    """Start the release of a unit after its client disconnected.

    Called by the WebSocket route's finally block. Marks the session as
    released, resets the unit, enqueues
    SESSION_END, and spawns the drain-and-release task — the unit stays
    claimed until SESSION_END propagates back to output_queue.
    """
    old_session = unit.session
    if old_session is None:
        # Already released (e.g. duplicate close callbacks racing).
        return
    old_session.released_at = time.monotonic()
    _clean_unit(unit)
    # Tag SESSION_END with this session's id so that, after a force
    # release, a late arrival can't satisfy the next session's drain.
    unit.input_queue.put(PipelineControlMessage(SESSION_END.kind, session_id=session_id))
    task = asyncio.create_task(_release_unit_after_drain(unit, old_session, session_id))
    _release_tasks.add(task)
    task.add_done_callback(_release_tasks.discard)


async def _dispatch_client_event(
    unit: PipelineUnit,
    session_id: str,
    raw: dict[str, Any],
    transport: SessionTransport,
) -> None:
    """Parse and apply one WebSocket client event."""
    service = unit.service
    event = service.parse_client_event(raw)
    if event is None:
        await transport.send_events(
            [service.make_error(f"Unknown or invalid event: {raw.get('type')}", "unknown_or_invalid_event")]
        )
        return

    if isinstance(event, InputAudioBufferAppendEvent):
        chunks = service.handle_audio_append(session_id, event)
        rt_cfg = service._state(session_id).runtime_config
        for chunk in chunks:
            unit.input_queue.put((chunk, rt_cfg))

    elif isinstance(event, InputAudioBufferCommitEvent):
        err = service.handle_audio_commit(session_id)
        if err:
            await transport.send_events([err])

    elif isinstance(event, SessionUpdateEvent):
        session_raw = raw.get("session")
        if isinstance(session_raw, dict) and "thinker" in session_raw:
            err = service.apply_thinker(session_id, session_raw["thinker"])
            if err:
                await transport.send_events([err])
                return
        err = service.handle_session_update(session_id, event)
        if err:
            await transport.send_events([err])
        else:
            await transport.send_events([service.build_session_updated(session_id)])

    elif isinstance(event, ConversationItemCreateEvent):
        events = service.handle_conversation_item_create(session_id, event)
        if events:
            await transport.send_events(events)

    elif isinstance(event, ResponseCreateEvent):
        result = service.handle_response_create(session_id, event)
        if result:
            if result.type != "error":
                unit.cancel_scope.new_response()
            await transport.send_events([result])

    elif isinstance(event, ResponseSpeakEvent):
        result = service.handle_response_speak(session_id, event)
        if result:
            if result.type != "error":
                unit.cancel_scope.new_response()
            await transport.send_events([result])

    elif isinstance(event, ResponseCancelEvent):
        # Same condition as barge-in: a turn is in flight when a response is
        # open *or* one is pending (the LLM captured its generation before its
        # first token), so "Stop" pressed while the model is still thinking or
        # a server-side search is running lands too. A truly spurious cancel
        # must not set the discard guard.
        st = service._state(session_id)
        if st.in_response or st.response_pending:
            unit.cancel_scope.cancel()
            st.response_pending = False
        flush_queue(unit.output_queue, preserve=keep_audio_sentinel)
        flush_queue(unit.text_output_queue, preserve=keep_user_text_event)
        # Drop any LLM request still waiting to be processed so it can't
        # recapture the post-cancel generation and emit stale output.
        flush_queue(unit.text_prompt_queue, preserve=keep_session_end)
        transport.discard_pending_audio()
        events = service.handle_response_cancel(session_id)
        if events:
            await transport.send_events(events)
        unit.response_playing.clear()


def create_app(
    unit: PipelineUnit,
    stop_event: ThreadingEvent,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        send_task = asyncio.create_task(_send_loop_for(unit))
        yield
        send_task.cancel()
        try:
            await send_task
        except asyncio.CancelledError:
            pass
        sess = unit.session
        if sess is not None and sess.transport is not None:
            try:
                await sess.transport.close()
            except Exception:
                pass

    if unit.service.turn_admissions is None:
        unit.service.turn_admissions = PipelineTurnInterrupter(unit)
    app = FastAPI(lifespan=lifespan)
    # Taken once at startup; /health compares it against disk so a launcher
    # can tell this process is running code that has since changed.
    source_snapshot = SourceSnapshot(BACKEND_SOURCES)

    def _claim_unit(transport: SessionTransport | None) -> PipelineUnit | None:
        """Atomically (between asyncio yield points) reserve the first idle unit.

        Creates a placeholder SessionState that the caller fills in with the
        session_id after RealtimeService.register().
        """
        existing = unit.session
        if existing is not None:
            # Previous client is gone and the drain is still finishing, or the
            # websocket already died. Unblock the drain so a refresh can retry.
            ws = getattr(existing.transport, "websocket", None)
            ws_dead = (
                ws is not None and getattr(ws, "client_state", None) is not None and ws.client_state.name != "CONNECTED"
            )
            if existing.quarantined_at is None and (existing.released_at is not None or ws_dead):
                existing.drained.set()
            return None
        unit.session = SessionState(transport=transport)
        return unit

    def _models_ready() -> bool:
        return unit.ready_gate.ready

    def _server_tools_enabled() -> bool:
        # The LLM handler owns a ServerToolExecutor once it is set up, and runs
        # research in this process. No client-side tools remain, so a backend
        # without this is one it cannot research with.
        return any(getattr(handler, "server_tools", None) is not None for handler in unit.handlers)

    @app.get("/health")
    def health() -> dict[str, Any]:
        ready = _models_ready()
        return {
            "status": "ok" if ready else "starting",
            "ready": ready,
            "server_tools": _server_tools_enabled(),
            **source_snapshot.describe(),
        }

    @app.websocket("/v1/realtime")
    async def realtime_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        if not _models_ready():
            await send_ws_event(
                ws,
                build_error_event(
                    "The voice models are still loading. Try again in a moment.",
                    error_type="server_starting",
                ),
            )
            await ws.close(code=1013, reason="Models still loading")
            return

        transport = WebSocketTransport(ws)
        unit = _claim_unit(transport)
        if unit is None:
            logger.warning("Rejected connection: the browser pipeline is already in use")
            # Stateless error event — rejection is not chargeable to any unit's usage metrics.
            await send_ws_event(
                ws,
                build_error_event(
                    "The browser pipeline is already in use. Disconnect the existing client first.",
                    error_type="session_limit_reached",
                ),
            )
            await ws.close(code=1008, reason="All session slots are in use")
            return

        pipeline_log_ctx.set(unit.index)
        # _claim_unit guarantees unit.session is not None for the returned unit.
        assert unit.session is not None
        # Everything after the claim runs inside try so the finally below always
        # releases the unit, even if session setup fails.
        session_id = ""
        try:
            session_id = unit.service.register()
            unit.session.session_id = session_id
            logger.info(f"Client connected to pipeline {unit.index} (session {session_id})")

            # Defensive: drain edge queues and reset events so stale data from a
            # previous session that survived SESSION_END propagation doesn't leak.
            _clean_unit(unit)

            await send_ws_event(ws, unit.service.build_session_created(session_id))

            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.receive_json(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue

                await _dispatch_client_event(unit, session_id, raw, transport)

        except WebSocketDisconnect:
            logger.info(f"Client {session_id} disconnected from pipeline {unit.index}")
        except Exception as e:
            logger.error(f"Client {session_id} on pipeline {unit.index} error: {type(e).__name__}: {e}", exc_info=True)
        finally:
            # Hold the session reference: the send loop's snapshot will still resolve
            # to this object until we clear unit.session, so any handler output that
            # arrives during the drain window is sent to the now-closed ws (silently
            # dropped) instead of leaking to whichever client claims this unit next.
            # _release_session spawns the drain-and-release as a separate task so
            # this finally returns immediately. Awaiting here is unreliable: after
            # WebSocketDisconnect propagates, subsequent awaits in the same task
            # can be skipped/cancelled by Starlette's runner and never resume.
            _release_session(unit, session_id)

    @app.get("/v1/usage")
    async def usage_endpoint() -> dict[str, Any]:
        return unit.service.get_usage()

    async def _send_loop_for(unit: PipelineUnit) -> None:
        """Per-pipeline send loop. Polls this unit's output queues and forwards
        to the transport currently attached via unit.session.

        Per-session scratch (pending_output_item) lives on SessionState, so it
        disappears together with the transport when the session is released —
        no stale sentinel can leak into the next claim.
        """
        pipeline_log_ctx.set(unit.index)
        while not stop_event.is_set():
            try:
                # Snapshot the session once per iteration; if the route releases the
                # unit mid-iteration, we continue against the prior snapshot which is
                # consistent (its transport is still valid until close() returns).
                session = unit.session
                transport = session.transport if session is not None else None
                session_id = session.session_id if session is not None else None
                had_work = False

                # Text events first. Interruption waits for turn_admitted.
                while True:
                    try:
                        text_msg = unit.text_output_queue.get_nowait()
                    except Empty:
                        break
                    had_work = True

                    if isinstance(text_msg, (AssistantTextEvent, ToolActivityEvent)) and _generation_is_discardable(
                        unit, text_msg.cancel_generation
                    ):
                        pass
                    elif transport is not None and isinstance(text_msg, PipelineEvent) and session_id:
                        events = unit.service.dispatch_pipeline_event(session_id, text_msg)
                        if events:
                            await transport.send_events(events)

                try:
                    if session is not None and session.pending_output_item is not None:
                        audio_chunk = session.pending_output_item
                        session.pending_output_item = None
                    else:
                        audio_chunk = unit.output_queue.get_nowait()
                    had_work = True

                    if _is_pipeline_end(audio_chunk):
                        await _drain_pending_response_events(transport, unit, session_id)
                        if transport is not None and session_id:
                            await transport.send_events(unit.service.finish_response(session_id))
                        break

                    if is_audio_done(audio_chunk):
                        audio_generation = _audio_generation(audio_chunk)
                        if audio_generation is not None and unit.cancel_scope.is_stale(audio_generation):
                            if session_id:
                                unit.service._state(session_id).response_pending = False
                            unit.cancel_scope.response_done(audio_generation)
                            unit.should_listen.set()
                            logger.info(f"Pipeline {unit.index}: stale response complete")
                            continue
                        await _drain_pending_response_events(transport, unit, session_id)
                        if transport is not None and session_id:
                            await transport.send_events(unit.service.finish_response(session_id))
                        if session_id:
                            unit.service._state(session_id).response_pending = False
                        unit.response_playing.clear()
                        unit.cancel_scope.response_done(audio_generation)
                        unit.should_listen.set()
                        logger.info(f"Pipeline {unit.index}: response complete")
                        continue

                    # SESSION_END travels from input_queue through every handler to
                    # output_queue. Observing it here means the chain has fully reset;
                    # signal the release path so it can clear unit.session. A tag from
                    # another session means the emitting session was force-released —
                    # its late SESSION_END must not stand in for this session's drain.
                    if is_control_message(audio_chunk, SESSION_END.kind):
                        chunk_session_id = getattr(audio_chunk, "session_id", None)
                        if session is not None and chunk_session_id in (None, session.session_id):
                            session.drained.set()
                            logger.debug(f"Pipeline {unit.index}: SESSION_END drained")
                        continue

                    if is_control_message(audio_chunk):
                        continue

                    if _should_discard_audio(unit, audio_chunk):
                        continue

                    audio_chunk = _to_audio_bytes(audio_chunk)

                    audio_batch = bytearray(audio_chunk)
                    while len(audio_batch) < MAX_AUDIO_BATCH_BYTES:
                        try:
                            next_chunk = unit.output_queue.get_nowait()
                        except Empty:
                            break

                        if (
                            _is_pipeline_end(next_chunk)
                            or is_audio_done(next_chunk)
                            or is_control_message(next_chunk, SESSION_END.kind)
                        ):
                            # Only stash if we still have a session; otherwise drop it.
                            if session is not None:
                                session.pending_output_item = next_chunk
                            break

                        if _should_discard_audio(unit, next_chunk):
                            continue

                        next_audio = _to_audio_bytes(next_chunk)
                        if len(audio_batch) + len(next_audio) > MAX_AUDIO_BATCH_BYTES:
                            if session is not None:
                                session.pending_output_item = next_chunk
                            break
                        audio_batch.extend(next_audio)

                    if not unit.response_playing.is_set():
                        unit.response_playing.set()
                        unit.should_listen.set()

                    if transport is not None and session_id:
                        await transport.send_audio_chunk(unit.service, session_id, bytes(audio_batch))
                except Empty:
                    pass

                if not had_work:
                    await asyncio.sleep(0.01)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Pipeline {unit.index} send loop error: {e}")
                await asyncio.sleep(0.1)

    return app
