// Transcribe one finalized utterance with Apple's on-device speech engine.
//
// Why a separate binary at all: SpeechAnalyzer and SpeechTranscriber are
// Swift-only (Speech.framework ships no Objective-C headers for them), so
// PyObjC cannot reach them. The older SFSpeechRecognizer is bridged but is the
// slower engine. This helper is the smallest thing that exposes the fast path
// to the Python pipeline.
//
// Protocol, deliberately dumb so the Python side stays simple:
//   stdin  raw little-endian 16-bit mono PCM at --sample-rate (no header)
//   stdout one JSON object, always, including for failures
//   status 0 when a transcript was produced (empty counts), 1 otherwise
//
// One process per spoken turn. That costs ~65ms of launch over keeping a
// process alive, and buys statelessness: no analyzer to reset between turns,
// nothing to restart when a turn goes wrong, no half-open pipe to detect.
//
// Audio never touches disk. It arrives on stdin and stays in memory, which is
// also why this does not use the framework's convenient file-based entry point.

import AVFoundation
import Foundation
import Speech

struct Output: Encodable {
    var text: String?
    var locale: String?
    var duration_s: Double?
    var error: String?
}

@available(macOS 26, *)
enum HelperError: Error, CustomStringConvertible {
    case unsupportedLocale(String, count: Int)
    case noAnalyzerFormat
    case cannotConvertAudio(String)
    case badPCM(Int)

    var description: String {
        switch self {
        case let .unsupportedLocale(id, count):
            return "locale \(id) is not one of the \(count) locales this Mac can transcribe"
        case .noAnalyzerFormat:
            return "the speech engine offered no audio format for this locale"
        case let .cannotConvertAudio(detail):
            return "could not convert the audio to the engine's format: \(detail)"
        case let .badPCM(bytes):
            return "stdin held \(bytes) bytes, which is not whole 16-bit samples"
        }
    }
}

@main
struct SpeechHelper {
    static func main() async {
        var localeID = "en-US"
        var sampleRate = 16000.0
        var mode = "transcribe"

        var args = Array(CommandLine.arguments.dropFirst())
        while let arg = args.first {
            args.removeFirst()
            switch arg {
            case "--locale":
                localeID = args.first ?? localeID
                if !args.isEmpty { args.removeFirst() }
            case "--sample-rate":
                sampleRate = Double(args.first ?? "") ?? sampleRate
                if !args.isEmpty { args.removeFirst() }
            case "--prepare", "--locales":
                mode = String(arg.dropFirst(2))
            case "-h", "--help":
                print("usage: speech-helper [--locale en-US] [--sample-rate 16000] [--prepare|--locales]")
                exit(0)
            default:
                emit(Output(error: "unknown argument \(arg)"))
                exit(1)
            }
        }

        guard #available(macOS 26, *) else {
            emit(Output(error: "native speech-to-text needs macOS 26 or newer"))
            exit(1)
        }

        switch mode {
        case "locales":
            let supported = await SpeechTranscriber.supportedLocales.map { $0.identifier(.bcp47) }.sorted()
            let installed = await SpeechTranscriber.installedLocales.map { $0.identifier(.bcp47) }.sorted()
            emitRaw(["supported": supported, "installed": installed])
            exit(0)
        case "prepare":
            do {
                let transcriber = try await module(for: localeID)
                try await install(for: transcriber)
                let installed = await SpeechTranscriber.installedLocales.map { $0.identifier(.bcp47) }
                emitRaw(["locale": localeID, "installed": installed.contains(localeID)])
                exit(0)
            } catch {
                emit(Output(error: String(describing: error)))
                exit(1)
            }
        default:
            break
        }

        do {
            let transcriber = try await module(for: localeID)
            try await install(for: transcriber)
            let pcm = FileHandle.standardInput.readDataToEndOfFile()
            let (text, seconds) = try await transcribe(pcm: pcm, sampleRate: sampleRate, transcriber: transcriber)
            emit(Output(text: text, locale: localeID, duration_s: seconds))
            exit(0)
        } catch {
            emit(Output(error: String(describing: error)))
            exit(1)
        }
    }

    // MARK: - engine

    @available(macOS 26, *)
    static func module(for localeID: String) async throws -> SpeechTranscriber {
        let locale = Locale(identifier: localeID)
        let supported = await SpeechTranscriber.supportedLocales
        let wanted = locale.identifier(.bcp47)
        guard supported.contains(where: { $0.identifier(.bcp47) == wanted }) else {
            throw HelperError.unsupportedLocale(localeID, count: supported.count)
        }
        // .transcription, not one of the progressive presets: the pipeline hands
        // over a finished utterance, so partial results would only be discarded.
        return SpeechTranscriber(locale: locale, preset: .transcription)
    }

    // First use of a language downloads its model. The Python side calls
    // --prepare at startup so a spoken turn never waits on this.
    @available(macOS 26, *)
    static func install(for transcriber: SpeechTranscriber) async throws {
        guard let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) else {
            return
        }
        try await request.downloadAndInstall()
    }

    @available(macOS 26, *)
    static func transcribe(
        pcm: Data,
        sampleRate: Double,
        transcriber: SpeechTranscriber
    ) async throws -> (String, Double) {
        guard pcm.count % 2 == 0 else { throw HelperError.badPCM(pcm.count) }
        let frames = pcm.count / 2
        let seconds = Double(frames) / sampleRate
        if frames == 0 { return ("", 0) }

        guard
            let inputFormat = AVAudioFormat(
                commonFormat: .pcmFormatInt16,
                sampleRate: sampleRate,
                channels: 1,
                interleaved: true
            ),
            let inputBuffer = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: AVAudioFrameCount(frames))
        else {
            throw HelperError.cannotConvertAudio("no \(Int(sampleRate))Hz mono input buffer")
        }
        inputBuffer.frameLength = AVAudioFrameCount(frames)
        pcm.withUnsafeBytes { raw in
            guard let src = raw.baseAddress, let dst = inputBuffer.int16ChannelData?[0] else { return }
            memcpy(dst, src, pcm.count)
        }

        guard let analyzerFormat = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
            throw HelperError.noAnalyzerFormat
        }
        let converted = try convert(inputBuffer, to: analyzerFormat)

        let analyzer = SpeechAnalyzer(modules: [transcriber])
        // Read results before the audio goes in: the sequence is finite and
        // finalized below, so anything emitted while nobody listens is lost.
        let collector = Task { () -> String in
            var text = ""
            for try await result in transcriber.results where result.isFinal {
                text += String(result.text.characters)
            }
            return text
        }

        let inputs = AsyncStream<AnalyzerInput>(bufferingPolicy: .unbounded) { continuation in
            continuation.yield(AnalyzerInput(buffer: converted))
            continuation.finish()
        }
        if let through = try await analyzer.analyzeSequence(inputs) {
            try await analyzer.finalizeAndFinish(through: through)
        } else {
            try await analyzer.finalizeAndFinishThroughEndOfInput()
        }

        let text = try await collector.value
        return (text.trimmingCharacters(in: .whitespacesAndNewlines), seconds)
    }

    // The engine picks its own format, which may not be the pipeline's 16 kHz,
    // so resampling happens here rather than being assumed away.
    @available(macOS 26, *)
    static func convert(_ buffer: AVAudioPCMBuffer, to format: AVAudioFormat) throws -> AVAudioPCMBuffer {
        if buffer.format == format { return buffer }
        guard let converter = AVAudioConverter(from: buffer.format, to: format) else {
            throw HelperError.cannotConvertAudio("no converter to \(format)")
        }
        let ratio = format.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 4096
        guard let output = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity) else {
            throw HelperError.cannotConvertAudio("no output buffer of \(capacity) frames")
        }

        var delivered = false
        var conversionError: NSError?
        let status = converter.convert(to: output, error: &conversionError) { _, inputStatus in
            if delivered {
                inputStatus.pointee = .endOfStream
                return nil
            }
            delivered = true
            inputStatus.pointee = .haveData
            return buffer
        }
        if status == .error || output.frameLength == 0 {
            throw HelperError.cannotConvertAudio(conversionError.map { $0.localizedDescription } ?? "empty result")
        }
        return output
    }

    // MARK: - output

    static func emit(_ output: Output) {
        let encoder = JSONEncoder()
        if let data = try? encoder.encode(output), let line = String(data: data, encoding: .utf8) {
            print(line)
        } else {
            print("{\"error\":\"could not encode the result\"}")
        }
    }

    static func emitRaw(_ payload: [String: Any]) {
        if let data = try? JSONSerialization.data(withJSONObject: payload),
            let line = String(data: data, encoding: .utf8)
        {
            print(line)
        }
    }
}
