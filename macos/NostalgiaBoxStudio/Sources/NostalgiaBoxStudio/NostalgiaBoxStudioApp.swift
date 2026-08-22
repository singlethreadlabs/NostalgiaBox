import AVFoundation
import AVKit
import AppKit
import SwiftData
import SwiftUI
import UniformTypeIdentifiers

@main
struct NostalgiaBoxStudioApp: App {
    var sharedModelContainer: ModelContainer = {
        let schema = Schema([MediaSource.self, LibraryMedia.self, StationChannel.self, ChannelAssignment.self, BumperAsset.self, ScheduledProgram.self, PlaybackEvent.self])
        return try! ModelContainer(for: schema)
    }()

    var body: some Scene {
        WindowGroup {
            StudioRootView()
        }
        .modelContainer(sharedModelContainer)
    }
}

struct StudioRootView: View {
    @Environment(\.modelContext) private var context
    @Query(sort: \StationChannel.number) private var channels: [StationChannel]
    @Query private var sources: [MediaSource]
    @Query private var libraryMedia: [LibraryMedia]
    @State private var selection: SidebarDestination? = .home
    @State private var importFolder = false
    @State private var message: String?
    @State private var firstStationScan: FirstStationScan?
    @State private var watchLaunchMessage: String?

    var body: some View {
        NavigationSplitView {
            List(selection: $selection) {
                Section("Station") {
                    Label("Home", systemImage: "house").tag(SidebarDestination.home)
                    Label("Watch", systemImage: "play.tv").tag(SidebarDestination.watch)
                    Label("Channels", systemImage: "rectangle.3.group").tag(SidebarDestination.channels)
                    Label("Schedule", systemImage: "calendar").tag(SidebarDestination.schedule)
                }
                Section("Studio") {
                    Label("Library", systemImage: "film.stack").tag(SidebarDestination.library)
                    Label("Bumper Studio", systemImage: "sparkles.rectangle.stack").tag(SidebarDestination.bumpers)
                    Label("Analytics", systemImage: "chart.bar").tag(SidebarDestination.analytics)
                }
                Section {
                    Label("Settings", systemImage: "gear").tag(SidebarDestination.settings)
                }
            }
            .navigationTitle("NostalgiaBox Studio")
        } detail: {
            switch selection ?? .home {
            case .home: HomeView(importFolder: $importFolder, scan: $firstStationScan, createFirstStation: createFirstStation)
            case .watch: WatchView(channels: channels, launchMessage: $watchLaunchMessage)
            case .channels: ChannelsView()
            case .schedule: ScheduleView()
            case .library: LibraryView(importFolder: $importFolder)
            case .bumpers: BumperStudioView()
            case .analytics: AnalyticsView()
            case .settings: SettingsView(importFolder: $importFolder) { source in
                Task { await importMedia(from: source) }
            }
            }
        }
        .fileImporter(isPresented: $importFolder, allowedContentTypes: [.folder], allowsMultipleSelection: false) { result in
            do {
                guard let url = try result.get().first else { return }
                let bookmark = try BookmarkStore.create(for: url)
                let source = MediaSource(displayName: url.lastPathComponent, bookmarkData: bookmark)
                context.insert(source)
                Task { await importMedia(from: source) }
            } catch { message = error.localizedDescription }
        }
        .alert("NostalgiaBox", isPresented: Binding(get: { message != nil }, set: { if !$0 { message = nil } })) { Button("OK", role: .cancel) {} } message: { Text(message ?? "") }
        .task {
            closeUnfinishedPlaybackEvents()
            refreshSourceHealth()
        }
    }

    private func closeUnfinishedPlaybackEvents() {
        if let unfinishedEvents = try? context.fetch(FetchDescriptor<PlaybackEvent>(predicate: #Predicate { $0.endedAt == nil })) {
            for event in unfinishedEvents {
                event.endedAt = .now
                event.accumulatedSeconds = max(0, event.endedAt!.timeIntervalSince(event.startedAt))
            }
        }
    }

    private func refreshSourceHealth() {
        for source in sources {
            do {
                let (url, fromBookmark) = try resolvedSourceURL(source, media: libraryMedia)
                let hasSecurityScope = url.startAccessingSecurityScopedResource()
                guard hasSecurityScope || (!BookmarkStore.isSandboxed && FileManager.default.fileExists(atPath: url.path)) else { throw StationError.missingFolder }
                defer { if hasSecurityScope { url.stopAccessingSecurityScopedResource() } }
                guard (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true else { throw StationError.missingFolder }
                source.statusMessage = fromBookmark ? "Ready · rescan to refresh the index." : "Ready with direct development access · add the folder again before distribution."
            } catch {
                source.statusMessage = "Unavailable · select this folder again to restore access."
                StudioLog.error("Source health check failed for \(source.displayName): \(error.localizedDescription)")
            }
        }
    }

    private func importMedia(from source: MediaSource) async {
        do {
            StudioLog.info("Import requested for source \(source.displayName).")
            let existingMedia = try context.fetch(FetchDescriptor<LibraryMedia>())
            let (url, fromBookmark) = try resolvedSourceURL(source, media: existingMedia)
            let retainedAccess = SourceAccessRegistry.retain(sourceID: source.id, url: url)
            guard retainedAccess || (!BookmarkStore.isSandboxed && FileManager.default.fileExists(atPath: url.path)) else { throw StationError.missingFolder }
            if fromBookmark {
                source.bookmarkData = try BookmarkStore.create(for: url)
                StudioLog.info("Refreshed the saved media bookmark for \(source.displayName).")
            } else {
                StudioLog.info("Rescanning \(source.displayName) with direct development access; its malformed bookmark was not replaced.")
            }
            let results = try await MediaScanner.scan(url: url)
            let assignments = try context.fetch(FetchDescriptor<ChannelAssignment>())
            let existingPrograms = try context.fetch(FetchDescriptor<ScheduledProgram>())
            var retainedPaths = Set<String>()
            var discardedIDs = Set<UUID>()
            for item in existingMedia {
                if item.path.hasPrefix("demo://") || !retainedPaths.insert(item.path).inserted {
                    discardedIDs.insert(item.id)
                }
            }
            assignments.filter { discardedIDs.contains($0.mediaID) }.forEach(context.delete)
            existingPrograms.filter { discardedIDs.contains($0.mediaID) }.forEach(context.delete)
            existingMedia.filter { discardedIDs.contains($0.id) }.forEach(context.delete)
            let knownPaths = Set(existingMedia.filter { !discardedIDs.contains($0.id) }.map(\.path))
            let scannedByPath = Dictionary(uniqueKeysWithValues: results.map { ($0.path, $0) })
            for item in existingMedia where item.sourceID == source.id && !discardedIDs.contains(item.id) {
                guard let scanned = scannedByPath[item.path] else {
                    item.supportRaw = MediaSupport.unsupported.rawValue
                    item.warning = "File is no longer present in this source folder."
                    continue
                }
                item.title = scanned.title
                item.duration = scanned.duration
                item.fileSize = scanned.fileSize
                item.modifiedAt = scanned.modifiedAt
                item.supportRaw = scanned.support.rawValue
                item.warning = scanned.warning
            }
            let imported = results.map {
                LibraryMedia(sourceID: source.id, path: $0.path, title: $0.title, duration: $0.duration, fileSize: $0.fileSize, modifiedAt: $0.modifiedAt, support: $0.support, warning: $0.warning)
            }.filter { !knownPaths.contains($0.path) }
            imported.forEach(context.insert)
            let retainedRealMedia = existingMedia.filter { !discardedIDs.contains($0.id) && !$0.path.hasPrefix("demo://") && $0.support == .playable }
            let realMedia = retainedRealMedia + imported.filter { $0.support == .playable }
            let channels = try context.fetch(FetchDescriptor<StationChannel>(sortBy: [SortDescriptor(\.number)]))
            if !realMedia.isEmpty, !channels.isEmpty {
                let allAssignments = try context.fetch(FetchDescriptor<ChannelAssignment>())
                for channel in channels {
                    let assignedIDs = Set(allAssignments.filter { $0.channelID == channel.id && !discardedIDs.contains($0.mediaID) }.map(\.mediaID))
                    if assignedIDs.isEmpty {
                        realMedia.forEach { context.insert(ChannelAssignment(channelID: channel.id, mediaID: $0.id)) }
                        try ScheduleEngine.generate(channelID: channel.id, media: realMedia, existing: [], now: .now).forEach(context.insert)
                        StudioLog.info("Initialized an empty channel pool for \(channel.name) with \(realMedia.count) playable titles.")
                    } else {
                        StudioLog.info("Preserved curated program pool for \(channel.name) during import.")
                    }
                }
                source.statusMessage = "Indexed \(results.count) video files and scheduled \(realMedia.count) playable titles on \(channels.count) channels."
                StudioLog.info("Import completed: \(realMedia.count) playable titles scheduled across \(channels.count) channels.")
            } else {
                source.statusMessage = results.isEmpty ? "No supported video files found." : "Indexed \(results.count) video files; no playable videos were found."
                StudioLog.error("Import completed without playable media for \(source.displayName).")
            }
            source.lastScannedAt = .now
            firstStationScan = FirstStationScan(
                sourceName: source.displayName,
                playableCount: results.filter { $0.support == .playable }.count,
                unsupportedCount: results.filter { $0.support != .playable }.count
            )
        } catch {
            source.statusMessage = error.localizedDescription
            StudioLog.error("Import failed for \(source.displayName): \(error.localizedDescription)")
        }
    }

    private func createFirstStation(name: String, mood: StationMood) {
        let playable = libraryMedia.filter { $0.support == .playable && !$0.path.hasPrefix("demo://") }
        guard !playable.isEmpty else { return }
        let channel = StationChannel(number: (channels.map(\.number).max() ?? 1) + 1, name: name, colorHex: mood.colorHex, theme: mood.rawValue)
        context.insert(channel)
        playable.forEach { context.insert(ChannelAssignment(channelID: channel.id, mediaID: $0.id)) }
        do {
            try ScheduleEngine.generate(channelID: channel.id, media: playable, existing: [], now: .now).forEach(context.insert)
            try context.save()
            watchLaunchMessage = "Your channel is live"
            firstStationScan = nil
            selection = .watch
        } catch {
            context.delete(channel)
            message = error.localizedDescription
        }
    }

    private func resolvedSourceURL(_ source: MediaSource, media: [LibraryMedia]) throws -> (url: URL, fromBookmark: Bool) {
        do {
            return (try BookmarkStore.resolve(source.bookmarkData), true)
        } catch {
            guard !BookmarkStore.isSandboxed,
                  let folder = DevelopmentSourceRecovery.folder(for: source.id, displayName: source.displayName, media: media) else {
                throw error
            }
            StudioLog.info("Bookmark decode failed for \(source.displayName); using direct development access to \(folder.path).")
            return (folder, false)
        }
    }
}

private enum SidebarDestination: Hashable { case home, watch, channels, schedule, library, bumpers, analytics, settings }

struct HomeView: View {
    @Binding var importFolder: Bool
    @Binding var scan: FirstStationScan?
    let createFirstStation: (String, StationMood) -> Void
    @Query private var channels: [StationChannel]
    @Query private var media: [LibraryMedia]
    @State private var stationName = "My Channel"
    @State private var mood: StationMood = .classic
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 28) {
                VStack(alignment: .leading, spacing: 10) {
                    Label(channels.isEmpty ? "Make your first channel" : "Your personal TV station", systemImage: "play.tv.fill")
                        .font(.title.bold())
                    Text(channels.isEmpty ? "Choose one folder. NostalgiaBox will make a live channel from the videos it can play." : "Build continuously scheduled channels from media you own or are authorized to use. Nothing leaves this Mac.")
                        .font(.title3)
                        .foregroundStyle(.secondary)
                    Button(channels.isEmpty ? "Choose Media Folder" : "Add Media Folder", systemImage: "folder.badge.plus") { importFolder = true }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.large)
                }
                HStack(spacing: 16) {
                    StationStat(value: channels.count, label: "Channels", icon: "rectangle.3.group")
                    StationStat(value: realMedia.count, label: "Playable titles", icon: "film")
                    StationStat(value: media.count - realMedia.count, label: "Needs attention", icon: "exclamationmark.triangle")
                }
                if let scan, channels.isEmpty {
                    GroupBox("Your scan is ready") {
                        VStack(alignment: .leading, spacing: 14) {
                            Text("\(scan.playableCount) playable video\(scan.playableCount == 1 ? "" : "s") found.").font(.title3.bold())
                            Text(scan.unsupportedCount == 0 ? "Everything supported can be added to your first channel." : "\(scan.unsupportedCount) file\(scan.unsupportedCount == 1 ? " needs" : "s need") attention and won’t be scheduled.").foregroundStyle(.secondary)
                            TextField("Channel name", text: $stationName).textFieldStyle(.roundedBorder)
                            Picker("Mood", selection: $mood) { ForEach(StationMood.allCases) { Text($0.rawValue).tag($0) } }
                            Button("Create Live Channel", systemImage: "play.circle.fill") { createFirstStation(stationName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "My Channel" : stationName, mood) }
                                .buttonStyle(.borderedProminent).disabled(scan.playableCount == 0)
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(4)
                    }
                } else if channels.isEmpty {
                    GroupBox("Three simple steps") { Text("Choose a folder → review what can play → name your channel. We’ll build the automatic schedule for you.").frame(maxWidth: .infinity, alignment: .leading).padding(4) }
                }
            }
            .padding(32)
            .frame(maxWidth: 920, alignment: .leading)
        }
        .navigationTitle("Welcome")
    }

    private var realMedia: [LibraryMedia] { media.filter { !$0.path.hasPrefix("demo://") && $0.support == .playable } }
}

struct LibraryView: View {
    @Binding var importFolder: Bool
    @Query(sort: \LibraryMedia.title) private var media: [LibraryMedia]
    @Query(sort: \StationChannel.number) private var channels: [StationChannel]
    @State private var query = ""
    @State private var filter: LibraryFilter = .playable
    @State private var addingToChannel: LibraryMedia?

    private var displayedMedia: [LibraryMedia] {
        let filtered = media.filter { item in
            switch filter {
            case .all: return true
            case .playable: return item.support == .playable
            case .attention: return item.support != .playable
            }
        }
        guard !query.isEmpty else { return filtered }
        return filtered.filter { $0.title.localizedCaseInsensitiveContains(query) || $0.path.localizedCaseInsensitiveContains(query) }
    }

    var body: some View {
        Group {
            if displayedMedia.isEmpty {
                ContentUnavailableView(
                    query.isEmpty ? "No \(filter.title.lowercased()) titles" : "No matching titles",
                    systemImage: query.isEmpty ? "film.stack" : "magnifyingglass",
                    description: Text(query.isEmpty ? "Add a media folder to build your local catalog." : "Try a different title or clear your search.")
                )
            } else {
                List(displayedMedia) { item in
                    LibraryRow(item: item, onAddToChannel: item.support == .playable && !channels.isEmpty ? { addingToChannel = item } : nil)
                        .listRowInsets(.init(top: 8, leading: 18, bottom: 8, trailing: 18))
                }
                .listStyle(.plain)
            }
        }
        .navigationTitle("Library")
        .searchable(text: $query, prompt: "Search titles or paths")
        .toolbar {
            ToolbarItem(placement: .primaryAction) { Button("Add Folder", systemImage: "folder.badge.plus") { importFolder = true } }
            ToolbarItem(placement: .automatic) {
                Picker("Catalog filter", selection: $filter) {
                    ForEach(LibraryFilter.allCases) { Text($0.title).tag($0) }
                }
                .pickerStyle(.segmented)
                .frame(width: 260)
            }
        }
        .safeAreaInset(edge: .top) {
            HStack {
                Label("\(media.filter { $0.support == .playable }.count) playable · \(media.filter { $0.support != .playable }.count) need attention", systemImage: "checkmark.circle")
                Spacer()
                Text(channels.isEmpty ? "Create a channel from Home to start programming." : "Add playable titles directly to a channel.").foregroundStyle(.secondary)
            }.font(.caption).padding(.horizontal).padding(.vertical, 8).background(.bar)
        }
        .sheet(item: $addingToChannel) { item in
            AddTitleToChannelSheet(item: item)
        }
    }
}

private enum LibraryFilter: String, CaseIterable, Identifiable {
    case playable, attention, all
    var id: Self { self }
    var title: String { rawValue.capitalized }
}

private struct StationStat: View {
    let value: Int
    let label: String
    let icon: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Image(systemName: icon).foregroundStyle(.tint)
            Text(value, format: .number).font(.title.bold()).monospacedDigit()
            Text(label).font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 14))
    }
}

struct ChannelsView: View {
    @Environment(\.modelContext) private var context
    @Query(sort: \StationChannel.number) private var channels: [StationChannel]
    @Query private var media: [LibraryMedia]
    @Query private var assignments: [ChannelAssignment]
    @Query private var programs: [ScheduledProgram]
    @State private var editingChannel: StationChannel?
    var body: some View {
        Group {
            if channels.isEmpty {
                ContentUnavailableView("No channels yet", systemImage: "rectangle.3.group", description: Text("Start in Home: choose a media folder and create your first live channel."))
            } else {
                ScrollView {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 260), spacing: 16)], spacing: 16) {
                    ForEach(channels) { channel in
                    ChannelRow(
                        channel: channel,
                        titleCount: assignments.filter { $0.channelID == channel.id }.count,
                        nowTitle: title(for: ScheduleEngine.current(from: programs.filter { $0.channelID == channel.id })),
                        nextTitle: title(for: programs.filter { $0.channelID == channel.id && $0.startsAt > .now }.sorted { $0.startsAt < $1.startsAt }.first),
                        onEdit: { editingChannel = channel },
                        onBuild: { buildSchedule(for: channel) }
                    )
                    }
                    }.padding(24)
                }
            }
        }
        .navigationTitle("Channels")
        .toolbar { Button("New Channel", systemImage: "plus") { createChannel() } }
        .sheet(item: $editingChannel) { channel in ChannelEditor(channel: channel) }
    }

    private func buildSchedule(for channel: StationChannel) {
        let assignedIDs = Set(assignments.filter { $0.channelID == channel.id }.map(\.mediaID))
        let playable = media.filter { assignedIDs.contains($0.id) && $0.support == .playable && !$0.path.hasPrefix("demo://") }
        guard !playable.isEmpty else { return }
        guard let generated = try? ScheduleEngine.generate(channelID: channel.id, media: playable, existing: programs.filter { $0.channelID == channel.id && $0.isOverride }, now: .now) else { return }
        programs.filter { $0.channelID == channel.id && !$0.isOverride }.forEach(context.delete)
        generated.forEach(context.insert)
    }

    private func createChannel() {
        let channel = StationChannel(number: (channels.map(\.number).max() ?? 1) + 1, name: "New Channel", colorHex: "E66B45")
        context.insert(channel)
        editingChannel = channel
    }

    private func title(for program: ScheduledProgram?) -> String? { program.flatMap { program in media.first(where: { $0.id == program.mediaID })?.title } }
}

private struct ChannelRow: View {
    let channel: StationChannel
    let titleCount: Int
    let nowTitle: String?
    let nextTitle: String?
    let onEdit: () -> Void
    let onBuild: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
            Text("CH \(String(format: "%02d", channel.number))")
                .font(.system(.title3, design: .monospaced, weight: .bold))
                .foregroundStyle(.white)
                .padding(9).background(Color(hex: channel.colorHex), in: RoundedRectangle(cornerRadius: 8))
            VStack(alignment: .leading, spacing: 3) {
                Text(channel.name).font(.headline)
                Text("\(titleCount) titles · \(channel.theme)").font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            }
            Divider()
            Text("NOW · \(nowTitle ?? "Automatic schedule is ready")").lineLimit(1).font(.subheadline.weight(.medium))
            Text("UP NEXT · \(nextTitle ?? "Build schedule to preview")").lineLimit(1).font(.caption).foregroundStyle(.secondary)
            HStack { Button("Refresh Schedule", action: onBuild).buttonStyle(.bordered).disabled(titleCount == 0); Spacer(); Button("Edit", action: onEdit).buttonStyle(.bordered) }
        }
        .padding(18).background(.quaternary, in: RoundedRectangle(cornerRadius: 16)).contentShape(Rectangle())
    }
}

private struct ChannelEditor: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(\.modelContext) private var context
    @Bindable var channel: StationChannel
    @Query private var media: [LibraryMedia]
    @Query private var assignments: [ChannelAssignment]
    @Query private var programs: [ScheduledProgram]
    @Query(sort: \BumperAsset.createdAt) private var bumpers: [BumperAsset]
    @State private var confirmDelete = false
    @State private var editingPool = false
    @State private var saveError: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Identity") {
                    TextField("Channel name", text: $channel.name)
                    Stepper("Channel \(channel.number)", value: $channel.number, in: 1...999)
                    TextField("Color hex", text: $channel.colorHex)
                    Picker("Theme", selection: $channel.theme) {
                        Text("Classic").tag("Classic")
                        Text("Saturday Morning").tag("Saturday Morning")
                        Text("Midnight").tag("Midnight")
                    }
                }
                Section("Programming") {
                    LabeledContent("Program pool", value: "\(assignedTitleCount) titles")
                    Text(assignedTitleCount == 0 ? "Choose titles before generating a schedule." : "Only titles in this pool are used when the schedule is built.")
                        .font(.caption).foregroundStyle(.secondary)
                    Button("Edit Program Pool", systemImage: "checklist") { editingPool = true }
                    Button("Build Schedule", systemImage: "calendar.badge.clock") { buildSchedule() }
                        .disabled(assignedTitleCount == 0)
                    Button("Use Entire Playable Library", systemImage: "film.stack") { useEntireLibraryAndBuildSchedule() }
                }
                Section("Presentation") {
                    Picker("Channel bumper", selection: $channel.bumperMediaID) {
                        Text("No bumper").tag(UUID?.none)
                        ForEach(bumpers) { bumper in
                            Text("\(bumper.channelName) · \(bumper.templateRaw)").tag(Optional(bumper.id))
                        }
                    }
                    Text(bumpers.isEmpty ? "Render a silent bumper in Bumper Studio to attach it here." : "The bumper plays before each title on this channel.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section {
                    Button("Delete Channel", role: .destructive) { confirmDelete = true }
                }
            }
            .formStyle(.grouped)
            .navigationTitle("Edit Channel")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save & Close") { saveAndDismiss() }
                }
            }
        }
        .padding()
        .frame(width: 440)
        .sheet(isPresented: $editingPool) { ChannelMediaPicker(channelID: channel.id) }
        .alert("Delete \(channel.name)?", isPresented: $confirmDelete) {
            Button("Delete", role: .destructive) { deleteChannel() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This removes this channel and its schedule. Your library is unchanged.")
        }
        .alert("Couldn’t Save Channel", isPresented: Binding(get: { saveError != nil }, set: { if !$0 { saveError = nil } })) {
            Button("OK", role: .cancel) { saveError = nil }
        } message: {
            Text(saveError ?? "Unknown error")
        }
    }

    private func buildSchedule() {
        let assignedIDs = Set(assignments.filter { $0.channelID == channel.id }.map(\.mediaID))
        let playable = media.filter { assignedIDs.contains($0.id) && $0.support == .playable && !$0.path.hasPrefix("demo://") }
        programs.filter { $0.channelID == channel.id && !$0.isOverride }.forEach(context.delete)
        let overrides = programs.filter { $0.channelID == channel.id && $0.isOverride }
        guard let generated = try? ScheduleEngine.generate(channelID: channel.id, media: playable, existing: overrides) else { return }
        generated.forEach(context.insert)
    }

    private var assignedTitleCount: Int { assignments.filter { $0.channelID == channel.id }.count }

    private func useEntireLibraryAndBuildSchedule() {
        let playable = media.filter { $0.support == .playable && !$0.path.hasPrefix("demo://") }
        assignments.filter { $0.channelID == channel.id }.forEach(context.delete)
        playable.forEach { context.insert(ChannelAssignment(channelID: channel.id, mediaID: $0.id)) }
        programs.filter { $0.channelID == channel.id && !$0.isOverride }.forEach(context.delete)
        guard let generated = try? ScheduleEngine.generate(channelID: channel.id, media: playable, existing: []) else { return }
        generated.forEach(context.insert)
    }

    private func deleteChannel() {
        assignments.filter { $0.channelID == channel.id }.forEach(context.delete)
        programs.filter { $0.channelID == channel.id }.forEach(context.delete)
        context.delete(channel)
        dismiss()
    }

    private func saveAndDismiss() {
        do {
            try context.save()
            StudioLog.info("Saved channel \(channel.number): \(channel.name).")
            dismiss()
        } catch {
            StudioLog.error("Failed to save channel \(channel.id.uuidString.prefix(8)): \(error.localizedDescription)")
            saveError = error.localizedDescription
        }
    }
}

private struct ChannelMediaPicker: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(\.modelContext) private var context
    let channelID: UUID
    @Query(sort: \LibraryMedia.title) private var media: [LibraryMedia]
    @Query private var assignments: [ChannelAssignment]
    @State private var query = ""

    private var displayedMedia: [LibraryMedia] {
        let realMedia = media.filter { !$0.path.hasPrefix("demo://") && $0.support == .playable }
        guard !query.isEmpty else { return realMedia }
        return realMedia.filter { $0.title.localizedCaseInsensitiveContains(query) }
    }

    var body: some View {
        NavigationStack {
            Group {
                if displayedMedia.isEmpty {
                    ContentUnavailableView(query.isEmpty ? "No playable titles" : "No matching titles", systemImage: query.isEmpty ? "film" : "magnifyingglass")
                } else {
                    List(displayedMedia) { item in
                        Toggle(isOn: binding(for: item)) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(item.title).lineLimit(1)
                                Text(Duration.seconds(item.duration), format: .time(pattern: .hourMinuteSecond))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .toggleStyle(.checkbox)
                    }
                    .listStyle(.plain)
                }
            }
            .searchable(text: $query, prompt: "Find a title")
            .navigationTitle("Program Pool · \(selectedCount)")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
                ToolbarItem(placement: .primaryAction) { Button("Clear Pool", role: .destructive) { clearPool() }.disabled(selectedCount == 0) }
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
        .frame(minWidth: 520, minHeight: 560)
    }

    private func binding(for item: LibraryMedia) -> Binding<Bool> {
        Binding(
            get: { assignments.contains { $0.channelID == channelID && $0.mediaID == item.id } },
            set: { enabled in
                if enabled {
                    if !assignments.contains(where: { $0.channelID == channelID && $0.mediaID == item.id }) { context.insert(ChannelAssignment(channelID: channelID, mediaID: item.id)) }
                } else {
                    assignments.filter { $0.channelID == channelID && $0.mediaID == item.id }.forEach(context.delete)
                }
            }
        )
    }

    private var selectedCount: Int { assignments.filter { $0.channelID == channelID }.count }

    private func clearPool() {
        assignments.filter { $0.channelID == channelID }.forEach(context.delete)
    }
}

private struct LibraryRow: View {
    let item: LibraryMedia
    let onAddToChannel: (() -> Void)?

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: item.support == .playable ? "film" : "exclamationmark.triangle")
                .foregroundStyle(item.support == .playable ? AnyShapeStyle(.tint) : AnyShapeStyle(.orange))
                .frame(width: 20)
            VStack(alignment: .leading, spacing: 4) {
                Text(item.title).lineLimit(1)
                HStack(spacing: 6) {
                    Text(item.support == .playable ? "Playable" : "Needs attention")
                    Text("·")
                    Text(item.modifiedAt, format: .relative(presentation: .named))
                }
                .font(.caption).foregroundStyle(.secondary)
                Text(item.path).lineLimit(1).font(.caption2).foregroundStyle(.tertiary)
                if let warning = item.warning { Text(warning).lineLimit(1).font(.caption2).foregroundStyle(.orange) }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 4) {
                Text(Duration.seconds(item.duration), format: .time(pattern: .hourMinuteSecond))
                Text(ByteCountFormatter.string(fromByteCount: item.fileSize, countStyle: .file))
            }
            .font(.caption).foregroundStyle(.secondary)
            if let onAddToChannel {
                Button("Add", systemImage: "plus") { onAddToChannel() }
                    .buttonStyle(.bordered)
                    .accessibilityLabel("Add \(item.title) to a channel")
            }
        }
    }
}

private struct AddTitleToChannelSheet: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(\.modelContext) private var context
    let item: LibraryMedia
    @Query(sort: \StationChannel.number) private var channels: [StationChannel]
    @Query private var assignments: [ChannelAssignment]
    @Query private var media: [LibraryMedia]
    @Query private var programs: [ScheduledProgram]

    var body: some View {
        NavigationStack {
            List(channels) { channel in
                Button { add(item, to: channel) } label: {
                    HStack {
                        Circle().fill(Color(hex: channel.colorHex)).frame(width: 12, height: 12)
                        VStack(alignment: .leading) {
                            Text(channel.name)
                            Text("CH \(channel.number) · \(titleCount(for: channel)) titles").font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if assignments.contains(where: { $0.channelID == channel.id && $0.mediaID == item.id }) { Image(systemName: "checkmark").foregroundStyle(.tint) }
                    }
                }.buttonStyle(.plain)
            }
            .navigationTitle("Add \(item.title)")
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } } }
        }
        .frame(minWidth: 420, minHeight: 300)
    }

    private func titleCount(for channel: StationChannel) -> Int { assignments.filter { $0.channelID == channel.id }.count }

    private func add(_ item: LibraryMedia, to channel: StationChannel) {
        guard !assignments.contains(where: { $0.channelID == channel.id && $0.mediaID == item.id }) else { return }
        context.insert(ChannelAssignment(channelID: channel.id, mediaID: item.id))
        let ids = Set(assignments.filter { $0.channelID == channel.id }.map(\.mediaID)).union([item.id])
        let pool = media.filter { ids.contains($0.id) && $0.support == .playable }
        let overrides = programs.filter { $0.channelID == channel.id && $0.isOverride }
        programs.filter { $0.channelID == channel.id && !$0.isOverride }.forEach(context.delete)
        if let schedule = try? ScheduleEngine.generate(channelID: channel.id, media: pool, existing: overrides) {
            schedule.forEach(context.insert)
        }
        dismiss()
    }
}

struct ScheduleView: View {
    @Environment(\.modelContext) private var context
    @Query(sort: \ScheduledProgram.startsAt) private var programs: [ScheduledProgram]
    @Query private var media: [LibraryMedia]
    @Query(sort: \StationChannel.number) private var channels: [StationChannel]
    @State private var selectedChannelID: UUID?
    @State private var editingProgram: ScheduledProgram?

    private var filteredPrograms: [ScheduledProgram] { programs.filter { selectedChannelID == nil || $0.channelID == selectedChannelID } }
    private var activePrograms: [ScheduledProgram] { filteredPrograms.filter { $0.startsAt <= .now && $0.endsAt > .now } }
    private var upcomingPrograms: [ScheduledProgram] { filteredPrograms.filter { $0.startsAt > .now }.prefix(80).map { $0 } }

    var body: some View {
        Group {
            if activePrograms.isEmpty && upcomingPrograms.isEmpty {
                ContentUnavailableView("No scheduled programs", systemImage: "calendar", description: Text("Build a schedule from the Channels screen after adding playable media."))
            } else {
                List {
                    Section {
                        Label("Automatic schedule", systemImage: "wand.and.stars")
                        Text("Your channel fills itself from its program pool. Pin or replace a title only when you want control.").font(.caption).foregroundStyle(.secondary)
                    }
                    if !activePrograms.isEmpty {
                        Section("On now") {
                            ForEach(activePrograms) { program in
                                ScheduleRow(program: program, title: title(for: program), channel: channel(for: program), isLive: true, onTogglePin: nil, onEdit: nil)
                            }
                        }
                    }
                    Section("Up next") {
                            ForEach(upcomingPrograms) { program in
                                ScheduleRow(program: program, title: title(for: program), channel: channel(for: program), isLive: false, onTogglePin: { program.isOverride.toggle() }, onEdit: { editingProgram = program })
                        }
                    }
                }
                .listStyle(.inset)
            }
        }
        .navigationTitle("Schedule")
        .toolbar {
            Picker("Channel", selection: $selectedChannelID) {
                Text("All channels").tag(UUID?.none)
                ForEach(channels) { channel in
                    Text("CH \(channel.number) · \(channel.name)").tag(Optional(channel.id))
                }
            }
            .frame(minWidth: 220)
        }
        .sheet(item: $editingProgram) { program in
            ScheduleOverrideEditor(program: program)
        }
    }

    private func title(for program: ScheduledProgram) -> String { media.first(where: { $0.id == program.mediaID })?.title ?? "Unavailable media" }
    private func channel(for program: ScheduledProgram) -> StationChannel? { channels.first(where: { $0.id == program.channelID }) }
}

private struct ScheduleRow: View {
    let program: ScheduledProgram
    let title: String
    let channel: StationChannel?
    let isLive: Bool
    let onTogglePin: (() -> Void)?
    let onEdit: (() -> Void)?

    var body: some View {
        HStack(spacing: 12) {
            Circle().fill(isLive ? .red : Color(hex: channel?.colorHex ?? "727272")).frame(width: 10, height: 10)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).lineLimit(1)
                Text(channel.map { "CH \($0.number) · \($0.name)" } ?? "Unknown channel")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if isLive {
                Text("LIVE").font(.caption.weight(.bold)).foregroundStyle(.red)
            } else {
                VStack(alignment: .trailing, spacing: 3) {
                    Text(program.startsAt, format: .dateTime.weekday(.abbreviated).hour().minute())
                    DisclosureGroup("Adjust") {
                        Button(program.isOverride ? "Unpin" : "Pin") { onTogglePin?() }
                        Button("Replace") { onEdit?() }
                    }.font(.caption2)
                }
                .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

private struct ScheduleOverrideEditor: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(\.modelContext) private var context
    @Bindable var program: ScheduledProgram
    @Query(sort: \LibraryMedia.title) private var media: [LibraryMedia]
    @Query private var assignments: [ChannelAssignment]
    @Query private var programs: [ScheduledProgram]
    @State private var query = ""
    @State private var selectedMediaID: UUID

    init(program: ScheduledProgram) {
        _program = Bindable(wrappedValue: program)
        _selectedMediaID = State(initialValue: program.mediaID)
    }

    private var pool: [LibraryMedia] {
        let ids = Set(assignments.filter { $0.channelID == program.channelID }.map(\.mediaID))
        let available = media.filter { ids.contains($0.id) && $0.support == .playable && !$0.path.hasPrefix("demo://") }
        guard !query.isEmpty else { return available }
        return available.filter { $0.title.localizedCaseInsensitiveContains(query) }
    }

    var body: some View {
        NavigationStack {
            List(pool) { item in
                Button {
                    selectedMediaID = item.id
                } label: {
                    HStack {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(item.title)
                            Text(Duration.seconds(item.duration), format: .time(pattern: .hourMinuteSecond))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if item.id == selectedMediaID { Image(systemName: "checkmark").foregroundStyle(.tint) }
                    }
                }
                .buttonStyle(.plain)
            }
            .searchable(text: $query, prompt: "Find a title in this channel")
            .navigationTitle("Replace Scheduled Title")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Save Override") { save() }.disabled(pool.isEmpty) }
            }
        }
        .frame(minWidth: 560, minHeight: 580)
    }

    private func save() {
        guard let item = media.first(where: { $0.id == selectedMediaID }) else { return }
        program.mediaID = item.id
        program.endsAt = program.startsAt.addingTimeInterval(max(1, item.duration))
        program.isOverride = true
        let boundary = program.startsAt
        var overrides = programs.filter { $0.channelID == program.channelID && $0.isOverride && $0.startsAt >= boundary }
        if !overrides.contains(where: { $0.id == program.id }) { overrides.append(program) }
        let ids = Set(assignments.filter { $0.channelID == program.channelID }.map(\.mediaID))
        let pool = media.filter { ids.contains($0.id) && $0.support == .playable && !$0.path.hasPrefix("demo://") }
        programs.filter { $0.channelID == program.channelID && !$0.isOverride && $0.startsAt >= boundary }.forEach(context.delete)
        if let generated = try? ScheduleEngine.generate(channelID: program.channelID, media: pool, existing: overrides, now: boundary) {
            generated.filter { !$0.isOverride }.forEach(context.insert)
        }
        StudioLog.info("Saved future schedule override for channel \(program.channelID.uuidString.prefix(8)).")
        dismiss()
    }
}

struct WatchView: View {
    @Environment(\.modelContext) private var context
    let channels: [StationChannel]
    @Binding var launchMessage: String?
    @Query private var programs: [ScheduledProgram]
    @Query private var media: [LibraryMedia]
    @Query private var sources: [MediaSource]
    @Query private var bumpers: [BumperAsset]
    @State private var selectedChannelID: UUID?
    @State private var player = AVPlayer()
    @State private var bumperAudioPlayer: AVAudioPlayer?
    @State private var accessedSourceURL: URL?
    @State private var sourceAccessRetainedByRegistry = false
    @State private var playerItemStatusObservation: NSKeyValueObservation?
    @State private var playerEndObserver: NSObjectProtocol?
    @State private var scheduledBoundaryTask: Task<Void, Never>?
    @State private var playbackMessage: String?
    @State private var activePlaybackEvent: PlaybackEvent?
    @State private var loadedProgramID: UUID?
    @State private var pendingProgramAfterBumper: UUID?
    @State private var channelBug: ChannelBug?
    @State private var channelBugHideTask: Task<Void, Never>?

    private var channel: StationChannel? {
        if let selectedChannelID, let selected = channels.first(where: { $0.id == selectedChannelID }) { return selected }
        return channels.first
    }

    private var selectedIndex: Int { channels.firstIndex(where: { $0.id == channel?.id }) ?? 0 }
    private var nextProgram: ScheduledProgram? {
        guard let channel, let current = ScheduleEngine.current(from: programs.filter({ $0.channelID == channel.id })) else { return nil }
        return programs.filter { $0.channelID == channel.id && $0.startsAt >= current.endsAt }.sorted { $0.startsAt < $1.startsAt }.first
    }

    var body: some View {
        let program = channel.flatMap { channel in ScheduleEngine.current(from: programs.filter { $0.channelID == channel.id }) }
        VStack(spacing: 16) {
            NativePlayerView(player: player)
                .overlay(alignment: .topLeading) {
                    if let playbackMessage {
                        VStack(spacing: 10) { Image(systemName: "exclamationmark.triangle").font(.system(size: 48)); Text(playbackMessage).multilineTextAlignment(.center) }.foregroundStyle(.white)
                    } else if program == nil || program.flatMap({ p in media.first(where: { $0.id == p.mediaID }) })?.path.hasPrefix("demo://") == true {
                        VStack { Image(systemName: "play.rectangle.fill").font(.system(size: 64)); Text(program.flatMap { p in media.first(where: { $0.id == p.mediaID }) }?.title ?? "Demo signal").font(.title2) }.foregroundStyle(.white)
                    } else if let channelBug {
                        ChannelBugView(bug: channelBug)
                            .padding(28)
                            .transition(.opacity)
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 20))
                .aspectRatio(16/9, contentMode: .fit)
                .frame(maxWidth: 1_200)
            VStack(alignment: .leading, spacing: 5) {
                Text("NOW") .font(.caption.weight(.bold)).foregroundStyle(.secondary)
                Text(program.flatMap { current in media.first(where: { $0.id == current.mediaID }) }?.title ?? "No program is scheduled").font(.title2.bold())
                Text(nextProgram.flatMap { upcoming in media.first(where: { $0.id == upcoming.mediaID }) }.map { "NEXT · \($0.title)" } ?? "Automatic schedule").foregroundStyle(.secondary)
            }
            .frame(maxWidth: 1_200, alignment: .leading)
            HStack {
                Button("Previous", systemImage: "chevron.left") { select(offset: -1) }
                    .keyboardShortcut("[", modifiers: [.command])
                    .help("Previous channel (Command-[)")
                Spacer()
                Text(channel.map { "CH \($0.number) · \($0.name)" } ?? "No channel").font(.headline)
                Spacer()
                Button("Full Screen", systemImage: "arrow.up.left.and.arrow.down.right") { NSApplication.shared.keyWindow?.toggleFullScreen(nil) }
                    .help("Toggle full-screen viewing")
                Button("Next", systemImage: "chevron.right") { select(offset: 1) }
                    .keyboardShortcut("]", modifiers: [.command])
                    .help("Next channel (Command-])")
            }
            .buttonStyle(.bordered)
        }
        .padding()
        .navigationTitle("Watch")
        .overlay(alignment: .top) {
            if let launchMessage {
                Label(launchMessage, systemImage: "checkmark.circle.fill")
                    .font(.headline).padding(12).background(.regularMaterial, in: Capsule()).padding()
            }
        }
        .onAppear {
            loadCurrentProgram()
            if launchMessage != nil {
                DispatchQueue.main.asyncAfter(deadline: .now() + 4) { launchMessage = nil }
            }
        }
        .onDisappear(perform: finishWatching)
        .onChange(of: media.map(\.id)) { _, _ in preferPlayableChannelAndLoad() }
        .onChange(of: programs.map(\.id)) { _, _ in preferPlayableChannelAndLoad() }
    }

    private func loadCurrentProgram() {
        guard let channel, let program = ScheduleEngine.current(from: programs.filter { $0.channelID == channel.id }), let item = media.first(where: { $0.id == program.mediaID }), !item.path.hasPrefix("demo://") else {
            StudioLog.info("No real program is available for the selected channel; showing demo placeholder.")
            detachViewer()
            releaseSourceAccess()
            playbackMessage = nil
            return
        }
        guard loadedProgramID != program.id || player.currentItem == nil else { return }
        load(program: program, channel: channel, item: item)
    }

    private func load(program: ScheduledProgram, channel: StationChannel, item: LibraryMedia) {
        finalizePlaybackEvent()
        detachViewer(resetProgram: false)
        releaseSourceAccess()
        loadedProgramID = program.id
        playbackMessage = nil
        showChannelBug(channel: channel, item: item)
        StudioLog.info("Loading channel \(channel.number): \(item.title).")
        if pendingProgramAfterBumper != program.id,
           let bumperID = channel.bumperMediaID,
           let bumper = bumpers.first(where: { $0.id == bumperID }),
           FileManager.default.fileExists(atPath: bumper.filePath) {
            pendingProgramAfterBumper = program.id
            play(bumper: bumper, before: program, channel: channel, item: item)
            return
        }
        pendingProgramAfterBumper = nil
        guard let sourceID = item.sourceID, let source = sources.first(where: { $0.id == sourceID }) else {
            StudioLog.error("Playback source record is missing for \(item.title).")
            playbackMessage = "This program is part of the synthetic demo."
            return
        }
        do {
            if let retainedURL = SourceAccessRegistry.url(for: source.id) {
                accessedSourceURL = retainedURL
                sourceAccessRetainedByRegistry = true
                StudioLog.info("Reusing retained folder access for source \(source.displayName).")
            } else {
                let sourceURL = try BookmarkStore.resolve(source.bookmarkData)
                guard sourceURL.startAccessingSecurityScopedResource() else {
                    StudioLog.error("Playback access was denied for source \(source.displayName).")
                    playbackMessage = "NostalgiaBox no longer has permission to read this media folder."
                    return
                }
                accessedSourceURL = sourceURL
                sourceAccessRetainedByRegistry = false
                StudioLog.info("Playback access granted for source \(source.displayName).")
            }
        } catch {
            // `swift run` is not sandboxed. Preserve the useful local-development
            // workflow when an old persisted bookmark cannot be decoded; a signed
            // App Store build still requires the user-selected security scope.
            let isSandboxed = ProcessInfo.processInfo.environment["APP_SANDBOX_CONTAINER_ID"] != nil
            if !isSandboxed && FileManager.default.fileExists(atPath: item.path) {
                StudioLog.info("Bookmark decode failed for \(source.displayName); using direct development access to \(item.title).")
            } else {
                StudioLog.error("Playback bookmark failed for source \(source.displayName): \(error.localizedDescription)")
                playbackMessage = "This media folder is unavailable. Add it again in Library."
                return
            }
        }
        let elapsed = max(0, Date.now.timeIntervalSince(program.startsAt))
        let title = item.title
        let mediaItem = AVPlayerItem(url: URL(fileURLWithPath: item.path))
        playerItemStatusObservation?.invalidate()
        playerItemStatusObservation = mediaItem.observe(\.status, options: [.initial, .new]) { observedItem, _ in
            switch observedItem.status {
            case .readyToPlay:
                StudioLog.info("AVPlayerItem is ready: \(title).")
            case .failed:
                StudioLog.error("AVPlayerItem failed for \(title): \(observedItem.error?.localizedDescription ?? "unknown error")")
            case .unknown:
                StudioLog.info("AVPlayerItem is loading: \(title).")
            @unknown default:
                StudioLog.error("AVPlayerItem reported an unknown status for \(title).")
            }
        }
        player.replaceCurrentItem(with: mediaItem)
        player.seek(to: CMTime(seconds: min(elapsed, max(item.duration - 0.1, 0)), preferredTimescale: 600))
        player.play()
        StudioLog.info("AVPlayer started \(item.title) at \(Int(elapsed)) seconds.")
        let event = PlaybackEvent(channelID: program.channelID, mediaID: item.id)
        context.insert(event)
        activePlaybackEvent = event
        if let playerEndObserver { NotificationCenter.default.removeObserver(playerEndObserver) }
        let channelID = channel.id
        let completedProgramID = program.id
        playerEndObserver = NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: mediaItem, queue: .main) { _ in
            Task { @MainActor in
                StudioLog.info("AVPlayerItem reached its end: \(title).")
                self.playNextProgram(channelID: channelID, completedProgramID: completedProgramID)
            }
        }
        scheduledBoundaryTask?.cancel()
        let remaining = max(0, program.endsAt.timeIntervalSinceNow)
        scheduledBoundaryTask = Task {
            do { try await Task.sleep(for: .seconds(remaining)) }
            catch { return }
            guard !Task.isCancelled else { return }
            await MainActor.run {
                StudioLog.info("Scheduled boundary reached for \(title); advancing channel \(channel.number).")
                self.playNextProgram(channelID: channelID, completedProgramID: completedProgramID)
            }
        }
    }

    private func play(bumper: BumperAsset, before program: ScheduledProgram, channel: StationChannel, item: LibraryMedia) {
        let bumperName = bumper.channelName
        let title = item.title
        let scene = BumperSceneStore.scene(for: bumper, accentHex: channel.colorHex)
        let visualPath = scene.videoPath.flatMap { FileManager.default.fileExists(atPath: $0) ? $0 : nil } ?? bumper.filePath
        let bumperItem = AVPlayerItem(url: URL(fileURLWithPath: visualPath))
        playerItemStatusObservation?.invalidate()
        playerItemStatusObservation = bumperItem.observe(\.status, options: [.initial, .new]) { observedItem, _ in
            if observedItem.status == .failed {
                StudioLog.error("Bumper playback failed for \(bumperName): \(observedItem.error?.localizedDescription ?? "unknown error")")
            }
        }
        player.replaceCurrentItem(with: bumperItem)
        player.play()
        if let audioPath = scene.audioPath, FileManager.default.fileExists(atPath: audioPath) {
            bumperAudioPlayer = try? AVAudioPlayer(contentsOf: URL(fileURLWithPath: audioPath))
            bumperAudioPlayer?.play()
        }
        StudioLog.info("Playing \(bumper.templateRaw) bumper for channel \(channel.number).")
        if let playerEndObserver { NotificationCenter.default.removeObserver(playerEndObserver) }
        let programID = program.id
        let channelID = channel.id
        let mediaID = item.id
        playerEndObserver = NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: bumperItem, queue: .main) { _ in
            Task { @MainActor in
                guard let program = self.programs.first(where: { $0.id == programID }),
                      let channel = self.channels.first(where: { $0.id == channelID }),
                      let item = self.media.first(where: { $0.id == mediaID }) else { return }
                StudioLog.info("Bumper completed; starting \(title).")
                self.load(program: program, channel: channel, item: item)
            }
        }
    }

    private func playNextProgram(channelID: UUID, completedProgramID: UUID) {
        guard channel?.id == channelID else {
            StudioLog.info("Ignoring program-end notification because the viewer changed channels.")
            return
        }
        guard let completed = programs.first(where: { $0.id == completedProgramID }),
              let activeChannel = channel else {
            StudioLog.error("Could not find the completed program while advancing playback.")
            return
        }
        guard let next = programs
            .filter({ $0.channelID == channelID && $0.startsAt >= completed.endsAt })
            .sorted(by: { $0.startsAt < $1.startsAt })
            .first,
              let nextItem = media.first(where: { $0.id == next.mediaID }) else {
            StudioLog.error("No later scheduled title is available after \(completedProgramID.uuidString.prefix(8)).")
            return
        }
        StudioLog.info("Program ended; advancing to \(nextItem.title) on channel \(activeChannel.number).")
        load(program: next, channel: activeChannel, item: nextItem)
    }

    private func preferPlayableChannelAndLoad() {
        if let candidate = channels.first(where: { channel in
            guard let program = ScheduleEngine.current(from: programs.filter({ $0.channelID == channel.id })), let item = media.first(where: { $0.id == program.mediaID }) else { return false }
            return !item.path.hasPrefix("demo://")
        }) { selectedChannelID = candidate.id }
        loadCurrentProgram()
    }

    private func select(offset: Int) {
        guard !channels.isEmpty else { return }
        let index = min(max(selectedIndex + offset, 0), channels.count - 1)
        selectedChannelID = channels[index].id
        loadCurrentProgram()
    }

    private func showChannelBug(channel: StationChannel, item: LibraryMedia) {
        channelBugHideTask?.cancel()
        withAnimation(.easeOut(duration: 0.12)) {
            channelBug = ChannelBug(
                channelNumber: channel.number,
                channelName: channel.name,
                programTitle: item.title,
                colorHex: channel.colorHex
            )
        }
        channelBugHideTask = Task {
            try? await Task.sleep(for: .seconds(4))
            guard !Task.isCancelled else { return }
            await MainActor.run {
                withAnimation(.easeOut(duration: 0.18)) { channelBug = nil }
            }
        }
    }

    private func releaseSourceAccess() {
        if let accessedSourceURL { StudioLog.info("Released media-folder access for \(accessedSourceURL.lastPathComponent).") }
        if !sourceAccessRetainedByRegistry { accessedSourceURL?.stopAccessingSecurityScopedResource() }
        accessedSourceURL = nil
        sourceAccessRetainedByRegistry = false
    }

    private func finishWatching() {
        finalizePlaybackEvent()
        channelBugHideTask?.cancel()
        detachViewer()
        releaseSourceAccess()
    }

    private func detachViewer(resetProgram: Bool = true) {
        bumperAudioPlayer?.stop()
        bumperAudioPlayer = nil
        player.replaceCurrentItem(with: nil)
        playerItemStatusObservation?.invalidate()
        playerItemStatusObservation = nil
        if let playerEndObserver { NotificationCenter.default.removeObserver(playerEndObserver) }
        playerEndObserver = nil
        scheduledBoundaryTask?.cancel()
        scheduledBoundaryTask = nil
        if resetProgram {
            loadedProgramID = nil
            pendingProgramAfterBumper = nil
        }
    }

    private func finalizePlaybackEvent() {
        guard let event = activePlaybackEvent else { return }
        event.endedAt = .now
        event.accumulatedSeconds = max(0, event.endedAt!.timeIntervalSince(event.startedAt))
        activePlaybackEvent = nil
    }
}

private struct NativePlayerView: NSViewRepresentable {
    let player: AVPlayer

    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView()
        view.player = player
        view.controlsStyle = .none
        view.videoGravity = .resizeAspect
        return view
    }

    func updateNSView(_ view: AVPlayerView, context: Context) {
        if view.player !== player { view.player = player }
    }
}

private struct ChannelBug: Equatable {
    let channelNumber: Int
    let channelName: String
    let programTitle: String
    let colorHex: String
}

private struct ChannelBugView: View {
    let bug: ChannelBug

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text("CH \(String(format: "%02d", bug.channelNumber))")
                .font(.system(.title2, design: .monospaced, weight: .bold))
                .foregroundStyle(Color(hex: bug.colorHex))
            Text(bug.channelName.uppercased())
                .font(.system(.title3, design: .monospaced, weight: .bold))
            Text(bug.programTitle)
                .font(.system(.body, design: .monospaced, weight: .medium))
                .lineLimit(2)
                .frame(maxWidth: 460, alignment: .leading)
        }
        .foregroundStyle(.white)
        .padding(14)
        .background(.black.opacity(0.72), in: RoundedRectangle(cornerRadius: 10))
        .shadow(color: .black.opacity(0.8), radius: 4, x: 2, y: 2)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Channel \(bug.channelNumber), \(bug.channelName), now playing \(bug.programTitle)")
    }
}

struct BumperStudioView: View {
    @Environment(\.modelContext) private var context
    @Query(sort: \StationChannel.number) private var channels: [StationChannel]
    @Query(sort: \BumperAsset.createdAt, order: .reverse) private var bumpers: [BumperAsset]
    @State private var template: BumperTemplate = .channelID
    @State private var inspectorMode: BumperInspectorMode = .content
    @State private var selectedChannelID: UUID?
    @State private var scene = BumperScene.default(template: .channelID, channelName: "", accentHex: "7B4DFF")
    @State private var editingBumperID: UUID?
    @State private var importingImage = false
    @State private var importingVideo = false
    @State private var importingAudio = false
    @State private var status: String?
    @State private var previewPlayer = AVPlayer()
    @State private var showingPreview = false

    private var selectedChannel: StationChannel? {
        channels.first(where: { $0.id == selectedChannelID })
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                Label("Optional customization", systemImage: "sparkles")
                    .font(.title2.bold())
                Text("Your channel is complete without bumpers. Add one here when you want a custom transition or channel ID.")
                    .foregroundStyle(.secondary)
                HStack(alignment: .top, spacing: 24) {
                    GroupBox("Inspector") {
                        VStack(alignment: .leading, spacing: 16) {
                            Picker("Editing", selection: $inspectorMode) { ForEach(BumperInspectorMode.allCases) { Text($0.rawValue).tag($0) } }
                                .pickerStyle(.segmented)
                            inspectorControls
                            Divider()
                            HStack {
                                Button("Play Preview", systemImage: "play.fill") { playPreview() }.buttonStyle(.bordered)
                                Button(renderButtonTitle, systemImage: "wand.and.stars") { renderAndAttach() }
                                    .buttonStyle(.borderedProminent)
                                    .disabled(selectedChannel == nil || scene.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                            }
                            if let status {
                                Label(status, systemImage: "checkmark.circle.fill")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .frame(width: 470, alignment: .leading)
                    }
                    VStack(alignment: .leading, spacing: 10) {
                        HStack { Text("Live canvas").font(.headline); Spacer(); Text("16:9").font(.caption).foregroundStyle(.secondary) }
                        BumperPreview(scene: scene)
                    }
                        .frame(minWidth: 420, maxWidth: .infinity)
                }

                BumperTimeline(scene: scene)

                GroupBox("Rendered bumpers") {
                    if bumpers.isEmpty {
                        ContentUnavailableView("No bumpers yet", systemImage: "sparkles.rectangle.stack", description: Text("Create a silent visual bumper and attach it to a channel."))
                            .frame(maxWidth: .infinity, minHeight: 180)
                    } else {
                        LazyVStack(spacing: 0) {
                            ForEach(bumpers) { bumper in
                                BumperAssetRow(
                                    bumper: bumper,
                                    attachmentName: channels.first(where: { $0.bumperMediaID == bumper.id }).map { "Attached to CH \($0.number) · \($0.name)" } ?? "Not attached",
                                    canAttach: selectedChannel != nil,
                                    onAttach: { attach(bumper) },
                                    onEdit: { edit(bumper) },
                                    onDelete: { delete(bumper) }
                                )
                                if bumper.id != bumpers.last?.id { Divider() }
                            }
                        }
                    }
                }
            }
            .padding()
        }
        .navigationTitle("Bumper Studio")
        .task { selectInitialChannelIfNeeded() }
        .onChange(of: selectedChannelID) { _, _ in syncSceneToChannel() }
        .onChange(of: template) { _, value in if editingBumperID == nil { scene.title = value.rawValue.uppercased() } }
        .fileImporter(isPresented: $importingImage, allowedContentTypes: [.image]) { importAsset($0, into: \BumperScene.imagePath) }
        .fileImporter(isPresented: $importingVideo, allowedContentTypes: [.movie]) { importAsset($0, into: \BumperScene.videoPath) }
        .fileImporter(isPresented: $importingAudio, allowedContentTypes: [.audio]) { importAsset($0, into: \BumperScene.audioPath) }
        .sheet(isPresented: $showingPreview, onDismiss: { previewPlayer.replaceCurrentItem(with: nil) }) {
            VStack(spacing: 16) {
                NativePlayerView(player: previewPlayer).aspectRatio(16 / 9, contentMode: .fit).background(.black)
                Text("Preview uses the current unsaved scene.").font(.caption).foregroundStyle(.secondary)
                Button("Done") { showingPreview = false }
            }
            .padding().frame(minWidth: 760, minHeight: 520)
        }
    }

    private var renderButtonTitle: String {
        guard let channel = selectedChannel else { return "Choose a Channel" }
        return "Render & Attach to CH \(channel.number)"
    }
    private var templateDescription: String {
        switch template {
        case .channelID: "A short channel-identification card."
        case .nowPlaying: "A title card for the currently scheduled program."
        case .transition: "A brief visual break between programs."
        }
    }

    @ViewBuilder private var inspectorControls: some View {
        switch inspectorMode {
        case .content:
            Picker("Target channel", selection: $selectedChannelID) {
                Text("Choose a channel").tag(UUID?.none)
                ForEach(channels) { channel in Text("CH \(channel.number) · \(channel.name)").tag(Optional(channel.id)) }
            }
            Picker("Template", selection: $template) { ForEach(BumperTemplate.allCases, id: \.self) { Text($0.rawValue).tag($0) } }
            Text(templateDescription).font(.caption).foregroundStyle(.secondary)
            TextField("Headline", text: $scene.title).textFieldStyle(.roundedBorder)
            TextField("Subtitle", text: $scene.subtitle).textFieldStyle(.roundedBorder)
        case .style:
            Picker("Font", selection: $scene.titleFontName) {
                Text("Menlo Bold").tag("Menlo-Bold"); Text("Helvetica Bold").tag("Helvetica-Bold")
                Text("Avenir Next Demi Bold").tag("AvenirNext-DemiBold"); Text("Georgia Bold").tag("Georgia-Bold")
                Text("Futura Condensed ExtraBold").tag("Futura-CondensedExtraBold"); Text("American Typewriter Bold").tag("AmericanTypewriter-Bold")
                Text("Copperplate Bold").tag("Copperplate-Bold"); Text("Impact").tag("Impact")
            }
            HStack { TextField("Title size", value: $scene.titleSize, format: .number).textFieldStyle(.roundedBorder); TextField("Subtitle size", value: $scene.subtitleSize, format: .number).textFieldStyle(.roundedBorder) }
            ColorPicker("Background", selection: colorBinding(\.backgroundHex))
            ColorPicker("Accent", selection: colorBinding(\.accentHex))
            ColorPicker("Text", selection: colorBinding(\.textHex))
        case .motion:
            Picker("Transition", selection: $scene.transition) { ForEach(BumperTransition.allCases, id: \.self) { Text($0.rawValue).tag($0) } }
            VStack(alignment: .leading) { Text("Duration · \(scene.duration, format: .number.precision(.fractionLength(2))) seconds"); Slider(value: $scene.duration, in: 0.25...10, step: 0.25) }
            Text("The transition controls the entry treatment for text and graphics. It plays across the full bumper duration.").font(.caption).foregroundStyle(.secondary)
        case .assets:
            HStack {
                Button(scene.imagePath == nil ? "Add Image" : "Replace Image", systemImage: "photo") { importingImage = true }
                Button(scene.videoPath == nil ? "Add Video" : "Replace Video", systemImage: "video") { importingVideo = true }
                Button(scene.audioPath == nil ? "Add Sound" : "Replace Sound", systemImage: "waveform") { importingAudio = true }
            }
            if scene.imagePath != nil || scene.videoPath != nil || scene.audioPath != nil { AssetChips(scene: $scene) }
            Text("Images are composited below text. A video is played as the bumper visual; sound plays with it.").font(.caption).foregroundStyle(.secondary)
        }
    }
    private func selectInitialChannelIfNeeded() {
        guard selectedChannelID == nil, let channel = channels.first else { return }
        selectedChannelID = channel.id
        scene = .default(template: template, channelName: channel.name, accentHex: channel.colorHex)
    }

    private func syncSceneToChannel() {
        guard let channel = selectedChannel else { return }
        guard editingBumperID == nil else { return }
        scene.subtitle = channel.name
        scene.accentHex = channel.colorHex
    }

    private func renderAndAttach() {
        do {
            guard let channel = selectedChannel else { return }
            let bumper: BumperAsset
            if let existing = bumpers.first(where: { $0.id == editingBumperID }) {
                bumper = existing
            } else {
                bumper = BumperAsset(template: template, channelName: scene.subtitle, filePath: try BumperStorage.destination(for: UUID()).path)
            }
            let previousAssetPaths = BumperStorage.sceneAssetPaths(BumperSceneStore.scene(for: bumper, accentHex: channel.colorHex))
            if editingBumperID == nil { context.insert(bumper) }
            try? FileManager.default.removeItem(atPath: bumper.filePath)
            try BumperRenderer.render(scene: scene, destination: URL(fileURLWithPath: bumper.filePath))
            bumper.templateRaw = template.rawValue
            try BumperSceneStore.save(scene, to: bumper)
            BumperStorage.removeManagedAssets(previousAssetPaths.subtracting(BumperStorage.sceneAssetPaths(scene)))
            channel.bumperMediaID = bumper.id
            editingBumperID = bumper.id
            status = "Rendered and attached to CH \(channel.number)."
        } catch { status = error.localizedDescription }
    }

    private var assetSummary: String {
        [scene.imagePath == nil ? nil : "image", scene.videoPath == nil ? nil : "video", scene.audioPath == nil ? nil : "sound"].compactMap { $0 }.joined(separator: " · ") + " imported into local bumper assets."
    }

    private func colorBinding(_ keyPath: WritableKeyPath<BumperScene, String>) -> Binding<Color> {
        Binding(get: { Color(hex: scene[keyPath: keyPath]) }, set: { scene[keyPath: keyPath] = NSColor($0).hexString })
    }

    private func playPreview() {
        do {
            let url: URL
            if let path = scene.videoPath, FileManager.default.fileExists(atPath: path) { url = URL(fileURLWithPath: path) }
            else {
                url = try BumperStorage.previewDestination()
                try BumperRenderer.render(scene: scene, destination: url)
            }
            previewPlayer.replaceCurrentItem(with: AVPlayerItem(url: url))
            previewPlayer.play()
            showingPreview = true
        } catch { status = "Preview failed: \(error.localizedDescription)" }
    }

    private func importAsset(_ result: Result<URL, Error>, into keyPath: WritableKeyPath<BumperScene, String?>) {
        do { scene[keyPath: keyPath] = try BumperStorage.importAsset(from: result.get()) }
        catch { status = error.localizedDescription }
    }

    private func edit(_ bumper: BumperAsset) {
        editingBumperID = bumper.id
        template = BumperTemplate(rawValue: bumper.templateRaw) ?? .channelID
        scene = BumperSceneStore.scene(for: bumper, accentHex: selectedChannel?.colorHex ?? "7B4DFF")
        if let attached = channels.first(where: { $0.bumperMediaID == bumper.id }) { selectedChannelID = attached.id }
        status = "Editing \(bumper.templateRaw) bumper. Render to apply changes."
    }

    private func attach(_ bumper: BumperAsset) {
        guard let channel = selectedChannel else { return }
        channel.bumperMediaID = bumper.id
        status = "Attached \(bumper.templateRaw) bumper to CH \(channel.number)."
    }

    private func delete(_ bumper: BumperAsset) {
        BumperStorage.removeManagedAssets(BumperStorage.sceneAssetPaths(BumperSceneStore.scene(for: bumper)))
        channels.filter { $0.bumperMediaID == bumper.id }.forEach { $0.bumperMediaID = nil }
        try? FileManager.default.removeItem(atPath: bumper.filePath)
        context.delete(bumper)
        status = "Deleted bumper."
    }
}

private enum BumperInspectorMode: String, CaseIterable, Identifiable {
    case content = "Content"
    case style = "Style"
    case motion = "Motion"
    case assets = "Assets"
    var id: Self { self }
}

private struct BumperPreview: View {
    let scene: BumperScene

    var body: some View {
        ZStack(alignment: .topLeading) {
            Color(hex: scene.backgroundHex)
            Color(hex: scene.accentHex).frame(height: 16)
            VStack(alignment: .leading, spacing: 8) {
                Spacer()
                Text(scene.title.uppercased())
                    .font(.custom(scene.titleFontName, size: min(scene.titleSize / 2.4, 48)))
                Text(scene.subtitle)
                    .font(.custom(scene.titleFontName, size: min(scene.subtitleSize / 2, 28)))
                Text("\(scene.transition.rawValue.uppercased()) · 1080p H.264 · \(scene.duration, format: .number.precision(.fractionLength(2))) SEC")
                    .font(.caption.monospaced())
                    .foregroundStyle(.white.opacity(0.6))
            }
            .foregroundStyle(Color(hex: scene.textHex))
            .padding(32)
        }
        .aspectRatio(16 / 9, contentMode: .fit)
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(alignment: .topTrailing) {
            Text("LIVE PREVIEW")
                .font(.caption2.bold())
                .foregroundStyle(.white.opacity(0.8))
                .padding(10)
        }
    }
}

private struct BumperTimeline: View {
    let scene: BumperScene

    var body: some View {
        GroupBox("Timeline") {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("0:00").font(.caption.monospaced())
                    Spacer()
                    Text("\(scene.duration, format: .number.precision(.fractionLength(2)))s").font(.caption.monospaced())
                }
                TimelineTrack(label: "Transition", color: .orange, length: min(1, max(0.12, scene.duration / 10)), detail: scene.transition.rawValue)
                TimelineTrack(label: "Text", color: .purple, length: 1, detail: "\(scene.title) / \(scene.subtitle)")
                if scene.imagePath != nil { TimelineTrack(label: "Image", color: .blue, length: 1, detail: "Full-frame background") }
                if scene.videoPath != nil { TimelineTrack(label: "Video", color: .red, length: 1, detail: "Direct bumper clip") }
                if scene.audioPath != nil { TimelineTrack(label: "Sound", color: .green, length: 1, detail: "Plays with bumper") }
            }
            .padding(.vertical, 4)
        }
    }
}

private struct TimelineTrack: View {
    let label: String
    let color: Color
    let length: Double
    let detail: String

    var body: some View {
        HStack(spacing: 12) {
            Text(label).font(.caption).frame(width: 76, alignment: .trailing)
            GeometryReader { proxy in
                RoundedRectangle(cornerRadius: 5)
                    .fill(color.opacity(0.75))
                    .frame(width: max(48, proxy.size.width * length))
                    .overlay(alignment: .leading) { Text(detail).font(.caption2).lineLimit(1).padding(.horizontal, 8).foregroundStyle(.white) }
            }
            .frame(height: 24)
        }
    }
}

private struct AssetChips: View {
    @Binding var scene: BumperScene

    var body: some View {
        HStack(spacing: 8) {
            if scene.imagePath != nil { chip("Image", systemImage: "photo") { scene.imagePath = nil } }
            if scene.videoPath != nil { chip("Video", systemImage: "video") { scene.videoPath = nil } }
            if scene.audioPath != nil { chip("Sound", systemImage: "waveform") { scene.audioPath = nil } }
        }
    }

    private func chip(_ title: String, systemImage: String, remove: @escaping () -> Void) -> some View {
        HStack(spacing: 4) {
            Label(title, systemImage: systemImage)
            Button(action: remove) { Image(systemName: "xmark.circle.fill") }.buttonStyle(.plain)
        }
        .font(.caption).padding(.horizontal, 8).padding(.vertical, 5)
        .background(.quaternary, in: Capsule())
    }
}

private extension NSColor {
    var hexString: String {
        let color = usingColorSpace(.sRGB) ?? self
        return String(format: "%02X%02X%02X", Int((color.redComponent * 255).rounded()), Int((color.greenComponent * 255).rounded()), Int((color.blueComponent * 255).rounded()))
    }
}

private struct BumperAssetRow: View {
    let bumper: BumperAsset
    let attachmentName: String
    let canAttach: Bool
    let onAttach: () -> Void
    let onEdit: () -> Void
    let onDelete: () -> Void

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: "sparkles.rectangle.stack.fill")
                .foregroundStyle(.tint)
                .font(.title3)
            VStack(alignment: .leading, spacing: 3) {
                Text("\(bumper.templateRaw) · \(bumper.channelName)")
                Text(attachmentName).font(.caption).foregroundStyle(attachmentName == "Not attached" ? Color.secondary : Color.accentColor)
            }
            Spacer()
            Button("Edit", action: onEdit).buttonStyle(.bordered)
            Button("Attach", action: onAttach).buttonStyle(.bordered).disabled(!canAttach)
            Button("Delete", role: .destructive, action: onDelete).buttonStyle(.borderless)
        }
        .padding(.vertical, 10)
    }
}

struct AnalyticsView: View {
    @Environment(\.modelContext) private var context
    @Query private var events: [PlaybackEvent]
    @Query private var media: [LibraryMedia]
    @Query(sort: \StationChannel.number) private var channels: [StationChannel]

    private var completedEvents: [PlaybackEvent] { events.filter { $0.endedAt != nil || $0.accumulatedSeconds > 0 } }
    private var totalViewingSeconds: Double { completedEvents.reduce(0) { $0 + $1.accumulatedSeconds } }
    private var watchedMediaIDs: Set<UUID> { Set(completedEvents.map(\.mediaID)) }
    private var secondsByChannel: [UUID: Double] {
        completedEvents.reduce(into: [:]) { result, event in result[event.channelID, default: 0] += event.accumulatedSeconds }
    }
    private var secondsByMedia: [UUID: Double] {
        completedEvents.reduce(into: [:]) { result, event in result[event.mediaID, default: 0] += event.accumulatedSeconds }
    }
    private var topChannels: [(StationChannel, Double)] {
        var result: [(StationChannel, Double)] = []
        for channel in channels {
            let seconds = secondsByChannel[channel.id, default: 0]
            if seconds > 0 { result.append((channel, seconds)) }
        }
        return Array(result.sorted { $0.1 > $1.1 }.prefix(5))
    }
    private var topTitles: [(LibraryMedia, Double)] {
        var result: [(LibraryMedia, Double)] = []
        for item in media {
            let seconds = secondsByMedia[item.id, default: 0]
            if seconds > 0 { result.append((item, seconds)) }
        }
        return Array(result.sorted { $0.1 > $1.1 }.prefix(5))
    }

    var body: some View {
        List {
            Section {
                HStack(spacing: 16) {
                    StationStat(value: Int(totalViewingSeconds / 60), label: "Minutes watched", icon: "clock")
                    StationStat(value: completedEvents.count, label: "Completed sessions", icon: "play.circle")
                    StationStat(value: max(0, media.filter { !$0.path.hasPrefix("demo://") && $0.support == .playable }.count - watchedMediaIDs.count), label: "Unplayed titles", icon: "tray")
                }
                .listRowInsets(.init(top: 12, leading: 18, bottom: 12, trailing: 18))
            }
            if !topChannels.isEmpty {
                Section("Most-watched channels") {
                    ForEach(topChannels, id: \.0.id) { channel, seconds in
                        LabeledContent("CH \(channel.number) · \(channel.name)", value: formattedDuration(seconds))
                    }
                }
            }
            if !topTitles.isEmpty {
                Section("Most-watched titles") {
                    ForEach(topTitles, id: \.0.id) { item, seconds in
                        LabeledContent(item.title, value: formattedDuration(seconds))
                    }
                }
            }
            Section("Privacy") {
                Text("Playback activity stays on this Mac. Nothing is uploaded.")
                    .foregroundStyle(.secondary)
                Button("Delete All Analytics", systemImage: "trash", role: .destructive) { events.forEach(context.delete) }
            }
        }
        .listStyle(.inset)
        .navigationTitle("Analytics")
    }

    private func formattedDuration(_ seconds: Double) -> String { Duration.seconds(seconds).formatted(.time(pattern: .hourMinuteSecond)) }
}

struct SettingsView: View {
    @Environment(\.modelContext) private var context
    @Binding var importFolder: Bool
    let rescan: (MediaSource) -> Void
    @Query private var sources: [MediaSource]
    @Query private var media: [LibraryMedia]
    @Query private var assignments: [ChannelAssignment]
    @Query private var programs: [ScheduledProgram]
    @Query private var events: [PlaybackEvent]
    @State private var confirmClearLibrary = false
    @State private var sourceToRemove: MediaSource?
    @State private var appDataBytes: Int64 = 0
    @State private var isExporting = false
    @State private var isImporting = false
    @State private var exportDocument = StationBackupDocument(backup: .empty)
    @State private var pendingBackup: StationBackup?
    @State private var importError: String?

    var body: some View {
        Form {
            Section("Media folders") {
                if sources.isEmpty {
                    ContentUnavailableView("No media folders", systemImage: "folder", description: Text("Add a folder to build your private local catalog."))
                } else {
                    ForEach(sources) { source in
                        MediaSourceRow(
                            source: source,
                            titleCount: media.filter { $0.sourceID == source.id }.count,
                            onRescan: { rescan(source) },
                            onRemove: { sourceToRemove = source }
                        )
                    }
                }
                Button("Add Media Folder", systemImage: "folder.badge.plus") { importFolder = true }
                    .buttonStyle(.borderedProminent)
            }
            Section("Local storage") {
                LabeledContent("Indexed titles", value: media.count.formatted())
                LabeledContent("Referenced media", value: ByteCountFormatter.string(fromByteCount: media.reduce(0) { $0 + $1.fileSize }, countStyle: .file))
                LabeledContent("NostalgiaBox app data", value: ByteCountFormatter.string(fromByteCount: appDataBytes, countStyle: .file))
                Text("Media stays in its original folders. NostalgiaBox stores only its local index, schedules, bookmarks, thumbnails, and bumpers.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Privacy & rights") {
                Label("No data leaves this Mac", systemImage: "lock.fill")
                Text("NostalgiaBox does not upload your media, catalog, schedule, paths, or viewing history.")
                    .font(.caption).foregroundStyle(.secondary)
                Label("Bring only authorized media", systemImage: "checkmark.shield")
                Text("Only add media you own or are authorized to use.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Local data") {
                LabeledContent("Playback events", value: events.count.formatted())
                Text("Deleting analytics does not change your library or schedule.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Backup") {
                Button("Export Station Data", systemImage: "square.and.arrow.up") { exportStationData() }
                Button("Import Station Data", systemImage: "square.and.arrow.down") { isImporting = true }
                Text("Backups include your local catalog, schedules, settings, analytics, and folder bookmarks. Source video files are never copied.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Danger zone") {
                Button("Clear Library Index", systemImage: "trash", role: .destructive) { confirmClearLibrary = true }
                Text("Removes saved folder access, catalog entries, assignments, and generated schedules. Your original video files and channel identities remain unchanged.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .frame(maxWidth: 820)
        .navigationTitle("Settings")
        .task(id: sources.count + media.count) { appDataBytes = applicationSupportSize() }
        .fileExporter(isPresented: $isExporting, document: exportDocument, contentType: .nostalgiaBoxStudioBackup, defaultFilename: "NostalgiaBox Studio Backup") { result in
            if case .failure(let error) = result { importError = error.localizedDescription }
        }
        .fileImporter(isPresented: $isImporting, allowedContentTypes: [.nostalgiaBoxStudioBackup]) { result in
            do {
                let url = try result.get()
                pendingBackup = try StationBackup.decode(from: Data(contentsOf: url))
            } catch { importError = error.localizedDescription }
        }
        .alert("Clear the library index?", isPresented: $confirmClearLibrary) {
            Button("Clear Index", role: .destructive) { clearLibrary() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This deletes local source bookmarks, indexed media, channel assignments, and generated schedules. Your original video files will not be changed.")
        }
        .alert("Remove \(sourceToRemove?.displayName ?? "folder")?", isPresented: Binding(get: { sourceToRemove != nil }, set: { if !$0 { sourceToRemove = nil } })) {
            Button("Remove Folder", role: .destructive) { if let sourceToRemove { remove(sourceToRemove) }; sourceToRemove = nil }
            Button("Cancel", role: .cancel) { sourceToRemove = nil }
        } message: {
            Text("This removes the folder’s bookmark and its indexed titles from NostalgiaBox. The original files are not changed. Rebuild affected channel schedules afterward.")
        }
        .alert("Replace local station data?", isPresented: Binding(get: { pendingBackup != nil }, set: { if !$0 { pendingBackup = nil } })) {
            Button("Replace Data", role: .destructive) { if let pendingBackup { restore(pendingBackup) }; self.pendingBackup = nil }
            Button("Cancel", role: .cancel) { pendingBackup = nil }
        } message: {
            Text("This replaces NostalgiaBox’s local catalog, schedules, analytics, and saved bookmarks. Your source video files will not be changed.")
        }
        .alert("Backup Error", isPresented: importErrorBinding) {
            Button("OK", role: .cancel) { importError = nil }
        } message: {
            Text(importError ?? "Unknown error")
        }
    }

    private var importErrorBinding: Binding<Bool> {
        Binding(get: { importError != nil }, set: { if !$0 { importError = nil } })
    }

    private func clearLibrary() {
        sources.forEach { SourceAccessRegistry.release(sourceID: $0.id) }
        programs.forEach(context.delete)
        assignments.forEach(context.delete)
        media.forEach(context.delete)
        sources.forEach(context.delete)
        StudioLog.info("Cleared the local library index and generated schedules at user request.")
    }

    private func remove(_ source: MediaSource) {
        SourceAccessRegistry.release(sourceID: source.id)
        let removedIDs = Set(media.filter { $0.sourceID == source.id }.map(\.id))
        assignments.filter { removedIDs.contains($0.mediaID) }.forEach(context.delete)
        programs.filter { removedIDs.contains($0.mediaID) }.forEach(context.delete)
        media.filter { removedIDs.contains($0.id) }.forEach(context.delete)
        context.delete(source)
        StudioLog.info("Removed media source \(source.displayName) and \(removedIDs.count) indexed titles at user request.")
    }

    private func applicationSupportSize() -> Int64 {
        guard let directory = try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: false)
            .appendingPathComponent("NostalgiaBoxStudio", isDirectory: true),
              let enumerator = FileManager.default.enumerator(at: directory, includingPropertiesForKeys: [.fileSizeKey]) else { return 0 }
        return enumerator.reduce(0) { partial, element in
            let file = element as? URL
            let size = (try? file?.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
            return partial + Int64(size)
        }
    }

    private func exportStationData() {
        exportDocument = StationBackupDocument(backup: StationBackup(sources: sources, media: media, channels: tryFetchChannels(), assignments: assignments, bumpers: tryFetchBumpers(), programs: programs, events: events))
        isExporting = true
    }

    private func tryFetchChannels() -> [StationChannel] { (try? context.fetch(FetchDescriptor<StationChannel>())) ?? [] }
    private func tryFetchBumpers() -> [BumperAsset] { (try? context.fetch(FetchDescriptor<BumperAsset>())) ?? [] }

    private func restore(_ backup: StationBackup) {
        events.forEach(context.delete)
        programs.forEach(context.delete)
        assignments.forEach(context.delete)
        media.forEach(context.delete)
        sources.forEach(context.delete)
        tryFetchChannels().forEach(context.delete)
        tryFetchBumpers().forEach(context.delete)
        backup.restore(into: context)
        StudioLog.info("Restored local station data from backup.")
    }
}

private struct MediaSourceRow: View {
    let source: MediaSource
    let titleCount: Int
    let onRescan: () -> Void
    let onRemove: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "folder.fill").foregroundStyle(.tint)
            VStack(alignment: .leading, spacing: 3) {
                Text(source.displayName)
                Text(detail)
                    .font(.caption).foregroundStyle(.secondary)
                if let status = source.statusMessage, !status.isEmpty {
                    Text(status).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer()
            Button("Rescan", action: onRescan).buttonStyle(.borderless)
            Button("Remove", role: .destructive, action: onRemove)
                .buttonStyle(.borderless)
        }
    }

    private var detail: String {
        let scanStatus = source.lastScannedAt.map { "Scanned \($0.formatted(.relative(presentation: .named)))" } ?? "Not scanned yet"
        return "\(titleCount) indexed titles · \(scanStatus)"
    }
}

private extension UTType {
    static let nostalgiaBoxStudioBackup = UTType(exportedAs: "com.singlethreadlabs.nostalgiabox.studio-backup")
}

private struct StationBackupDocument: FileDocument {
    static let readableContentTypes: [UTType] = [.nostalgiaBoxStudioBackup]
    let backup: StationBackup

    init(backup: StationBackup) { self.backup = backup }
    init(configuration: ReadConfiguration) throws {
        guard let data = configuration.file.regularFileContents else { throw CocoaError(.fileReadCorruptFile) }
        backup = try StationBackup.decode(from: data)
    }

    func fileWrapper(configuration: WriteConfiguration) throws -> FileWrapper {
        FileWrapper(regularFileWithContents: try JSONEncoder().encode(backup))
    }
}

private struct StationBackup: Codable {
    struct Source: Codable { let id: UUID; let displayName: String; let bookmarkData: Data; let lastScannedAt: Date?; let statusMessage: String? }
    struct Media: Codable { let id: UUID; let sourceID: UUID?; let path: String; let title: String; let duration: Double; let fileSize: Int64; let modifiedAt: Date; let supportRaw: String; let warning: String?; let playCount: Int }
    struct Channel: Codable { let id: UUID; let number: Int; let name: String; let colorHex: String; let theme: String; let bumperMediaID: UUID? }
    struct Assignment: Codable { let id: UUID; let channelID: UUID; let mediaID: UUID; let kind: String }
    struct Bumper: Codable {
        struct ImportedAsset: Codable { let originalPath: String; let data: Data }
        let id: UUID; let templateRaw: String; let channelName: String; let filePath: String; let createdAt: Date; let fileData: Data?; let sceneData: Data?; let importedAssets: [ImportedAsset]?
    }
    struct Program: Codable { let id: UUID; let channelID: UUID; let mediaID: UUID; let startsAt: Date; let endsAt: Date; let kind: String; let isOverride: Bool }
    struct Event: Codable { let id: UUID; let channelID: UUID; let mediaID: UUID; let startedAt: Date; let endedAt: Date?; let accumulatedSeconds: Double }

    let sources: [Source]
    let media: [Media]
    let channels: [Channel]
    let assignments: [Assignment]
    let bumpers: [Bumper]
    let programs: [Program]
    let events: [Event]

    private init(sources: [Source], media: [Media], channels: [Channel], assignments: [Assignment], bumpers: [Bumper], programs: [Program], events: [Event]) {
        self.sources = sources
        self.media = media
        self.channels = channels
        self.assignments = assignments
        self.bumpers = bumpers
        self.programs = programs
        self.events = events
    }

    static let empty = StationBackup(
        sources: [Source](), media: [Media](), channels: [Channel](), assignments: [Assignment](),
        bumpers: [Bumper](), programs: [Program](), events: [Event]()
    )

    init(sources: [MediaSource], media: [LibraryMedia], channels: [StationChannel], assignments: [ChannelAssignment], bumpers: [BumperAsset], programs: [ScheduledProgram], events: [PlaybackEvent]) {
        self.sources = sources.map { Source(id: $0.id, displayName: $0.displayName, bookmarkData: $0.bookmarkData, lastScannedAt: $0.lastScannedAt, statusMessage: $0.statusMessage) }
        self.media = media.map { Media(id: $0.id, sourceID: $0.sourceID, path: $0.path, title: $0.title, duration: $0.duration, fileSize: $0.fileSize, modifiedAt: $0.modifiedAt, supportRaw: $0.supportRaw, warning: $0.warning, playCount: $0.playCount) }
        self.channels = channels.map { Channel(id: $0.id, number: $0.number, name: $0.name, colorHex: $0.colorHex, theme: $0.theme, bumperMediaID: $0.bumperMediaID) }
        self.assignments = assignments.map { Assignment(id: $0.id, channelID: $0.channelID, mediaID: $0.mediaID, kind: $0.kind) }
        self.bumpers = bumpers.map { bumper in
            let scene = BumperSceneStore.scene(for: bumper)
            let importedAssets = BumperStorage.sceneAssetPaths(scene).compactMap { path -> Bumper.ImportedAsset? in
                guard BumperStorage.isManagedAsset(path), let data = try? Data(contentsOf: URL(fileURLWithPath: path)) else { return nil }
                return Bumper.ImportedAsset(originalPath: path, data: data)
            }
            return Bumper(id: bumper.id, templateRaw: bumper.templateRaw, channelName: bumper.channelName, filePath: bumper.filePath, createdAt: bumper.createdAt, fileData: try? Data(contentsOf: URL(fileURLWithPath: bumper.filePath)), sceneData: bumper.sceneData, importedAssets: importedAssets)
        }
        self.programs = programs.map { Program(id: $0.id, channelID: $0.channelID, mediaID: $0.mediaID, startsAt: $0.startsAt, endsAt: $0.endsAt, kind: $0.kind, isOverride: $0.isOverride) }
        self.events = events.map { Event(id: $0.id, channelID: $0.channelID, mediaID: $0.mediaID, startedAt: $0.startedAt, endedAt: $0.endedAt, accumulatedSeconds: $0.accumulatedSeconds) }
    }

    static func decode(from data: Data) throws -> StationBackup { try JSONDecoder().decode(StationBackup.self, from: data) }

    func restore(into context: ModelContext) {
        for record in sources { let model = MediaSource(displayName: record.displayName, bookmarkData: record.bookmarkData, statusMessage: record.statusMessage); model.id = record.id; model.lastScannedAt = record.lastScannedAt; context.insert(model) }
        for record in media { let model = LibraryMedia(sourceID: record.sourceID, path: record.path, title: record.title, duration: record.duration, fileSize: record.fileSize, modifiedAt: record.modifiedAt, support: MediaSupport(rawValue: record.supportRaw) ?? .unsupported, warning: record.warning); model.id = record.id; model.playCount = record.playCount; context.insert(model) }
        for record in channels { let model = StationChannel(number: record.number, name: record.name, colorHex: record.colorHex, theme: record.theme); model.id = record.id; model.bumperMediaID = record.bumperMediaID; context.insert(model) }
        for record in assignments { let model = ChannelAssignment(channelID: record.channelID, mediaID: record.mediaID, kind: record.kind); model.id = record.id; context.insert(model) }
        for record in bumpers {
            var filePath = record.filePath
            if let fileData = record.fileData, let destination = try? BumperStorage.destination(for: record.id) {
                do {
                    try fileData.write(to: destination, options: .atomic)
                    filePath = destination.path
                } catch {
                    StudioLog.error("Could not restore bumper \(record.channelName): \(error.localizedDescription)")
                }
            }
            var scene = record.sceneData.flatMap { try? JSONDecoder().decode(BumperScene.self, from: $0) }
            for asset in record.importedAssets ?? [] {
                guard let restoredPath = try? BumperStorage.restoreAsset(data: asset.data, originalPath: asset.originalPath) else { continue }
                if scene?.imagePath == asset.originalPath { scene?.imagePath = restoredPath }
                if scene?.videoPath == asset.originalPath { scene?.videoPath = restoredPath }
                if scene?.audioPath == asset.originalPath { scene?.audioPath = restoredPath }
            }
            let sceneData = scene.flatMap { try? JSONEncoder().encode($0) } ?? record.sceneData
            let model = BumperAsset(template: BumperTemplate(rawValue: record.templateRaw) ?? .channelID, channelName: record.channelName, filePath: filePath); model.id = record.id; model.templateRaw = record.templateRaw; model.createdAt = record.createdAt; model.sceneData = sceneData; context.insert(model)
        }
        for record in programs { let model = ScheduledProgram(channelID: record.channelID, mediaID: record.mediaID, startsAt: record.startsAt, endsAt: record.endsAt, kind: record.kind, isOverride: record.isOverride); model.id = record.id; context.insert(model) }
        for record in events { let model = PlaybackEvent(channelID: record.channelID, mediaID: record.mediaID, startedAt: record.startedAt); model.id = record.id; model.endedAt = record.endedAt; model.accumulatedSeconds = record.accumulatedSeconds; context.insert(model) }
    }
}

private extension Color { init(hex: String) { let value = UInt64(hex, radix: 16) ?? 0; self.init(.sRGB, red: Double((value >> 16) & 0xFF) / 255, green: Double((value >> 8) & 0xFF) / 255, blue: Double(value & 0xFF) / 255, opacity: 1) } }
