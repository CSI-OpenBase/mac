import CryptoKit
import Darwin
import Foundation
import Security

struct BackendSession: Equatable {
    let baseURL: URL
    let token: String
    let nonce: String
}

@MainActor
final class BackendController {
    private let logger: AppLogger
    private let urlSession: URLSession
    private var process: Process?
    private var supervisorControl: Pipe?
    private var standardOutput: Pipe?
    private var standardError: Pipe?
    private var supervisorCommandSent = false
    private var expectedStop = false
    private var acceptsStarts = true

    var onUnexpectedExit: ((String) -> Void)?
    private(set) var currentSession: BackendSession?

    init(logger: AppLogger) {
        self.logger = logger
        let configuration = URLSessionConfiguration.ephemeral
        configuration.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        configuration.timeoutIntervalForRequest = 3
        configuration.timeoutIntervalForResource = 5
        configuration.connectionProxyDictionary = [:]
        urlSession = URLSession(configuration: configuration)
    }

    var isRunning: Bool {
        process?.isRunning == true
    }

    func start(
        workspaceURL: URL,
        progress: @escaping (String) -> Void
    ) async throws -> BackendSession {
        guard acceptsStarts else { throw CancellationError() }
        try await stop()
        guard acceptsStarts else { throw CancellationError() }
        try verifyWorkspace(workspaceURL)

        let backendURL = try resolveBackendURL()
        let launcherURL = try resolveLauncherURL()
        let port = try reserveLoopbackPort()
        let token = try randomSecret()
        let nonce = try randomSecret()
        let baseURL = URL(string: "http://127.0.0.1:\(port)/")!
        let session = BackendSession(baseURL: baseURL, token: token, nonce: nonce)

        let child = Process()
        child.executableURL = launcherURL
        child.arguments = [backendURL.path]
        child.currentDirectoryURL = backendURL.deletingLastPathComponent()

        let control = Pipe()
        child.standardInput = control
        supervisorControl = control
        supervisorCommandSent = false

        let output = Pipe()
        let error = Pipe()
        child.standardOutput = output
        child.standardError = error
        standardOutput = output
        standardError = error
        attachLogging(to: output, source: "backend")
        attachLogging(to: error, source: "backend")

        var environment = ProcessInfo.processInfo.environment
        environment.removeValue(forKey: "CSI_OPENBASE_SESSION_HOME")
        environment.removeValue(forKey: "LOCALAPPDATA")
        environment["CSI_OPENBASE_HOME"] = workspaceURL.path
        environment["CSI_OPENBASE_SESSION_HOME"] = try sessionHome(for: workspaceURL).path
        environment["CSI_OPENBASE_HOST"] = "127.0.0.1"
        environment["CSI_OPENBASE_PORT"] = String(port)
        environment["CSI_OPENBASE_DESKTOP_TOKEN"] = token
        environment["CSI_OPENBASE_INSTANCE_NONCE"] = nonce
        environment["CSI_OPENBASE_SESSION_SECRET"] = token
        environment["CSI_OPENBASE_PARENT_PID"] = String(getpid())
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        child.environment = environment

        expectedStop = false
        child.terminationHandler = { [weak self] terminatedProcess in
            let exitCode = terminatedProcess.terminationStatus
            Task { @MainActor [weak self] in
                self?.processDidExit(terminatedProcess, exitCode: exitCode)
            }
        }

        logger.write(
            "mac",
            "Launching \(backendURL.lastPathComponent) through the bundled supervisor on \(baseURL.absoluteString) with data home \(workspaceURL.path)"
        )
        progress("正在启动本地服务...")
        do {
            try child.run()
            process = child
            currentSession = session
            try await waitUntilHealthy(child, session: session, progress: progress)
            logger.write("mac", "Backend health check passed")
            return session
        } catch {
            let startupError = error
            logger.write("mac", "Backend startup failed: \(error.localizedDescription)")
            expectedStop = true
            do {
                try await terminate(child, force: false)
            } catch {
                logger.write(
                    "mac",
                    "Backend startup cleanup failed: \(error.localizedDescription)"
                )
                throw error
            }
            clearProcess(ifMatching: child)
            expectedStop = false
            throw startupError
        }
    }

    func stop() async throws {
        guard let child = process else {
            currentSession = nil
            return
        }

        guard child.isRunning else {
            clearProcess(ifMatching: child)
            expectedStop = false
            return
        }

        expectedStop = true
        let session = currentSession
        logger.write("mac", "Stopping backend process")

        if let session {
            let forceRequired: Bool
            do {
                forceRequired = try await requestGracefulShutdown(session)
            } catch {
                logger.write("mac", "Graceful shutdown unavailable: \(error.localizedDescription)")
                try await terminate(child, force: false)
                clearProcess(ifMatching: child)
                expectedStop = false
                return
            }
            if forceRequired {
                logger.write("mac", "Backend reported an active browser job; forcing shutdown")
                try await terminate(child, force: true)
            } else if !(await waitForSupervisorToExit(child, timeout: 5)) {
                logger.write("mac", "Backend accepted shutdown but did not exit before timeout")
                try await terminate(child, force: false)
            }
        } else {
            try await terminate(child, force: false)
        }

        clearProcess(ifMatching: child)
        expectedStop = false
    }

    func preventFutureStarts() {
        acceptsStarts = false
    }

    private func resolveBackendURL() throws -> URL {
        let environment = ProcessInfo.processInfo.environment
        var candidates: [URL] = []
#if DEBUG
        if let override = environment["CSI_OPENBASE_BACKEND"], !override.isEmpty {
            candidates.append(URL(fileURLWithPath: override))
        }
#endif

        if let resources = Bundle.main.resourceURL {
            let backendRoot = resources.appendingPathComponent("backend", isDirectory: true)
            candidates.append(
                backendRoot
                    .appendingPathComponent("CSI.OpenBase.Backend", isDirectory: true)
                    .appendingPathComponent("CSI.OpenBase.Backend")
            )
            candidates.append(backendRoot.appendingPathComponent("CSI.OpenBase.Backend"))
        }

        guard let candidate = candidates.first(where: {
            FileManager.default.isExecutableFile(atPath: $0.path)
        }) else {
            throw BackendError.backendMissing(candidates.map(\.path))
        }
        return candidate.resolvingSymlinksInPath()
    }

    private func resolveLauncherURL() throws -> URL {
        var candidates: [URL] = []
#if DEBUG
        let environment = ProcessInfo.processInfo.environment
        if let override = environment["CSI_OPENBASE_LAUNCHER"], !override.isEmpty {
            candidates.append(URL(fileURLWithPath: override))
        }
        if let executableDirectory = Bundle.main.executableURL?.deletingLastPathComponent() {
            candidates.append(executableDirectory.appendingPathComponent("CSIBackendLauncher"))
        }
#endif
        if let resources = Bundle.main.resourceURL {
            candidates.append(resources.appendingPathComponent("CSIBackendLauncher"))
        }

        guard let candidate = candidates.first(where: {
            FileManager.default.isExecutableFile(atPath: $0.path)
        }) else {
            throw BackendError.launcherMissing(candidates.map(\.path))
        }
        return candidate.resolvingSymlinksInPath()
    }

    private func verifyWorkspace(_ url: URL) throws {
        let fileManager = FileManager.default
        try fileManager.createDirectory(at: url, withIntermediateDirectories: true)

        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: url.path, isDirectory: &isDirectory),
              isDirectory.boolValue else {
            throw BackendError.workspaceUnavailable("所选路径不是目录。")
        }

        let probe = url.appendingPathComponent(".csi-write-\(UUID().uuidString).tmp")
        do {
            try Data().write(to: probe, options: .atomic)
            try fileManager.removeItem(at: probe)
        } catch {
            try? fileManager.removeItem(at: probe)
            throw BackendError.workspaceUnavailable(error.localizedDescription)
        }
    }

    private func sessionHome(for workspaceURL: URL) throws -> URL {
        let fileManager = FileManager.default
        let applicationSupport = fileManager.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first!
        let normalized = workspaceURL.resolvingSymlinksInPath().path
        let digest = SHA256.hash(data: Data(normalized.utf8))
        let key = digest.prefix(8).map { String(format: "%02x", $0) }.joined()
        let url = applicationSupport
            .appendingPathComponent("CSI OpenBase", isDirectory: true)
            .appendingPathComponent("sessions", isDirectory: true)
            .appendingPathComponent(key, isDirectory: true)
        try fileManager.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    private func waitUntilHealthy(
        _ child: Process,
        session: BackendSession,
        progress: @escaping (String) -> Void
    ) async throws {
        let deadline = Date().addingTimeInterval(60)
        var lastStatus: Int?

        while Date() < deadline {
            try Task.checkCancellation()
            guard child.isRunning else {
                throw BackendError.exited(child.terminationStatus)
            }

            do {
                var request = URLRequest(
                    url: session.baseURL.appendingPathComponent("health"),
                    cachePolicy: .reloadIgnoringLocalAndRemoteCacheData,
                    timeoutInterval: 2
                )
                request.setValue(session.token, forHTTPHeaderField: "X-CSI-Desktop-Token")
                let (data, response) = try await urlSession.data(for: request)
                if let http = response as? HTTPURLResponse {
                    lastStatus = http.statusCode
                    if http.statusCode == 200,
                       let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                       payload["status"] as? String == "ok",
                       payload["mode"] as? String == "local-archive",
                       payload["instance_nonce"] as? String == session.nonce {
                        return
                    }
                    if http.statusCode == 503 {
                        progress("本地服务已启动，正在等待后端就绪...")
                    }
                }
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                // The listener may not have bound the port yet.
            }

            try await Task.sleep(nanoseconds: 600_000_000)
        }

        if lastStatus == 503 {
            throw BackendError.unhealthy
        }
        throw BackendError.healthTimeout
    }

    private func requestGracefulShutdown(_ session: BackendSession) async throws -> Bool {
        var request = URLRequest(
            url: session.baseURL.appendingPathComponent("api/shutdown"),
            timeoutInterval: 5
        )
        request.httpMethod = "POST"
        request.setValue(session.token, forHTTPHeaderField: "X-CSI-Desktop-Token")
        let (data, response) = try await urlSession.data(for: request)
        guard let http = response as? HTTPURLResponse,
              (200..<300).contains(http.statusCode) else {
            throw BackendError.shutdownRejected
        }
        let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        return payload?["force_required"] as? Bool == true
    }

    private func terminate(_ child: Process, force: Bool) async throws {
        guard child.isRunning else { return }
        sendSupervisorCommand(force ? 0x4b : 0x54) // "K" or "T"
        if !(await waitForSupervisorToExit(child, timeout: 5)) {
            logger.write(
                "mac",
                "Backend supervisor did not exit after its process-group cleanup deadline"
            )
            throw BackendError.supervisorTimeout
        }
    }

    private func sendSupervisorCommand(_ command: UInt8) {
        guard !supervisorCommandSent, let control = supervisorControl else { return }
        supervisorCommandSent = true
        do {
            try control.fileHandleForWriting.write(contentsOf: Data([command]))
        } catch {
            logger.write("mac", "Could not write backend supervisor command: \(error.localizedDescription)")
        }
        try? control.fileHandleForWriting.close()
    }

    private func waitForSupervisorToExit(
        _ child: Process,
        timeout: TimeInterval
    ) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while child.isRunning, Date() < deadline {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        return !child.isRunning
    }

    private func attachLogging(to pipe: Pipe, source: String) {
        let logger = logger
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                handle.readabilityHandler = nil
                return
            }
            if let text = String(data: data, encoding: .utf8) {
                logger.write(source, text)
            }
        }
    }

    private func processDidExit(_ child: Process, exitCode: Int32) {
        guard process === child else { return }
        let wasExpected = expectedStop
        clearProcess(ifMatching: child)
        if !wasExpected {
            let message = "本地服务意外退出（代码 \(exitCode)）。请打开日志查看原因。"
            logger.write("mac", message)
            onUnexpectedExit?(message)
        }
    }

    private func clearProcess(ifMatching child: Process) {
        if let activeProcess = process, activeProcess !== child {
            return
        }
        standardOutput?.fileHandleForReading.readabilityHandler = nil
        standardError?.fileHandleForReading.readabilityHandler = nil
        try? supervisorControl?.fileHandleForWriting.close()
        supervisorControl = nil
        supervisorCommandSent = false
        standardOutput = nil
        standardError = nil
        if process === child {
            process = nil
        }
        currentSession = nil
    }

    private func randomSecret() throws -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        let status = bytes.withUnsafeMutableBytes { buffer in
            SecRandomCopyBytes(kSecRandomDefault, buffer.count, buffer.baseAddress!)
        }
        guard status == errSecSuccess else {
            throw BackendError.randomFailure
        }
        return bytes.map { String(format: "%02x", $0) }.joined()
    }

    private func reserveLoopbackPort() throws -> Int {
        let descriptor = socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else { throw BackendError.portUnavailable }
        defer { Darwin.close(descriptor) }

        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = in_port_t(0).bigEndian
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))

        let bindResult = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindResult == 0 else { throw BackendError.portUnavailable }

        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let nameResult = withUnsafeMutablePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.getsockname(descriptor, $0, &length)
            }
        }
        guard nameResult == 0 else { throw BackendError.portUnavailable }
        return Int(UInt16(bigEndian: address.sin_port))
    }
}

enum BackendError: LocalizedError {
    case backendMissing([String])
    case launcherMissing([String])
    case workspaceUnavailable(String)
    case portUnavailable
    case randomFailure
    case exited(Int32)
    case unhealthy
    case healthTimeout
    case shutdownRejected
    case supervisorTimeout

    var errorDescription: String? {
        switch self {
        case .backendMissing(let candidates):
            let checked = candidates.isEmpty ? "未生成候选路径" : candidates.joined(separator: "\n")
            return "未找到可执行的 CSI.OpenBase.Backend。已检查：\n\(checked)"
        case .launcherMissing(let candidates):
            let checked = candidates.isEmpty ? "未生成候选路径" : candidates.joined(separator: "\n")
            return "未找到后端进程 supervisor。已检查：\n\(checked)"
        case .workspaceUnavailable(let reason):
            return "无法使用所选工作目录：\(reason)"
        case .portUnavailable:
            return "无法分配本机回环端口。"
        case .randomFailure:
            return "无法生成安全的桌面会话密钥。"
        case .exited(let code):
            return "后端启动后立即退出（代码 \(code)）。请打开日志查看原因。"
        case .unhealthy:
            return "后端持续返回不可用状态。请打开日志查看诊断信息。"
        case .healthTimeout:
            return "等待本地服务响应超时。请确认应用包中的后端及浏览器文件完整。"
        case .shutdownRejected:
            return "后端未接受安全关闭请求。"
        case .supervisorTimeout:
            return "旧的本地服务未能在安全期限内退出。为避免两个实例同时访问工作目录，已拒绝重新启动；请退出应用后重试。"
        }
    }
}
