import Foundation

/// Single source of truth for local service URLs.
///
/// Voice talks to one local server: the realtime voice backend over a
/// WebSocket. The FastAPI sidecar that used to sit beside it on `:7860` was
/// removed — Pi owns web reading and memory now.
///
/// The default matches the port `run-browser.sh` uses and can be pointed
/// elsewhere with a user default, so Voice can be run against a second stack
/// on an offset port without disturbing the everyday one:
///
///     defaults write dev.jldynamics.Voice voice.wsUrl "ws://127.0.0.1:8866/v1/realtime"
public enum LocalService {
    static let defaultVoiceWebSocket = "ws://127.0.0.1:8766/v1/realtime"

    private static func url(overrideKey: String, fallback: String) -> URL {
        guard let raw = UserDefaults.standard.string(forKey: overrideKey)?
            .trimmingCharacters(in: .whitespacesAndNewlines),
            !raw.isEmpty,
            let overridden = URL(string: raw)
        else {
            return URL(string: fallback)!
        }
        return overridden
    }

    public static var voiceWebSocket: URL {
        url(overrideKey: "voice.wsUrl", fallback: defaultVoiceWebSocket)
    }
}
