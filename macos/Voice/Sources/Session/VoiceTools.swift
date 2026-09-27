import Foundation

public struct VoiceToolResult: Sendable {
    public let output: String

    public init(output: String) {
        self.output = output
    }
}

/// Tools the app runs itself.
///
/// Research (`bash`, a curl the model writes) runs inside the Python server's
/// response loop, so a "let me check" is followed by the answer with no client
/// round trip. This executor owns nothing client-side any more: every tool
/// the model can call runs on the server. It still publishes the tool
/// definitions for `session.update`.
///
/// `read_page`, `search_chat_history`, `remember` and `forget` were removed
/// with the sidecar that served them. Pi owns web reading and memory now.
/// `screenshot` was removed: Luna no longer sees the screen.
public final class VoiceToolExecutor: @unchecked Sendable {
    public static let shared = VoiceToolExecutor()

    // Tool toggles in UserDefaults
    // Persist under legacy key "tools.web_search" so existing user settings are preserved.
    public var researchEnabled: Bool {
        get { UserDefaults.standard.object(forKey: "tools.web_search") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "tools.web_search") }
    }


    /// Tool names the server executes inside the response. Anything else the
    /// model calls is forwarded to `run(name:argsJson:)`.
    public static let serverSideTools: Set<String> = ["bash"]

    /// Definitions sent in `session.update`. Kept short on purpose: every word
    /// here is re-read by the model on every turn, and the *how* (when to
    /// search) lives in the server's system prompt, not in tool descriptions.
    public func activeToolDefinitions() -> [[String: Any]] {
        var defs = [[String: Any]]()
        if researchEnabled {
            defs.append(Self.tool(
                "bash",
                "Research the voice model runs itself in this reply. Use curl -sL to search or fetch a "
                    + "public page and strip HTML with python3. For who holds an office or a similar current "
                    + "fact, curl Wikipedia or a primary page — not a news feed. For latest news, use Google "
                    + "News RSS with when:1d and today's date, print pubDate, keep the last 24 hours. Search "
                    + "HTML often blocks curl; retry a primary page. "
                    + "No web_search tool. A page that needs a login cannot be read here; say so.",
                properties: [
                    "command": Self.string("A curl-based command. Pipes to python3/head/rg are fine."),
                    "timeout": ["type": "number", "description": "Seconds to wait. Default 15, max 30."],
                ],
                required: ["command"]
            ))
        }
        return defs
    }

    private static func tool(
        _ name: String, _ description: String,
        properties: [String: Any] = [:], required: [String] = []
    ) -> [String: Any] {
        [
            "type": "function", "name": name, "description": description,
            "parameters": ["type": "object", "properties": properties, "required": required] as [String: Any],
        ]
    }

    private static func string(_ description: String) -> [String: Any] {
        ["type": "string", "description": description]
    }

    /// No client-side tools remain. The `argsJson` parameter stays: the server
    /// always sends the call's arguments, and tests/test_tool_contract.py reads
    /// this signature to check the client dispatches exactly the client-side tools.
    public func run(name: String, argsJson: String) async -> VoiceToolResult {
        NSLog("[VoiceTools] executing tool name=\(name)")
        return VoiceToolResult(output: "\(name) runs on the server and is unavailable in this session. Answer without it and say you could not check.")
    }
}
