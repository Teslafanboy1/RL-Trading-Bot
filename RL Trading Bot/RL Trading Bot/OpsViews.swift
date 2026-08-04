//
//  OpsViews.swift — Calendar (bot schedule + earnings + blackouts),
//  Activity (unified chronological ops feed), and Health (system deep-dive:
//  heartbeat, control plane, state-file freshness, watchdog, server).
//

import SwiftUI

// MARK: - Calendar

struct CalendarView: View {
    @Environment(AppModel.self) private var model

    private var p: CalendarPayload? { model.calendarPayload }

    var body: some View {
        Group {
            if p == nil {
                LoadingScreen(title: "Loading calendar…", detail: "schedule, blackouts, earnings")
                    .background(DS.bg)
            } else {
                Screen(title: "Calendar", subtitle: AppTab.calendar.blurb,
                       refresh: { await model.load(.calendar) }) {
                    if let err = model.tabErrors[.calendar] { ErrorBox(text: err) }
                    scheduleCard
                    AdaptivePair(compactBelow: 820) {
                        blackoutCard
                    } second: {
                        universeCard
                    }
                    earningsCard
                }
            }
        }
        .task { if p == nil { await model.load(.calendar) } }
    }

    private var scheduleCard: some View {
        Card("Bot schedule (ET)", subtitle: "server time \(Fmt.when(p?.serverTime))") {
            VStack(alignment: .leading, spacing: DS.s3) {
                StatusDot(color: p?.marketOpen == true ? DS.up : DS.textFaint,
                          label: p?.marketOpen == true ? "MARKET OPEN" : "MARKET CLOSED")
                let events = p?.schedule ?? []
                if events.isEmpty {
                    EmptyNote(text: "No scheduled events returned.", icon: "clock")
                } else {
                    VStack(spacing: 0) {
                        ForEach(events) { ev in
                            TRow(showsDivider: ev.id != events.last?.id) {
                                TCell(weight: 1.4) {
                                    Text(whenText(ev.when))
                                        .font(DSFont.num(12)).foregroundStyle(DS.textMuted)
                                        .lineLimit(1)
                                }
                                Image(systemName: icon(for: ev.kind))
                                    .font(.system(size: 13))
                                    .foregroundStyle(color(for: ev.kind))
                                    .frame(width: 22)
                                TCell(weight: 3) {
                                    Text(ev.label ?? "—").font(DSFont.body(14))
                                        .foregroundStyle(DS.text)
                                }
                                TCell(align: .trailing) {
                                    Tag(countdown(ev.when), tone: tone(for: ev.kind), size: 11)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    private func whenText(_ ts: String?) -> String {
        guard let d = Fmt.date(ts) else { return ts ?? "—" }
        return d.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated)
            .day().hour().minute())
    }

    private func countdown(_ ts: String?) -> String {
        guard let d = Fmt.date(ts) else { return "—" }
        let s = d.timeIntervalSinceNow
        if s <= 0 { return "now" }
        if s < 3600 { return "in \(Int(s / 60))m" }
        if s < 86_400 { return String(format: "in %.1fh", s / 3600) }
        return String(format: "in %.1fd", s / 86_400)
    }

    private func icon(for kind: String?) -> String {
        switch kind {
        case "research": "brain"
        case "open": "bell"
        case "midweek": "stethoscope"
        case "close": "moon.zzz"
        default: "calendar"
        }
    }

    private func color(for kind: String?) -> Color {
        switch kind {
        case "research": DS.accent
        case "open": DS.up
        case "midweek": DS.caution
        default: DS.textMuted
        }
    }

    private func tone(for kind: String?) -> TagTone {
        switch kind {
        case "research": .accent
        case "open": .up
        case "midweek": .caution
        default: .neutral
        }
    }

    private var blackoutCard: some View {
        Card("Blackout windows", subtitle: "the bot skips new entries inside these") {
            VStack(spacing: DS.s2) {
                KeyValueRow(key: "Earnings blackout",
                            value: "\(p?.blackoutWindows?["earnings_days"]?.compact ?? "—") days before report")
                KeyValueRow(key: "Fed-meeting blackout",
                            value: "\(p?.blackoutWindows?["fed_meeting_days"]?.compact ?? "—") days before FOMC")
            }
        }
    }

    private var universeCard: some View {
        Card("Names to watch for reports", subtitle: "held first, then the watchlist") {
            VStack(alignment: .leading, spacing: DS.s3) {
                Text("HELD").font(DSFont.label(10)).kerning(0.5).foregroundStyle(DS.textFaint)
                if (p?.held ?? []).isEmpty {
                    EmptyNote(text: "Nothing held.")
                } else {
                    symbolWrap(p?.held ?? [], .up)
                }
                Text("WATCHLIST").font(DSFont.label(10)).kerning(0.5)
                    .foregroundStyle(DS.textFaint)
                symbolWrap(p?.watchlist ?? [], .neutral)
            }
        }
    }

    private func symbolWrap(_ syms: [String], _ tone: TagTone) -> some View {
        ChipCloud(items: syms, minWidth: 68) { s in
            Button { model.analyze(s) } label: { Tag(s, tone: tone, size: 11) }
                .buttonStyle(.plain)
        }
    }

    // Earnings arrive as free-form broker JSON — render rows when a list of
    // objects is found, else fall back to a readable dump.
    private var earningsRows: [[String: JSONValue]]? {
        func firstArray(_ v: JSONValue?) -> [[String: JSONValue]]? {
            guard let v else { return nil }
            if let arr = v.arrayValue {
                let objs = arr.compactMap(\.objectValue)
                return objs.isEmpty ? nil : objs
            }
            if let obj = v.objectValue {
                for key in obj.keys.sorted() {
                    if let found = firstArray(obj[key]) { return found }
                }
            }
            return nil
        }
        return firstArray(p?.earnings)
    }

    private var earningsCard: some View {
        Card("Earnings — next 2 weeks", subtitle: "held names are blackout candidates") {
            if let msg = p?.earnings?["unavailable"]?.stringValue
                ?? p?.earnings?["error"]?.stringValue {
                WarningBox(text: msg)
            } else if let rows = earningsRows {
                let held = Set(p?.held ?? [])
                VStack(spacing: 0) {
                    let shown = Array(rows.prefix(30))
                    ForEach(Array(shown.enumerated()), id: \.offset) { i, r in
                        let sym = r["symbol"]?.stringValue ?? r["ticker"]?.stringValue ?? "—"
                        TRow(showsDivider: i < shown.count - 1) {
                            TCell {
                                HStack(spacing: 7) {
                                    Text(sym).font(DSFont.bold(13))
                                        .foregroundStyle(held.contains(sym) ? DS.up : DS.text)
                                    if held.contains(sym) { Tag("HELD", tone: .up, size: 9) }
                                }
                            }
                            TCell(align: .trailing) {
                                Text(r["date"]?.stringValue ?? r["report_date"]?.stringValue
                                     ?? r["expected_report_date"]?.stringValue ?? "—")
                                    .font(DSFont.num(12)).foregroundStyle(DS.text)
                            }
                            TCell(align: .trailing) {
                                Text(r["time"]?.stringValue ?? r["timing"]?.stringValue ?? "")
                                    .font(DSFont.body(11)).foregroundStyle(DS.textMuted)
                            }
                        }
                    }
                }
            } else if let e = p?.earnings {
                ScrollView {
                    Text(e.pretty)
                        .font(DSFont.mono(11)).foregroundStyle(DS.consoleText)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(DS.s3)
                }
                .frame(maxHeight: 260)
                .background(DS.consoleBG,
                            in: RoundedRectangle(cornerRadius: DS.rMd, style: .continuous))
            } else {
                EmptyNote(text: "No earnings data.", icon: "megaphone")
            }
        }
    }
}

// MARK: - Activity

struct ActivityView: View {
    @Environment(AppModel.self) private var model
    @State private var search = ""
    @State private var kindFilter: String = "all"

    private var events: [LogEvent] {
        var evs = model.logsPayload?.events ?? []
        if kindFilter != "all" { evs = evs.filter { $0.kind == kindFilter } }
        if !search.isEmpty {
            let q = search.lowercased()
            evs = evs.filter {
                "\($0.title ?? "") \($0.detail ?? "") \($0.kind ?? "")".lowercased().contains(q)
            }
        }
        return evs
    }

    private static let kinds: [(String, String)] = [
        ("all", "All"), ("manual", "Manual"), ("change", "Changes"),
        ("watchdog", "Watchdog"), ("agent", "Agent"),
    ]

    var body: some View {
        Group {
            if model.logsPayload == nil {
                LoadingScreen(title: "Loading activity…", detail: "every stream, newest first")
                    .background(DS.bg)
            } else {
                Screen(title: "Activity",
                       subtitle: "\(events.count) events · every stream, newest first",
                       refresh: { await model.load(.activity) }) {
                    if let err = model.tabErrors[.activity] { ErrorBox(text: err) }
                    Card {
                        WidthReader(compactBelow: 620) { compact in
                            let pills = FilterPills(options: Self.kinds, selection: $kindFilter)
                            let field = OrganicField(prompt: "search events", text: $search,
                                                     width: 260)
                            if compact {
                                VStack(alignment: .leading, spacing: DS.s3) { pills; field }
                            } else {
                                HStack(spacing: DS.s3) { pills; Spacer(minLength: DS.s2); field }
                            }
                        }
                    }
                    Card(padding: DS.s4) {
                        if events.isEmpty {
                            EmptyNote(text: "No events match.", icon: "clock.arrow.circlepath")
                        } else {
                            VStack(spacing: 0) {
                                ForEach(Array(events.enumerated()), id: \.offset) { i, ev in
                                    ActivityRow(event: ev, isLast: i == events.count - 1)
                                }
                            }
                        }
                    }
                }
            }
        }
        .task { if model.logsPayload == nil { await model.load(.activity) } }
    }
}

struct ActivityRow: View {
    let event: LogEvent
    let isLast: Bool

    var body: some View {
        TRow(showsDivider: !isLast) {
            Text(Fmt.clock(event.ts))
                .font(DSFont.num(12))
                .foregroundStyle(DS.textFaint)
                .frame(width: 66, alignment: .leading)
            Tag(event.kind ?? "system", tone: tone, size: 10)
            TCell(weight: 5) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(event.title ?? "—")
                        .font(DSFont.semibold(13))
                        .foregroundStyle(DS.text)
                        .lineLimit(3)
                    if let d = event.detail, !d.isEmpty {
                        Text(d).font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                            .lineLimit(2)
                    }
                }
            }
            TCell(align: .trailing) {
                HStack(spacing: 6) {
                    if let sev = event.severity {
                        Tag(sev, tone: sev == "MAJOR" ? .down : .caution, size: 9)
                    }
                    Text(Fmt.when(event.ts))
                        .font(DSFont.num(11)).foregroundStyle(DS.textFaint).lineLimit(1)
                }
            }
        }
    }

    private var tone: TagTone {
        if event.ok == false || event.severity == "MAJOR" { return .down }
        switch event.kind {
        case "manual": return .accent
        case "change": return .caution
        case "agent": return .sage
        default: return .neutral
        }
    }
}

// MARK: - Health

struct HealthView: View {
    @Environment(AppModel.self) private var model
    @State private var actionError: String?
    @State private var checking = false
    @State private var lastChecked: Date?

    private var p: HealthPayload? { model.healthPayload }

    var body: some View {
        Group {
            if p == nil {
                LoadingScreen(title: "Checking health…", detail: "heartbeat, control plane, files")
                    .background(DS.bg)
            } else {
                Screen(title: "Health", subtitle: AppTab.health.blurb,
                       refresh: { await runDiagnostics() }) {
                    if let err = model.tabErrors[.health] { ErrorBox(text: err) }
                    if let err = actionError { ErrorBox(text: err) }
                    diagnosticsBar
                    systemGrid
                    AdaptivePair(compactBelow: 860) {
                        controlCard
                    } second: {
                        serverCard
                    }
                    filesCard
                    watchdogCard
                    warningsCard
                }
            }
        }
        .task { if p == nil { await model.load(.health) } }
    }

    private func runDiagnostics() async {
        checking = true
        await model.refreshTick()
        await model.load(.health)
        checking = false
        lastChecked = Date()
    }

    private var diagnosticsBar: some View {
        Card {
            HStack(spacing: DS.s3) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("System diagnostics")
                        .font(DSFont.heading(18)).foregroundStyle(DS.text)
                    Text(checking ? "Checking…"
                         : "last checked \(lastChecked.map { $0.formatted(date: .omitted, time: .standard) } ?? "not yet this session")")
                        .font(DSFont.body(13)).foregroundStyle(DS.textMuted)
                }
                Spacer()
                Button { Task { await runDiagnostics() } } label: {
                    if checking {
                        ProgressView().controlSize(.small)
                    } else {
                        Label("Run diagnostics", systemImage: "waveform.path.ecg")
                    }
                }
                .buttonStyle(.organicPrimary)
                .disabled(checking)
            }
        }
    }

    private struct SystemCheck: Identifiable {
        let id = UUID()
        let name: String
        let state: String
        let tone: TagTone
        let metric: String
        let detail: String
    }

    private var checks: [SystemCheck] {
        let bot = p?.bot
        let age = bot?.heartbeat?.ageSeconds
        let botTone: TagTone = switch bot?.state {
        case "running": .up
        case "paused": .caution
        case "halted", "stale": .down
        default: .neutral
        }
        let broker = p?.broker?.mode
        let files = p?.files ?? []
        let stale = files.filter { $0.exists != true || ($0.ageSeconds ?? 0) > 26 * 3600 }
        let watchdogLines = (p?.watchdogLog ?? [])
        let watchdogAlerts = watchdogLines.filter { $0.contains("ALERT") }.count
        return [
            SystemCheck(name: "Bot heartbeat",
                        state: (bot?.state ?? "unknown").uppercased(), tone: botTone,
                        metric: Fmt.ago(age),
                        detail: bot?.cycle.map { "\($0.state ?? "—") · \($0.task ?? "")" } ?? "no cycle status"),
            SystemCheck(name: "Broker path",
                        state: broker == "direct-mcp" ? "HEALTHY" : "DEGRADED",
                        tone: broker == "direct-mcp" ? .up : .caution,
                        metric: broker ?? "—",
                        detail: p?.broker?.reason ?? ""),
            SystemCheck(name: "Market data",
                        state: bot?.marketOpen == true ? "OPEN" : "CLOSED",
                        tone: bot?.marketOpen == true ? .up : .neutral,
                        metric: "\(p?.equityPoints ?? 0) equity points",
                        detail: p?.lastEquity.map { "last \(Fmt.usd($0.total)) · \(Fmt.when($0.ts))" } ?? "no equity snapshots"),
            SystemCheck(name: "Risk engine",
                        state: p?.control?.halt?.halted == true ? "HALTED"
                            : p?.control?.pause?.paused == true ? "PAUSED" : "ARMED",
                        tone: p?.control?.halt?.halted == true ? .down
                            : p?.control?.pause?.paused == true ? .caution : .up,
                        metric: "\((p?.control?.overrides ?? [:]).count) overrides",
                        detail: "\((p?.control?.dnt ?? []).count) blocked symbols"),
            SystemCheck(name: "Watchdog",
                        state: watchdogAlerts > 0 ? "ALERTING" : "QUIET",
                        tone: watchdogAlerts > 0 ? .down : .up,
                        metric: "\(watchdogLines.count) log lines",
                        detail: watchdogAlerts > 0 ? "\(watchdogAlerts) alert lines"
                            : "independent launchd dead-man"),
            SystemCheck(name: "State files",
                        state: stale.isEmpty ? "FRESH" : "STALE",
                        tone: stale.isEmpty ? .up : .caution,
                        metric: "\(files.count - stale.count)/\(files.count) fresh",
                        detail: stale.isEmpty ? "all state files current"
                            : stale.map(\.label).joined(separator: ", ")),
        ]
    }

    private var systemGrid: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 260), spacing: DS.s4)],
                  spacing: DS.s4) {
            ForEach(checks) { c in
                Card(padding: DS.s4) {
                    VStack(alignment: .leading, spacing: DS.s2) {
                        HStack(spacing: 8) {
                            Circle().fill(c.tone.fg).frame(width: 10, height: 10)
                            Text(c.name).font(DSFont.semibold(14)).foregroundStyle(DS.text)
                            Spacer()
                            Tag(c.state, tone: c.tone, size: 10)
                        }
                        Text(c.metric).font(DSFont.heading(20)).foregroundStyle(DS.text)
                            .lineLimit(1).minimumScaleFactor(0.6)
                        Text(c.detail).font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                            .lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(maxWidth: .infinity, minHeight: 96, alignment: .topLeading)
                }
            }
        }
    }

    private var controlCard: some View {
        Card("Control plane", subtitle: "read by the bot at the top of every cycle") {
            VStack(spacing: DS.s2) {
                KeyValueRow(key: "PAUSE file",
                            value: p?.control?.pause?.paused == true ? "present" : "absent",
                            valueColor: p?.control?.pause?.paused == true ? DS.caution : nil)
                KeyValueRow(key: "HALT file",
                            value: p?.control?.halt?.halted == true ? "PRESENT" : "absent",
                            valueColor: p?.control?.halt?.halted == true ? DS.down : nil)
                KeyValueRow(key: "Do-not-trade",
                            value: (p?.control?.dnt ?? []).isEmpty
                                   ? "none" : (p?.control?.dnt ?? []).joined(separator: ", "))
                KeyValueRow(key: "Stop overrides",
                            value: (p?.control?.overrides ?? [:]).isEmpty
                                   ? "none"
                                   : (p?.control?.overrides ?? [:]).keys.sorted()
                                        .joined(separator: ", "))
                KeyValueRow(key: "Latest picks file", value: p?.latestPicks?.file ?? "none")
                if p?.bot?.halt?.halted == true {
                    ErrorBox(text: "HALT active — \(p?.bot?.halt?.detail ?? "kill-switch fired")")
                }
                if p?.bot?.pause?.paused == true {
                    WarningBox(text: "PAUSED by operator — protective exits still run.")
                }
            }
        }
    }

    private var serverCard: some View {
        Card("Dashboard server", subtitle: "the process this app talks to") {
            VStack(spacing: DS.s2) {
                KeyValueRow(key: "Broker path", value: p?.broker?.mode ?? "—",
                            valueColor: p?.broker?.mode == "direct-mcp" ? DS.up : DS.caution)
                if let reason = p?.broker?.reason, !reason.isEmpty {
                    Text(reason).font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                KeyValueRow(key: "PIN configured",
                            value: p?.pinConfigured == true ? "yes" : "NO — run --set-pin",
                            valueColor: p?.pinConfigured == true ? nil : DS.down)
                KeyValueRow(key: "Server uptime",
                            value: Fmt.ago(p?.server?.uptimeSeconds)
                                .replacingOccurrences(of: " ago", with: ""))
                KeyValueRow(key: "Bind",
                            value: "\(p?.server?.host ?? "—"):\(p?.server?.port.map(String.init) ?? "—")")
                KeyValueRow(key: "Equity points", value: "\(p?.equityPoints ?? 0)")
                if let last = p?.lastEquity {
                    KeyValueRow(key: "Last equity",
                                value: "\(Fmt.usd(last.total)) · \(Fmt.when(last.ts))")
                }
                HStack(spacing: DS.s2) {
                    Button {
                        Task { await model.retryDirectBroker(); await model.load(.health) }
                    } label: { Label("Retry direct broker", systemImage: "bolt.horizontal") }
                        .buttonStyle(.organicSoft)
                    Button {
                        Task {
                            actionError = await model.brokerRefresh()
                            await model.load(.health)
                        }
                    } label: { Label("Broker refresh", systemImage: "arrow.clockwise") }
                        .buttonStyle(.organicSoft)
                }
                .padding(.top, DS.s1)
            }
        }
    }

    private var filesCard: some View {
        Card("State-file freshness", subtitle: "what the bot has written, and when") {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 190), spacing: DS.s3)],
                      alignment: .leading, spacing: DS.s3) {
                ForEach(p?.files ?? []) { f in
                    HStack(spacing: 8) {
                        Circle().fill(freshnessColor(f)).frame(width: 9, height: 9)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(f.label).font(DSFont.semibold(13)).foregroundStyle(DS.text)
                                .lineLimit(1)
                            Text(f.exists == true ? Fmt.ago(f.ageSeconds) : "missing")
                                .font(DSFont.num(11)).foregroundStyle(DS.textMuted)
                        }
                        Spacer(minLength: 0)
                    }
                    .help(f.path ?? "")
                }
            }
        }
    }

    private func freshnessColor(_ f: StateFile) -> Color {
        guard f.exists == true, let age = f.ageSeconds else { return DS.textFaint }
        if age < 30 * 60 { return DS.up }
        if age < 26 * 3600 { return DS.caution }
        return DS.down
    }

    @ViewBuilder
    private var watchdogCard: some View {
        let lines = p?.watchdogLog ?? []
        if !lines.isEmpty {
            Card("Watchdog log", subtitle: "independent dead-man, runs under launchd every 5 min") {
                ScrollView {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(lines.prefix(30).enumerated()), id: \.offset) { _, line in
                            Text(line)
                                .font(DSFont.mono(11))
                                .foregroundStyle(line.contains("ALERT")
                                                 ? Color(hex: "#e08a76") : DS.consoleText)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    .padding(DS.s3)
                }
                .frame(maxHeight: 240)
                .background(DS.consoleBG,
                            in: RoundedRectangle(cornerRadius: DS.rMd, style: .continuous))
            }
        }
    }

    @ViewBuilder
    private var warningsCard: some View {
        let warns = p?.agentWarnings ?? []
        if !warns.isEmpty {
            Card("Agent log warnings", subtitle: "WARNING lines scraped from the bot's own log") {
                VStack(alignment: .leading, spacing: 5) {
                    ForEach(Array(warns.enumerated()), id: \.offset) { _, w in
                        Text(w.line ?? "")
                            .font(DSFont.mono(11))
                            .foregroundStyle(DS.caution)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .lineLimit(3)
                    }
                }
            }
        }
    }
}
