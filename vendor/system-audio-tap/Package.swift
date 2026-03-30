// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "system-audio-tap",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "system-audio-tap",
            path: "Sources",
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("CoreAudio"),
                .linkedFramework("AVFAudio"),
                .linkedFramework("ScreenCaptureKit"),
            ]
        )
    ]
)
