import Foundation
import Testing
@testable import NostalgiaBoxStudio

@Test func scheduleAvoidsImmediateRepeat() throws {
    let channel = UUID()
    let first = LibraryMedia(sourceID: nil, path: "/one.mp4", title: "One", duration: 60, fileSize: 1, modifiedAt: .now, support: .playable)
    let second = LibraryMedia(sourceID: nil, path: "/two.mp4", title: "Two", duration: 60, fileSize: 1, modifiedAt: .now, support: .playable)
    let programs = try ScheduleEngine.generate(channelID: channel, media: [first, second], existing: [], now: Date(timeIntervalSince1970: 0), horizon: 300)
    for pair in zip(programs, programs.dropFirst()) { #expect(pair.0.mediaID != pair.1.mediaID) }
}

@Test func currentProgramUsesWallClock() {
    let channel = UUID(); let media = UUID(); let now = Date(timeIntervalSince1970: 100)
    let program = ScheduledProgram(channelID: channel, mediaID: media, startsAt: now.addingTimeInterval(-30), endsAt: now.addingTimeInterval(30))
    #expect(ScheduleEngine.current(from: [program], at: now)?.id == program.id)
}

@Test func scheduleRetainsFuturePinnedProgramWithoutGap() throws {
    let channel = UUID()
    let first = LibraryMedia(sourceID: nil, path: "/one.mp4", title: "One", duration: 60, fileSize: 1, modifiedAt: .now, support: .playable)
    let second = LibraryMedia(sourceID: nil, path: "/two.mp4", title: "Two", duration: 60, fileSize: 1, modifiedAt: .now, support: .playable)
    let start = Date(timeIntervalSince1970: 0)
    let pinned = ScheduledProgram(channelID: channel, mediaID: second.id, startsAt: start.addingTimeInterval(120), endsAt: start.addingTimeInterval(180), isOverride: true)
    let programs = try ScheduleEngine.generate(channelID: channel, media: [first, second], existing: [pinned], now: start, horizon: 300)
    #expect(programs.contains(where: { $0.id == pinned.id }))
    #expect(zip(programs, programs.dropFirst()).allSatisfy { $0.endsAt == $1.startsAt })
}

@Test func scheduleOnlyUsesItsCuratedPool() throws {
    let channel = UUID()
    let allowed = [
        LibraryMedia(sourceID: nil, path: "/one.mp4", title: "One", duration: 60, fileSize: 1, modifiedAt: .now, support: .playable),
        LibraryMedia(sourceID: nil, path: "/two.mp4", title: "Two", duration: 60, fileSize: 1, modifiedAt: .now, support: .playable)
    ]
    let programs = try ScheduleEngine.generate(channelID: channel, media: allowed, existing: [], now: Date(timeIntervalSince1970: 0), horizon: 360)
    let curatedIDs = Set(allowed.map(\.id))
    #expect(programs.allSatisfy { curatedIDs.contains($0.mediaID) })
}

@Test func futureOverrideTakesPrecedenceAtItsStartTime() throws {
    let channel = UUID()
    let first = LibraryMedia(sourceID: nil, path: "/one.mp4", title: "One", duration: 60, fileSize: 1, modifiedAt: .now, support: .playable)
    let replacement = LibraryMedia(sourceID: nil, path: "/two.mp4", title: "Two", duration: 90, fileSize: 1, modifiedAt: .now, support: .playable)
    let start = Date(timeIntervalSince1970: 0)
    let override = ScheduledProgram(channelID: channel, mediaID: replacement.id, startsAt: start.addingTimeInterval(120), endsAt: start.addingTimeInterval(210), isOverride: true)
    let programs = try ScheduleEngine.generate(channelID: channel, media: [first, replacement], existing: [override], now: start, horizon: 360)
    #expect(ScheduleEngine.current(from: programs, at: start.addingTimeInterval(120))?.id == override.id)
    #expect(ScheduleEngine.current(from: programs, at: start.addingTimeInterval(209))?.mediaID == replacement.id)
}

@Test func developmentSourceRecoveryFindsNamedSourceFolder() throws {
    let sourceID = UUID()
    let root = FileManager.default.temporaryDirectory.appendingPathComponent("NostalgiaBoxStudioTests-\(UUID().uuidString)")
    let shows = root.appendingPathComponent("Shows")
    let nested = shows.appendingPathComponent("Cartoons/Season 1")
    try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: root) }

    let item = LibraryMedia(sourceID: sourceID, path: nested.appendingPathComponent("episode.mp4").path, title: "Episode", duration: 60, fileSize: 1, modifiedAt: .now, support: .playable)
    #expect(DevelopmentSourceRecovery.folder(for: sourceID, displayName: "Shows", media: [item]) == shows.standardizedFileURL)
}

@Test func bumperSceneRoundTripsEditableVariables() throws {
    var scene = BumperScene.default(template: .transition, channelName: "Night Flight", accentHex: "FF00AA")
    scene.titleSize = 96
    scene.transition = .wipe
    scene.duration = 3.5
    scene.audioPath = "/tmp/sting.m4a"
    let decoded = try JSONDecoder().decode(BumperScene.self, from: JSONEncoder().encode(scene))
    #expect(decoded.titleSize == 96)
    #expect(decoded.transition == .wipe)
    #expect(decoded.duration == 3.5)
    #expect(decoded.audioPath == "/tmp/sting.m4a")
}
