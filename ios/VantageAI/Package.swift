// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "VantageAI",
    defaultLocalization: "en",
    platforms: [
        .iOS(.v16),
        .macOS(.v13),
        .watchOS(.v9),
        .visionOS(.v1),
    ],
    products: [
        // Existing AI client library (all original files in one module)
        .library(name: "VantageAI", targets: ["VantageAI"]),

        // Native iOS companion core
        .library(name: "VantageCompanionCore", targets: ["VantageCompanionCore"]),
    ],
    targets: [
        // Existing AI client, settings, views, Siri intent — one module as originally designed
        .target(
            name: "VantageAI",
            dependencies: [],
            path: "Sources/VantageAI"
        ),

        // Native iOS companion core
        .target(
            name: "VantageCompanionCore",
            dependencies: [],
            path: "Sources/VantageCompanionCore"
        ),
        .testTarget(
            name: "VantageCompanionCoreTests",
            dependencies: ["VantageCompanionCore"],
            path: "Tests/VantageCompanionCoreTests"
        ),
    ]
)