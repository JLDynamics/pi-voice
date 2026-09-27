// Does Voice Isolation reach the audio route this app actually uses?
//
// Voice.app captures through AVAudioEngine with Apple voice processing on.
// Microphone modes are documented on AVCaptureDevice, and the header warns
// the active mode "may differ from the preferredMicrophoneMode if the
// application's active audio route does not support" it. So the two values
// together answer the question, and only with the same audio route in place.
//
// macOS also only offers Mic Mode in Control Centre while something is
// actually capturing, so this holds the microphone open and watches the
// setting change, rather than reading it once from a process holding nothing.
//
//   xcrun swift macos/Voice/scripts/micmode.swift
//
// While it runs: open Control Centre, find Mic Mode, choose Voice Isolation.
// Audio is captured to drive the input, never recorded or written anywhere.
import AVFoundation

let WATCH_SECONDS = 40

func describe(_ mode: AVCaptureDevice.MicrophoneMode) -> String {
    switch mode {
    case .standard: return "standard"
    case .wideSpectrum: return "wideSpectrum"
    case .voiceIsolation: return "voiceIsolation"
    @unknown default: return "unknown(\(mode.rawValue))"
    }
}

let engine = AVAudioEngine()
let input = engine.inputNode
do {
    // Match Voice.app exactly: voice processing on, AGC off.
    try input.setVoiceProcessingEnabled(true)
    input.isVoiceProcessingAGCEnabled = false
    print("route:  voice-processing route (as Voice.app uses)")
} catch {
    print("route:  plain capture — voice processing unavailable: \(error.localizedDescription)")
}

// A tap is what makes this a real capture, so macOS treats us as an app that
// is using the microphone and offers Mic Mode in Control Centre. The buffer
// is discarded; nothing is stored.
input.installTap(onBus: 0, bufferSize: 4096, format: input.outputFormat(forBus: 0)) { _, _ in }

do {
    engine.prepare()
    try engine.start()
} catch {
    print("Could not open the microphone: \(error.localizedDescription)")
    print("Grant microphone access to your terminal and try again.")
    exit(1)
}

print("microphone is open for \(WATCH_SECONDS)s — nothing is recorded")
print("")
print("NOW: open Control Centre, click Mic Mode, choose Voice Isolation.")
print("(If Mic Mode is missing, this Mac does not offer it for this input.)")
print("")

var lastLine = ""
var sawIsolation = false
for tick in 0..<WATCH_SECONDS {
    let preferred = describe(AVCaptureDevice.preferredMicrophoneMode)
    let active = describe(AVCaptureDevice.activeMicrophoneMode)
    let line = "preferred=\(preferred)  active=\(active)"
    if line != lastLine {
        print(String(format: "[%2ds] %@", tick, line))
        lastLine = line
        if active == "voiceIsolation" { sawIsolation = true }
    }
    Thread.sleep(forTimeInterval: 1.0)
}

engine.stop()
input.removeTap(onBus: 0)

let preferred = describe(AVCaptureDevice.preferredMicrophoneMode)
let active = describe(AVCaptureDevice.activeMicrophoneMode)
print("")
if sawIsolation || active == "voiceIsolation" {
    print("APPLIED — Voice Isolation reaches this audio route.")
    print("Worth a real trial: use Voice with someone talking in another room.")
} else if preferred != "standard", preferred != active {
    print("CHOSEN BUT NOT APPLIED — preferred=\(preferred), active=\(active).")
    print("This app's audio route does not support it. The built-in path is closed.")
} else {
    print("NOT SEEN — Mic Mode never moved off standard.")
    print("Either it was not selected, or this Mac does not offer it for this input.")
}
