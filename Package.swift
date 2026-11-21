// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "dollcore",
    platforms: [.macOS(.v26)],
    products: [
        .executable(name: "dollcore", targets: ["DollCLI"]),
        .executable(name: "dollctl", targets: ["DollCtl"]),
    ],
    dependencies: [
        // The memory embedding (Qwen3-Embedding-0.6B) runs on MLX.
        .package(url: "https://github.com/ml-explore/mlx-swift-lm", .upToNextMinor(from: "3.32.3")),
        .package(url: "https://github.com/huggingface/swift-transformers", from: "1.3.0"),
        .package(url: "https://github.com/huggingface/swift-huggingface", from: "0.9.0"),
        // Her voice (M6): Qwen3-TTS 0.6B on MLX.
        .package(url: "https://github.com/ningyos/voice-mlx", .upToNextMinor(from: "0.1.0")),
    ],
    targets: [
        .target(name: "DollCore",
                dependencies: [
                    .product(name: "MLXEmbedders", package: "mlx-swift-lm"),
                    .product(name: "Tokenizers", package: "swift-transformers"),
                    .product(name: "HuggingFace", package: "swift-huggingface"),
                    .product(name: "VoiceMLX", package: "voice-mlx"),
                    .product(name: "VoiceAudio", package: "voice-mlx"),
                ],
                linkerSettings: [.linkedLibrary("sqlite3")]),
        .executableTarget(name: "DollCLI", dependencies: ["DollCore"]),
        .executableTarget(name: "DollCtl", dependencies: ["DollCore"]),
        .executableTarget(name: "mac-computer", dependencies: ["DollCore"], path: "experiments/m7-mac/Runner"),
        .executableTarget(name: "m6-calendar", dependencies: ["DollCore"], path: "experiments/m6-calendar/Runner"),
        .executableTarget(name: "m6-drives-real", dependencies: ["DollCore"], path: "experiments/m6-drives/real/Runner"),
        // Fixtures/ holds the test character pack, read by path at run time (not a resource).
        .testTarget(name: "DollCoreTests", dependencies: ["DollCore"], exclude: ["Fixtures"]),
    ]
)
