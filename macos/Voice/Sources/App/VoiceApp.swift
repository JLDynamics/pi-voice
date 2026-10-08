import AppKit
import Foundation

/// Voice has no window. Pi is the face; this process is only ears and mouth,
/// talking NDJSON over stdio through ``HeadlessBridge``.
///
/// The SwiftUI panel (orb, transcript, settings, saved chats) was removed once
/// Pi became the only way in — it was ~1800 lines that no longer ran. The
/// `--headless` argument selects headless tools (spawn_thinking, stop_thinking) in
/// LiveVoiceBackend and identifies Voice processes for orphan reaping.
@main
enum VoiceMain {
    /// `NSApplication.delegate` is weak, so the delegate is held here.
    @MainActor private static var delegate: AppDelegate?

    @MainActor
    static func main() {
        let app = NSApplication.shared
        let appDelegate = AppDelegate()
        delegate = appDelegate
        app.delegate = appDelegate
        app.run()
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let session: SessionController
    private var signalSources: [DispatchSourceSignal] = []

    private static let defaultWSURL = LocalService.voiceWebSocket

    private static func makeBackend() -> VoiceBackend {
        if UserDefaults.standard.bool(forKey: "voice.useMock") {
            return MockVoiceBackend()
        }
        let raw = UserDefaults.standard.string(forKey: "voice.wsUrl") ?? defaultWSURL.absoluteString
        let url = URL(string: raw) ?? defaultWSURL
        return LiveVoiceBackend(url: url)
    }

    override init() {
        self.session = SessionController(backend: AppDelegate.makeBackend())
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        setenv("VOICE_THINKER", "luna", 1)
        if let raw = ProcessInfo.processInfo.environment["VOICE_HISTORY"],
           let data = raw.data(using: .utf8),
           let turns = try? JSONSerialization.jsonObject(with: data) as? [[String: String]] {
            session.seedHistory(turns.compactMap { turn in
                guard let role = turn["role"], let text = turn["text"] else { return nil }
                return (role: role, text: text)
            })
        }
        unsetenv("VOICE_HISTORY")
        stopLauncherOnSignals()
        // No Dock icon and no menu bar presence: this is a background audio bridge.
        NSApp.setActivationPolicy(.accessory)
        HeadlessBridge.shared.attach(session: session)
        if !session.isLive {
            session.toggleSession()
        }
    }

    /// Pi escalates a slow `quit` to SIGTERM, and orphan reaping sends SIGTERM.
    /// The default action killed Voice without `applicationWillTerminate`, so the
    /// `run-browser.sh` it started (and the backend on :8766) kept running with
    /// no owner. Route those signals through the normal terminate path.
    private func stopLauncherOnSignals() {
        for sig in [SIGTERM, SIGINT, SIGHUP] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler {
                LocalServiceStarter.shared.stop()
                NSApp.terminate(nil)
            }
            source.resume()
            signalSources.append(source)
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        LocalServiceStarter.shared.stop()
        Task { await session.end() }
    }
}
