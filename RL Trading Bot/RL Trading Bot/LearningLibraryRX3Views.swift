//
//  LearningLibraryRX3Views.swift — the learning loop (source accuracy,
//  learned rules, rewrite queue, verdicts), the research/analysis library,
//  and the RX-3 rotation paper tracker.
//

import SwiftUI
import Charts

// MARK: - Learning

struct LearningView: View {
    @Environment(AppModel.self) private var model
    @State private var document: PostmortemMeta?

    private var p: LearningPayload? { model.learningPayload }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let err = model.tabErrors[.learning] { ErrorBox(text: err) }
                AdaptivePair(compactBelow: 700) {
                    leaderboardCard
                } second: {
                    skillVersionsCard
                }
                learnedRulesCard
                observationsCard
                queueCard
                verdictsCard
                analysesCard
            }
            .padding()
        }
        .refreshable { await model.load(.learning) }
        .task { if p == nil { await model.load(.learning) } }
        .sheet(item: $document) { pm in
            DocumentSheet(title: pm.file, loader: { await model.postmortemText(pm.file) })
        }
        .overlay {
            if p == nil { ContentUnavailableView("Loading learning loop…",
                                                 systemImage: "graduationcap") }
        }
    }

    private var leaderboardCard: some View {
        GroupBox {
            VStack(spacing: 8) {
                if (p?.leaderboard ?? []).isEmpty {
                    Text("No source data yet.").font(.callout).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                ForEach(p?.leaderboard ?? []) { row in
                    VStack(spacing: 2) {
                        HStack {
                            Text(row.source).font(.callout)
                            Spacer()
                            Text("\(row.wins ?? 0)W/\(row.losses ?? 0)L")
                                .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                            Text(row.accuracy.map { Fmt.pct($0 * 100, dp: 0, signed: false) } ?? "—")
                                .font(.caption.bold().monospacedDigit())
                            Text("w \(Fmt.num(row.weight, dp: 2))")
                                .font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                        }
                        Gauge(value: row.accuracy ?? 0, in: 0...1) { EmptyView() }
                            .gaugeStyle(.accessoryLinearCapacity)
                            .tint((row.accuracy ?? 0) >= 0.5 ? Color.up : Color.down)
                            .scaleEffect(y: 0.7)
                    }
                }
            }
        } label: { Label("Source accuracy leaderboard", systemImage: "scalemass") }
    }

    private var skillVersionsCard: some View {
        GroupBox {
            VStack(spacing: 4) {
                if (p?.skillVersions ?? []).isEmpty {
                    Text("No skill rewrites yet.").font(.callout).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                ForEach(p?.skillVersions ?? []) { s in
                    KeyValueRow(key: s.skill,
                                value: "v\(String(format: "%03d", s.latest ?? 0)) · \(s.versions ?? 0) snapshots")
                }
            }
        } label: { Label("Skill file versions", systemImage: "doc.badge.gearshape") }
    }

    @ViewBuilder
    private var learnedRulesCard: some View {
        let rules = p?.learnedRules?.arrayValue ?? []
        if !rules.isEmpty {
            GroupBox {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(Array(rules.enumerated()), id: \.offset) { _, r in
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(r["id"]?.stringValue ?? "LR?") · \(r["added"]?.stringValue ?? "")")
                                .font(.caption.bold().monospaced())
                                .foregroundStyle(Color.accentColor)
                            Text(r["rule"]?.stringValue ?? r.compact)
                                .font(.callout)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } label: { Label("Learned rules (from postmortems)", systemImage: "checkmark.shield") }
        }
    }

    @ViewBuilder
    private var observationsCard: some View {
        let obs = p?.observations?.arrayValue ?? []
        if !obs.isEmpty {
            GroupBox {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(obs.enumerated()), id: \.offset) { _, o in
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Image(systemName: "eye")
                                .font(.caption).foregroundStyle(Color.caution)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(o["id"]?.stringValue ?? "OBS?")
                                    .font(.caption.bold().monospaced())
                                    .foregroundStyle(Color.caution)
                                Text(o["observation"]?.stringValue ?? o["note"]?.stringValue ?? o.compact)
                                    .font(.callout)
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } label: {
                Label("Observations pending confirmation (need 3+ similar outcomes)",
                      systemImage: "hourglass")
            }
        }
    }

    @ViewBuilder
    private var queueCard: some View {
        let queue = p?.rewriteQueue ?? []
        if !queue.isEmpty {
            GroupBox {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(queue) { e in
                        HStack(spacing: 8) {
                            Image(systemName: e.done == true ? "checkmark.circle.fill" : "clock.fill")
                                .foregroundStyle(e.done == true ? Color.up : Color.caution)
                                .font(.caption)
                            VStack(alignment: .leading, spacing: 1) {
                                HStack(spacing: 6) {
                                    Text(e.tradeId ?? "—").font(.caption.bold().monospaced())
                                    Text(e.symbol ?? "").font(.caption.monospaced())
                                    if let out = e.outcome {
                                        Text(out).font(.caption2.bold())
                                            .foregroundStyle(out == "WIN" ? Color.up : Color.down)
                                    }
                                    Text(e.pnl ?? "").font(.caption2.monospacedDigit())
                                        .foregroundStyle(.secondary)
                                }
                                Text(e.done == true ? "processed by skill_5" : "queued for skill_5 rewrite")
                                    .font(.caption2).foregroundStyle(.secondary)
                            }
                            Spacer()
                        }
                    }
                }
            } label: { Label("Strategy-rewrite queue", systemImage: "arrow.triangle.branch") }
        }
    }

    @ViewBuilder
    private var verdictsCard: some View {
        let verdicts = p?.verdicts ?? []
        if !verdicts.isEmpty {
            GroupBox {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(Array(verdicts.enumerated()), id: \.offset) { _, v in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(v.file ?? "—").font(.caption.monospaced())
                                .foregroundStyle(.secondary)
                            let sources = (v.sources ?? [:]).sorted { $0.key < $1.key }
                            FlowChips(items: sources.map { (name: $0.key, ok: $0.value) })
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } label: { Label("Per-trade source verdicts", systemImage: "checklist") }
        }
    }

    private var analysesCard: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 6) {
                if (p?.postmortems ?? []).isEmpty {
                    Text("No postmortems or victories yet.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                ForEach(p?.postmortems ?? []) { pm in
                    Button { document = pm } label: {
                        HStack(spacing: 8) {
                            Image(systemName: pm.kind == "victory" ? "trophy.fill" : "stethoscope")
                                .foregroundStyle(pm.kind == "victory" ? Color.up : Color.down)
                                .frame(width: 18)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(pm.title ?? pm.file).font(.callout).lineLimit(1)
                                Text("\(pm.file) · \((pm.tradeIds ?? []).joined(separator: ", "))")
                                    .font(.caption2.monospaced()).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Image(systemName: "chevron.right")
                                .font(.caption2).foregroundStyle(.tertiary)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        } label: { Label("All analyses", systemImage: "books.vertical") }
    }
}

/// Wrapping row of right/wrong source chips.
struct FlowChips: View {
    let items: [(name: String, ok: Bool)]

    var body: some View {
        WidthReader(compactBelow: 0) { _ in
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 110), spacing: 6)],
                      alignment: .leading, spacing: 4) {
                ForEach(items, id: \.name) { item in
                    Label(item.name, systemImage: item.ok ? "checkmark" : "xmark")
                        .font(.caption2.weight(.semibold))
                        .lineLimit(1)
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background((item.ok ? Color.up : Color.down).opacity(0.12),
                                    in: Capsule())
                        .foregroundStyle(item.ok ? Color.up : Color.down)
                }
            }
        }
    }
}

// MARK: - Library

struct LibraryView: View {
    @Environment(AppModel.self) private var model
    @State private var search = ""
    @State private var research: ResearchFileMeta?
    @State private var analysis: PostmortemMeta?

    private var p: LibraryPayload? { model.libraryPayload }

    private var researchFiles: [ResearchFileMeta] {
        (p?.research ?? []).filter {
            search.isEmpty || $0.file.lowercased().contains(search.lowercased())
        }
    }
    private var analyses: [PostmortemMeta] {
        (p?.postmortems ?? []).filter {
            search.isEmpty
            || $0.file.lowercased().contains(search.lowercased())
            || ($0.title ?? "").lowercased().contains(search.lowercased())
        }
    }

    var body: some View {
        List {
            if let err = model.tabErrors[.library] { ErrorBox(text: err) }
            Section("Research (\(researchFiles.count)) — weekend picks + midweek reviews") {
                ForEach(researchFiles) { f in
                    Button { research = f } label: {
                        HStack(spacing: 8) {
                            Image(systemName: f.kind == "picks" ? "lightbulb.fill" : "stethoscope.circle")
                                .foregroundStyle(f.kind == "picks" ? Color.caution : Color.accentColor)
                                .frame(width: 18)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(f.file).font(.callout.monospaced()).lineLimit(1)
                                Text(f.kind == "picks" ? "research picks · \(f.date ?? "")"
                                     : "midweek review · \(f.date ?? "")")
                                    .font(.caption2).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Image(systemName: "chevron.right").font(.caption2).foregroundStyle(.tertiary)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
            Section("Analyses (\(analyses.count)) — postmortems + victories") {
                ForEach(analyses) { pm in
                    Button { analysis = pm } label: {
                        HStack(spacing: 8) {
                            Image(systemName: pm.kind == "victory" ? "trophy.fill" : "stethoscope")
                                .foregroundStyle(pm.kind == "victory" ? Color.up : Color.down)
                                .frame(width: 18)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(pm.title ?? pm.file).font(.callout).lineLimit(1)
                                Text(pm.file).font(.caption2.monospaced()).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Image(systemName: "chevron.right").font(.caption2).foregroundStyle(.tertiary)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .searchable(text: $search, prompt: "filename / title")
        .refreshable { await model.load(.library) }
        .task { if p == nil { await model.load(.library) } }
        .sheet(item: $research) { f in
            DocumentSheet(title: f.file, loader: { await model.researchText(f.file) })
        }
        .sheet(item: $analysis) { pm in
            DocumentSheet(title: pm.file, loader: { await model.postmortemText(pm.file) })
        }
        .overlay {
            if p == nil { ContentUnavailableView("Loading library…", systemImage: "books.vertical") }
        }
    }
}

// MARK: - RX-3 rotation

struct RX3View: View {
    @Environment(AppModel.self) private var model

    private var p: RX3Payload? { model.rx3Payload }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let err = model.tabErrors[.rx3] { ErrorBox(text: err) }
                hero
                curveCard
                AdaptivePair(compactBelow: 700) {
                    bookCard
                } second: {
                    gatesCard
                }
                historyCard
                configCard
            }
            .padding()
        }
        .refreshable { await model.load(.rx3) }
        .task { if p == nil { await model.load(.rx3) } }
        .overlay {
            if p == nil { ContentUnavailableView("Loading RX-3 paper track…",
                                                 systemImage: "arrow.triangle.2.circlepath") }
        }
    }

    private var hero: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 14) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Paper equity").font(.caption).foregroundStyle(.secondary)
                        Text(Fmt.usd(p?.meta?.equity)).font(.title2.bold().monospacedDigit())
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Since start").font(.caption).foregroundStyle(.secondary)
                        Text(Fmt.pct(p?.meta?.returnPct))
                            .font(.title3.bold().monospacedDigit())
                            .foregroundStyle((p?.meta?.returnPct ?? 0).pnlColor)
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Cash").font(.caption).foregroundStyle(.secondary)
                        Text(Fmt.usd(p?.meta?.cash)).font(.title3.monospacedDigit())
                    }
                    Spacer()
                    VStack(alignment: .trailing, spacing: 2) {
                        Text("last run \(Fmt.when(p?.meta?.lastRun))")
                            .font(.caption2).foregroundStyle(.secondary)
                        Text("started \(Fmt.when(p?.meta?.startDate))")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                }
                let days = p?.paperDays ?? 0
                let gate = p?.promotionGateDays ?? 10
                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Text("Promotion ladder — paper phase")
                            .font(.caption.weight(.semibold))
                        Spacer()
                        Text("\(days)/\(gate) days")
                            .font(.caption.bold().monospacedDigit())
                            .foregroundStyle(days >= gate ? Color.up : Color.caution)
                    }
                    ProgressView(value: Double(min(days, gate)), total: Double(gate))
                        .tint(days >= gate ? Color.up : Color.caution)
                    Text("≥\(gate) paper days matching the engine → 2 weeks half size → full. Flipping live is an operator action.")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
        } label: {
            Label("RX-3 — deterministic rotation × vol throttle × RISKX (paper)",
                  systemImage: "arrow.triangle.2.circlepath")
        }
    }

    private struct CurvePoint: Identifiable {
        let id = UUID()
        let ts: String
        let series: String
        let value: Double
    }

    private var curveData: [CurvePoint] {
        var out: [CurvePoint] = []
        let curve = p?.curve ?? []
        if let base = curve.first?.value, base > 0 {
            for pt in curve {
                if let v = pt.value, let ts = pt.ts {
                    out.append(CurvePoint(ts: ts, series: "RX-3", value: v / base * 100))
                }
            }
        }
        let spy = p?.spySinceStart ?? []
        if let base = spy.first?.close, base > 0 {
            for pt in spy {
                if let c = pt.close, let ts = pt.ts {
                    out.append(CurvePoint(ts: ts, series: "SPY", value: c / base * 100))
                }
            }
        }
        return out
    }

    @ViewBuilder
    private var curveCard: some View {
        let data = curveData
        GroupBox {
            if data.count < 3 {
                Text("Curve appears after a few paper days.")
                    .font(.callout).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 60, alignment: .leading)
            } else {
                Chart(data) { pt in
                    LineMark(x: .value("Day", pt.ts), y: .value("Indexed", pt.value))
                        .foregroundStyle(by: .value("Series", pt.series))
                        .lineStyle(StrokeStyle(lineWidth: 1.8))
                }
                .chartForegroundStyleScale(["RX-3": Color.accentColor, "SPY": Color.secondary])
                .chartYScale(domain: .automatic(includesZero: false))
                .chartXAxis {
                    AxisMarks(values: .automatic(desiredCount: 5))
                }
                .frame(height: 190)
            }
        } label: { Label("Paper equity vs SPY (indexed to 100)", systemImage: "chart.xyaxis.line") }
    }

    private var bookCard: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 6) {
                    Text("Leaders").font(.caption).foregroundStyle(.secondary)
                    ForEach(p?.latest?.leaders ?? p?.leaders ?? [], id: \.self) { s in
                        chip(s, .up)
                    }
                    if let def = p?.latest?.defensive, !def.isEmpty {
                        Text("Defensive").font(.caption).foregroundStyle(.secondary)
                        ForEach(def, id: \.self) { s in chip(s, .caution) }
                    }
                    Spacer()
                }
                Divider()
                ForEach(p?.positions ?? []) { pos in
                    HStack {
                        Text(pos.symbol).font(.callout.monospaced())
                        Spacer()
                        Text("\(Fmt.num(pos.shares, dp: 4)) sh")
                            .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                        Text(Fmt.usd(pos.price ?? pos.lastPx))
                            .font(.callout.monospacedDigit())
                        Text(Fmt.pct(pos.changePct))
                            .font(.caption.monospacedDigit())
                            .foregroundStyle((pos.changePct ?? 0).pnlColor)
                        Text(Fmt.usd(pos.value))
                            .font(.callout.monospacedDigit())
                            .frame(minWidth: 64, alignment: .trailing)
                    }
                }
                if let latest = p?.latest {
                    Divider()
                    HStack(spacing: 12) {
                        gauge("RISKX", latest.riskx, "risk-appetite gate")
                        gauge("Vol scale", latest.volScale, "vol throttle")
                        gauge("Cash", latest.cashW, "weight parked")
                    }
                }
            }
        } label: { Label("Current paper book", systemImage: "briefcase") }
    }

    private func gauge(_ label: String, _ value: Double?, _ help: String) -> some View {
        VStack(spacing: 2) {
            Text(label).font(.caption2).foregroundStyle(.secondary)
            Gauge(value: min(max(value ?? 0, 0), 1), in: 0...1) { EmptyView() }
                .gaugeStyle(.accessoryLinearCapacity)
                .tint(Color.accentColor)
            Text(Fmt.pct((value ?? 0) * 100, dp: 0, signed: false))
                .font(.caption.bold().monospacedDigit())
        }
        .help(help)
    }

    private var gatesCard: some View {
        GroupBox {
            VStack(spacing: 4) {
                KeyValueRow(key: "Mode", value: p?.config?["mode"]?.stringValue ?? "paper")
                KeyValueRow(key: "Paper enabled",
                            value: p?.config?["paper_enabled"]?.boolValue == true ? "yes" : "no")
                KeyValueRow(key: "Top N", value: p?.config?["top_n"]?.compact ?? "—")
                KeyValueRow(key: "Target vol", value: p?.config?["target_vol"]?.compact ?? "—")
                KeyValueRow(key: "Universe",
                            value: "\((p?.config?["universe"]?.arrayValue ?? []).count) names")
                if let note = p?.meta?.note {
                    Divider().padding(.vertical, 2)
                    Text(note).font(.caption2).foregroundStyle(.secondary)
                }
            }
        } label: { Label("Rotation config", systemImage: "gearshape") }
    }

    @ViewBuilder
    private var historyCard: some View {
        let hist = p?.history ?? []
        if !hist.isEmpty {
            GroupBox {
                VStack(spacing: 6) {
                    ForEach(hist) { day in
                        HStack(spacing: 8) {
                            Text(day.date ?? "—").font(.caption.monospaced())
                                .frame(width: 84, alignment: .leading)
                            Text(Fmt.usd(day.equity)).font(.caption.monospacedDigit())
                                .frame(width: 70, alignment: .trailing)
                            Text((day.leaders ?? []).joined(separator: "+"))
                                .font(.caption2.monospaced()).foregroundStyle(Color.up)
                            if let def = day.defensive, !def.isEmpty {
                                Text(def.joined(separator: "+"))
                                    .font(.caption2.monospaced()).foregroundStyle(Color.caution)
                            }
                            Spacer()
                            Text("riskx \(Fmt.num(day.riskx, dp: 2))")
                                .font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                            Text("vol \(Fmt.num(day.volScale, dp: 2))")
                                .font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                            Text("cash \(Fmt.pct((day.cashW ?? 0) * 100, dp: 0, signed: false))")
                                .font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                        }
                    }
                }
            } label: { Label("Daily decisions", systemImage: "calendar.day.timeline.left") }
        }
    }

    private var configCard: some View {
        GroupBox {
            Text(p?.config?.pretty ?? "—")
                .font(.caption.monospaced())
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        } label: { Label("Raw rotation config", systemImage: "curlybraces") }
    }

    private func chip(_ text: String, _ color: Color) -> some View {
        Text(text).font(.caption.bold().monospaced())
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(color.opacity(0.15), in: Capsule())
            .foregroundStyle(color)
    }
}
