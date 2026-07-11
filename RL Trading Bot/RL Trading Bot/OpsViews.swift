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
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let err = model.tabErrors[.calendar] { ErrorBox(text: err) }
                scheduleCard
                AdaptivePair(compactBelow: 660) {
                    blackoutCard
                } second: {
                    universeCard
                }
                earningsCard
            }
            .padding()
        }
        .refreshable { await model.load(.calendar) }
        .task { if p == nil { await model.load(.calendar) } }
        .overlay {
            if p == nil { ContentUnavailableView("Loading calendar…",
                                                 systemImage: "calendar.badge.clock") }
        }
    }

    private var scheduleCard: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    StatusDot(color: p?.marketOpen == true ? .up : .secondary,
                              label: p?.marketOpen == true ? "MARKET OPEN" : "MARKET CLOSED")
                    Spacer()
                    Text("server \(Fmt.when(p?.serverTime))")
                        .font(.caption2).foregroundStyle(.secondary)
                }
                ForEach(p?.schedule ?? []) { ev in
                    HStack(spacing: 10) {
                        Image(systemName: icon(for: ev.kind))
                            .foregroundStyle(color(for: ev.kind))
                            .frame(width: 20)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(ev.label ?? "—").font(.callout)
                            Text(whenText(ev.when))
                                .font(.caption2.monospaced()).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Text(countdown(ev.when))
                            .font(.caption.bold().monospacedDigit())
                            .foregroundStyle(color(for: ev.kind))
                    }
                }
            }
        } label: { Label("Bot schedule (ET)", systemImage: "clock") }
    }

    private func whenText(_ ts: String?) -> String {
        guard let d = Fmt.date(ts) else { return ts ?? "—" }
        return d.formatted(.dateTime.weekday(.wide).month(.abbreviated).day().hour().minute())
    }

    private func countdown(_ ts: String?) -> String {
        guard let d = Fmt.date(ts) else { return "" }
        let s = d.timeIntervalSinceNow
        if s <= 0 { return "now" }
        if s < 3600 { return "in \(Int(s / 60))m" }
        if s < 86_400 { return String(format: "in %.1fh", s / 3600) }
        return String(format: "in %.1fd", s / 86_400)
    }

    private func icon(for kind: String?) -> String {
        switch kind {
        case "research": "brain.head.profile"
        case "open": "bell.fill"
        case "midweek": "stethoscope"
        case "close": "moon.zzz.fill"
        default: "calendar"
        }
    }

    private func color(for kind: String?) -> Color {
        switch kind {
        case "research": .accentColor
        case "open": .up
        case "midweek": .caution
        case "close": .secondary
        default: .primary
        }
    }

    private var blackoutCard: some View {
        GroupBox {
            VStack(spacing: 4) {
                KeyValueRow(key: "Earnings blackout",
                            value: "\(p?.blackoutWindows?["earnings_days"]?.compact ?? "—") days before report")
                KeyValueRow(key: "Fed-meeting blackout",
                            value: "\(p?.blackoutWindows?["fed_meeting_days"]?.compact ?? "—") days before FOMC")
                Text("The bot skips new entries inside these windows.")
                    .font(.caption2).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, 4)
            }
        } label: { Label("Blackout windows", systemImage: "nosign") }
    }

    private var universeCard: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 6) {
                Text("Held").font(.caption).foregroundStyle(.secondary)
                symbolWrap(p?.held ?? [], .up)
                Text("Watchlist").font(.caption).foregroundStyle(.secondary)
                symbolWrap(p?.watchlist ?? [], .secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } label: { Label("Names to watch for reports", systemImage: "list.star") }
    }

    private func symbolWrap(_ syms: [String], _ color: Color) -> some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 62), spacing: 5)],
                  alignment: .leading, spacing: 5) {
            ForEach(syms, id: \.self) { s in
                Button { model.analyze(s) } label: {
                    Text(s).font(.caption.weight(.semibold).monospaced())
                        .padding(.horizontal, 7).padding(.vertical, 2)
                        .background(color.opacity(0.12), in: Capsule())
                        .foregroundStyle(color == .secondary ? Color.primary : color)
                }
                .buttonStyle(.plain)
            }
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
        GroupBox {
            if let msg = p?.earnings?["unavailable"]?.stringValue
                ?? p?.earnings?["error"]?.stringValue {
                WarningBox(text: msg)
            } else if let rows = earningsRows {
                let held = Set(p?.held ?? [])
                VStack(spacing: 6) {
                    ForEach(Array(rows.prefix(30).enumerated()), id: \.offset) { _, r in
                        let sym = r["symbol"]?.stringValue ?? r["ticker"]?.stringValue ?? "—"
                        HStack(spacing: 8) {
                            Text(sym).font(.callout.monospaced())
                                .foregroundStyle(held.contains(sym) ? Color.up : Color.primary)
                            if held.contains(sym) {
                                Text("HELD").font(.caption2.bold()).foregroundStyle(Color.up)
                            }
                            Spacer()
                            Text(r["date"]?.stringValue ?? r["report_date"]?.stringValue
                                 ?? r["expected_report_date"]?.stringValue ?? "—")
                                .font(.caption.monospaced())
                            Text(r["time"]?.stringValue ?? r["timing"]?.stringValue ?? "")
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            } else if let e = p?.earnings {
                Text(e.pretty)
                    .font(.caption.monospaced())
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                Text("No earnings data.").font(.callout).foregroundStyle(.secondary)
            }
        } label: { Label("Earnings — next 2 weeks", systemImage: "megaphone") }
    }
}

// MARK: - Activity

struct ActivityView: View {
    @Environment(AppModel.self) private var model
    @State private var search = ""
    @State private var kindFilter: String? = nil

    private var events: [LogEvent] {
        var evs = model.logsPayload?.events ?? []
        if let k = kindFilter { evs = evs.filter { $0.kind == k } }
        if !search.isEmpty {
            let q = search.lowercased()
            evs = evs.filter {
                "\($0.title ?? "") \($0.detail ?? "") \($0.kind ?? "")".lowercased().contains(q)
            }
        }
        return evs
    }

    private static let kinds: [(key: String?, label: String, icon: String)] = [
        (nil, "All", "tray.full"),
        ("manual", "Manual", "hand.tap"),
        ("change", "Changes", "gearshape.2"),
        ("watchdog", "Watchdog", "dog"),
        ("agent", "Agent", "cpu"),
    ]

    var body: some View {
        List {
            if let err = model.tabErrors[.activity] { ErrorBox(text: err) }
            Section {
                Picker("Kind", selection: $kindFilter) {
                    ForEach(Self.kinds, id: \.label) { k in
                        Label(k.label, systemImage: k.icon).tag(k.key)
                    }
                }
                .pickerStyle(.segmented)
                .listRowBackground(Color.clear)
            }
            Section("\(events.count) events — every stream, newest first") {
                ForEach(Array(events.enumerated()), id: \.offset) { _, ev in
                    ActivityRow(event: ev)
                }
            }
        }
        .searchable(text: $search, prompt: "search events")
        .refreshable { await model.load(.activity) }
        .task { if model.logsPayload == nil { await model.load(.activity) } }
        .overlay {
            if model.logsPayload == nil {
                ContentUnavailableView("Loading activity…", systemImage: "clock.arrow.circlepath")
            }
        }
    }
}

struct ActivityRow: View {
    let event: LogEvent

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: icon)
                .font(.caption)
                .foregroundStyle(color)
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 1) {
                Text(event.title ?? "—")
                    .font(.callout.monospaced())
                    .lineLimit(3)
                HStack(spacing: 6) {
                    Text(event.kind ?? "").font(.caption2.bold()).foregroundStyle(color)
                    if let sev = event.severity {
                        Text(sev).font(.caption2.bold())
                            .foregroundStyle(sev == "MAJOR" ? Color.down : Color.caution)
                    }
                    if let d = event.detail, !d.isEmpty {
                        Text(d).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
            }
            Spacer()
            Text(Fmt.when(event.ts))
                .font(.caption2.monospaced())
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 1)
    }

    private var icon: String {
        switch event.kind {
        case "manual": "hand.tap.fill"
        case "change": "gearshape.2.fill"
        case "watchdog": "dog.fill"
        case "agent": "cpu.fill"
        default: "circle.fill"
        }
    }

    private var color: Color {
        if event.ok == false || event.severity == "MAJOR" { return .down }
        switch event.kind {
        case "manual": return .accentColor
        case "change": return .caution
        case "watchdog": return .secondary
        default: return .secondary
        }
    }
}

// MARK: - Health

struct HealthView: View {
    @Environment(AppModel.self) private var model
    @State private var actionError: String?

    private var p: HealthPayload? { model.healthPayload }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let err = model.tabErrors[.health] { ErrorBox(text: err) }
                if let err = actionError { ErrorBox(text: err) }
                botCard
                AdaptivePair(compactBelow: 700) {
                    controlCard
                } second: {
                    serverCard
                }
                filesCard
                watchdogCard
                warningsCard
            }
            .padding()
        }
        .refreshable { await model.load(.health) }
        .task { if p == nil { await model.load(.health) } }
        .overlay {
            if p == nil { ContentUnavailableView("Checking health…",
                                                 systemImage: "heart.text.square") }
        }
    }

    private var botCard: some View {
        let bot = p?.bot
        let state = bot?.state ?? "unknown"
        let color: Color = switch state {
        case "running": .up
        case "paused": .caution
        case "halted", "stale": .down
        default: .secondary
        }
        return GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 10) {
                    StatusDot(color: color, label: state.uppercased())
                    StatusDot(color: bot?.marketOpen == true ? .up : .secondary,
                              label: bot?.marketOpen == true ? "MARKET OPEN" : "MARKET CLOSED")
                    Spacer()
                    Text("heartbeat \(Fmt.ago(bot?.heartbeat?.ageSeconds))")
                        .font(.caption.monospaced())
                        .foregroundStyle((bot?.heartbeat?.ageSeconds ?? 0) > 1800 ? Color.down : Color.secondary)
                }
                if let cyc = bot?.cycle {
                    KeyValueRow(key: "Cycle",
                                value: "\(cyc.state ?? "—") · \(cyc.task ?? "") · \(Fmt.when(cyc.ts))")
                    if let detail = cyc.detail, !detail.isEmpty {
                        Text(detail).font(.caption2).foregroundStyle(.secondary)
                    }
                }
                if bot?.halt?.halted == true {
                    ErrorBox(text: "HALT active — \(bot?.halt?.detail ?? "kill-switch fired")")
                }
                if bot?.pause?.paused == true {
                    WarningBox(text: "PAUSED by operator — protective exits still run")
                }
            }
        } label: { Label("Bot status", systemImage: "brain") }
    }

    private var controlCard: some View {
        GroupBox {
            VStack(spacing: 4) {
                KeyValueRow(key: "PAUSE file",
                            value: p?.control?.pause?.paused == true ? "present" : "absent",
                            valueColor: p?.control?.pause?.paused == true ? .caution : .primary)
                KeyValueRow(key: "HALT file",
                            value: p?.control?.halt?.halted == true ? "PRESENT" : "absent",
                            valueColor: p?.control?.halt?.halted == true ? .down : .primary)
                KeyValueRow(key: "Do-not-trade",
                            value: (p?.control?.dnt ?? []).isEmpty
                                   ? "none" : (p?.control?.dnt ?? []).joined(separator: ", "))
                KeyValueRow(key: "Stop overrides",
                            value: (p?.control?.overrides ?? [:]).isEmpty
                                   ? "none" : (p?.control?.overrides ?? [:]).keys.sorted().joined(separator: ", "))
                KeyValueRow(key: "Latest picks file",
                            value: p?.latestPicks?.file ?? "none")
            }
        } label: { Label("Control plane (read each bot cycle)", systemImage: "switch.2") }
    }

    private var serverCard: some View {
        GroupBox {
            VStack(spacing: 4) {
                KeyValueRow(key: "Broker path", value: p?.broker?.mode ?? "—",
                            valueColor: p?.broker?.mode == "direct-mcp" ? .up : .caution)
                Text(p?.broker?.reason ?? "")
                    .font(.caption2).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                KeyValueRow(key: "PIN configured",
                            value: p?.pinConfigured == true ? "yes" : "NO — run --set-pin",
                            valueColor: p?.pinConfigured == true ? .primary : .down)
                KeyValueRow(key: "Server uptime", value: Fmt.ago(p?.server?.uptimeSeconds)
                    .replacingOccurrences(of: " ago", with: ""))
                KeyValueRow(key: "Bind", value: "\(p?.server?.host ?? "—"):\(p?.server?.port.map(String.init) ?? "—")")
                KeyValueRow(key: "Equity points", value: "\(p?.equityPoints ?? 0)")
                if let last = p?.lastEquity {
                    KeyValueRow(key: "Last equity", value: "\(Fmt.usd(last.total)) · \(Fmt.when(last.ts))")
                }
                HStack(spacing: 8) {
                    Button {
                        Task { await model.retryDirectBroker(); await model.load(.health) }
                    } label: { Label("Retry direct broker", systemImage: "bolt.horizontal") }
                    Button {
                        Task { actionError = await model.brokerRefresh(); await model.load(.health) }
                    } label: { Label("Broker refresh", systemImage: "arrow.clockwise") }
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .padding(.top, 4)
            }
        } label: { Label("Dashboard server", systemImage: "server.rack") }
    }

    private var filesCard: some View {
        GroupBox {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 175), spacing: 10)],
                      alignment: .leading, spacing: 8) {
                ForEach(p?.files ?? []) { f in
                    HStack(spacing: 6) {
                        Circle()
                            .fill(freshnessColor(f))
                            .frame(width: 8, height: 8)
                        VStack(alignment: .leading, spacing: 0) {
                            Text(f.label).font(.caption.weight(.semibold))
                            Text(f.exists == true ? Fmt.ago(f.ageSeconds) : "missing")
                                .font(.caption2.monospaced())
                                .foregroundStyle(.secondary)
                        }
                    }
                    .help(f.path ?? "")
                }
            }
        } label: { Label("State-file freshness", systemImage: "doc.badge.clock") }
    }

    private func freshnessColor(_ f: StateFile) -> Color {
        guard f.exists == true, let age = f.ageSeconds else { return .secondary }
        if age < 30 * 60 { return .up }
        if age < 26 * 3600 { return .caution }
        return .down
    }

    @ViewBuilder
    private var watchdogCard: some View {
        let lines = p?.watchdogLog ?? []
        if !lines.isEmpty {
            GroupBox {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(Array(lines.prefix(25).enumerated()), id: \.offset) { _, line in
                        Text(line)
                            .font(.caption2.monospaced())
                            .foregroundStyle(line.contains("ALERT") ? Color.down : Color.secondary)
                            .lineLimit(2)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } label: { Label("Watchdog log (independent dead-man)", systemImage: "dog") }
        }
    }

    @ViewBuilder
    private var warningsCard: some View {
        let warns = p?.agentWarnings ?? []
        if !warns.isEmpty {
            GroupBox {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(warns.enumerated()), id: \.offset) { _, w in
                        Text(w.line ?? "")
                            .font(.caption2.monospaced())
                            .foregroundStyle(Color.caution)
                            .lineLimit(2)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } label: { Label("Agent log warnings", systemImage: "exclamationmark.triangle") }
        }
    }
}
