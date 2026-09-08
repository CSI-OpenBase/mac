// swift-tools-version: 5.10

import PackageDescription

let package = Package(
    name: "CSI-OpenBase-Mac",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .executable(name: "CSIOpenBaseMac", targets: ["CSIOpenBaseMac"]),
        .executable(name: "CSIBackendLauncher", targets: ["CSIBackendLauncher"]),
    ],
    targets: [
        .executableTarget(
            name: "CSIOpenBaseMac",
            path: "Sources/CSIOpenBaseMac",
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("CryptoKit"),
                .linkedFramework("Security"),
                .linkedFramework("SwiftUI"),
                .linkedFramework("WebKit"),
            ]
        ),
        .executableTarget(
            name: "CSIBackendLauncher",
            path: "Sources/CSIBackendLauncher"
        ),
    ]
)
