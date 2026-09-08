import AppKit
import Foundation
import SwiftUI
import WebKit

@MainActor
struct LocalWebView: NSViewRepresentable {
    let session: BackendSession
    @Binding var navigationError: String?

    func makeCoordinator() -> Coordinator {
        Coordinator(navigationError: $navigationError)
    }

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()

        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.allowsMagnification = true
        context.coordinator.webView = webView
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        context.coordinator.navigationError = $navigationError
        guard context.coordinator.loadedNonce != session.nonce,
              context.coordinator.pendingNonce != session.nonce else {
            return
        }
        context.coordinator.load(session, in: webView)
    }

    static func dismantleNSView(_ webView: WKWebView, coordinator: Coordinator) {
        webView.stopLoading()
        webView.navigationDelegate = nil
        webView.uiDelegate = nil
        coordinator.invalidate()
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        weak var webView: WKWebView?
        var navigationError: Binding<String?>
        private(set) var loadedNonce: String?
        private(set) var pendingNonce: String?
        private var session: BackendSession?
        private var loadTask: Task<Void, Never>?

        init(navigationError: Binding<String?>) {
            self.navigationError = navigationError
        }

        func load(_ session: BackendSession, in webView: WKWebView) {
            self.session = session
            pendingNonce = session.nonce
            loadTask?.cancel()
            loadTask = Task { @MainActor [weak self, weak webView] in
                let result = await DesktopCookieStore.shared.install(session)
                guard let self,
                      !Task.isCancelled,
                      let webView,
                      self.webView === webView,
                      self.session?.nonce == session.nonce,
                      self.pendingNonce == session.nonce else {
                    return
                }

                switch result {
                case .installed:
                    self.loadedNonce = session.nonce
                    self.pendingNonce = nil
                    var request = URLRequest(
                        url: session.baseURL,
                        cachePolicy: .reloadIgnoringLocalAndRemoteCacheData,
                        timeoutInterval: 10
                    )
                    // This header authenticates only the bootstrap request. Later
                    // navigation uses the HttpOnly cookie installed above.
                    request.setValue(
                        session.token,
                        forHTTPHeaderField: "X-CSI-Desktop-Token"
                    )
                    webView.load(request)
                case .superseded:
                    self.pendingNonce = nil
                    self.loadTask = Task { @MainActor [weak self, weak webView] in
                        await Task.yield()
                        guard let self,
                              let webView,
                              self.webView === webView,
                              self.session?.nonce == session.nonce,
                              self.loadedNonce != session.nonce,
                              self.pendingNonce == nil else {
                            return
                        }
                        self.load(session, in: webView)
                    }
                case .invalidCookie:
                    self.report("无法建立安全的本地网页会话。")
                }
            }
        }

        func invalidate() {
            loadTask?.cancel()
            loadTask = nil
            session = nil
            pendingNonce = nil
            loadedNonce = nil
            webView = nil
            DesktopCookieStore.shared.enqueueClear()
        }

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            guard let url = navigationAction.request.url else {
                decisionHandler(.cancel)
                return
            }
            if isAllowedLocalURL(url) || url.scheme == "about" {
                decisionHandler(.allow)
                return
            }
            if url.scheme == "http" || url.scheme == "https" {
                NSWorkspace.shared.open(url)
            }
            decisionHandler(.cancel)
        }

        func webView(
            _ webView: WKWebView,
            didFail navigation: WKNavigation!,
            withError error: Error
        ) {
            report("本地页面加载失败：\(error.localizedDescription)")
        }

        func webView(
            _ webView: WKWebView,
            didFailProvisionalNavigation navigation: WKNavigation!,
            withError error: Error
        ) {
            report("无法连接本地服务：\(error.localizedDescription)")
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            report("网页组件意外退出，请点击重新启动。")
        }

        func webView(
            _ webView: WKWebView,
            createWebViewWith configuration: WKWebViewConfiguration,
            for navigationAction: WKNavigationAction,
            windowFeatures: WKWindowFeatures
        ) -> WKWebView? {
            if let url = navigationAction.request.url {
                if isAllowedLocalURL(url) {
                    webView.load(navigationAction.request)
                } else if url.scheme == "http" || url.scheme == "https" {
                    NSWorkspace.shared.open(url)
                }
            }
            return nil
        }

        private func isAllowedLocalURL(_ url: URL) -> Bool {
            guard let session else { return false }
            return url.scheme == "http"
                && url.host == "127.0.0.1"
                && url.port == session.baseURL.port
        }

        private func report(_ message: String) {
            navigationError.wrappedValue = message
        }
    }
}

enum DesktopCookieInstallResult {
    case installed
    case superseded
    case invalidCookie
}

@MainActor
final class DesktopCookieStore {
    static let shared = DesktopCookieStore()

    private let cookieStore = WKWebsiteDataStore.default().httpCookieStore
    private var generation: UInt64 = 0
    private var activeNonce: String?
    private var operationTail: Task<Void, Never>?

    private init() {}

    func install(_ session: BackendSession) async -> DesktopCookieInstallResult {
        generation &+= 1
        let requestedGeneration = generation
        activeNonce = session.nonce
        let predecessor = operationTail
        let operation = Task { @MainActor [weak self] in
            await predecessor?.value
            guard let self,
                  self.isCurrent(requestedGeneration, nonce: session.nonce) else {
                return DesktopCookieInstallResult.superseded
            }

            await self.deleteDesktopCookies()
            guard self.isCurrent(requestedGeneration, nonce: session.nonce) else {
                return DesktopCookieInstallResult.superseded
            }

            let responseHeaders = [
                "Set-Cookie": "csi_desktop=\(session.token); Path=/; HttpOnly; SameSite=Strict"
            ]
            guard let cookie = HTTPCookie.cookies(
                withResponseHeaderFields: responseHeaders,
                for: session.baseURL
            ).first, cookie.isHTTPOnly else {
                return DesktopCookieInstallResult.invalidCookie
            }

            await self.setCookie(cookie)
            guard self.isCurrent(requestedGeneration, nonce: session.nonce) else {
                return DesktopCookieInstallResult.superseded
            }
            return DesktopCookieInstallResult.installed
        }
        operationTail = Task { @MainActor in
            _ = await operation.value
        }
        return await operation.value
    }

    func clear() async {
        let operation = enqueueClear()
        await operation.value
    }

    @discardableResult
    func enqueueClear() -> Task<Void, Never> {
        generation &+= 1
        let requestedGeneration = generation
        activeNonce = nil
        let predecessor = operationTail
        let operation = Task { @MainActor [weak self] in
            await predecessor?.value
            guard let self, self.generation == requestedGeneration else { return }
            await self.deleteDesktopCookies()
        }
        operationTail = operation
        return operation
    }

    private func isCurrent(_ requestedGeneration: UInt64, nonce: String) -> Bool {
        generation == requestedGeneration && activeNonce == nonce
    }

    private func deleteDesktopCookies() async {
        let cookies: [HTTPCookie] = await withCheckedContinuation { continuation in
            cookieStore.getAllCookies { continuation.resume(returning: $0) }
        }
        for cookie in cookies where cookie.name == "csi_desktop" {
            await withCheckedContinuation {
                (continuation: CheckedContinuation<Void, Never>) in
                cookieStore.delete(cookie) { continuation.resume() }
            }
        }
    }

    private func setCookie(_ cookie: HTTPCookie) async {
        await withCheckedContinuation {
            (continuation: CheckedContinuation<Void, Never>) in
            cookieStore.setCookie(cookie) { continuation.resume() }
        }
    }
}
