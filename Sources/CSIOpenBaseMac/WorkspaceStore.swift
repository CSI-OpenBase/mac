import Foundation

struct WorkspaceSelection {
    let url: URL
    let warning: String?
}

@MainActor
final class WorkspaceStore {
    private static let bookmarkKey = "workspaceBookmark"
    private static let pathKey = "workspacePath"

    private let defaults: UserDefaults
    private var scopedURL: URL?
    private var hasSecurityScope = false

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    func load() -> WorkspaceSelection {
        var warnings: [String] = []
        if let bookmark = defaults.data(forKey: Self.bookmarkKey) {
            var stale = false
            do {
                let url = try URL(
                    resolvingBookmarkData: bookmark,
                    options: [.withSecurityScope],
                    relativeTo: nil,
                    bookmarkDataIsStale: &stale
                )
                activate(url)
                if !hasSecurityScope {
                    warnings.append(
                        "无法启用已保存目录的安全书签，已继续使用保存的路径；若目录不可写，请重新选择。"
                    )
                }
                if stale, let warning = persist(url) {
                    warnings.append("安全书签已过期且刷新失败：\(warning)")
                }
                return WorkspaceSelection(
                    url: url,
                    warning: warnings.isEmpty ? nil : warnings.joined(separator: "\n")
                )
            } catch {
                defaults.removeObject(forKey: Self.bookmarkKey)
                warnings.append(
                    "无法恢复已保存目录的安全书签，已回退到保存路径：\(error.localizedDescription)"
                )
            }
        }

        if let savedPath = defaults.string(forKey: Self.pathKey), !savedPath.isEmpty {
            let url = URL(fileURLWithPath: savedPath, isDirectory: true)
                .standardizedFileURL
            activate(url)
            return WorkspaceSelection(
                url: url,
                warning: warnings.isEmpty ? nil : warnings.joined(separator: "\n")
            )
        }

        let fileManager = FileManager.default
        let base = fileManager.urls(for: .documentDirectory, in: .userDomainMask).first
            ?? fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let url = base.appendingPathComponent("CSI OpenBase", isDirectory: true)
        do {
            try fileManager.createDirectory(at: url, withIntermediateDirectories: true)
        } catch {
            warnings.append("无法预先创建默认工作目录：\(error.localizedDescription)")
        }
        activate(url)
        if let warning = persist(url) {
            warnings.append(warning)
        }
        return WorkspaceSelection(
            url: url,
            warning: warnings.isEmpty ? nil : warnings.joined(separator: "\n")
        )
    }

    func set(_ url: URL) throws -> WorkspaceSelection {
        let normalized = url.standardizedFileURL
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(
            atPath: normalized.path,
            isDirectory: &isDirectory
        ), isDirectory.boolValue else {
            throw WorkspaceError.notDirectory
        }

        stopAccessing()
        activate(normalized)
        let warning = persist(normalized)
        return WorkspaceSelection(url: normalized, warning: warning)
    }

    func stopAccessing() {
        if hasSecurityScope {
            scopedURL?.stopAccessingSecurityScopedResource()
        }
        scopedURL = nil
        hasSecurityScope = false
    }

    private func activate(_ url: URL) {
        scopedURL = url
        hasSecurityScope = url.startAccessingSecurityScopedResource()
    }

    private func persist(_ url: URL) -> String? {
        // The path is an intentional non-sandbox fallback if bookmark creation or
        // restoration fails. Backend startup still verifies write access.
        defaults.set(url.path, forKey: Self.pathKey)
        do {
            let bookmark = try url.bookmarkData(
                options: [.withSecurityScope],
                includingResourceValuesForKeys: nil,
                relativeTo: nil
            )
            defaults.set(bookmark, forKey: Self.bookmarkKey)
            return nil
        } catch {
            defaults.removeObject(forKey: Self.bookmarkKey)
            return "无法保存工作目录的安全书签，已保存路径作为回退；下次启动可能需要重新选择：\(error.localizedDescription)"
        }
    }
}

enum WorkspaceError: LocalizedError {
    case notDirectory

    var errorDescription: String? {
        switch self {
        case .notDirectory:
            return "所选路径不是可用目录。"
        }
    }
}
