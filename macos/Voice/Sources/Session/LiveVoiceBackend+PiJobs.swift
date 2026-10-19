import AVFoundation
import Darwin
import Foundation

/// Pi job channel: results, job updates from the extension, the silent progress loop and the journal.
/// Split out of LiveVoiceBackend.swift by role; stored state stays in the class.
extension LiveVoiceBackend {
    /// Pi's finished work, on the final channel for Agent to speak.
    ///
    /// Tagged, because it arrives on the same channel as the user's own words
    /// and is otherwise indistinguishable from them — the model would answer
    /// Pi's report as though the user had just said it. The voice prompt
    /// explains `[FINAL]`. The full text is cached so a stopped or failed job
    /// is not rewritten as done when findings arrive with it.
    func postResult(id: String, speak: String, full: String) {
        let trimmed = speak.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !closed, !id.isEmpty else { return }
        let complete = full.trimmingCharacters(in: .whitespacesAndNewlines)
        piJobs.finish(id: id, text: complete.isEmpty ? trimmed : complete)
        piProgressTask?.cancel()
        piProgressTask = nil
        appendPiJournal(PiJobTracker.journalLine(
            now: Date(), event: "result", id: id,
            brief: piJobs.jobs[id]?.brief ?? "",
            extra: ["chars": complete.count]
        ))
        let outcome = piJobs.jobs[id]?.state
        deliverContext(PiJobTracker.finalChannel(excerpt: trimmed, outcome: outcome), followUp: true)
    }

    /// Extension-pushed job phase (`job_update` over stdio). Feeds the local
    /// mirror the status channel speaks from. Only `failed` speaks on its
    /// own. `dropped` stays silent: the dispatch never reached Pi (muted), so
    /// there is no failure to report.
    func updatePiJob(id: String, status: String, note: String?) {
        guard let state = PiJobTracker.State(rawValue: status) else { return }
        let previousState = piJobs.jobs[id]?.state
        piJobs.update(id: id, state: state, note: note)
        guard piJobs.jobs[id]?.state == state, previousState != state || state == .working else { return }
        piProgressGeneration += 1
        if state == .stopped || state == .superseded || state == .dropped || state == .failed || state == .done {
            if !piJobs.hasActive { piProgressTask?.cancel(); piProgressTask = nil }
        }
        if state == .failed {
            deliverContext(PiJobTracker.failureChannel(reason: note ?? ""), followUp: true)
        }
        if state == .done || state == .stopped || state == .failed {
            appendPiJournal(PiJobTracker.journalLine(
                now: Date(), event: status, id: id,
                brief: piJobs.jobs[id]?.brief ?? ""
            ))
        }
    }

    /// Structured activity from the extension (`progress` over stdio). Codex
    /// renders its voice status from items, not prose; this keeps the tool
    /// name, the running count and a short argument summary so the status line
    /// can say "Editing 2 files +1 more" without asking the model.
    func updatePiProgress(id: String, tool: String, phase: String, summary: String, active: String) {
        guard let parsed = PiJobTracker.Phase(rawValue: phase) else { return }
        piJobs.progress(id: id, tool: tool, phase: parsed, summary: summary, active: active)
        piProgressGeneration += 1
    }

    /// Answer a `check_thinking` call from the job mirror.
    ///
    /// The extension can answer from Pi's session (it knows the live turn and
    /// the partial answer), and when it does the `status` line carries the
    /// real findings. Until it lands, the mirror is the best truth available:
    /// the model gets the phase and what Pi is doing, never an invented answer.
    func requestPiStatus(id: String?, request: String, callId: String) {
        // A model that asks twice in one response must not get two round trips:
        // answer the earlier call from the mirror now, then read once for this one.
        if pendingStatusCallId != nil { resolvePiStatusFromMirror() }
        pendingStatusCallId = callId
        pendingStatusRequest = request
        onStatusRequest?(id ?? "", request)
        // An extension that predates this tool cannot answer; the mirror still
        // holds the phase, and the model must never be left waiting on a
        // function_call_output that will not arrive.
        statusTimeout?.cancel()
        statusTimeout = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            guard !Task.isCancelled, let self, self.pendingStatusCallId == callId else { return }
            NSLog("[LiveVoice] check_thinking unanswered by the extension; answering from the mirror")
            self.resolvePiStatusFromMirror()
        }
    }

    /// The extension's answer to `check_thinking`; replies to the tool call.
    func resolvePiStatus(id: String, payloadJson: String) {
        statusTimeout?.cancel()
        statusTimeout = nil
        guard let callId = pendingStatusCallId else { return }
        pendingStatusCallId = nil
        pendingStatusRequest = nil
        sendToolOutput(callId: callId, output: payloadJson)
        toolScope.finish(callId, generation: toolScope.generation)
        requestFollowUp(callId: callId)
    }

    /// The extension could not answer (no live session, an old build). Fall back
    /// to the local mirror so the model still gets a truthful phase.
    func resolvePiStatusFromMirror() {
        guard let callId = pendingStatusCallId else { return }
        let request = pendingStatusRequest ?? ""
        pendingStatusCallId = nil
        pendingStatusRequest = nil
        let payload = piJobs.snapshotPayload(id: nil, request: request)
        sendToolOutput(callId: callId, output: Self.json(payload))
        toolScope.finish(callId, generation: toolScope.generation)
        requestFollowUp(callId: callId)
    }

    /// Record a changed Pi note as silent [STATUS] context. No follow-up, so it
    /// is not spoken. Insert only between turns, so it cannot cut off a sentence.
    ///
    /// Two paths feed it: the extension's structured `progress` (fast, on every
    /// tool transition) and this 3 s loop (a floor, so a long silent step still
    /// gets one check-in). Codex lets [COMMENTARY] flow freely; the throttle
    /// here only guards the echo/playback window, and both paths share
    /// `piProgressGeneration` so one note is never injected twice.
    func startPiProgressLoop(id: String) {
        piProgressTask?.cancel()
        lastPiProgressAt = Date()
        lastPiProgressNote = nil
        piProgressGeneration += 1
        piProgressTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                guard !Task.isCancelled, let self, !self.closed,
                      self.piJobs.activeId == id, self.piJobs.hasActive else { return }
                let now = Date()
                let elapsed = now.timeIntervalSince(self.lastPiProgressAt)
                let note = self.piJobs.jobs[id]?.lastNote
                let changed = note != nil && note != self.lastPiProgressNote
                guard PiJobTracker.shouldSpeakProgress(elapsed: elapsed, changed: changed) else { continue }
                guard self.canInjectContext else { continue }
                self.lastPiProgressAt = now
                self.lastPiProgressNote = note
                self.piProgressGeneration += 1
                self.sendUserText(PiJobTracker.statusChannel(note: note ?? ""))
            }
        }
    }

    /// Push a structured activity straight to the model as [STATUS].
    ///
    /// Unlike the loop this is not time-throttled: it fires when Pi starts or
    /// finishes a tool, which is exactly when the answer to "how is it going"
    /// changes. It still waits for a quiet window so it never cuts a sentence.
    func pushPiProgress(id: String, note: String) {
        guard !closed, piJobs.activeId == id, piJobs.hasActive else { return }
        let trimmed = note.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed != lastPiProgressNote else { return }
        guard canInjectContext else { return }
        lastPiProgressAt = Date()
        lastPiProgressNote = trimmed
        piProgressGeneration += 1
        sendUserText(PiJobTracker.statusChannel(note: trimmed))
    }

    /// Nothing is streaming, playing, or being spoken into: safe to insert
    /// context without stepping on a turn.
    var canInjectContext: Bool {
        activeResponseId.isEmpty && !responseCreateRequested && !audio.isPlaying
            && !userSpeechActive && implicitTurnSince == nil
    }

    func appendPiJournal(_ line: String) {
        // Same line in Voice's stderr log, so one job id finds the handoff in
        // the journal, in this log, and in the extension's lines beside it.
        NSLog("[PiJob] %@", line)
        PiJobTracker.appendJournal(directory: PiJobTracker.journalDirectory(), line: line)
    }
}
