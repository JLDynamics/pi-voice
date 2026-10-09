import AVFoundation
import Darwin
import Foundation

/// Session setup and tool plumbing: session.update, history replay, tool execution, tool outputs and response requests.
/// Split out of LiveVoiceBackend.swift by role; stored state stays in the class.
extension LiveVoiceBackend {
    func sendSessionUpdate() {
        // Persona only. The server's system prompt adds the research guidance
        // and the date. Nothing about tools needs to be re-sent from here.
        let tools = CommandLine.arguments.contains("--headless")
            ? headlessTalkerTools()
            : VoiceToolExecutor.shared.activeToolDefinitions()
        var sess: [String: Any] = [
            "type": "realtime",
            "instructions": instructions,
            "audio": ["output": ["voice": voice]],
        ]
        let thinker = ProcessInfo.processInfo.environment["VOICE_THINKER"]?.lowercased()
        if thinker == "luna" || thinker == "pi" {
            sess["thinker"] = thinker as Any
        }
        // Pi owns tools. Sending Luna's client tools would invite a think
        // follow-up this session must not start.
        if thinker != "pi", !tools.isEmpty {
            sess["tools"] = tools
            sess["tool_choice"] = "auto"
        }
        send([
            "type": "session.update",
            "session": sess,
        ])
    }

    func headlessTalkerTools() -> [[String: Any]] {
        // Agent talks. One handoff sends real work to the open Pi session.
        // Progress and the answer arrive as [STATUS] and [FINAL], not as tools
        // the model polls. stop_thinking cancels the current job and leaves
        // the voice call up; `/voice stop` is what ends the call.
        return HeadlessTools.definitions
    }

    static func json(_ object: [String: Any]) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: object),
              let text = String(data: data, encoding: .utf8), !text.isEmpty else { return "{}" }
        return text
    }

    /// Status and result answers are the point of the call — unlike the
    /// fire-and-forget hand-offs, Luna must speak them, so ask the model to
    /// continue once the output lands. Mirrors the supervised-tool path minus
    /// the scope bookkeeping these synchronous answers do not need.
    func requestFollowUp(callId: String) {
        requestResponse()
        if toolScope.pendingIds.isEmpty {
            armFollowUpWatchdog(callId: callId, generation: toolScope.generation)
        }
    }

    /// Inject the dated startup pack into the live conversation. No response is
    /// requested, so the pack is context and is not spoken.
    func replayHistory() {
        var replayable: [(role: String, text: String)] = []
        for m in historyMessages {
            let text = m.text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            if m.role == "user" || m.role == "assistant" {
                replayable.append((m.role, text))
            } else if m.role == "tool" {
                let name = (m.name ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                let shown = text.count > 500 ? String(text.prefix(497)) + "..." : text
                replayable.append(("assistant", "[Earlier I used \(name.isEmpty ? "tool" : name)] \(shown)"))
            }
        }
        for m in replayable {
            let type = m.role == "assistant" ? "output_text" : "input_text"
            send([
                "type": "conversation.item.create",
                "item": [
                    "type": "message",
                    "role": m.role,
                    "content": [["type": type, "text": m.text]],
                ] as [String: Any],
            ])
        }
        if !replayable.isEmpty {
            NSLog("[LiveVoice] replayed %d saved message(s)", replayable.count)
        }
    }

    func cancelToolWork() {
        if !closed {
            for callId in toolScope.pendingIds {
                sendToolOutput(callId: callId, output: "This tool was cancelled because the conversation moved on. Its result is unavailable.")
            }
        }
        toolScope.cancel()
        serverToolNames.removeAll()
        handoffAckCallId = nil
        onToolsCancelled?()
        responseRequestPending = false
    }

    /// Run a tool the server forwarded to the client (`spawn_thinking` /
    /// `stop_thinking`), post its output, and leave the voice line free.
    /// Progress and the final answer are pushed later on their channels.
    /// A handoff while a job is active steers it, and the ack says so. A
    /// handoff while the mic is muted still returns an ack here; the
    /// extension drops it and reports `dropped`, which stays silent.
    func executeTool(name: String, argsJson: String, callId: String) {
        guard !closed, seenToolCalls.insert(callId).inserted else { return }
        if name == HeadlessTools.spawnThinking {
            let brief = HeadlessTools.spawnBrief(argsJson)
            // The same task again within seconds: keep Pi's current work.
            if let current = piJobs.activeRepeat(of: brief) {
                sendToolOutput(callId: callId, output: Self.json(PiJobTracker.repeatAck(id: current)))
                handoffAckCallId = callId
                appendPiJournal(PiJobTracker.journalLine(now: Date(), event: "repeat", id: current, brief: brief))
                return
            }
            let id = UUID().uuidString
            // Read before ask(): the new id becomes active there.
            let steering = piJobs.hasActive
            piJobs.ask(id: id, brief: brief)
            startPiProgressLoop(id: id)
            sendToolOutput(callId: callId, output: Self.json(PiJobTracker.handoffAck(id: id, steering: steering)))
            handoffAckCallId = callId
            appendPiJournal(PiJobTracker.journalLine(now: Date(), event: steering ? "steered" : "queued", id: id, brief: brief))
            if !brief.isEmpty { onSpawnThinking?(id, brief) }
            return
        }
        if name == HeadlessTools.stopThinking {
            handoffAckCallId = callId
            if piJobs.hasActive {
                sendToolOutput(callId: callId, output: "{\"status\":\"stopping\"}")
                onStopThinking?()
            } else {
                sendToolOutput(callId: callId, output: "{\"status\":\"idle\"}")
            }
            return
        }
        let generation = toolScope.generation
        NSLog("[LiveVoice] tool call: %@ id=%@", name, callId)
        onToolActive?(name)
        let task = Task {
            let result = await VoiceToolExecutor.shared.run(name: name, argsJson: argsJson)
            guard !Task.isCancelled, !self.closed, self.toolScope.generation == generation else {
                NSLog("[LiveVoice] tool %@ dropping result pre-send: cancelled=%@ closed=%@ genMatch=%@",
                      name, "\(Task.isCancelled)", "\(!self.closed)", "\(self.toolScope.generation == generation)")
                return
            }
            NSLog("[LiveVoice] tool %@ done: output=%d chars", name, result.output.count)
            self.sendToolOutput(callId: callId, output: result.output)
            guard !Task.isCancelled, !self.closed, self.toolScope.generation == generation else {
                NSLog("[LiveVoice] tool %@ dropping follow-up post-send: cancelled=%@ closed=%@ genMatch=%@",
                      name, "\(Task.isCancelled)", "\(!self.closed)", "\(self.toolScope.generation == generation)")
                return
            }
            self.toolScope.finish(callId, generation: generation)
            self.onToolDone?(name, result.output)
            self.requestResponse()
            if self.toolScope.pendingIds.isEmpty {
                self.armFollowUpWatchdog(callId: callId, generation: generation)
            }
        }
        toolScope.insert(task, id: callId)
    }

    func sendToolOutput(callId: String, output: String) {
        send([
            "type": "conversation.item.create",
            "item": [
                "type": "function_call_output",
                "call_id": callId,
                "output": output,
            ] as [String: Any],
        ])
    }


    /// Ask the model to continue after a tool result.
    ///
    /// A tool that returns faster than the in-flight response finishes (a
    /// bridge miss answers immediately) would otherwise race it, and the server
    /// answers an overlapping create with
    /// `conversation_already_has_active_response`. Nothing retried that, so the
    /// turn died after the spoken acknowledgement and the tool result was never
    /// used — which also stops any fallback chain at its first rung. Defer
    /// instead, and flush on `response.done`.
    ///
    /// Parallel tools in one response must also wait for each other. Sending a
    /// follow-up after the first of several `web_search` results makes the
    /// model answer twice with the same content once the rest arrive.
    func requestResponse() {
        guard toolScope.pendingIds.isEmpty else {
            NSLog("[LiveVoice] follow-up waiting for %d remaining tool(s)", toolScope.pendingIds.count)
            return
        }
        guard activeResponseId.isEmpty else {
            NSLog("[LiveVoice] follow-up deferred (response %@ active)", activeResponseId)
            responseRequestPending = true
            return
        }
        if responseCreateRequested {
            NSLog("[LiveVoice] follow-up deferred (create already requested)")
            responseRequestPending = true
            return
        }
        NSLog("[LiveVoice] follow-up: sending response.create now")
        sendResponseCreate()
    }

    func sendResponseCreate() {
        guard !closed, !responseCreateRequested else {
            NSLog("[LiveVoice] sendResponseCreate skipped: closed=%@ alreadyRequested=%@",
                  "\(!closed)", "\(responseCreateRequested)")
            return
        }
        responseCreateRequested = true
        NSLog("[LiveVoice] sending response.create (follow-up)")
        send([
            "type": "response.create",
            "response": [:] as [String: Any],
        ])
    }

    /// Retry a tool follow-up once if the server never starts a response.
    /// Covers silent drops between tool completion and response.created
    /// (e.g. a create lost in an echo-cancel race). Stands down on any newer
    /// response, a superseding generation, or close. A duplicate create that
    /// arrives after the server already started one is rejected by the server
    /// and ignored by the client, so the retry is safe.
    func armFollowUpWatchdog(callId: String, generation: UUID) {
        let requestedAt = Date()
        DispatchQueue.main.asyncAfter(deadline: .now() + 10) { [weak self] in
            guard let self, !self.closed, self.toolScope.generation == generation else {
                NSLog("[LiveVoice] follow-up watchdog %@: stood down (superseded)", callId)
                return
            }
            guard self.lastResponseCreatedAt < requestedAt else {
                NSLog("[LiveVoice] follow-up watchdog %@: server responded, stood down", callId)
                return
            }
            NSLog("[LiveVoice] follow-up watchdog %@: no response in 10s, resending response.create", callId)
            self.responseCreateRequested = false
            self.responseRequestPending = false
            self.sendResponseCreate()
        }
    }

    func send(_ object: [String: Any]) {
        socket.send(object)
    }
}
