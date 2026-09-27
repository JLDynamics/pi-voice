import AVFoundation
import Foundation

/// Resample / PCM16 helpers. Called from the audio tap thread, so not MainActor.
final class PCMBridge: @unchecked Sendable {
    private let playLock = NSLock()
    private var playConverter: AVAudioConverter?
    private var playOutRate: Double = 0
    private let micRate: Double = 16_000
    private let ttsRate: Double = 24_000
    private let gainLock = NSLock()
    private var _micGain: Float = 3.0

    /// Mic input gain, read from defaults once per session (see start()).
    /// It used to be read inside micPCM16, i.e. a synchronized UserDefaults
    /// lookup on every mic-tap buffer (~50 Hz on the audio thread).
    var micGain: Float {
        get {
            gainLock.lock()
            defer { gainLock.unlock() }
            return _micGain
        }
        set {
            gainLock.lock()
            defer { gainLock.unlock() }
            _micGain = newValue
        }
    }

    func reset() {
        playLock.lock()
        playConverter = nil
        playOutRate = 0
        playLock.unlock()
    }

    func micPCM16(from buffer: AVAudioPCMBuffer) -> [UInt8]? {
        // No shared mutable state here; keep off the playback lock so the
        // ~50Hz mic tap never blocks audio deltas (and vice versa).

        let inFormat = buffer.format
        let inFrames = Int(buffer.frameLength)
        let chCount = Int(inFormat.channelCount)
        guard inFrames > 0, inFormat.sampleRate > 0, chCount > 0 else { return nil }

        // Calibrated mic input gain. macOS input volume is often set low and
        // browsers compensate with AGC; we get the raw tap, so apply a
        // modest software boost (with hard clamping below). Override with:
        //   defaults write dev.jldynamics.Voice voice.micGain -float 4.0
        // Cached per session — never read UserDefaults in this hot path.
        let gain = micGain
        let ratio = micRate / inFormat.sampleRate
        let outFrames = max(1, Int((Double(inFrames) * ratio).rounded()))
        var pcm = [Int16](repeating: 0, count: outFrames)

        func sample(at frame: Int) -> Float {
            let i = max(0, min(frame, inFrames - 1))
            if let floats = buffer.floatChannelData {
                var mixed: Float = 0
                for c in 0..<chCount { mixed += floats[c][i] }
                return mixed / Float(chCount)
            }
            if let ints = buffer.int16ChannelData {
                var mixed: Float = 0
                for c in 0..<chCount { mixed += Float(ints[c][i]) / Float(Int16.max) }
                return mixed / Float(chCount)
            }
            return 0
        }

        if abs(ratio - 1.0) < 0.001 {
            for i in 0..<outFrames {
                let s = max(-1, min(1, sample(at: i) * gain))
                pcm[i] = Int16(s * Float(Int16.max))
            }
        } else {
            for i in 0..<outFrames {
                let src = Double(i) / ratio
                let i0 = Int(src)
                let i1 = min(i0 + 1, inFrames - 1)
                let frac = Float(src - Double(i0))
                let mixed = sample(at: i0) * (1 - frac) + sample(at: i1) * frac
                let s = max(-1, min(1, mixed * gain))
                pcm[i] = Int16(s * Float(Int16.max))
            }
        }

        return pcm.withUnsafeBufferPointer { ptr in
            Array(UnsafeRawBufferPointer(start: ptr.baseAddress, count: outFrames * 2))
        }
    }

    func playbackBuffer(base64 b64: String, dest: AVAudioFormat) -> AVAudioPCMBuffer? {
        playLock.lock()
        defer { playLock.unlock() }

        guard dest.sampleRate > 0 else { return nil }
        guard let data = Data(base64Encoded: b64), data.count >= 2 else { return nil }
        let sampleCount = data.count / MemoryLayout<Int16>.size
        guard sampleCount > 0 else { return nil }

        guard let srcFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: ttsRate,
            channels: 1,
            interleaved: true
        ) else { return nil }

        guard let src = AVAudioPCMBuffer(
            pcmFormat: srcFormat,
            frameCapacity: AVAudioFrameCount(sampleCount)
        ) else { return nil }
        src.frameLength = AVAudioFrameCount(sampleCount)
        guard let dst = src.int16ChannelData?[0] else { return nil }
        data.copyBytes(
            to: UnsafeMutableRawBufferPointer(start: UnsafeMutableRawPointer(dst), count: sampleCount * 2)
        )

        if playConverter == nil || abs(playOutRate - dest.sampleRate) > 0.5 {
            playConverter = AVAudioConverter(from: srcFormat, to: dest)
            playOutRate = dest.sampleRate
        }
        guard let converter = playConverter else { return nil }

        let ratio = dest.sampleRate / ttsRate
        let outFrames = AVAudioFrameCount(Double(sampleCount) * ratio) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: dest, frameCapacity: outFrames) else { return nil }

        var error: NSError?
        var consumed = false
        converter.convert(to: out, error: &error) { _, status in
            if consumed {
                status.pointee = .noDataNow
                return nil
            }
            consumed = true
            status.pointee = .haveData
            return src
        }
        if error != nil || out.frameLength == 0 { return nil }
        return out
    }

}
