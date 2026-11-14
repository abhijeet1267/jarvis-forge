// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "Vantage",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "Vantage", targets: ["Vantage"])
    ],
    targets: [
        .executableTarget(
            name: "Vantage",
            path: "Sources"
        ),
        .testTarget(
            name: "VantageTests",
            dependencies: ["Vantage"],
            path: "Tests/VantageTests"
        )
    ]
)