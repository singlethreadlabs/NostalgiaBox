# NostalgiaBox Studio for Mac

Native macOS 15+ BYO-media station builder. It uses App Sandbox selected-folder access, SwiftData, and AVFoundation only; it does not upload or modify source media. `Assets/nostalgiabox-studio-icon-source.png` is original source artwork generated for this project; `Assets.xcassets` contains its App Store icon derivative.

Open `NostalgiaBoxStudio.xcodeproj` in Xcode for the signed macOS application target, or open `Package.swift` for the package and tests. For local development:

```sh
cd macos/NostalgiaBoxStudio
swift test
swift run NostalgiaBoxStudio
```

The application target applies `NostalgiaBoxStudio.entitlements`, which deliberately grants only App Sandbox and user-selected read-only file access. Set the Apple Developer team before archiving for the App Store.
