// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "NostalgiaBoxStudio",
    platforms: [.macOS(.v15)],
    products: [
        .executable(name: "NostalgiaBoxStudio", targets: ["NostalgiaBoxStudio"]),
    ],
    targets: [
        .executableTarget(
            name: "NostalgiaBoxStudio",
            path: "Sources/NostalgiaBoxStudio"
        ),
        .testTarget(
            name: "NostalgiaBoxStudioTests",
            dependencies: ["NostalgiaBoxStudio"],
            path: "Tests/NostalgiaBoxStudioTests"
        ),
    ]
)
