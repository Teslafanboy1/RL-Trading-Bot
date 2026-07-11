//
//  StrategyShadowConsole.swift — strategy config + version timeline,
//  the options-shadow paper sleeve with its go-live checklist, and the
//  console (alerts + manual-action journal).
//

import SwiftUI

// MARK: - Strategy

struct StrategyView: View {
    @Environment(AppModel.self) private var model
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
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let err = model.tabErrors[.strategy] { ErrorBox(text: err) }
                configRow
                learnedRulesCard
                timelineCard
                rawConfigCard
            }
            .padding()
        }
        .opacity(s == nil ? 0 : 1)
        .refreshable { await model.load(.strategy) }
        .task { if s == nil { await model.load(.strategy) } }
        .overlay {
            if s == nil { ContentUnavailableView("Loading strategy…", systemImage: "gearshape.2") }
        }
    }

    private var configRow: some View {
        let cfg = s?.config
        let rm = cfg?["risk_management"]
        let research = cfg?["research"]
        return AdaptivePair(compactBelow: 660) {
            GroupBox {
                VStack(spacing: 4) {
                    KeyValueRow(key: "goal", value: cfg?["goal"]?.stringValue ?? "—")
                    KeyValueRow(key: "stop loss",
                                value: Fmt.pct((rm?["stop_loss_pct"]?.numberValue ?? 0) * 100, dp: 0, signed: false))
                    KeyValueRow(key: "trailing stop",
                                value: Fmt.pct((rm?["trailing_stop_pct"]?.numberValue ?? 0) * 100, dp: 0, signed: false))
                    KeyValueRow(key: "ribbon-sell exit",
                                value: rm?["exit_on_ribbon_sell"]?.boolValue == true ? "ON" : "off (advisory)")
                    KeyValueRow(key: "scale into winners",
                                value: rm?["scale_into_winners"]?.boolValue == true ? "yes" : "no")
                    KeyValueRow(key: "monthly halt DD",
                                value: Fmt.pct((rm?["monthly_halt_dd"]?.numberValue ?? 0) * 100, dp: 0, signed: false))
                    KeyValueRow(key: "min confidence",
                                value: research?["min_confidence_to_trade"]?.compact ?? "—")
                }
            } label: {
                Label("Live config — v\(s?.version?.compact ?? "?")", systemImage: "slider.horizontal.3")
            }
        } second: {
            GroupBox {
                let weights = research?["source_weights"]?.objectValue ?? [:]
                VStack(spacing: 6) {
                    ForEach(weights.keys.sorted(), id: \.self) { k in
                        let w = weights[k]?.numberValue ?? 0
                        VStack(spacing: 2) {
                            KeyValueRow(key: k, value: Fmt.pct(w * 100, dp: 1, signed: false))
                            Gauge(value: min(w, 0.5), in: 0...0.5) { EmptyView() }
                                .gaugeStyle(.accessoryLinearCapacity)
                                .tint(Color.accentColor)
                        }
                    }
                }
            } label: { Label("Source weights", systemImage: "scalemass") }
        }
    }

    @ViewBuilder
    private var learnedRulesCard: some View {
        let rules = s?.config?["learned_rules"]?.arrayValue ?? []
        if !rules.isEmpty {
            GroupBox {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(Array(rules.enumerated()), id: \.offset) { _, r in
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(r["id"]?.stringValue ?? "LR?") (\(r["added"]?.stringValue ?? ""))")
                                .font(.caption.bold().monospaced())
                                .foregroundStyle(Color.accentColor)
                            Text(r["rule"]?.stringValue ?? "")
                                .font(.callout)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } label: { Label("Learned rules", systemImage: "graduationcap.fill") }
        }
    }

    private var timelineCard: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(timeline.prefix(50)) { e in
                    HStack(alignment: .top, spacing: 10) {
                        VStack(spacing: 0) {
                            Circle()
                                .fill(e.severity == "MAJOR" ? Color.down : Color.accentColor)
                                .frame(width: 9, height: 9)
                            Rectangle().fill(.quaternary).frame(width: 1.5)
                        }
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(e.when.prefix(16)) \(e.severity.map { "· \($0)" } ?? "") \(e.trades.joined(separator: ","))")
                                .font(.caption2.monospaced()).foregroundStyle(.tertiary)
                            Text(e.title).font(.subheadline.weight(.semibold))
                            if !e.detail.isEmpty {
                                Text(e.detail).font(.caption).foregroundStyle(.secondary)
                                    .lineLimit(4)
                            }
                        }
                        .padding(.bottom, 12)
                        Spacer(minLength: 0)
                    }
                    .fixedSize(horizontal: false, vertical: true)
                }
            }
        } label: {
            let skills = (s?.skillVersions ?? [:])
                .map { "\($0.key.replacingOccurrences(of: "skill_", with: "s"))v\($0.value.latest ?? 0)" }
                .sorted().joined(separator: " · ")
            Label("Version timeline (\(timeline.count)) — skills: \(skills)",
                  systemImage: "clock.arrow.circlepath")
                .lineLimit(1)
                .truncationMode(.tail)
        }
    }

    private var rawConfigCard: some View {
        GroupBox {
            DisclosureGroup("full strategy.json (view)") {
                ScrollView {
                    Text(s?.config?.pretty ?? "—")
                        .font(.caption2.monospaced())
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
                .frame(maxHeight: 380)
            }
        }
    }
}

// MARK: - Shadow

struct ShadowView: View {
    @Environment(AppModel.self) private var model
    private var sh: ShadowPayload? { model.shadowPayload }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let err = model.tabErrors[.shadow] { ErrorBox(text: err) }
                goLiveCard
                AdaptivePair(compactBelow: 660) {
                    recordCard
                } second: {
                    rx3Card
                }
                positionsGrid
                momentumCard
            }
            .padding()
        }
        .opacity(sh == nil ? 0 : 1)
        .refreshable { await model.load(.shadow) }
        .task { if sh == nil { await model.load(.shadow) } }
        .overlay {
            if sh == nil { ContentUnavailableView("Loading shadow…", systemImage: "moon.stars") }
        }
    }

    private var goLiveCard: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(sh?.goLive?.gates ?? []) { gate in
                    HStack(alignment: .firstTextBaseline) {
                        Image(systemName: gate.met == true ? "checkmark.circle.fill" : "circle")
                            .foregroundStyle(gate.met == true ? Color.up : Color.secondary)
                        Text(gate.gate).font(.callout)
                        Spacer()
                        Text(gate.current?.compact ?? "—")
                            .font(.caption.monospaced()).foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                }
            }
        } label: {
            Label("Options go-live checklist — enabled=\(sh?.goLive?.enabled == true ? "true" : "false") · shadow=\(sh?.goLive?.shadowMode == true ? "true" : "false")",
                  systemImage: "checklist")
        }
    }

    private var recordCard: some View {
        GroupBox {
            let s = sh?.optionsShadow?.summary
            VStack(spacing: 4) {
                KeyValueRow(key: "open / closed", value: "\(s?.open ?? 0) / \(s?.closed ?? 0)")
                KeyValueRow(key: "win rate",
                            value: Fmt.pct((s?.winRate ?? 0) * 100, dp: 0, signed: false))
                KeyValueRow(key: "avg % on premium", value: Fmt.pct(s?.avgPctOnPremium),
                            valueColor: (s?.avgPctOnPremium ?? 0).pnlColor)
                KeyValueRow(key: "total P&L / contract",
                            value: Fmt.usdSigned(s?.totalPnlPerContractUsd),
                            valueColor: (s?.totalPnlPerContractUsd ?? 0).pnlColor)
            }
        } label: { Label("Shadow record", systemImage: "doc.text.magnifyingglass") }
    }

    private var rx3Card: some View {
        GroupBox {
            if let rx3 = sh?.rx3, rx3.objectValue?.isEmpty == false {
                ScrollView {
                    Text(rx3.pretty.prefix(2500))
                        .font(.caption2.monospaced())
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
                .frame(maxHeight: 170)
            } else {
                Text("no paper days recorded yet — starts on the next market-day cycle")
                    .font(.callout).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        } label: { Label("RX-3 paper (deterministic rotation)", systemImage: "arrow.triangle.2.circlepath") }
    }

    private var positionsGrid: some View {
        let cols = [GridItem(.adaptive(minimum: 320), spacing: 12)]
        let positions = Array((sh?.optionsShadow?.positions ?? []).reversed())
        return GroupBox {
            LazyVGrid(columns: cols, alignment: .leading, spacing: 12) {
                ForEach(positions, id: \.stableId) { p in
                    shadowCard(p)
                }
            }
        } label: {
            Label("Shadow option positions (\(positions.count))", systemImage: "square.stack.3d.up")
        }
    }

    private func shadowCard(_ p: ShadowPosition) -> some View {
        let isOpen = p.status == "open"
        return VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("\(p.underlying ?? "?") $\(Fmt.num(p.strike, dp: 0)) \(p.type ?? "")")
                    .font(.subheadline.bold().monospaced())
                Text(p.expiry ?? "").font(.caption2.monospaced()).foregroundStyle(.secondary)
                Spacer()
                Text(isOpen ? "OPEN" : Fmt.pct(p.pnlPctOnPremium))
                    .font(.caption.bold().monospacedDigit())
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background((isOpen ? Color.secondary : (p.pnlPctOnPremium ?? 0).pnlColor)
                        .opacity(0.15), in: Capsule())
                    .foregroundStyle(isOpen ? Color.secondary : (p.pnlPctOnPremium ?? 0).pnlColor)
            }
            row("Signal", (p.signalSource ?? "ema_ribbon")
                + (p.catalyst.map { " — \($0)" } ?? ""))
            row("Premium", "in \(Fmt.usd(p.entryPremium))"
                + (p.exitPremium.map { " → out \(Fmt.usd($0))" } ?? ""))
            row("Underlying", "entry \(Fmt.usd(p.entryUnderlying))"
                + (p.underlyingPriceNow.map { " → now \(Fmt.usd($0))" } ?? ""))
            if let reason = p.exitReason { row("Exit", reason) }
            if p.oversizedForAccount == true {
                Text("⚠ oversized for account (\(Fmt.pct(p.pctOfAccount, dp: 1, signed: false)) — why it's paper-only)")
                    .font(.caption2).foregroundStyle(Color.caution)
            }
        }
        .padding(10)
        .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
    }

    private func row(_ k: String, _ v: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(k).font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
                .frame(width: 68, alignment: .leading)
            Text(v).font(.caption)
        }
    }

    @ViewBuilder
    private var momentumCard: some View {
        if let mw = sh?.momentumWatch, !mw.isEmpty {
            GroupBox {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(mw.keys.sorted(), id: \.self) { sym in
                        HStack(alignment: .firstTextBaseline) {
                            Text(sym).font(.subheadline.bold().monospaced())
                            Text("conf \(mw[sym]?.confidence ?? 0) — \(mw[sym]?.catalyst ?? "")")
                                .font(.callout)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } label: { Label("Today's momentum watch (research-fed)", systemImage: "sparkles") }
        }
    }
}

// MARK: - Console

struct ConsoleView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        List {
            Section {
                if model.alerts.isEmpty {
                    Label("no active alerts", systemImage: "checkmark.circle.fill")
                        .foregroundStyle(Color.up)
                } else {
                    ForEach(Array(model.alerts.enumerated()), id: \.offset) { _, a in
                        VStack(alignment: .leading, spacing: 2) {
                            Label(a.title ?? "", systemImage: icon(for: a.level))
                                .font(.callout.weight(.semibold))
                                .foregroundStyle(color(for: a.level))
                            if let d = a.detail, !d.isEmpty {
                                Text(d).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .padding(.vertical, 2)
                    }
                }
            } header: { Text("Alerts (\(model.alerts.count))") }

            Section {
                if model.actions.isEmpty {
                    Text("no manual actions yet — everything so far was the bot")
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(Array(model.actions.enumerated()), id: \.offset) { _, a in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text(String((a.ts ?? "").dropFirst(5).prefix(11)))
                                .font(.caption2.monospaced()).foregroundStyle(.tertiary)
                            Text(a.action ?? "").font(.callout.bold().monospaced())
                            Text(a.params?.compact.prefix(90) ?? "")
                                .font(.caption).foregroundStyle(.secondary)
                                .lineLimit(1)
                            Spacer()
                            let ok = a.result?["ok"]?.boolValue != false
                            Image(systemName: ok ? "checkmark" : "xmark")
                                .foregroundStyle(ok ? Color.up : Color.down)
                        }
                    }
                }
            } header: {
                Text("Manual action journal — \u{201C}did I do that or did the bot\u{201D}")
            }
        }
        .refreshable { await model.load(.console) }
        .task { await model.load(.console) }
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
        case "critical": .down
        case "warn": .caution
        default: .secondary
        }
    }
}
