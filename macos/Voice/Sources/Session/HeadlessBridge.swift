import AppKit
import Darwin
import Foundation

@MainActor
protocol HeadlessBackend: AnyObject {
    var onSpoken: ((String) -> Void)? { get set }
    var onSpawnThinking: ((String, String) -> Void)? { get set }
    var onStopThinking: (() -> Void)? { get set }
    func ingestUserText(_ text: String)
    func postResult(id: String, speak: String, full: String)
    func updatePiJob(id: String, status: String, note: String?)
}

/// NDJSON pipe between headless Voice and the Pi `/voice` loop.
@MainActor
final class HeadlessBridge {
    static let shared = HeadlessBridge()

    private var session: SessionController?
    private var stdin: DispatchSourceRead?
    private var announcedReady = false
    private var spokenItemId: String?

    /// Injected sink for testing; if nil, writes to standardOutput.
    var emitSink: (([String: Any]) -> Void)?
    var onTerminate: (() -> Void) = { NSApp.terminate(nil) }
    /// Stops the backend launcher on `quit`. Injected in tests so a test never
    /// stops a real service.
    var onStopService: (() -> Void) = { LocalServiceStarter.shared.stop() }
    var onExit: ((Int32) -> Void) = { code in
        fflush(stdout)
        LocalServiceStarter.shared.stop()
        exit(code)
    }

    func attach(session: SessionController, listenToStdin: Bool = true) {
        self.session = session
        spokenItemId = nil

        session.onFatalStart = { [weak self] _ in
            self?.onExit(HeadlessExitCode.audioUnavailable)
        }
        session.onHeard = { [weak self] text, itemId in
            self?.emit(["type": "heard", "text": text, "item_id": itemId ?? ""])
        }
        session.onSpokenDelta = { [weak self] text in
            guard let self, !text.isEmpty else { return }
            let id = self.spokenItemId ?? UUID().uuidString
            self.spokenItemId = id
            self.emit(["type": "spoken_delta", "text": text, "item_id": id])
        }
        session.onAgentDone = { [weak self] in
            self?.spokenItemId = nil
        }
        session.onRequestError = { [weak self] message in
            self?.emit(["type": "request_error", "message": message])
        }
        session.onSpeechStarted = { [weak self] in
            self?.emit(["type": "speech_started"])
        }
        session.onStateChanged = { [weak self] state in
            switch state {
            case .listening:
                guard let self, !self.announcedReady else { return }
                self.announcedReady = true
                self.emit(["type": "ready"])
            case .failed(let message):
                self?.emit(["type": "error", "message": message])
            default:
                break
            }
        }
        session.onSpoken = { [weak self] text in
            guard let self else { return }
            let id = self.spokenItemId ?? UUID().uuidString
            self.spokenItemId = nil
            self.emit(["type": "spoken", "text": text, "item_id": id])
        }
        session.onSpawnThinking = { [weak self] id, brief in
            self?.emit(["type": "work", "id": id, "brief": brief])
        }
        session.onStopThinking = { [weak self] in
            self?.emit(["type": "stop_work"])
        }

        if listenToStdin {
            listenStdin()
        }
    }

    private func listenStdin() {
        let handle = FileHandle.standardInput
        let fd = handle.fileDescriptor
        let flags = fcntl(fd, F_GETFL)
        if flags >= 0 { _ = fcntl(fd, F_SETFL, flags | O_NONBLOCK) }
        let source = DispatchSource.makeReadSource(fileDescriptor: fd, queue: .main)
        var leftover = Data()
        source.setEventHandler { [weak self] in
            var buf = [UInt8](repeating: 0, count: 4096)
            let n = buf.withUnsafeMutableBytes { read(fd, $0.baseAddress, $0.count) }
            if n == 0 {
                self?.onTerminate()
                return
            }
            if n < 0 {
                if errno == EAGAIN || errno == EWOULDBLOCK { return }
                return
            }
            leftover.append(contentsOf: buf.prefix(n))
            while let range = leftover.range(of: Data([0x0A])) {
                let line = leftover.subdata(in: leftover.startIndex..<range.lowerBound)
                leftover.removeSubrange(leftover.startIndex...range.lowerBound)
                self?.handle(line: line)
            }
        }
        source.resume()
        stdin = source
    }

    func handle(line: String) {
        guard let data = line.data(using: .utf8) else { return }
        handle(line: data)
    }

    func handle(line: Data) {
        let command: HeadlessCommand
        switch HeadlessCommand.decode(line) {
        case .success(let decoded): command = decoded
        case .failure(let ignored):
            logIgnored(ignored.reason)
            return
        }
        switch command {
        case .quit:
            session?.requestEnd()
            onStopService()
            onTerminate()
        case .mute(let muted):
            session?.setMuted(muted)
        case .interrupt:
            session?.interrupt()
        case .user(let text):
            session?.interrupt()
            session?.ingestUserText(text)
        case .result(let id, let speak, let full):
            session?.postResult(id: id, speak: speak, full: full)
        case .jobUpdate(let id, let status, let note):
            session?.updatePiJob(id: id, status: status.rawValue, note: note)
        }
    }

    /// Reasons already written to stderr. A bad sender repeats itself; one line
    /// per distinct reason is enough to find it in `pi-voice.<pid>.stderr.log`.
    private var loggedIgnoreReasons = Set<String>()
    /// Test hook: every ignored line's reason, logged or not.
    var onIgnoredLine: ((String) -> Void)?

    private func logIgnored(_ reason: String) {
        onIgnoredLine?(reason)
        guard loggedIgnoreReasons.insert(reason).inserted else { return }
        NSLog("[HeadlessBridge] ignored stdin line: %@", reason)
    }

    func emit(_ object: [String: Any]) {
        if let emitSink {
            emitSink(object)
            return
        }
        guard let data = try? JSONSerialization.data(withJSONObject: object),
              let line = String(data: data, encoding: .utf8) else { return }
        FileHandle.standardOutput.write(Data((line + "\n").utf8))
    }
}

/// One stdin message from the extension, decoded and checked against
/// `contracts/pi-voice.json` `stdio.toVoice`. Anything else is ignored with a
/// reason instead of decoding to empty defaults.
enum HeadlessCommand: Equatable {
    case quit
    case mute(Bool)
    case interrupt
    case user(String)
    case result(id: String, speak: String, full: String)
    case jobUpdate(id: String, status: PiJobTracker.State, note: String?)

    struct Ignored: Error, Equatable { let reason: String }

    static func decode(_ line: Data) -> Result<HeadlessCommand, Ignored> {
        guard let any = try? JSONSerialization.jsonObject(with: line) else {
            return .failure(Ignored(reason: "not JSON"))
        }
        guard let object = any as? [String: Any] else { return .failure(Ignored(reason: "not a JSON object")) }
        guard let type = object["type"] as? String else { return .failure(Ignored(reason: "no string type")) }
        func string(_ key: String) -> Result<String, Ignored> {
            guard let value = object[key] as? String else {
                return .failure(Ignored(reason: "\(type): missing string \(key)"))
            }
            return .success(value)
        }
        switch type {
        case "quit": return .success(.quit)
        case "interrupt": return .success(.interrupt)
        case "mute":
            guard let muted = object["muted"] as? Bool else { return .failure(Ignored(reason: "mute: missing boolean muted")) }
            return .success(.mute(muted))
        case "user":
            return string("text").map { .user($0) }
        case "result":
            return string("id").flatMap { id in
                string("speak").map { speak in
                    // An older extension sent no `full`; the spoken text stands in.
                    .result(id: id, speak: speak, full: object["full"] as? String ?? speak)
                }
            }
        case "job_update":
            return string("id").flatMap { id in
                string("status").flatMap { raw in
                    guard !id.isEmpty, let status = PiJobTracker.State(rawValue: raw) else {
                        return .failure(Ignored(reason: "job_update: unknown status \(raw)"))
                    }
                    return .success(.jobUpdate(id: id, status: status, note: object["note"] as? String))
                }
            }
        default:
            return .failure(Ignored(reason: "unknown type \(type)"))
        }
    }
}
