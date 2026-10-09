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

    /// Record a changed Pi note as silent [STATUS] context. No follow-up, so it
    /// is not spoken. Insert only between turns, so it cannot cut off a sentence.
    func startPiProgressLoop(id: String) {
        piProgressTask?.cancel()
        lastPiProgressAt = Date()
        lastPiProgressNote = nil
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
                guard self.activeResponseId.isEmpty, !self.responseCreateRequested,
                      !self.audio.isPlaying, !self.userSpeechActive,
                      self.implicitTurnSince == nil else { continue }
                self.lastPiProgressAt = now
                self.lastPiProgressNote = note
                self.sendUserText(PiJobTracker.statusChannel(note: note ?? ""))
            }
        }
    }

    func appendPiJournal(_ line: String) {
        // Same line in Voice's stderr log, so one job id finds the handoff in
        // the journal, in this log, and in the extension's lines beside it.
        NSLog("[PiJob] %@", line)
        PiJobTracker.appendJournal(directory: PiJobTracker.journalDirectory(), line: line)
    }
}
