import AppKit
import Darwin
import Foundation

@MainActor
protocol HeadlessBackend: AnyObject {
    var onSpoken: ((String) -> Void)? { get set }
    var onAskPi: ((String, String) -> Void)? { get set }
    var onStopPi: (() -> Void)? { get set }
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

    func attach(session: SessionController, listenToStdin: Bool = true) {
        self.session = session
        spokenItemId = nil
        let priorFinal = session.backend.onUserFinal
        session.backend.onUserFinal = { [weak self] text, itemId in
            priorFinal?(text, itemId)
            self?.emit(["type": "heard", "text": text, "item_id": itemId ?? ""])
        }
        let priorAgentDelta = session.backend.onAgentDelta
        session.backend.onAgentDelta = { [weak self] text in
            priorAgentDelta?(text)
            guard let self, !text.isEmpty else { return }
            let id = self.spokenItemId ?? UUID().uuidString
            self.spokenItemId = id
            self.emit(["type": "spoken_delta", "text": text, "item_id": id])
        }
        let priorAgentDone = session.backend.onAgentDone
        session.backend.onAgentDone = { [weak self] in
            priorAgentDone?()
            self?.spokenItemId = nil
        }
        let priorSpeech = session.backend.onUserSpeechStarted
        session.backend.onUserSpeechStarted = { [weak self] in
            priorSpeech?()
            self?.emit(["type": "speech_started"])
        }
        let priorState = session.backend.onState
        session.backend.onState = { [weak self] state in
            priorState?(state)
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
        if let headless = session.backend as? HeadlessBackend {
            headless.onSpoken = { [weak self] text in
                guard let self else { return }
                let id = self.spokenItemId ?? UUID().uuidString
                self.spokenItemId = nil
                self.emit(["type": "spoken", "text": text, "item_id": id])
            }
            headless.onAskPi = { [weak self] id, brief in
                self?.emit(["type": "work", "id": id, "brief": brief])
            }
            headless.onStopPi = { [weak self] in
                self?.emit(["type": "stop_work"])
            }
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
        guard
            let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
            let type = object["type"] as? String
        else { return }
        if type == "quit" {
            session?.requestEnd()
            LocalServiceStarter.shared.stop()
            onTerminate()
            return
        }
        if type == "mute", let muted = object["muted"] as? Bool {
            session?.setMuted(muted)
            return
        }
        if type == "interrupt" {
            session?.interrupt()
            return
        }
        if type == "user" {
            let text = object["text"] as? String ?? ""
            session?.interrupt()
            (session?.backend as? HeadlessBackend)?.ingestUserText(text)
            return
        }
        if type == "result" {
            let id = object["id"] as? String ?? ""
            let speak = object["speak"] as? String ?? ""
            let full = object["full"] as? String ?? speak
            (session?.backend as? HeadlessBackend)?.postResult(id: id, speak: speak, full: full)
            return
        }
        if type == "job_update" {
            let id = object["id"] as? String ?? ""
            let status = object["status"] as? String ?? ""
            let note = object["note"] as? String
            guard !id.isEmpty, !status.isEmpty else { return }
            (session?.backend as? HeadlessBackend)?.updatePiJob(id: id, status: status, note: note)
        }
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
