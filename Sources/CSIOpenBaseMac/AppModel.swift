import AppKit
import Combine
import Foundation

@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var workspaceURL: URL
    @Published private(set) var session: BackendSession?
    @Published private(set) var statusMessage = "准备启动"
    @Published private(set) var isBusy = false
    @Published var alertMessage: String?
    @Published var navigationError: String? {
        didSet {
            if let navigationError {
                statusMessage = navigationError
            }
        }
    }

    private let logger: AppLogger
    private let workspaceStore: WorkspaceStore
    private let backend: BackendController
    private var hasStarted = false
    private var isShuttingDown = false

    init() {
        let logger = AppLogger()
        let workspaceStore = WorkspaceStore()
        let initialWorkspace = workspaceStore.load()
        self.logger = logger
        self.workspaceStore = workspaceStore
        workspaceURL = initialWorkspace.url
        backend = BackendController(logger: logger)
        if let warning = initialWorkspace.warning {
            statusMessage = warning.replacingOccurrences(of: "\n", with: " ")
            alertMessage = warning
            logger.write("mac", "Workspace persistence warning: \(warning)")
        }
        backend.onUnexpectedExit = { [weak self] message in
            guard let self else { return }
            self.session = nil
            self.statusMessage = message
            self.alertMessage = message
            let cleanup = DesktopCookieStore.shared.enqueueClear()
            Task { await cleanup.value }
        }
    }

    var canInteract: Bool {
        !isBusy && !isShuttingDown
    }

    var logURL: URL {
        logger.fileURL
    }

    func startOnce() async {
        guard !hasStarted else { return }
        hasStarted = true
        await restartBackend()
    }

    func restartBackend() async {
        guard !isBusy, !isShuttingDown else { return }
        isBusy = true
        navigationError = nil
        session = nil
        await DesktopCookieStore.shared.clear()
        guard !isShuttingDown else { return }
        statusMessage = "正在准备本地服务..."
        defer {
            if !isShuttingDown {
                isBusy = false
            }
        }

        do {
            let activeSession = try await backend.start(
                workspaceURL: workspaceURL,
                progress: { [weak self] message in
                    self?.statusMessage = message
                }
            )
            guard !isShuttingDown else {
                try? await backend.stop()
                return
            }
            session = activeSession
            statusMessage = "已连接本地服务"
        } catch is CancellationError {
            statusMessage = "启动已取消"
        } catch {
            let message = error.localizedDescription
            logger.write("mac", "Startup error: \(message)")
            statusMessage = message.replacingOccurrences(of: "\n", with: " ")
            alertMessage = message
        }
    }

    func chooseWorkspace() async {
        guard canInteract else { return }
        let panel = NSOpenPanel()
        panel.title = "选择 CSI OpenBase 工作目录"
        panel.message = "账号导出、视频档案和评论将保存在此目录。"
        panel.prompt = "选择"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = workspaceURL

        guard await panel.begin() == .OK,
              !isShuttingDown,
              let selectedURL = panel.url else {
            return
        }

        isBusy = true
        session = nil
        statusMessage = "正在切换工作目录..."
        do {
            try await backend.stop()
        } catch {
            let message = error.localizedDescription
            logger.write("mac", "Workspace change blocked by backend stop failure: \(message)")
            statusMessage = message
            alertMessage = message
            isBusy = false
            return
        }
        guard !isShuttingDown else { return }
        do {
            let selection = try workspaceStore.set(selectedURL)
            workspaceURL = selection.url
            logger.write("mac", "Data home changed to \(workspaceURL.path)")
            if let warning = selection.warning {
                logger.write("mac", "Workspace persistence warning: \(warning)")
                statusMessage = warning.replacingOccurrences(of: "\n", with: " ")
                alertMessage = warning
            }
        } catch {
            let message = error.localizedDescription
            logger.write("mac", "Workspace selection failed: \(message)")
            statusMessage = message
            alertMessage = message
            isBusy = false
            return
        }
        isBusy = false
        await restartBackend()
    }

    func openLog() {
        if !NSWorkspace.shared.open(logger.fileURL) {
            alertMessage = "无法打开日志：\(logger.fileURL.path)"
        }
    }

    func shutdown() async {
        guard !isShuttingDown else { return }
        isShuttingDown = true
        backend.preventFutureStarts()
        isBusy = true
        session = nil
        statusMessage = "正在关闭本地服务..."
        await DesktopCookieStore.shared.clear()
        do {
            try await backend.stop()
        } catch {
            logger.write(
                "mac",
                "Bounded shutdown ended before supervisor exit: \(error.localizedDescription)"
            )
        }
        workspaceStore.stopAccessing()
        logger.write("mac", "CSI OpenBase macOS host stopped")
        logger.close()
    }
}
