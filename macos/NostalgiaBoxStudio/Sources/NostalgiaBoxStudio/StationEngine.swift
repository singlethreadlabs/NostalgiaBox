import AVFoundation
import AppKit
import CoreVideo
import Foundation
import OSLog
import SwiftData

enum StudioLog {
    private static let logger = Logger(subsystem: "com.singlethreadlabs.nostalgiabox.studio", category: "playback")

    static func info(_ message: String) {
        logger.info("\(message, privacy: .public)")
        print("[NostalgiaBoxStudio] \(message)")
    }

    static func error(_ message: String) {
        logger.error("\(message, privacy: .public)")
        print("[NostalgiaBoxStudio] ERROR: \(message)")
    }
}

@MainActor
enum SourceAccessRegistry {
    private static var activeURLs: [UUID: URL] = [:]

    static func retain(sourceID: UUID, url: URL) -> Bool {
        guard url.startAccessingSecurityScopedResource() else { return false }
        activeURLs[sourceID] = url
        StudioLog.info("Retained folder access for \(url.lastPathComponent) for this app session.")
        return true
    }

    static func url(for sourceID: UUID) -> URL? { activeURLs[sourceID] }

    static func release(sourceID: UUID) {
        guard let url = activeURLs.removeValue(forKey: sourceID) else { return }
        url.stopAccessingSecurityScopedResource()
        StudioLog.info("Released retained folder access for \(url.lastPathComponent).")
    }
}

enum BumperStorage {
    static func destination(for id: UUID) throws -> URL {
        let directory = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            .appendingPathComponent("NostalgiaBoxStudio/Bumpers", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory.appendingPathComponent("\(id.uuidString).mp4")
    }

    static func previewDestination() throws -> URL {
        try destination(for: UUID())
    }

    static func importAsset(from url: URL) throws -> String {
        let hasSecurityScope = url.startAccessingSecurityScopedResource()
        defer { if hasSecurityScope { url.stopAccessingSecurityScopedResource() } }
        let directory = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            .appendingPathComponent("NostalgiaBoxStudio/BumperAssets", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let destination = directory.appendingPathComponent("\(UUID().uuidString).\(url.pathExtension)")
        try FileManager.default.copyItem(at: url, to: destination)
        return destination.path
    }

    static func sceneAssetPaths(_ scene: BumperScene) -> Set<String> {
        Set([scene.imagePath, scene.videoPath, scene.audioPath].compactMap { $0 })
    }

    static func isManagedAsset(_ path: String) -> Bool {
        path.contains("/NostalgiaBoxStudio/BumperAssets/")
    }

    static func removeManagedAssets(_ paths: Set<String>) {
        for path in paths where isManagedAsset(path) {
            try? FileManager.default.removeItem(atPath: path)
        }
    }

    static func restoreAsset(data: Data, originalPath: String) throws -> String {
        let directory = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            .appendingPathComponent("NostalgiaBoxStudio/BumperAssets", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let destination = directory.appendingPathComponent("\(UUID().uuidString).\(URL(fileURLWithPath: originalPath).pathExtension)")
        try data.write(to: destination, options: .atomic)
        return destination.path
    }
}

enum BumperSceneStore {
    static func scene(for bumper: BumperAsset, accentHex: String = "7B4DFF") -> BumperScene {
        guard let data = bumper.sceneData, let scene = try? JSONDecoder().decode(BumperScene.self, from: data) else {
            return .default(template: BumperTemplate(rawValue: bumper.templateRaw) ?? .channelID, channelName: bumper.channelName, accentHex: accentHex)
        }
        return scene
    }

    static func save(_ scene: BumperScene, to bumper: BumperAsset) throws {
        bumper.sceneData = try JSONEncoder().encode(scene)
        bumper.channelName = scene.subtitle
    }
}

enum StationError: LocalizedError {
    case missingFolder
    case noPlayableMedia
    case renderFailed

    var errorDescription: String? {
        switch self {
        case .missingFolder: "The selected media folder is no longer available."
        case .noPlayableMedia: "This channel needs at least one playable video."
        case .renderFailed: "NostalgiaBox could not render this bumper."
        }
    }
}

enum BookmarkStore {
    static var isSandboxed: Bool { ProcessInfo.processInfo.environment["APP_SANDBOX_CONTAINER_ID"] != nil }

    static func create(for url: URL) throws -> Data {
        try url.bookmarkData(options: .withSecurityScope, includingResourceValuesForKeys: nil, relativeTo: nil)
    }

    static func resolve(_ data: Data) throws -> URL {
        var stale = false
        let url = try URL(resolvingBookmarkData: data, options: .withSecurityScope, relativeTo: nil, bookmarkDataIsStale: &stale)
        // A stale bookmark still resolves to the selected location. It needs to
        // be refreshed for a future App Store build, but rejecting it here
        // prevents playback even though the user’s media is available now.
        StudioLog.info("Resolved media bookmark for \(url.lastPathComponent); stale=\(stale).")
        return url
    }
}

enum DevelopmentSourceRecovery {
    static func folder(for sourceID: UUID, displayName: String, media: [LibraryMedia]) -> URL? {
        let files = media
            .filter { $0.sourceID == sourceID && !$0.path.hasPrefix("demo://") }
            .map { URL(fileURLWithPath: $0.path).deletingLastPathComponent().standardizedFileURL }
        guard var commonFolder = files.first else { return nil }
        if let namedFolder = files.lazy.compactMap({ folder -> URL? in
            var candidate = folder
            while candidate.path != "/" {
                if candidate.lastPathComponent.localizedCaseInsensitiveCompare(displayName) == .orderedSame { return candidate }
                let parent = candidate.deletingLastPathComponent()
                guard parent.path != candidate.path else { return nil }
                candidate = parent
            }
            return nil
        }).first {
            return FileManager.default.fileExists(atPath: namedFolder.path) ? namedFolder : nil
        }
        for folder in files.dropFirst() {
            while folder.path != commonFolder.path && !folder.path.hasPrefix(commonFolder.path + "/") {
                let parent = commonFolder.deletingLastPathComponent()
                guard parent.path != commonFolder.path else { return nil }
                commonFolder = parent
            }
        }
        return FileManager.default.fileExists(atPath: commonFolder.path) ? commonFolder : nil
    }
}

struct MediaScanResult: Sendable {
    let path: String
    let title: String
    let duration: Double
    let fileSize: Int64
    let modifiedAt: Date
    let support: MediaSupport
    let warning: String?
}

enum MediaScanner {
    static let supportedExtensions: Set<String> = ["mkv", "mp4", "m4v", "mov"]

    static func scan(url: URL) async throws -> [MediaScanResult] {
        StudioLog.info("Starting media scan for \(url.lastPathComponent).")
        let hasSecurityScope = url.startAccessingSecurityScopedResource()
        guard hasSecurityScope || (!BookmarkStore.isSandboxed && FileManager.default.fileExists(atPath: url.path)) else {
            StudioLog.error("Media scan was denied folder access for \(url.lastPathComponent).")
            throw StationError.missingFolder
        }
        if !hasSecurityScope { StudioLog.info("Scanning \(url.lastPathComponent) with direct development access.") }
        defer { if hasSecurityScope { url.stopAccessingSecurityScopedResource() } }
        let keys: Set<URLResourceKey> = [.isRegularFileKey, .fileSizeKey, .contentModificationDateKey]
        let enumerator = FileManager.default.enumerator(at: url, includingPropertiesForKeys: Array(keys), options: [.skipsHiddenFiles, .skipsPackageDescendants])
        var results: [MediaScanResult] = []
        while let file = enumerator?.nextObject() as? URL {
            guard let values = try? file.resourceValues(forKeys: keys), values.isRegularFile == true else { continue }
            let extensionSupported = supportedExtensions.contains(file.pathExtension.lowercased())
            guard extensionSupported else { continue }
            let asset = AVURLAsset(url: file)
            do {
                let duration = try await asset.load(.duration)
                let seconds = duration.seconds
                let tracks = try await asset.loadTracks(withMediaType: .video)
                let valid = seconds.isFinite && seconds > 0 && !tracks.isEmpty
                results.append(MediaScanResult(path: file.path, title: file.deletingPathExtension().lastPathComponent, duration: valid ? seconds : 0, fileSize: Int64(values.fileSize ?? 0), modifiedAt: values.contentModificationDate ?? .now, support: valid ? .playable : .unsupported, warning: valid ? nil : "No playable video track or duration."))
            } catch {
                results.append(MediaScanResult(path: file.path, title: file.deletingPathExtension().lastPathComponent, duration: 0, fileSize: Int64(values.fileSize ?? 0), modifiedAt: values.contentModificationDate ?? .now, support: .unsupported, warning: "AVFoundation could not read this file."))
            }
        }
        let sorted = results.sorted { $0.title.localizedStandardCompare($1.title) == .orderedAscending }
        StudioLog.info("Completed media scan: \(sorted.count) supported files, \(sorted.filter { $0.support == .playable }.count) playable.")
        return sorted
    }
}

enum ScheduleEngine {
    static func generate(channelID: UUID, media: [LibraryMedia], existing: [ScheduledProgram], now: Date = .now, horizon: TimeInterval = 48 * 60 * 60) throws -> [ScheduledProgram] {
        let playable = media.filter { $0.support == .playable && $0.duration > 0 }
        guard !playable.isEmpty else { throw StationError.noPlayableMedia }
        let cutoff = now.addingTimeInterval(horizon)
        let overrides = existing.filter { $0.isOverride && $0.endsAt > now }.sorted { $0.startsAt < $1.startsAt }
        var generated: [ScheduledProgram] = []
        var cursor = now
        var lastID: UUID?
        var index = 0

        func appendAutomatic(until limit: Date) {
            while cursor < limit {
                let choices = playable.filter { $0.id != lastID }
                let item = (choices.isEmpty ? playable : choices)[index % (choices.isEmpty ? playable.count : choices.count)]
                let end = min(cursor.addingTimeInterval(max(item.duration, 1)), limit)
                generated.append(ScheduledProgram(channelID: channelID, mediaID: item.id, startsAt: cursor, endsAt: end))
                cursor = end
                lastID = item.id
                index += 1
            }
        }

        for override in overrides where override.startsAt < cutoff {
            appendAutomatic(until: max(cursor, override.startsAt))
            guard override.endsAt > cursor else { continue }
            generated.append(override)
            cursor = override.endsAt
            lastID = override.mediaID
        }

        while cursor < cutoff {
            let choices = playable.filter { $0.id != lastID }
            let item = (choices.isEmpty ? playable : choices)[index % (choices.isEmpty ? playable.count : choices.count)]
            let duration = max(item.duration, 1)
            generated.append(ScheduledProgram(channelID: channelID, mediaID: item.id, startsAt: cursor, endsAt: cursor.addingTimeInterval(duration)))
            cursor = cursor.addingTimeInterval(duration)
            lastID = item.id
            index += 1
        }
        StudioLog.info("Generated \(generated.count) programs from \(playable.count) playable titles for channel \(channelID.uuidString.prefix(8)).")
        return generated
    }

    static func current(from programs: [ScheduledProgram], at date: Date = .now) -> ScheduledProgram? {
        programs.first { $0.startsAt <= date && $0.endsAt > date }
    }
}

enum BumperRenderer {
    static func render(scene: BumperScene, destination: URL) throws {
        let size = CGSize(width: 1920, height: 1080)
        let writer = try AVAssetWriter(outputURL: destination, fileType: .mp4)
        let settings: [String: Any] = [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: size.width, AVVideoHeightKey: size.height]
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
        let attributes: [String: Any] = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: Int(size.width), kCVPixelBufferHeightKey as String: Int(size.height)]
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: attributes)
        guard writer.canAdd(input) else { throw StationError.renderFailed }
        writer.add(input)
        guard writer.startWriting() else { throw writer.error ?? StationError.renderFailed }
        writer.startSession(atSourceTime: .zero)
        let fps: Int32 = 30
        let frameCount = max(1, Int32((max(0.25, min(scene.duration, 10)) * Double(fps)).rounded()))
        for frame in 0..<frameCount {
            while !input.isReadyForMoreMediaData { RunLoop.current.run(until: Date().addingTimeInterval(0.005)) }
            guard let buffer = makeFrame(size: size, scene: scene, progress: Double(frame) / Double(max(frameCount - 1, 1))) else { throw StationError.renderFailed }
            guard adaptor.append(buffer, withPresentationTime: CMTime(value: Int64(frame), timescale: fps)) else { throw writer.error ?? StationError.renderFailed }
        }
        input.markAsFinished()
        writer.finishWriting {}
        while writer.status == .writing { RunLoop.current.run(until: Date().addingTimeInterval(0.01)) }
        guard writer.status == .completed else { throw writer.error ?? StationError.renderFailed }
    }

    private static func makeFrame(size: CGSize, scene: BumperScene, progress: Double) -> CVPixelBuffer? {
        var pixelBuffer: CVPixelBuffer?
        let attributes: [String: Any] = [kCVPixelBufferCGImageCompatibilityKey as String: true, kCVPixelBufferCGBitmapContextCompatibilityKey as String: true]
        guard CVPixelBufferCreate(kCFAllocatorDefault, Int(size.width), Int(size.height), kCVPixelFormatType_32ARGB, attributes as CFDictionary, &pixelBuffer) == kCVReturnSuccess, let pixelBuffer else { return nil }
        CVPixelBufferLockBaseAddress(pixelBuffer, [])
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, []) }
        guard let context = CGContext(data: CVPixelBufferGetBaseAddress(pixelBuffer), width: Int(size.width), height: Int(size.height), bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pixelBuffer), space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue) else { return nil }
        context.setFillColor(NSColor(hex: scene.backgroundHex).cgColor)
        context.fill(CGRect(origin: .zero, size: size))
        let opacity: CGFloat
        switch scene.transition {
        case .fade: opacity = CGFloat(min(1, progress * 4))
        case .zoom: opacity = CGFloat(min(1, progress * 3))
        default: opacity = 1
        }
        context.setFillColor(NSColor(hex: scene.accentHex).cgColor)
        context.fill(CGRect(x: 0, y: 0, width: size.width, height: 52))
        let titleFont = NSFont(name: scene.titleFontName, size: CGFloat(scene.titleSize)) ?? NSFont.monospacedSystemFont(ofSize: CGFloat(scene.titleSize), weight: .bold)
        let subtitleFont = NSFont(name: scene.titleFontName.replacingOccurrences(of: "-Bold", with: ""), size: CGFloat(scene.subtitleSize)) ?? NSFont.monospacedSystemFont(ofSize: CGFloat(scene.subtitleSize), weight: .regular)
        let zoom = scene.transition == .zoom ? 0.92 + progress * 0.08 : 1
        let offset = scene.transition == .wipe ? (1 - progress) * -420 : 0
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(cgContext: context, flipped: false)
        if let imagePath = scene.imagePath, let image = NSImage(contentsOf: URL(fileURLWithPath: imagePath)) {
            image.draw(in: CGRect(x: 0, y: 0, width: size.width, height: size.height), from: .zero, operation: .sourceOver, fraction: 0.35)
        }
        let draw = { (text: String, font: NSFont, rect: CGRect) in
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(cgContext: context, flipped: false)
            (text as NSString).draw(in: rect, withAttributes: [.font: font, .foregroundColor: NSColor(hex: scene.textHex).withAlphaComponent(opacity)])
            NSGraphicsContext.restoreGraphicsState()
        }
        let titleRect = CGRect(x: 120 + offset, y: 560, width: 1680 * zoom, height: 130)
        draw(scene.title.uppercased(), titleFont, titleRect)
        draw(scene.subtitle, subtitleFont, CGRect(x: 126 + offset, y: 485, width: 1680 * zoom, height: 70))
        if scene.transition == .staticNoise {
            context.setStrokeColor(NSColor.white.withAlphaComponent(0.15).cgColor)
            for line in stride(from: 0, through: Int(size.height), by: 12) { context.stroke(CGRect(x: 0, y: CGFloat(line), width: size.width, height: 1)) }
        }
        NSGraphicsContext.restoreGraphicsState()
        return pixelBuffer
    }
}

private extension NSColor {
    convenience init(hex: String) {
        let value = UInt64(hex.trimmingCharacters(in: CharacterSet(charactersIn: "#")), radix: 16) ?? 0
        self.init(srgbRed: CGFloat((value >> 16) & 0xFF) / 255, green: CGFloat((value >> 8) & 0xFF) / 255, blue: CGFloat(value & 0xFF) / 255, alpha: 1)
    }
}
