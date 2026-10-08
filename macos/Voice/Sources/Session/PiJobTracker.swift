import Foundation

/// Agent-side mirror of Pi's job lifecycle.
///
/// The voice model does not poll this. `spawn_thinking` starts work, or
/// steers the running job onto a new brief; the extension pushes `job_update` lines as phases change; `[STATUS]`
/// and `[FINAL]` channels are what Agent speaks. The payloads below stay so
/// tests can prove a result does not rewrite a stopped or failed job.
///
/// Pure value semantics and injected `now` throughout so RuntimeTests can
/// cover it without a backend.
struct PiJobTracker {
    enum State: String {
        case queued
        case working
        case done
        case stopped
        case superseded
        case dropped
        case failed
    }

    struct Job {
        var id: String
        var brief: String
        var state: State
        var startedAt: Date
        var updatedAt: Date
        var lastNote: String?
    }

    struct StoredResult {
        var id: String
        var brief: String
        var text: String
        var settledAt: Date
    }

    static let maxResults = 10
    static let statusExcerpt = 140
    static let defaultPageLimit = 1500
    static let maxPageLimit = 4000
    static let journalFileName = "pi-voice.jobs.jsonl"

    /// Whether a changed note is old enough to record as silent context.
    /// Unchanged time never qualifies: there is no repeating check-in.
    static func shouldSpeakProgress(elapsed: TimeInterval, changed: Bool) -> Bool {
        changed && elapsed >= 8
    }

    /// Progress channel. Context only; the caller must not request a follow-up.
    static func statusChannel(note: String) -> String {
        let body = note.trimmingCharacters(in: .whitespacesAndNewlines)
        let status = body.isEmpty ? "Pi is still working" : body
        return "[STATUS] \(status). Background progress for context only; do not speak it. This is not the user."
    }

    /// Failure is a status update of its own. `dropped` stays silent: mute
    /// discarded the handoff before Pi saw it.
    static func failureChannel(reason: String) -> String {
        let body = reason.trimmingCharacters(in: .whitespacesAndNewlines)
        let why = body.isEmpty ? "Pi could not complete the task" : body
        return "[STATUS] The task failed: \(why). Explain briefly what remains unresolved. This is not the user."
    }

    /// Final channel. A stopped, superseded, or failed job stays partial even
    /// when some findings arrived with it.
    static func finalChannel(excerpt: String, outcome: State?) -> String {
        let trimmed = excerpt.trimmingCharacters(in: .whitespacesAndNewlines)
        if let outcome, outcome == .stopped || outcome == .superseded || outcome == .failed {
            return "[FINAL] Partial findings from an incomplete task (\(outcome.rawValue)). Say what was found and what remains unresolved. This is not the user: \(trimmed)"
        }
        return "[FINAL] Pi finished. Relay the outcome in one or two sentences. This is not the user: \(trimmed)"
    }

    /// Codex's wording for a second handoff during an active one. The latest
    /// brief replaces the running task; nothing waits behind it.
    static let steerAck = "This was sent to steer the current Pi task. Pi is redirected to this brief and the earlier task is replaced."

    /// What the voice model gets back from `spawn_thinking`. A call while a job
    /// is active steers it (the extension marks the old id superseded), so the
    /// ack must not read like a second job lining up: Agent once told the user
    /// a new task was waiting behind the first when Pi had already dropped it.
    static func handoffAck(id: String, steering: Bool) -> [String: Any] {
        guard steering else {
            return [
                "status": "started", "id": id,
                "note": "Pi has the task. If you have not said so yet, say one short acknowledgement. Do not call spawn_thinking again for this.",
            ]
        }
        return [
            "status": "steering", "id": id,
            "note": "\(steerAck) If you have not said so yet, say one short line such as \"Okay, I've redirected Pi to that instead.\" Do not call spawn_thinking again for this.",
        ]
    }

    private(set) var jobs: [String: Job] = [:]
    private(set) var order: [String] = []
    private(set) var activeId: String?
    private(set) var results: [StoredResult] = []
    /// Latest job to reach a terminal state. Distinguishes "Pi failed" from
    /// "nothing ever started" for tests and the journal.
    private(set) var lastTerminal: Job?

    var hasActive: Bool {
        guard let id = activeId, let job = jobs[id] else { return false }
        return job.state == .queued || job.state == .working
    }

    /// Record a fresh `spawn_thinking`. Never supersedes locally: the extension
    /// owns that decision (it can drop the new work, e.g. while muted) and
    /// reports it back via `job_update`. Superseding here would strand a
    /// phantom job whenever the drop path runs.
    mutating func ask(id: String, brief: String, now: Date = Date()) {
        jobs[id] = Job(id: id, brief: brief, state: .queued, startedAt: now, updatedAt: now, lastNote: nil)
        order.append(id)
        if order.count > 64 { order.removeFirst(order.count - 64) }
        activeId = id
    }

    /// Fold an extension `job_update` into the mirror. Terminal states stick:
    /// a stale `working` arriving after a supersede must not resurrect the job.
    mutating func update(id: String, state: State, note: String?, now: Date = Date()) {
        guard var job = jobs[id] else { return }
        if isTerminal(job.state) { return }
        job.state = state
        job.updatedAt = now
        if let note = note?.trimmingCharacters(in: .whitespacesAndNewlines), !note.isEmpty {
            job.lastNote = String(note.prefix(200))
        }
        jobs[id] = job
        if isTerminal(state) {
            lastTerminal = job
            if activeId == id { activeId = nil }
        }
    }

    private func isTerminal(_ state: State) -> Bool {
        switch state {
        case .queued, .working:
            return false
        case .done, .stopped, .superseded, .dropped, .failed:
            return true
        }
    }

    /// Cache a finished result. Unknown ids (an older bridge) still store under
    /// their id with an empty brief. The voice model hears the `[FINAL]`
    /// excerpt; this cache is what keeps a partial outcome from becoming done.
    mutating func finish(id: String, brief: String? = nil, text: String, now: Date = Date()) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            update(id: id, state: .done, note: nil, now: now)
            return
        }
        let storedBrief = brief ?? jobs[id]?.brief ?? ""
        if var job = jobs[id], !isTerminal(job.state) {
            job.state = .done
            job.updatedAt = now
            jobs[id] = job
            lastTerminal = job
        }
        results.removeAll { $0.id == id }
        results.append(StoredResult(
            id: id,
            brief: storedBrief,
            text: trimmed,
            settledAt: now
        ))
        if results.count > Self.maxResults {
            results.removeFirst(results.count - Self.maxResults)
        }
        if activeId == id { activeId = nil }
    }

    /// What Pi is doing now, or the last finished job. Not a model tool.
    func statusPayload(now: Date = Date()) -> [String: Any] {
        if let id = activeId, let job = jobs[id],
           job.state == .queued || job.state == .working {
            var payload: [String: Any] = [
                "status": job.state == .working ? "working" : "queued",
                "id": job.id,
                "brief": Self.excerpt(job.brief, Self.statusExcerpt),
                "elapsed_s": max(0, Int(now.timeIntervalSince(job.startedAt))),
            ]
            if let note = job.lastNote, !note.isEmpty {
                payload["detail"] = note
            }
            return payload
        }
        if let terminal = lastTerminal {
            var info: [String: Any] = [
                "id": terminal.id,
                "brief": Self.excerpt(terminal.brief, Self.statusExcerpt),
                "ago_s": max(0, Int(now.timeIntervalSince(terminal.updatedAt))),
                "outcome": terminal.state.rawValue,
            ]
            if let note = terminal.lastNote, !note.isEmpty {
                info["detail"] = note
            }
            return ["status": "idle", "last": info]
        }
        if let last = results.last {
            return [
                "status": "idle",
                "last": [
                    "id": last.id,
                    "brief": Self.excerpt(last.brief, Self.statusExcerpt),
                    "ago_s": max(0, Int(now.timeIntervalSince(last.settledAt))),
                    "outcome": "done",
                ] as [String: Any],
            ]
        }
        return ["status": "idle"]
    }

    /// Paged read of a cached result, for tests. Defaults to the most recent
    /// result; unknown ids answer with the ids that were stored.
    func resultsPayload(id: String?, cursor: Int, limit: Int) -> [String: Any] {
        let target = id?.trimmingCharacters(in: .whitespacesAndNewlines)
        let stored: StoredResult?
        if let target, !target.isEmpty {
            stored = results.first { $0.id == target }
        } else {
            stored = results.last
        }
        guard let stored else {
            if let target, !target.isEmpty {
                return [
                    "error": "unknown_id",
                    "ids": results.suffix(5).map { $0.id },
                ]
            }
            return ["error": "no_results"]
        }
        let text = stored.text
        let start = max(0, min(cursor, text.count))
        let page = max(1, min(limit <= 0 ? Self.defaultPageLimit : limit, Self.maxPageLimit))
        let from = text.index(text.startIndex, offsetBy: start, limitedBy: text.endIndex) ?? text.endIndex
        let to = text.index(from, offsetBy: page, limitedBy: text.endIndex) ?? text.endIndex
        let next = text.distance(from: text.startIndex, to: to)
        return [
            "id": stored.id,
            "brief": Self.excerpt(stored.brief, Self.statusExcerpt),
            "total_chars": text.count,
            "cursor": next,
            "done": to == text.endIndex,
            "text": String(text[from..<to]),
        ]
    }

    static func excerpt(_ value: String, _ max: Int) -> String {
        let collapsed = value
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
        guard collapsed.count > max else { return collapsed }
        return String(collapsed.prefix(max)).trimmingCharacters(in: .whitespaces) + "…"
    }

    /// Compact elapsed for status payloads is seconds; this is the human form.
    static func elapsedString(since: Date, now: Date) -> String {
        let seconds = max(0, Int(now.timeIntervalSince(since)))
        if seconds < 60 { return "\(seconds)s" }
        return "\(seconds / 60)m\(seconds % 60)s"
    }

    // MARK: - Journal

    static func journalLine(now: Date, event: String, id: String, brief: String, extra: [String: Any] = [:]) -> String {
        var object: [String: Any] = [
            "t": ISO8601DateFormatter().string(from: now),
            "event": event,
            "id": id,
            "brief": excerpt(brief, statusExcerpt),
        ]
        for (key, value) in extra { object[key] = value }
        guard let data = try? JSONSerialization.data(withJSONObject: object),
              let line = String(data: data, encoding: .utf8) else { return "" }
        return line
    }

    /// Append one JSON line, trimming the file back to half once it passes
    /// maxBytes. A diagnostic log ("what did Pi say earlier?"), not the
    /// re-read path — finished text is served from memory.
    static func appendJournal(directory: URL, line: String, maxBytes: Int = 262_144) {
        guard !line.isEmpty else { return }
        let url = directory.appendingPathComponent(journalFileName)
        let data = Data((line + "\n").utf8)
        guard FileManager.default.fileExists(atPath: url.path) else {
            try? data.write(to: url)
            return
        }
        guard let handle = try? FileHandle(forWritingTo: url) else { return }
        defer { try? handle.close() }
        _ = try? handle.seekToEnd()
        try? handle.write(contentsOf: data)
        guard let size = try? handle.offset(), size > maxBytes,
              let all = try? Data(contentsOf: url), all.count > maxBytes else { return }
        let tail = all.suffix(maxBytes / 2)
        guard let newline = tail.firstIndex(of: 0x0A) else { return }
        try? Data(tail[tail.index(after: newline)...]).write(to: url)
    }
}
