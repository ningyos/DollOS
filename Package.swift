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
        .executableTarget(name: "m1-skeleton", dependencies: ["DollCore"], path: "experiments/m1-skeleton/Runner"),
        .executableTarget(name: "m3b-kind", dependencies: ["DollCore"], path: "experiments/m3b/KindRunner"),
        .executableTarget(name: "m3b-extract", dependencies: ["DollCore"], path: "experiments/m3b/ExtractRunner"),
        .testTarget(name: "DollCoreTests", dependencies: ["DollCore"]),
    ]
)
