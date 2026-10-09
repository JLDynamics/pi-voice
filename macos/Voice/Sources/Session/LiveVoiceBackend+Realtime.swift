import AVFoundation
import Darwin
import Foundation

/// Realtime WebSocket events: the receive pump, audio deltas, and handleMessage's event switch.
/// Split out of LiveVoiceBackend.swift by role; stored state stays in the class.
extension LiveVoiceBackend {
    /// Receive off the MainActor. URLSession delivers on the session queue;
    /// awaiting receive() on MainActor deadlocks connecting forever.
    /// Audio deltas are decoded here so JSON/base64/resample do not hitch the panel.
    static func pump(
        _ ws: URLSessionWebSocketTask,
        owner: LiveVoiceBackend?,
        pcm: PCMBridge,
        generation: UUID
    ) async {
        while !Task.isCancelled {
            let closed = await MainActor.run { owner?.closed != false || owner?.connectionGeneration != generation }
            if closed { break }
            do {
                let message = try await ws.receive()
                let text: String?
                switch message {
                case .string(let value): text = value
                case .data(let data): text = String(data: data, encoding: .utf8)
                @unknown default: text = nil
                }
                guard let text else { continue }
                if let delta = parseAudioDelta(text) {
                    let dest = await MainActor.run { () -> AVAudioFormat? in
                        guard let owner, owner.connectionGeneration == generation, !owner.closed else { return nil }
                        if owner.shouldDropAudio(responseId: delta.responseId) { return nil }
                        return owner.audio.playbackFormat
                    }
                    guard let dest, let buffer = pcm.playbackBuffer(base64: delta.b64, dest: dest) else { continue }
                    await MainActor.run {
                        owner?.playDecodedAudio(buffer, responseId: delta.responseId, generation: generation)
                    }
                } else {
                    await MainActor.run {
                        guard owner?.connectionGeneration == generation else { return }
                        owner?.handle(.string(text))
                    }
                }
            } catch {
                if Task.isCancelled { break }
                if Self.isHandshakeNotReady(error) {
                    let waiting = await MainActor.run {
                        owner?.connectionGeneration == generation
                            && owner?.closed == false
                            && owner?.connection == .awaitingSession
                    }
                    if waiting {
                        NSLog("[LiveVoice] receive before handshake, retrying: \(error.localizedDescription)")
                        try? await Task.sleep(nanoseconds: 50_000_000)
                        continue
                    }
                }
                NSLog("[LiveVoice] receive failed: \(error.localizedDescription)")
                await MainActor.run {
                    guard !Task.isCancelled, owner?.connectionGeneration == generation else { return }
                    owner?.fail("Can't reach the service")
                }
                break
            }
        }
    }

    /// `URLSessionWebSocketTask.receive()` can throw POSIX ENOTCONN if it
    /// runs in the window between `resume()` and the TCP handshake.
    static func isHandshakeNotReady(_ error: Error) -> Bool {
        let ns = error as NSError
        if ns.domain == NSPOSIXErrorDomain && ns.code == Int(ENOTCONN) { return true }
        return ns.localizedDescription.localizedCaseInsensitiveContains("socket is not connected")
    }

    struct AudioDelta {
        let responseId: String
        let b64: String
    }

    static func parseAudioDelta(_ text: String) -> AudioDelta? {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = json["type"] as? String,
              type == "response.audio.delta" || type == "response.output_audio.delta",
              let b64 = json["delta"] as? String, !b64.isEmpty
        else { return nil }
        let responseId: String
        if let id = json["response_id"] as? String {
            responseId = id
        } else if let response = json["response"] as? [String: Any], let id = response["id"] as? String {
            responseId = id
        } else {
            responseId = ""
        }
        return AudioDelta(responseId: responseId, b64: b64)
    }

    func shouldDropAudio(responseId: String) -> Bool {
        !responseId.isEmpty && cancelledIds.contains(responseId)
    }

    func playDecodedAudio(
        _ buffer: AVAudioPCMBuffer,
        responseId: String,
        generation: UUID
    ) {
        guard connectionGeneration == generation, !closed, !shouldDropAudio(responseId: responseId) else { return }
        audio.play(buffer)
        onState?(.agentSpeaking)
    }

    func handle(_ message: URLSessionWebSocketTask.Message) {
        switch message {
        case .string(let text):
            handleMessage(text)
        case .data(let data):
            if let text = String(data: data, encoding: .utf8) {
                handleMessage(text)
            }
        @unknown default:
            break
        }
    }

    func handleMessage(_ text: String) {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = json["type"] as? String
        else { return }

        switch type {
        case "session.created":
            sendSessionUpdate()
            replayHistory()
            connection = .ready
            mic.setAccepting(true)
            handshakeTimeout?.cancel()
            handshakeTimeout = nil
            if !closed { onState?(.listening) }

        case "response.created":
            responseCreateRequested = false
            implicitTurnSince = nil
            lastResponseCreatedAt = Date()
            if let response = json["response"] as? [String: Any],
               let id = response["id"] as? String {
                activeResponseId = id
            }

        case "input_audio_buffer.speech_started":
            userSpeechActive = true
            onUserSpeechStarted?()
            // A response can be finished server-side while its audio is still
            // queued on this Mac. Stop that queue at confirmed user speech,
            // without waiting for the user's transcription to finish.
            if json["interrupt_response"] as? Bool == true, audio.isPlaying {
                audio.clearPlayback()
            }
            let responseActive = !activeResponseId.isEmpty
            if !closed,
               VoiceSpeechStartPolicy.shouldShowListening(
                   responseActive: responseActive,
                   playing: audio.isPlaying
               ) {
                onState?(.listening)
            }

        case "input_audio_buffer.turn_admitted":
            if let admission = parseTurnAdmission(json) {
                handleTurnAdmission(admission)
            }

        case "input_audio_buffer.speech_stopped":
            userSpeechActive = false
            // The turn is over on the server's side: show that the model is
            // working, not that the panel is idly listening. speech_started,
            // turn_ignored, audio or response.done all move it on.
            if !closed,
               VoiceSpeechStartPolicy.shouldShowListening(
                   responseActive: !activeResponseId.isEmpty,
                   playing: audio.isPlaying
               ) {
                onState?(.thinking)
            }

        case "conversation.item.input_audio_transcription.completed":
            let itemId = json["item_id"] as? String
            let transcript = (json["transcript"] as? String ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if transcript.isEmpty {
                // An empty final must not call onUserFinal — that would commit
                // an empty bubble.
                break
            }
            lastInputItemId = itemId
            // The server now answers this turn on its own; see VoiceContextHold.
            implicitTurnSince = Date()
            onUserFinal?(transcript, itemId)

        case "response.audio_transcript.delta", "response.output_audio_transcript.delta":
            if cancelledIds.contains(responseId(in: json)) { return }
            if let delta = json["delta"] as? String, !delta.isEmpty {
                pushAgentDelta(delta)
            }

        case "response.audio.delta", "response.output_audio.delta":
            let rid = responseId(in: json)
            if !rid.isEmpty, cancelledIds.contains(rid) { return }
            if let b64 = json["delta"] as? String,
               let buffer = pcm.playbackBuffer(base64: b64, dest: audio.playbackFormat) {
                audio.play(buffer)
            }
            onState?(.agentSpeaking)

        case "response.done":
            var cancelled = false
            if let response = json["response"] as? [String: Any],
               let id = response["id"] as? String {
                if !activeResponseId.isEmpty, id != activeResponseId { return }
                if id == activeResponseId { activeResponseId = "" }
                if response["status"] as? String == "cancelled" {
                    cancelled = true
                    rememberCancelled(id)
                    cancelToolWork()
                    audio.clearPlayback()
                }
            }
            // The reply is written back; held Pi context now follows it.
            releaseHeldContext()
            let ackCallId = handoffAckCallId
            handoffAckCallId = nil
            let acknowledge = VoiceToolFollowUp.shouldAcknowledgeHandoff(
                handoffCalled: ackCallId != nil, spokenText: agentText, cancelled: cancelled, muted: muted
            )
            if !serverToolNames.isEmpty {
                // A server-run tool whose output never arrived (the turn was
                // interrupted mid-call) must not leave the pill spinning.
                serverToolNames.removeAll()
                onToolsCancelled?()
            }
            finishAgentTurn()
            if !audio.isPlaying {
                if !closed { onState?(.listening) }
            }
            if responseRequestPending && !audio.isPlaying,
               VoiceToolFollowUp.shouldSend(
                   pendingTools: toolScope.pendingIds.count,
                   responseActive: !activeResponseId.isEmpty
               ) {
                responseRequestPending = false
                sendResponseCreate()
            }
            if acknowledge, let ackCallId, !responseCreateRequested, !responseRequestPending {
                NSLog("[LiveVoice] handoff %@ was silent; asking for an acknowledgement", ackCallId)
                requestFollowUp(callId: ackCallId)
            }

        case "response.function_call_arguments.done":
            if cancelledIds.contains(responseId(in: json)) { return }
            guard let name = json["name"] as? String,
                  let callId = json["call_id"] as? String
            else { break }
            let argsJson = json["arguments"] as? String ?? "{}"
            executeTool(name: name, argsJson: argsJson, callId: callId)

        case "conversation.item.created":
            // Tools the server ran inside the response arrive as the items they
            // created: a function_call when the call starts, its
            // function_call_output when it finishes. Show them; never run them.
            guard let item = json["item"] as? [String: Any],
                  let callId = item["call_id"] as? String else { break }
            if item["type"] as? String == "function_call", let name = item["name"] as? String {
                serverToolNames[callId] = name
                onToolActive?(name)
            } else if item["type"] as? String == "function_call_output",
                      let name = serverToolNames.removeValue(forKey: callId) {
                onToolDone?(name, item["output"] as? String ?? "")
            }

        case "error":
            // Transport close is fatal; server events like turn_ignored are not.
            if let error = json["error"] as? [String: Any], VoiceServerError.kind(error) == "turn_ignored" {
                releaseHeldContext()
                onTurnDropped?()
                if !closed, !audio.isPlaying, activeResponseId.isEmpty {
                    onState?(.listening)
                }
            } else if let error = json["error"] as? [String: Any] {
                if VoiceServerError.kind(error) == "response_cancel_not_active" { break }
                onRequestError?(error["message"] as? String ?? "The reply failed. Please retry.")
                responseCreateRequested = false
                responseRequestPending = false
            }

        default:
            break
        }
    }

    /// SessionController appends with `+=`, so only new text may be forwarded.
    /// If the server sends cumulative text, strip the prefix we already have.
    func pushAgentDelta(_ incoming: String) {
        let chunk: String
        if incoming.hasPrefix(agentText), incoming.count >= agentText.count {
            chunk = String(incoming.dropFirst(agentText.count))
            agentText = incoming
        } else {
            chunk = incoming
            agentText += incoming
        }
        guard !chunk.isEmpty else { return }
        onAgentDelta?(chunk)
        onState?(.agentSpeaking)
    }

    func finishAgentTurn() {
        let spoken = agentText.trimmingCharacters(in: .whitespacesAndNewlines)
        agentText = ""
        // Publish the finished transcript when the response completes. Audio
        // may still be playing for many seconds; the Pi chat should not wait
        // for the speaker queue to drain before showing what Luna is saying.
        if !spoken.isEmpty { onSpoken?(spoken) }
        onAgentDone?()
    }

    func parseTurnAdmission(_ json: [String: Any]) -> VoiceTurnAdmission? {
        guard let id = json["admission_id"] as? String, !id.isEmpty else { return nil }
        let itemID = json["item_id"] as? String ?? ""
        let interrupts = json["interrupt_output"] as? Bool ?? true
        return VoiceTurnAdmission(id: id, itemID: itemID, interruptsOutput: interrupts)
    }

    func handleTurnAdmission(_ admission: VoiceTurnAdmission) {
        guard VoiceTurnAdmissionPolicy.shouldApply(
            id: admission.id,
            seen: seenAdmissionIds,
            interruptsOutput: admission.interruptsOutput
        ) else { return }
        seenAdmissionIds.insert(admission.id)
        if seenAdmissionIds.count > 32, let oldest = seenAdmissionIds.first {
            seenAdmissionIds.remove(oldest)
        }
        cancelToolWork()
        rememberCancelled(activeResponseId)
        if audio.isPlaying {
            audio.clearPlayback()
        }
    }

    func rememberCancelled(_ id: String) {
        guard !id.isEmpty else { return }
        cancelledIds.insert(id)
        if cancelledIds.count > 32, let oldest = cancelledIds.first {
            cancelledIds.remove(oldest)
        }
    }

    func responseId(in json: [String: Any]) -> String {
        if let id = json["response_id"] as? String { return id }
        if let response = json["response"] as? [String: Any],
           let id = response["id"] as? String {
            return id
        }
        return ""
    }
}
