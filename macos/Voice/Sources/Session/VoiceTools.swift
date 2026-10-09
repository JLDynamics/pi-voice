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

/// The two tools headless Voice publishes in Pi mode (`VOICE_THINKER=luna`).
/// Names and the `brief` argument are part of the cross-language contract in
/// `contracts/pi-voice.json`; the Python prompt names them too. Lives here, not
/// in LiveVoiceBackend.swift, so `test.sh` compiles and checks it.
enum HeadlessTools {
    static let spawnThinking = "spawn_thinking"
    static let stopThinking = "stop_thinking"
    static let briefArgument = "brief"

    static var definitions: [[String: Any]] { [spawnThinkingTool, stopThinkingTool] }

    static let spawnThinkingTool: [String: Any] = [
        "type": "function",
        "name": spawnThinking,
        "description": "Hand real work to the open Pi session: files, PDFs, resumes, the shell, web research, code changes, what is on screen, controlling the computer (click, type, fill forms, navigate), or anything you are not sure about. You cannot see the screen or click yourself. Answer casual chat yourself when you already have the context; do not call this for that. A call while Pi is working steers it: the latest brief replaces the current task, so a correction does not need stop_thinking. Call again only when the task really changes, not when the user just confirms, repeats, says continue, rewords the same task, or asks how it is going (answer that from [STATUS]). Then say something like \"Okay, I've redirected Pi to that instead.\" Never say it is queued or runs after that or next. Include paths the user gave. Ask for a missing save destination. Returns immediately. Say one short acknowledgement in this same turn, then wait. Progress arrives as [STATUS] and the answer as [FINAL]. Do not claim you already did the work.",
        "parameters": [
            "type": "object",
            "properties": [
                briefArgument: [
                    "type": "string",
                    "description": "What Pi should do or change in the current task.",
                ],
            ],
            "required": [briefArgument],
        ] as [String: Any],
    ]

    /// Spoken cancel for the current handoff. Fire-and-forget, like the
    /// handoff itself, so the voice line can say what stopped. `/voice stop`
    /// still ends the call; this does not.
    static let stopThinkingTool: [String: Any] = [
        "type": "function",
        "name": stopThinking,
        "description": "Stop the Pi task that is running now, when the user explicitly asks you to stop, cancel, or says never mind. Does not end the voice conversation. A correction or follow-up is spawn_thinking, not this. Returns immediately. Say what you stopped.",
        "parameters": [
            "type": "object",
            "properties": [:] as [String: Any],
        ] as [String: Any],
    ]

    /// The trimmed `brief` of a `spawn_thinking` call, or "" when missing.
    static func spawnBrief(_ argsJson: String) -> String {
        guard let data = argsJson.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let brief = object[briefArgument] as? String
        else { return "" }
        return brief.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
