import SwiftUI

struct ContentView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            statusBar
            Divider()
            content
        }
        .frame(minWidth: 900, minHeight: 620)
        .alert(
            "CSI OpenBase",
            isPresented: Binding(
                get: { model.alertMessage != nil },
                set: { if !$0 { model.alertMessage = nil } }
            ),
            actions: {
                Button("确定", role: .cancel) {
                    model.alertMessage = nil
                }
            },
            message: {
                Text(model.alertMessage ?? "")
            }
        )
    }

    private var header: some View {
        VStack(spacing: 12) {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("CSI OpenBase")
                        .font(.title2.weight(.semibold))
                    Text("创作者数据本地归档")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    model.openLog()
                } label: {
                    Label("打开日志", systemImage: "doc.text.magnifyingglass")
                }
                Button {
                    Task { await model.restartBackend() }
                } label: {
                    Label("重新启动", systemImage: "arrow.clockwise")
                }
                .buttonStyle(.borderedProminent)
                .disabled(!model.canInteract)
            }

            HStack(spacing: 10) {
                Text("工作目录")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                Text(model.workspaceURL.path)
                    .font(.system(.callout, design: .monospaced))
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                Button {
                    Task { await model.chooseWorkspace() }
                } label: {
                    Label("选择目录", systemImage: "folder")
                }
                .disabled(!model.canInteract)
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 14)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var statusBar: some View {
        HStack(spacing: 10) {
            if model.isBusy {
                ProgressView()
                    .controlSize(.small)
            } else {
                Image(systemName: model.session == nil ? "exclamationmark.circle" : "checkmark.circle.fill")
                    .foregroundStyle(model.session == nil ? Color.secondary : Color.green)
            }
            Text(model.statusMessage)
                .font(.callout)
                .lineLimit(2)
            Spacer()
        }
        .padding(.horizontal, 18)
        .frame(minHeight: 38)
        .background(Color(nsColor: .controlBackgroundColor))
    }

    @ViewBuilder
    private var content: some View {
        if let session = model.session {
            LocalWebView(
                session: session,
                navigationError: $model.navigationError
            )
        } else {
            VStack(spacing: 14) {
                ProgressView()
                    .controlSize(.large)
                    .opacity(model.isBusy ? 1 : 0)
                Text(model.isBusy ? "正在连接本地服务" : "本地服务尚未连接")
                    .font(.headline)
                if !model.isBusy {
                    Button {
                        Task { await model.restartBackend() }
                    } label: {
                        Label("重新启动", systemImage: "arrow.clockwise")
                    }
                    .buttonStyle(.borderedProminent)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color(nsColor: .textBackgroundColor))
        }
    }
}
