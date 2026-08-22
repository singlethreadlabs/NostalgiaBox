import Foundation
import SwiftData

enum MediaSupport: String, CaseIterable, Codable, Sendable {
    case playable
    case unsupported
    case unavailable
}

struct FirstStationScan: Equatable {
    let sourceName: String
    let playableCount: Int
    let unsupportedCount: Int
}

enum StationMood: String, CaseIterable, Identifiable {
    case classic = "Classic"
    case saturdayMorning = "Saturday Morning"
    case midnight = "Midnight"

    var id: Self { self }
    var colorHex: String {
        switch self {
        case .classic: "7B4DFF"
        case .saturdayMorning: "E66B45"
        case .midnight: "1F6FEB"
        }
    }
}

enum BumperTemplate: String, CaseIterable, Codable, Sendable {
    case channelID = "Channel ID"
    case nowPlaying = "Now Playing"
    case transition = "Transition"
}

enum BumperTransition: String, CaseIterable, Codable, Sendable {
    case cut = "Cut"
    case fade = "Fade"
    case wipe = "Wipe"
    case zoom = "Zoom"
    case staticNoise = "Static"
}

struct BumperScene: Codable, Sendable {
    var title: String
    var subtitle: String
    var titleFontName: String
    var titleSize: Double
    var subtitleSize: Double
    var backgroundHex: String
    var accentHex: String
    var textHex: String
    var transition: BumperTransition
    var duration: Double
    var imagePath: String?
    var videoPath: String?
    var audioPath: String?

    static func `default`(template: BumperTemplate, channelName: String, accentHex: String) -> BumperScene {
        BumperScene(
            title: template.rawValue.uppercased(), subtitle: channelName,
            titleFontName: "Menlo-Bold", titleSize: 82, subtitleSize: 42,
            backgroundHex: "000000", accentHex: accentHex, textHex: "FFFFFF",
            transition: .fade, duration: 1, imagePath: nil, videoPath: nil, audioPath: nil
        )
    }
}

@Model
final class MediaSource {
    @Attribute(.unique) var id: UUID
    var displayName: String
    var bookmarkData: Data
    var lastScannedAt: Date?
    var statusMessage: String?

    init(displayName: String, bookmarkData: Data, statusMessage: String? = nil) {
        self.id = UUID()
        self.displayName = displayName
        self.bookmarkData = bookmarkData
        self.statusMessage = statusMessage
    }
}

@Model
final class LibraryMedia {
    @Attribute(.unique) var id: UUID
    var sourceID: UUID?
    var path: String
    var title: String
    var duration: Double
    var fileSize: Int64
    var modifiedAt: Date
    var supportRaw: String
    var warning: String?
    var playCount: Int

    init(sourceID: UUID?, path: String, title: String, duration: Double, fileSize: Int64, modifiedAt: Date, support: MediaSupport, warning: String? = nil) {
        self.id = UUID()
        self.sourceID = sourceID
        self.path = path
        self.title = title
        self.duration = duration
        self.fileSize = fileSize
        self.modifiedAt = modifiedAt
        self.supportRaw = support.rawValue
        self.warning = warning
        self.playCount = 0
    }

    var support: MediaSupport { MediaSupport(rawValue: supportRaw) ?? .unsupported }
}

@Model
final class StationChannel {
    @Attribute(.unique) var id: UUID
    var number: Int
    var name: String
    var colorHex: String
    var theme: String
    var bumperMediaID: UUID?

    init(number: Int, name: String, colorHex: String, theme: String = "Classic") {
        self.id = UUID()
        self.number = number
        self.name = name
        self.colorHex = colorHex
        self.theme = theme
    }
}

@Model
final class ChannelAssignment {
    @Attribute(.unique) var id: UUID
    var channelID: UUID
    var mediaID: UUID
    var kind: String

    init(channelID: UUID, mediaID: UUID, kind: String = "show") {
        self.id = UUID()
        self.channelID = channelID
        self.mediaID = mediaID
        self.kind = kind
    }
}

@Model
final class BumperAsset {
    @Attribute(.unique) var id: UUID
    var templateRaw: String
    var channelName: String
    var filePath: String
    var createdAt: Date
    var sceneData: Data?

    init(template: BumperTemplate, channelName: String, filePath: String) {
        self.id = UUID()
        self.templateRaw = template.rawValue
        self.channelName = channelName
        self.filePath = filePath
        self.createdAt = .now
        self.sceneData = nil
    }
}

@Model
final class ScheduledProgram {
    @Attribute(.unique) var id: UUID
    var channelID: UUID
    var mediaID: UUID
    var startsAt: Date
    var endsAt: Date
    var kind: String
    var isOverride: Bool

    init(channelID: UUID, mediaID: UUID, startsAt: Date, endsAt: Date, kind: String = "show", isOverride: Bool = false) {
        self.id = UUID()
        self.channelID = channelID
        self.mediaID = mediaID
        self.startsAt = startsAt
        self.endsAt = endsAt
        self.kind = kind
        self.isOverride = isOverride
    }
}

@Model
final class PlaybackEvent {
    @Attribute(.unique) var id: UUID
    var channelID: UUID
    var mediaID: UUID
    var startedAt: Date
    var endedAt: Date?
    var accumulatedSeconds: Double

    init(channelID: UUID, mediaID: UUID, startedAt: Date = .now) {
        self.id = UUID()
        self.channelID = channelID
        self.mediaID = mediaID
        self.startedAt = startedAt
        self.accumulatedSeconds = 0
    }
}

struct ProgramCandidate: Identifiable, Hashable, Sendable {
    let id: UUID
    let channelID: UUID
    let mediaID: UUID
    let title: String
    let startsAt: Date
    let endsAt: Date
    let isOverride: Bool
}
