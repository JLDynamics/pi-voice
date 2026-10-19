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
        /// Thinking effort requested for this job (`enums.thinkingEffort`).
        var effort: String
        /// Structured activity for the live status line: the last tool Pi
        /// touched and every tool still running. Codex renders its voice
        /// status from items like this instead of a prose note, so "+2 more"
        /// and the tool's own name survive without a model round trip.
        var tool: String?
        var phase: Phase?
        var active: [String] = []
    }

    enum Phase: String {
        case start
        case update
        case end
        case error
    }

    /// How hard Pi thinks on one job, the levels the voice model may pick.
    /// Matches `enums.thinkingEffort` in `contracts/pi-voice.json`. Codex sends
    /// the same choice per handoff as `backend_thinking_effort`; the difference
    /// is that pi's only hook is the session-wide thinking level, so the
    /// extension applies it at the start of the job and restores it after.
    static let efforts = ["off", "minimal", "low", "medium", "high"]
    /// The fast default. A voice caller is waiting, and most jobs are lookups.
    static let defaultEffort = "off"

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
    /// Characters of Pi's answer that `check_thinking` hands the voice model
    /// while the job is still running. The whole answer is cached for `[FINAL]`.
    static let snapshotHead = 900
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

    /// Human label for one structured activity, the way Codex labels its
    /// in-progress item ("Running command", "Editing 3 files").
    static func activityLabel(tool: String, phase: Phase, activeCount: Int, summary: String) -> String {
        let detail = summary.trimmingCharacters(in: .whitespacesAndNewlines)
        let base: String
        switch tool {
        case "web_search", "source_check": base = "searching the web"
        case "fetch_content", "get_search_content": base = "reading sources"
        case "read", "grep", "find", "ls": base = "reading files"
        case "edit", "write", "apply_patch": base = "editing files"
        case "bash": base = "running a command"
        default: base = "using a tool"
        }
        var label: String
        if phase == .end { label = "finished \(base)" }
        else if phase == .error { label = "\(base) failed" }
        else { label = base }
        if !detail.isEmpty { label += " (\(detail))" }
        if activeCount > 1 { label += " + \(activeCount - 1) more" }
        return "Pi is \(label)"
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
    static func handoffAck(id: String, steering: Bool, parallel: Bool = false) -> [String: Any] {
        // Parallel workers: a second brief is its own job running beside the
        // first, so the ack must not say the earlier task was replaced — it was
        // not. Codex has the same distinction between create_thread and steering.
        if steering && parallel {
            return [
                "status": "started_alongside", "id": id,
                "note": "Pi is now running this as a second job, alongside the one already working. Say you have started it too; do not say anything was replaced. Both answers will arrive separately.",
            ]
        }
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

    /// How soon after a job starts a near-identical brief counts as a repeat.
    static let repeatWindow: TimeInterval = 30

    /// What `spawn_thinking` returns for a repeat of the running task. Nothing
    /// reaches Pi, so its in-flight work (a web search, say) keeps going.
    static func repeatAck(id: String) -> [String: Any] {
        [
            "status": "already_working", "id": id,
            "note": "Pi is already on this same task, so nothing was sent and its work continues. If you have not said so yet, say briefly that Pi is still on it. Do not call spawn_thinking again for this.",
        ]
    }

    /// Lowercased words and numbers, the comparison unit for `sameTask`.
    static func taskWords(_ text: String) -> Set<String> {
        Set(text.lowercased().split { !($0.isLetter || $0.isNumber) }.map(String.init))
    }

    /// Words whose presence never changes what a brief asks for.
    static let fillerWords: Set<String> = [
        "a", "an", "the", "and", "or", "for", "from", "of", "to", "in", "on", "at", "with", "about",
        "please", "me", "my", "us", "our", "it", "this", "that", "is", "are", "can", "could", "you",
        "just", "again", "now", "still", "ok", "okay", "yes", "yeah", "so", "then", "do", "go", "ahead",
    ]

    /// Two briefs ask for the same thing when they differ only in case,
    /// punctuation, word order, or filler words. "Search the latest AI news."
    /// and "search for the latest AI news" match; "AI news" and "area news" do
    /// not, because one real word changed.
    static func sameTask(_ a: String, _ b: String) -> Bool {
        let x = taskWords(a).subtracting(fillerWords), y = taskWords(b).subtracting(fillerWords)
        guard !x.isEmpty else { return false }
        return x == y
    }

    /// The active job's id when `brief` repeats it within `repeatWindow` of its
    /// start. A small reword, "continue", or a confirmation that the voice model
    /// still turned into a handoff would otherwise steer Pi onto the same task
    /// and throw away the work in progress. A real change still steers.
    func activeRepeat(of brief: String, now: Date = Date()) -> String? {
        guard let id = activeId, let job = jobs[id],
              job.state == .queued || job.state == .working,
              now.timeIntervalSince(job.startedAt) <= Self.repeatWindow,
              Self.sameTask(job.brief, brief) else { return nil }
        return id
    }

    var hasActive: Bool {
        guard let id = activeId, let job = jobs[id] else { return false }
        return job.state == .queued || job.state == .working
    }

    /// Record a fresh `spawn_thinking`. Never supersedes locally: the extension
    /// owns that decision (it can drop the new work, e.g. while muted) and
    /// reports it back via `job_update`. Superseding here would strand a
    /// phantom job whenever the drop path runs.
    mutating func ask(id: String, brief: String, effort: String = PiJobTracker.defaultEffort, now: Date = Date()) {
        let level = Self.efforts.contains(effort) ? effort : Self.defaultEffort
        jobs[id] = Job(
            id: id, brief: brief, state: .queued, startedAt: now, updatedAt: now,
            lastNote: nil, effort: level
        )
        order.append(id)
        if order.count > 64 { order.removeFirst(order.count - 64) }
        activeId = id
    }
    /// Fold an extension `job_update` into the mirror. Terminal states stick:
    /// a stale `working` arriving after a supersede must not resurrect the job.
    /// One exception: `superseded -> done`, sent when Pi had already finished
    /// the old job's answer before the redirect, so it is complete, not partial.
    mutating func update(id: String, state: State, note: String?, now: Date = Date()) {
        guard var job = jobs[id] else { return }
        if isTerminal(job.state), !(job.state == .superseded && state == .done) { return }
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

    /// Fold one structured `progress` line: remember the last tool and keep the
    /// set of tools still running, so the status line can count them.
    mutating func progress(id: String, tool: String, phase: Phase, summary: String, active: String, now: Date = Date()) {
        guard var job = jobs[id], !isTerminal(job.state) else { return }
        let name = tool.trimmingCharacters(in: .whitespacesAndNewlines)
        if !name.isEmpty {
            job.tool = name
            job.phase = phase
            job.lastNote = Self.activityLabel(
                tool: name, phase: phase,
                activeCount: max(1, active.split(separator: ",").count),
                summary: summary
            )
        }
        job.active = active.split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        job.updatedAt = now
        jobs[id] = job
    }

    /// What `check_thinking` answers: progress plus whatever Pi has said so
    /// far, without touching the job. Codex's voice model reads thread state;
    /// this is the read-only half of that, kept to one bounded string.
    func snapshotPayload(id: String?, request: String, now: Date = Date()) -> [String: Any] {
        let asked = id?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let target = asked.isEmpty ? activeId : asked
        var payload: [String: Any] = ["request": request]
        if let target, let job = jobs[target] {
            payload["id"] = job.id
            payload["brief"] = Self.excerpt(job.brief, Self.statusExcerpt)
            payload["status"] = isTerminal(job.state) ? "settled" : job.state.rawValue
            payload["elapsed_s"] = max(0, Int(now.timeIntervalSince(job.startedAt)))
            payload["outcome"] = job.state.rawValue
            payload["effort"] = job.effort
            if let note = job.lastNote, !note.isEmpty { payload["doing"] = note }
            if !job.active.isEmpty { payload["running"] = job.active }
            if let stored = results.last(where: { $0.id == job.id }) {
                payload["answer"] = Self.excerpt(stored.text, Self.snapshotHead)
                payload["answer_chars"] = stored.text.count
            } else if let note = job.lastNote, !note.isEmpty {
                payload["answer"] = note
            }
            if !isTerminal(job.state) {
                payload["note"] = "Relay this as a short spoken update in your own words; it is not finished. Do not call stop_thinking for a status question."
            }
            return payload
        }
        if let last = lastTerminal {
            payload["status"] = "settled"
            payload["id"] = last.id
            payload["outcome"] = last.state.rawValue
            payload["brief"] = Self.excerpt(last.brief, Self.statusExcerpt)
            payload["ago_s"] = max(0, Int(now.timeIntervalSince(last.updatedAt)))
            if let stored = results.last(where: { $0.id == last.id }) {
                payload["answer"] = Self.excerpt(stored.text, Self.snapshotHead)
                payload["answer_chars"] = stored.text.count
            }
            return payload
        }
        payload["status"] = "idle"
        payload["note"] = "Nothing has been handed to Pi in this call."
        return payload
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

    /// Foundation ignores TMPDIR on macOS; match the extension's lease/stderr paths.
    static func journalDirectory(environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        if let tmpdir = environment["TMPDIR"]?.trimmingCharacters(in: .whitespacesAndNewlines), !tmpdir.isEmpty {
            return URL(fileURLWithPath: tmpdir, isDirectory: true)
        }
        return FileManager.default.temporaryDirectory
    }

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
    /// First journal write failure is logged; later ones stay quiet (a full
    /// or read-only TMPDIR fails every write the same way).
    nonisolated(unsafe) private static var journalFailureLogged = false

    static func appendJournal(directory: URL, line: String, maxBytes: Int = 262_144) {
        guard !line.isEmpty else { return }
        let url = directory.appendingPathComponent(journalFileName)
        let data = Data((line + "\n").utf8)
        do {
            guard FileManager.default.fileExists(atPath: url.path) else {
                try data.write(to: url)
                return
            }
            let handle = try FileHandle(forWritingTo: url)
            defer { try? handle.close() }
            try handle.seekToEnd()
            try handle.write(contentsOf: data)
            let size = try handle.offset()
            guard size > maxBytes else { return }
            let all = try Data(contentsOf: url)
            guard all.count > maxBytes else { return }
            let tail = all.suffix(maxBytes / 2)
            guard let newline = tail.firstIndex(of: 0x0A) else { return }
            try Data(tail[tail.index(after: newline)...]).write(to: url)
        } catch {
            guard !journalFailureLogged else { return }
            journalFailureLogged = true
            NSLog("[PiJob] journal write failed at %@: %@", url.path, error.localizedDescription)
        }
    }
}
