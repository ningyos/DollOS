// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "dollcore",
    platforms: [.macOS(.v26)],
    products: [
        .executable(name: "dollcore", targets: ["DollCLI"]),
    ],
    targets: [
        .target(name: "DollCore", linkerSettings: [.linkedLibrary("sqlite3")]),
        .executableTarget(name: "DollCLI", dependencies: ["DollCore"]),
        .testTarget(name: "DollCoreTests", dependencies: ["DollCore"]),
    ]
)
