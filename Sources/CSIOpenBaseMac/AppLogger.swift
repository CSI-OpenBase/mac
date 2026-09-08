import Foundation

final class AppLogger: @unchecked Sendable {
    private let lock = NSLock()
    private var handle: FileHandle?

    let fileURL: URL

    init() {
        let fileManager = FileManager.default
        let logsRoot = fileManager.urls(for: .libraryDirectory, in: .userDomainMask)
            .first!
            .appendingPathComponent("Logs/CSI OpenBase", isDirectory: true)
        try? fileManager.createDirectory(
            at: logsRoot,
            withIntermediateDirectories: true
        )

        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        fileURL = logsRoot.appendingPathComponent(
            "mac-\(formatter.string(from: Date())).log"
        )

        if !fileManager.fileExists(atPath: fileURL.path) {
            fileManager.createFile(atPath: fileURL.path, contents: nil)
        }
        handle = try? FileHandle(forWritingTo: fileURL)
        try? handle?.seekToEnd()
        write("mac", "CSI OpenBase macOS host starting (\(ProcessInfo.processInfo.operatingSystemVersionString))")
    }

    func write(_ source: String, _ message: String) {
        let clean = message.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return }

        let timestamp = ISO8601DateFormatter().string(from: Date())
        guard let data = "\(timestamp) [\(source)] \(clean)\n".data(using: .utf8) else {
            return
        }
        lock.lock()
        defer { lock.unlock() }
        try? handle?.write(contentsOf: data)
    }

    func close() {
        lock.lock()
        defer { lock.unlock() }
        try? handle?.synchronize()
        try? handle?.close()
        handle = nil
    }

    deinit {
        close()
    }
}
