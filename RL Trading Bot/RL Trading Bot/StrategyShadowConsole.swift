//
//  StrategyShadowConsole.swift — strategy config + version timeline,
//  the options-shadow paper sleeve with its go-live checklist, and the
//  console (alerts + manual-action journal).
//

import SwiftUI

// MARK: - Strategy

struct StrategyView: View {
    @Environment(AppModel.self) private var model
    @State private var showRaw = false
    @State private var actionError: String?

    private var s: StrategyPayload? { model.strategyPayload }

    struct TimelineEvent: Identifiable {
        let id = UUID()
        let when: String
        let title: String
        let detail: String
        let severity: String?
        let trades: [String]
    }

    private var timeline: [TimelineEvent] {
        var events: [TimelineEvent] = []
        for e in s?.changeEvents ?? [] {
            events.append(TimelineEvent(
                when: e.timestamp ?? "",
                title: "\(e.kind ?? "change"): \(e.target ?? "") v\(e.version?.compact ?? "?")",
                detail: e.summary ?? "", severity: e.severity, trades: e.tradeIds ?? []))
        }
        for v in s?.versionHistory ?? [] {
            events.append(TimelineEvent(
                when: v.date ?? "",
                title: "strategy v\(v.version?.compact ?? "?")",
                detail: v.summary?.compact ?? "", severity: v.severity,
                trades: v.tradeIds ?? []))
        }
        events.sort { $0.when > $1.when }
        var seen = Set<String>()
        return events.filter { e in
            let key = "\(e.when.prefix(16))|\(e.title)"
            return seen.insert(key).inserted
        }
    }

    var body: some View {
        Group {
            if s == nil {
                LoadingScreen(title: "Loading strategy…", detail: "live config and version history")
                    .background(DS.bg)
            } else {
                Screen(title: "Strategy", subtitle: AppTab.strategy.blurb,
                       refresh: { await model.load(.strategy) }) {
                    if let err = model.tabErrors[.strategy] { ErrorBox(text: err) }
                    if let err = actionError { ErrorBox(text: err) }
                    headlineCard
                    AdaptivePair(compactBelow: 820) {
                        paramsCard
                    } second: {
                        weightsCard
                    }
                    learnedRulesCard
                    timelineCard
                    rawConfigCard
                }
            }
        }
        .task { if s == nil { await model.load(.strategy) } }
    }

    private var headlineCard: some View {
        let paused = model.overview?.bot?.pause?.paused == true
        let halted = model.overview?.bot?.halt?.halted == true
        let cfg = s?.config
        return Card {
            VStack(alignment: .leading, spacing: DS.s3) {
                WidthReader(compactBelow: 620) { compact in
                    let left = VStack(alignment: .leading, spacing: 4) {
                        Text("RX-3 momentum rotation + EMA ribbon")
                            .font(DSFont.heading(20)).foregroundStyle(DS.text)
                        Text("strategy.json v\(s?.version?.compact ?? "?") · \((s?.skillVersions ?? [:]).count) versioned skills")
                            .font(DSFont.body(13)).foregroundStyle(DS.textMuted)
                    }
                    let right = HStack(spacing: DS.s2) {
                        if halted { Tag("HALTED", tone: .down, size: 12) }
                        PillToggle(onLabel: "Active", offLabel: "Paused", isOn: !paused) {
                            model.requireArm {
                                Task {
                                    actionError = paused
                                        ? await model.resumeBot() : await model.pauseBot()
                                }
                            }
                        }
                    }
                    if compact {
                        VStack(alignment: .leading, spacing: DS.s3) { left; right }
                    } else {
                        HStack { left; Spacer(minLength: DS.s2); right }
                    }
                }
                Text(cfg?["goal"]?.stringValue
                     ?? "Deterministic top-momentum rotation throttled by realized vol and a cross-asset risk-appetite gate, with a hard stop, a trailing stop and an account-level kill-switch underneath it.")
                    .font(DSFont.body(14)).foregroundStyle(DS.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var paramsCard: some View {
        let cfg = s?.config
        let rm = cfg?["risk_management"]
        let research = cfg?["research"]
        return Card("Parameters", subtitle: "the live values every cycle reads") {
            VStack(spacing: DS.s2) {
                KeyValueRow(key: "Ribbon", value: "plain EMA 8 / 13 / 21 / 55")
                KeyValueRow(key: "Stop loss",
                            value: Fmt.pct((rm?["stop_loss_pct"]?.numberValue ?? 0) * 100,
                                           dp: 0, signed: false))
                KeyValueRow(key: "Trailing stop",
                            value: Fmt.pct((rm?["trailing_stop_pct"]?.numberValue ?? 0) * 100,
                                           dp: 0, signed: false))
                KeyValueRow(key: "Ribbon-sell exit",
                            value: rm?["exit_on_ribbon_sell"]?.boolValue == true
                                ? "ON" : "off (advisory)")
                KeyValueRow(key: "Scale into winners",
                            value: rm?["scale_into_winners"]?.boolValue == true ? "yes" : "no")
                KeyValueRow(key: "Monthly halt DD",
                            value: Fmt.pct((rm?["monthly_halt_dd"]?.numberValue ?? 0) * 100,
                                           dp: 0, signed: false))
                KeyValueRow(key: "Min confidence to trade",
                            value: research?["min_confidence_to_trade"]?.compact ?? "—")
                KeyValueRow(key: "Settlement", value: "T+1 — settled cash only")
            }
        }
    }

    private var weightsCard: some View {
        let weights = s?.config?["research"]?["source_weights"]?.objectValue ?? [:]
        return Card("Source weights", subtitle: "rebalanced from measured accuracy") {
            if weights.isEmpty {
                EmptyNote(text: "No source weights in the config.", icon: "scalemass")
            } else {
                VStack(spacing: DS.s2) {
                    ForEach(weights.keys.sorted(), id: \.self) { k in
                        let w = weights[k]?.numberValue ?? 0
                        BarRow(label: k, valueText: Fmt.pct(w * 100, dp: 1, signed: false),
                               fraction: min(w / 0.5, 1))
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var learnedRulesCard: some View {
        let rules = s?.config?["learned_rules"]?.arrayValue ?? []
        if !rules.isEmpty {
            Card("Learned rules", subtitle: "written by skill_5 after real trades") {
                VStack(alignment: .leading, spacing: DS.s3) {
                    ForEach(Array(rules.enumerated()), id: \.offset) { _, r in
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 8) {
                                Tag(r["id"]?.stringValue ?? "LR?", tone: .accent, size: 10)
                                Text(r["added"]?.stringValue ?? "")
                                    .font(DSFont.num(11)).foregroundStyle(DS.textFaint)
                            }
                            Text(r["rule"]?.stringValue ?? r.compact)
                                .font(DSFont.body(14)).foregroundStyle(DS.text)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
        }
    }

    private var timelineCard: some View {
        let skills = (s?.skillVersions ?? [:])
            .map { "\($0.key.replacingOccurrences(of: "skill_", with: "s"))v\($0.value.latest ?? 0)" }
            .sorted().joined(separator: " · ")
        return Card("Version timeline",
                    subtitle: "\(timeline.count) recorded changes · skills: \(skills)") {
            if timeline.isEmpty {
                EmptyNote(text: "No change events recorded yet.", icon: "clock.arrow.circlepath")
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(timeline.prefix(50)) { e in
                        HStack(alignment: .top, spacing: DS.s3) {
                            VStack(spacing: 0) {
                                Circle()
                                    .fill(e.severity == "MAJOR" ? DS.down : DS.accent)
                                    .frame(width: 10, height: 10)
                                Rectangle().fill(DS.divider).frame(width: 1.5)
                            }
                            VStack(alignment: .leading, spacing: 3) {
                                HStack(spacing: 7) {
                                    Text(e.when.prefix(16))
                                        .font(DSFont.num(11)).foregroundStyle(DS.textFaint)
                                    if let sev = e.severity {
                                        Tag(sev, tone: sev == "MAJOR" ? .down : .neutral, size: 9)
                                    }
                                    if !e.trades.isEmpty {
                                        Tag(e.trades.joined(separator: ","), tone: .neutral, size: 9)
                                    }
                                }
                                Text(e.title).font(DSFont.semibold(14)).foregroundStyle(DS.text)
                                if !e.detail.isEmpty {
                                    Text(e.detail).font(DSFont.body(13))
                                        .foregroundStyle(DS.textMuted).lineLimit(4)
                                }
                            }
                            .padding(.bottom, DS.s3)
                            Spacer(minLength: 0)
                        }
                        .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    private var rawConfigCard: some View {
        Card {
            VStack(alignment: .leading, spacing: DS.s3) {
                Button { showRaw.toggle() } label: {
                    HStack(spacing: 8) {
                        Image(systemName: showRaw ? "chevron.down" : "chevron.right")
                            .font(.system(size: 10, weight: .bold))
                        Text("Full strategy.json")
                            .font(DSFont.semibold(14))
                        Spacer()
                    }
                    .foregroundStyle(DS.text)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                if showRaw {
                    ScrollView {
                        Text(s?.config?.pretty ?? "—")
                            .font(DSFont.mono(11))
                            .foregroundStyle(DS.consoleText)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .textSelection(.enabled)
                            .padding(DS.s3)
                    }
                    .frame(maxHeight: 400)
                    .background(DS.consoleBG,
                                in: RoundedRectangle(cornerRadius: DS.rMd, style: .continuous))
                }
            }
        }
    }
}

// MARK: - Shadow

struct ShadowView: View {
    @Environment(AppModel.self) private var model
    private var sh: ShadowPayload? { model.shadowPayload }

    var body: some View {
        Group {
            if sh == nil {
                LoadingScreen(title: "Loading shadow…", detail: "paper options sleeve")
                    .background(DS.bg)
            } else {
                Screen(title: "Shadow", subtitle: AppTab.shadow.blurb,
                       refresh: { await model.load(.shadow) }) {
                    if let err = model.tabErrors[.shadow] { ErrorBox(text: err) }
                    statTiles
                    AdaptivePair(compactBelow: 860, ratio: 1.2) {
                        goLiveCard
                    } second: {
                        momentumCard
                    }
                    positionsCard
                    rx3Card
                }
            }
        }
        .task { if sh == nil { await model.load(.shadow) } }
    }

    private var statTiles: some View {
        let s = sh?.optionsShadow?.summary
        let live = sh?.goLive
        return StatRow {
            StatCard(label: "Shadow mode",
                     value: live?.shadowMode == true ? "ON" : "off",
                     delta: live?.enabled == true ? "REAL ORDERS ARMED" : "paper only — no real option order",
                     deltaTone: live?.enabled == true ? .down : .up,
                     sublabel: "set in strategy.json → options")
            StatCard(label: "Open / closed",
                     value: "\(s?.open ?? 0) / \(s?.closed ?? 0)",
                     sublabel: "spread-inclusive paper record")
            StatCard(label: "Win rate",
                     value: Fmt.pct((s?.winRate ?? 0) * 100, dp: 0, signed: false),
                     delta: "\(s?.wins ?? 0) winners",
                     deltaTone: (s?.winRate ?? 0) >= 0.5 ? .up : .down,
                     sublabel: "closed shadows only")
            StatCard(label: "Avg % on premium",
                     value: Fmt.pct(s?.avgPctOnPremium),
                     delta: "\(Fmt.usdSigned(s?.totalPnlPerContractUsd)) / contract",
                     deltaTone: .forValue(s?.avgPctOnPremium),
                     sublabel: "account-size independent")
        }
    }

    private var goLiveCard: some View {
        Card("Options go-live checklist",
             subtitle: "enabled=\(sh?.goLive?.enabled == true ? "true" : "false") · shadow=\(sh?.goLive?.shadowMode == true ? "true" : "false")") {
            let gates = sh?.goLive?.gates ?? []
            if gates.isEmpty {
                EmptyNote(text: "No gates configured.", icon: "checklist")
            } else {
                VStack(spacing: 0) {
                    ForEach(gates) { gate in
                        TRow(showsDivider: gate.id != gates.last?.id) {
                            Image(systemName: gate.met == true
                                  ? "checkmark.circle.fill" : "circle")
                                .font(.system(size: 14))
                                .foregroundStyle(gate.met == true ? DS.up : DS.textFaint)
                                .frame(width: 20)
                            TCell(weight: 4) {
                                Text(gate.gate).font(DSFont.body(14)).foregroundStyle(DS.text)
                            }
                            TCell(align: .trailing) {
                                Text(gate.current?.compact ?? "—")
                                    .font(DSFont.num(12)).foregroundStyle(DS.textMuted)
                                    .lineLimit(1)
                            }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var momentumCard: some View {
        let mw = sh?.momentumWatch ?? [:]
        Card("Today's momentum watch", subtitle: "research-fed catalysts — zero extra tokens") {
            if mw.isEmpty {
                EmptyNote(text: "No momentum watch block in the latest research.",
                          icon: "sparkles")
            } else {
                VStack(spacing: 0) {
                    let keys = mw.keys.sorted()
                    ForEach(keys, id: \.self) { sym in
                        TRow(showsDivider: sym != keys.last) {
                            TCell {
                                Text(sym).font(DSFont.bold(14)).foregroundStyle(DS.text)
                            }
                            TCell(align: .leading) {
                                Tag("conf \(mw[sym]?.confidence ?? 0)",
                                    tone: (mw[sym]?.confidence ?? 0) >= 75 ? .up : .caution,
                                    size: 10)
                            }
                            TCell(weight: 4) {
                                Text(mw[sym]?.catalyst ?? "")
                                    .font(DSFont.body(13)).foregroundStyle(DS.text).lineLimit(2)
                            }
                        }
                    }
                }
            }
        }
    }

    private var positionsCard: some View {
        let positions = Array((sh?.optionsShadow?.positions ?? []).reversed())
        return Card("Shadow option positions",
                    subtitle: "\(positions.count) paper trades · entry at ask, exit at bid") {
            if positions.isEmpty {
                EmptyNote(text: "No shadow positions opened yet.", icon: "square.stack.3d.up")
            } else {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 330), spacing: DS.s3)],
                          alignment: .leading, spacing: DS.s3) {
                    ForEach(positions, id: \.stableId) { p in shadowTile(p) }
                }
            }
        }
    }

    private func shadowTile(_ p: ShadowPosition) -> some View {
        let isOpen = p.status == "open"
        return Tile(padding: DS.s3) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 7) {
                    Text("\(p.underlying ?? "?") $\(Fmt.num(p.strike, dp: 0)) \(p.type ?? "")")
                        .font(DSFont.bold(14)).foregroundStyle(DS.text)
                    Text(p.expiry ?? "").font(DSFont.num(11)).foregroundStyle(DS.textFaint)
                    Spacer()
                    Tag(isOpen ? "OPEN" : Fmt.pct(p.pnlPctOnPremium),
                        tone: isOpen ? .neutral
                            : ((p.pnlPctOnPremium ?? 0) >= 0 ? .up : .down),
                        size: 10)
                }
                row("Signal", (p.signalSource ?? "ema_ribbon")
                    + (p.catalyst.map { " — \($0)" } ?? ""))
                row("Premium", "in \(Fmt.usd(p.entryPremium))"
                    + (p.exitPremium.map { " → out \(Fmt.usd($0))" } ?? ""))
                row("Underlying", "entry \(Fmt.usd(p.entryUnderlying))"
                    + (p.underlyingPriceNow.map { " → now \(Fmt.usd($0))" } ?? ""))
                if let reason = p.exitReason { row("Exit", reason) }
                if p.oversizedForAccount == true {
                    Text("⚠ oversized for account (\(Fmt.pct(p.pctOfAccount, dp: 1, signed: false))) — why it's paper-only")
                        .font(DSFont.body(11)).foregroundStyle(DS.caution)
                }
            }
        }
    }

    private func row(_ k: String, _ v: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 7) {
            Text(k.uppercased()).font(DSFont.label(9)).kerning(0.4)
                .foregroundStyle(DS.textFaint)
                .frame(width: 72, alignment: .leading)
            Text(v).font(DSFont.body(12)).foregroundStyle(DS.text)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var rx3Card: some View {
        Card("RX-3 paper snapshot", subtitle: "deterministic rotation state, as recorded") {
            if let rx3 = sh?.rx3, rx3.objectValue?.isEmpty == false {
                ScrollView {
                    Text(String(rx3.pretty.prefix(4000)))
                        .font(DSFont.mono(11))
                        .foregroundStyle(DS.consoleText)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                        .padding(DS.s3)
                }
                .frame(maxHeight: 220)
                .background(DS.consoleBG,
                            in: RoundedRectangle(cornerRadius: DS.rMd, style: .continuous))
            } else {
                EmptyNote(text: "No paper days recorded yet — starts on the next market-day cycle.",
                          icon: "arrow.triangle.2.circlepath")
            }
        }
    }
}

// MARK: - Console

struct ConsoleView: View {
    @Environment(AppModel.self) private var model
    @State private var filter: LogLevel = .all

    enum LogLevel: String, CaseIterable, Hashable {
        case all = "All", info = "Info", warn = "Warn", error = "Error"
    }

    private struct Line: Identifiable {
        let id = UUID()
        let time: String
        let level: String
        let text: String
    }

    private var lines: [Line] {
        var out: [Line] = []
        for a in model.alerts {
            let when = a.ageSeconds.map { Date().addingTimeInterval(-$0) } ?? Date()
            out.append(Line(time: when.formatted(.dateTime.hour(.twoDigits(amPM: .omitted))
                                                 .minute(.twoDigits).second(.twoDigits)),
                            level: (a.level ?? "info").uppercased(),
                            text: "\(a.title ?? "")\(a.detail.map { " — \($0)" } ?? "")"))
        }
        for a in model.actions {
            let ok = a.result?["ok"]?.boolValue != false
            out.append(Line(time: Fmt.clock(a.ts),
                            level: ok ? "INFO" : "ERROR",
                            text: "\(a.action ?? "action") \(a.params?.compact ?? "")"))
        }
        for ev in (model.logsPayload?.events ?? []).prefix(80) {
            let lvl = ev.ok == false || ev.severity == "MAJOR" ? "ERROR"
                : ev.severity != nil ? "WARN" : "INFO"
            out.append(Line(time: Fmt.clock(ev.ts), level: lvl,
                            text: "\(ev.kind ?? ""): \(ev.title ?? "")"
                                + (ev.detail.map { " — \($0)" } ?? "")))
        }
        return out.filter { l in
            switch filter {
            case .all: true
            case .info: l.level == "INFO"
            case .warn: l.level == "WARN"
            case .error: l.level == "ERROR" || l.level == "CRITICAL"
            }
        }
    }

    var body: some View {
        Screen(title: "Console", subtitle: AppTab.console.blurb,
               refresh: { await model.load(.console) }) {
            alertsCard
            FilterPills(options: LogLevel.allCases.map { ($0, $0.rawValue) }, selection: $filter)
            logPanel
            journalCard
        }
        .task {
            await model.load(.console)
            if model.logsPayload == nil { await model.load(.activity) }
        }
    }

    private var alertsCard: some View {
        Card("Alerts", subtitle: "\(model.alerts.count) active") {
            if model.alerts.isEmpty {
                HStack(spacing: 8) {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(DS.up)
                    Text("No active alerts.").font(DSFont.body(14)).foregroundStyle(DS.text)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                // the bot can stack dozens of repeat alerts; show the newest
                // few so the log panel below stays reachable
                let shown = Array(model.alerts.prefix(8))
                VStack(spacing: 0) {
                    ForEach(Array(shown.enumerated()), id: \.offset) { i, a in
                        TRow(showsDivider: i < shown.count - 1) {
                            Image(systemName: icon(for: a.level))
                                .font(.system(size: 13))
                                .foregroundStyle(color(for: a.level))
                                .frame(width: 20)
                            TCell(weight: 5) {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(a.title ?? "")
                                        .font(DSFont.semibold(14))
                                        .foregroundStyle(color(for: a.level))
                                    if let d = a.detail, !d.isEmpty {
                                        Text(d).font(DSFont.body(12))
                                            .foregroundStyle(DS.textMuted).lineLimit(3)
                                    }
                                }
                            }
                            TCell(align: .trailing) {
                                Tag((a.level ?? "info").uppercased(),
                                    tone: a.level == "critical" ? .down
                                        : a.level == "warn" ? .caution : .neutral,
                                    size: 10)
                            }
                        }
                    }
                }
                if model.alerts.count > shown.count {
                    Text("+ \(model.alerts.count - shown.count) older alerts — the full stream is in the log below.")
                        .font(DSFont.body(12))
                        .foregroundStyle(DS.textMuted)
                        .padding(.top, DS.s2)
                }
            }
        }
    }

    private var logPanel: some View {
        let rows = lines
        return VStack(alignment: .leading, spacing: 0) {
            if rows.isEmpty {
                Text("(nothing at this level)")
                    .font(DSFont.mono(12)).foregroundStyle(DS.consoleText.opacity(0.6))
                    .padding(DS.s4)
            } else {
                ForEach(rows) { l in
                    HStack(alignment: .top, spacing: DS.s3) {
                        Text(l.time)
                            .font(DSFont.mono(11))
                            .foregroundStyle(DS.consoleText.opacity(0.55))
                            .frame(width: 62, alignment: .leading)
                        Text(l.level)
                            .font(DSFont.mono(11, .bold))
                            .foregroundStyle(levelColor(l.level))
                            .frame(width: 48, alignment: .leading)
                        Text(l.text)
                            .font(DSFont.mono(11))
                            .foregroundStyle(DS.consoleText)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .textSelection(.enabled)
                    }
                    .padding(.horizontal, DS.s4)
                    .padding(.vertical, 5)
                }
                .padding(.vertical, DS.s3)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(DS.consoleBG,
                    in: RoundedRectangle(cornerRadius: DS.rCard, style: .continuous))
    }

    private func levelColor(_ level: String) -> Color {
        switch level {
        case "ERROR", "CRITICAL": Color(hex: "#e08a76")
        case "WARN": Color(hex: "#f6a06b")
        default: Color(hex: "#aebf92")
        }
    }

    private var journalCard: some View {
        Card("Manual action journal",
             subtitle: "\u{201C}did I do that, or did the bot?\u{201D}") {
            if model.actions.isEmpty {
                EmptyNote(text: "No manual actions yet — everything so far was the bot.",
                          icon: "hand.tap")
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(model.actions.enumerated()), id: \.offset) { i, a in
                        let ok = a.result?["ok"]?.boolValue != false
                        TRow(showsDivider: i < model.actions.count - 1) {
                            TCell(weight: 1.2) {
                                Text(String((a.ts ?? "").dropFirst(5).prefix(11)))
                                    .font(DSFont.num(11)).foregroundStyle(DS.textFaint)
                            }
                            TCell(weight: 1.6) {
                                Text(a.action ?? "").font(DSFont.semibold(13))
                                    .foregroundStyle(DS.text)
                            }
                            TCell(weight: 4) {
                                Text(a.params?.compact ?? "")
                                    .font(DSFont.mono(11)).foregroundStyle(DS.textMuted)
                                    .lineLimit(2)
                            }
                            TCell(align: .trailing) {
                                Image(systemName: ok ? "checkmark" : "xmark")
                                    .font(.system(size: 11, weight: .bold))
                                    .foregroundStyle(ok ? DS.up : DS.down)
                            }
                        }
                    }
                }
            }
        }
    }

    private func icon(for level: String?) -> String {
        switch level {
        case "critical": "exclamationmark.octagon.fill"
        case "warn": "exclamationmark.triangle.fill"
        default: "info.circle"
        }
    }

    private func color(for level: String?) -> Color {
        switch level {
        case "critical": DS.down
        case "warn": DS.caution
        default: DS.text
        }
    }
}
