import Foundation

/// Start this checkout's voice backend before opening the microphone.
/// Custom service addresses remain externally managed.
///
/// There used to be a second service here — the FastAPI sidecar on `:7860`,
/// started ahead of the models so saved chats and memory loaded early. It was
/// removed with the sidecar itself, so this now supervises one process.
@MainActor
final class LocalServiceStarter {
    static let shared = LocalServiceStarter()
    /// `run-browser.sh`, which owns the voice backend it starts.
    private var launcher: Process?
    private var log: FileHandle?
    private var starting = false
    private var waiters: [CheckedContinuation<Void, Error>] = []

    /// Keep models warm between conversations, but release our own launcher
    /// when Voice quits. Its trap leaves externally started services alone.
    func stop() {
        starting = false
        let pending = waiters
        waiters = []
        pending.forEach { $0.resume(throwing: CancellationError()) }
        if launcher?.isRunning == true { launcher?.terminate() }
        launcher = nil
        try? log?.close()
        log = nil
    }

    static func manages(voice: URL) -> Bool {
        let local = ["localhost", "127.0.0.1"]
        return voice.scheme == "ws" && local.contains(voice.host ?? "") && voice.port == 8766
            && voice.path == "/v1/realtime"
    }

    /// What a probe of the local service found.
    enum ServiceStatus: Equatable {
        /// Running this checkout's current code, models loaded.
        case ready
        /// Current code, still loading.
        case starting
        /// Not current. The launcher decides whether replacement is safe;
        /// this probe is readiness-only, never proof of ownership.
        case stale
        /// Nothing answered.
        case unreachable
    }

    private func fetchJSON(_ url: URL) async -> (code: Int, json: [String: Any]?)? {
        var request = URLRequest(url: url)
        request.timeoutInterval = 0.8
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            return (code, try? JSONSerialization.jsonObject(with: data) as? [String: Any])
        } catch { return nil }
    }

    /// Whether a health payload describes this checkout's current code.
    /// Older servers report no fingerprint, so they cannot be current.
    static func runsCurrentCode(_ json: [String: Any], root: String?) -> Bool {
        guard let fingerprint = json["fingerprint"] as? String, !fingerprint.isEmpty,
              json["stale"] as? Bool == false,
              let source = json["source"] as? String, let root else { return false }
        return samePath(source, root)
    }

    static func samePath(_ a: String, _ b: String) -> Bool {
        URL(fileURLWithPath: a).resolvingSymlinksInPath().standardizedFileURL.path
            == URL(fileURLWithPath: b).resolvingSymlinksInPath().standardizedFileURL.path
    }

    static func voiceStatus(code: Int, json: [String: Any]?, root: String?) -> ServiceStatus {
        guard code == 200, let json, json["ready"] != nil else { return .stale }
        guard runsCurrentCode(json, root: root) else { return .stale }
        let ready = json["ready"] as? Bool == true
        return ready ? .ready : .starting
    }

    private func voiceStatus(_ voice: URL) async -> ServiceStatus {
        var address = URLComponents(url: voice, resolvingAgainstBaseURL: false)!
        address.scheme = "http"
        address.query = nil
        address.path = "/health"
        guard let healthURL = address.url, let reply = await fetchJSON(healthURL) else { return .unreachable }
        return Self.voiceStatus(code: reply.code, json: reply.json, root: try? repositoryRoot())
    }

    private func probe() async -> ServiceStatus {
        await voiceStatus(LocalService.voiceWebSocket)
    }

    func ensureReady(voice: URL) async throws {
        guard Self.manages(voice: voice) else { return }
        if await probe() == .ready { return }
        try await bringUp()
    }

    private func bringUp() async throws {
        try Task.checkCancellation()
        var state = await probe()
        if state == .ready { return }
        if starting {
            try await withCheckedThrowingContinuation { waiters.append($0) }
            state = await probe()
            if state == .ready { return }
        }
        starting = true
        defer {
            starting = false
            let pending = waiters
            waiters = []
            pending.forEach { $0.resume() }
        }
        if state == .stale {
            // The launcher replaces verified same-checkout stale listeners, but a launcher
            // of ours would otherwise keep supervising the ones it replaces.
            await stopOwnLauncher()
        }
        try spawnLauncher()
        try await waitUntilReady()
    }

    /// Terminate a launcher this app started and wait for it to exit; its
    /// EXIT trap stops the service it owns.
    private func stopOwnLauncher() async {
        guard let running = launcher, running.isRunning else {
            launcher = nil
            return
        }
        running.terminate()
        let deadline = Date().addingTimeInterval(15)
        while Date() < deadline, running.isRunning {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        launcher = nil
    }

    private func spawnLauncher() throws {
        if launcher?.isRunning == true { return }
        let path = try repositoryRoot()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [path + "/run-browser.sh", "--reuse-running"]
        process.currentDirectoryURL = URL(fileURLWithPath: path)
        var environment = ProcessInfo.processInfo.environment
        // Match the child's working directory so Bash need not reconstruct
        // it by walking protected parent directories on macOS.
        environment["PWD"] = path
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        environment["PORT"] = "8766"
        process.environment = environment
        let logURL = URL(fileURLWithPath: "/tmp/voice-service-startup.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        try? log?.close()
        log = try FileHandle(forWritingTo: logURL)
        process.standardOutput = log
        process.standardError = log
        try process.run()
        launcher = process
    }

    private func waitUntilReady() async throws {
        let deadline = Date().addingTimeInterval(180)
        while Date() < deadline {
            try Task.checkCancellation()
            if await probe() == .ready { return }
            if let child = launcher, !child.isRunning {
                if await probe() == .ready { return }
                throw StartupError("Local service startup failed. Check /tmp/voice-service-startup.log and /tmp/chatbot-server.log.")
            }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        throw StartupError("The local service is still starting. Check /tmp/chatbot-server.log, then try again.")
    }

    private func repositoryRoot() throws -> String {
        guard let location = Bundle.main.url(forResource: "RepositoryPath", withExtension: "txt"),
              let path = try? String(contentsOf: location, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
              FileManager.default.isExecutableFile(atPath: path + "/run-browser.sh") else {
            throw StartupError("Local services are stopped. Rebuild Voice from your project, or start run-browser.sh.")
        }
        return path
    }

    struct StartupError: LocalizedError {
        let message: String
        init(_ message: String) { self.message = message }
        var errorDescription: String? { message }
    }
}
