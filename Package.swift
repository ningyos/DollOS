// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "dollcore",
    platforms: [.macOS(.v26)],
    products: [
        .executable(name: "dollcore", targets: ["DollCLI"]),
    ],
    dependencies: [
        // The memory embedding (Qwen3-Embedding-0.6B) runs on MLX.
        .package(url: "https://github.com/ml-explore/mlx-swift-lm", .upToNextMinor(from: "3.32.3")),
        .package(url: "https://github.com/huggingface/swift-transformers", from: "1.3.0"),
        .package(url: "https://github.com/huggingface/swift-huggingface", from: "0.9.0"),
    ],
    targets: [
        .target(name: "DollCore",
                dependencies: [
                    .product(name: "MLXEmbedders", package: "mlx-swift-lm"),
                    .product(name: "Tokenizers", package: "swift-transformers"),
                    .product(name: "HuggingFace", package: "swift-huggingface"),
                ],
                linkerSettings: [.linkedLibrary("sqlite3")]),
        .executableTarget(name: "DollCLI", dependencies: ["DollCore"]),
        .executableTarget(name: "m2-computer", dependencies: ["DollCore"], path: "experiments/m2/Runner"),
        .testTarget(name: "DollCoreTests", dependencies: ["DollCore"]),
    ]
)
