import Darwin

func check(
    _ condition: @autoclosure () -> Bool,
    _ message: @autoclosure () -> String = "",
    file: StaticString = #fileID,
    line: UInt = #line
) {
    if !condition() {
        let msg = message()
        let text = msg.isEmpty ? "CHECK FAILED \(file):\(line)\n" : "CHECK FAILED \(file):\(line): \(msg)\n"
        fputs(text, stderr)
        fflush(stderr)
        exit(1)
    }
}
