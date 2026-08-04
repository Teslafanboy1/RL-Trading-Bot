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
        Group {
            if p == nil {
                LoadingScreen(title: "Loading learning loop…",
                              detail: "source accuracy, rules, rewrite queue")
                    .background(DS.bg)
            } else {
                Screen(title: "Learning", subtitle: AppTab.learning.blurb,
                       refresh: { await model.load(.learning) }) {
                    if let err = model.tabErrors[.learning] { ErrorBox(text: err) }
                    statTiles
                    AdaptivePair(compactBelow: 860, ratio: 1.4) {
                        leaderboardCard
                    } second: {
                        skillVersionsCard
                    }
                    accuracyChartCard
                    learnedRulesCard
                    observationsCard
                    queueCard
                    verdictsCard
                    analysesCard
                }
            }
        }
        .task { if p == nil { await model.load(.learning) } }
        .sheet(item: $document) { pm in
            DocumentSheet(title: pm.file, loader: { await model.postmortemText(pm.file) })
        }
    }

    private var statTiles: some View {
        let board = p?.leaderboard ?? []
        let totalN = board.reduce(0) { $0 + ($1.n ?? (($1.wins ?? 0) + ($1.losses ?? 0))) }
        let avgAcc = board.isEmpty ? nil
            : board.compactMap(\.accuracy).reduce(0, +) / Double(max(1, board.compactMap(\.accuracy).count))
        let pending = (p?.rewriteQueue ?? []).filter { $0.done != true }.count
        let rules = (p?.learnedRules?.arrayValue ?? []).count
        let latestSkill = (p?.skillVersions ?? []).map { $0.latest ?? 0 }.max() ?? 0
        return StatRow {
            StatCard(label: "Learning events",
                     value: "\(totalN)",
                     delta: "\(p?.postmortems?.count ?? 0) analyses on disk",
                     deltaTone: .neutral,
                     sublabel: "source verdicts credited")
            StatCard(label: "Avg source accuracy",
                     value: avgAcc.map { Fmt.pct($0 * 100, dp: 0, signed: false) } ?? "—",
                     delta: "\(board.count) tracked sources",
                     deltaTone: (avgAcc ?? 0) >= 0.5 ? .up : .down,
                     sublabel: "drives the weight rebalance")
            StatCard(label: "Rewrite queue",
                     value: "\(pending)",
                     delta: pending == 0 ? "nothing waiting" : "waiting for skill_5",
                     deltaTone: pending == 0 ? .up : .caution,
                     sublabel: "one entry consumed per cycle")
            StatCard(label: "Learned rules",
                     value: "\(rules)",
                     delta: "latest skill v\(String(format: "%03d", latestSkill))",
                     deltaTone: .neutral,
                     sublabel: "needs 3+ similar outcomes to promote")
        }
    }

    private var leaderboardCard: some View {
        Card("Source accuracy leaderboard", subtitle: "wins and losses credited by postmortems") {
            let rows = p?.leaderboard ?? []
            if rows.isEmpty {
                EmptyNote(text: "No source data yet.", icon: "scalemass")
            } else {
                VStack(spacing: DS.s3) {
                    ForEach(rows) { row in
                        VStack(alignment: .leading, spacing: 5) {
                            HStack {
                                Text(row.source).font(DSFont.semibold(14))
                                    .foregroundStyle(DS.text)
                                Spacer()
                                Text("\(row.wins ?? 0)W / \(row.losses ?? 0)L")
                                    .font(DSFont.num(12)).foregroundStyle(DS.textMuted)
                                Text(row.accuracy.map {
                                    Fmt.pct($0 * 100, dp: 0, signed: false) } ?? "—")
                                    .font(DSFont.num(13, .bold))
                                    .foregroundStyle((row.accuracy ?? 0) >= 0.5 ? DS.up : DS.down)
                                Tag("w \(Fmt.num(row.weight, dp: 2))", tone: .neutral, size: 10)
                            }
                            MeterBar(value: row.accuracy ?? 0,
                                     tint: (row.accuracy ?? 0) >= 0.5 ? DS.accent2 : DS.down)
                        }
                    }
                }
            }
        }
    }

    private var skillVersionsCard: some View {
        Card("Skill file versions", subtitle: "every rewrite is snapshotted and reversible") {
            let rows = p?.skillVersions ?? []
            if rows.isEmpty {
                EmptyNote(text: "No skill rewrites yet.", icon: "doc.badge.gearshape")
            } else {
                VStack(spacing: DS.s2) {
                    ForEach(rows) { s in
                        KeyValueRow(key: s.skill,
                                    value: "v\(String(format: "%03d", s.latest ?? 0)) · \(s.versions ?? 0) snapshots")
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var accuracyChartCard: some View {
        let rows = (p?.leaderboard ?? []).filter { $0.accuracy != nil }
        if rows.count > 1 {
            Card("Accuracy vs weight", subtitle: "the bot pays attention in proportion to being right") {
                Chart(rows) { row in
                    BarMark(x: .value("Source", row.source),
                            y: .value("Accuracy", (row.accuracy ?? 0) * 100))
                        .foregroundStyle((row.accuracy ?? 0) >= 0.5 ? DS.accent2 : DS.down)
                        .cornerRadius(5)
                    PointMark(x: .value("Source", row.source),
                              y: .value("Weight", (row.weight ?? 0) * 100))
                        .foregroundStyle(DS.accent)
                        .symbolSize(70)
                }
                .chartYAxis {
                    AxisMarks(position: .leading) { _ in
                        AxisGridLine().foregroundStyle(DS.divider)
                        AxisValueLabel().font(DSFont.num(10))
                    }
                }
                .chartXAxis {
                    AxisMarks { _ in AxisValueLabel().font(DSFont.num(10)) }
                }
                .frame(height: 200)
                HStack(spacing: DS.s3) {
                    Tag("bar = accuracy %", tone: .sage, size: 10)
                    Tag("dot = weight %", tone: .accent, size: 10)
                }
            }
        }
    }

    @ViewBuilder
    private var learnedRulesCard: some View {
        let rules = p?.learnedRules?.arrayValue ?? []
        if !rules.isEmpty {
            Card("Learned rules", subtitle: "promoted from postmortems after repeat evidence") {
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

    @ViewBuilder
    private var observationsCard: some View {
        let obs = p?.observations?.arrayValue ?? []
        if !obs.isEmpty {
            Card("Observations pending confirmation",
                 subtitle: "need 3+ similar outcomes before they become rules") {
                VStack(alignment: .leading, spacing: DS.s3) {
                    ForEach(Array(obs.enumerated()), id: \.offset) { _, o in
                        HStack(alignment: .top, spacing: DS.s2) {
                            Image(systemName: "eye")
                                .font(.system(size: 12)).foregroundStyle(DS.caution)
                                .frame(width: 18)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(o["id"]?.stringValue ?? "OBS?")
                                    .font(DSFont.semibold(12)).foregroundStyle(DS.caution)
                                Text(o["observation"]?.stringValue
                                     ?? o["note"]?.stringValue ?? o.compact)
                                    .font(DSFont.body(14)).foregroundStyle(DS.text)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var queueCard: some View {
        let queue = p?.rewriteQueue ?? []
        if !queue.isEmpty {
            Card("Strategy-rewrite queue", subtitle: "each close queues one skill_5 review") {
                VStack(spacing: 0) {
                    ForEach(Array(queue.enumerated()), id: \.offset) { i, e in
                        TRow(showsDivider: i < queue.count - 1) {
                            Image(systemName: e.done == true ? "checkmark.circle.fill" : "clock.fill")
                                .font(.system(size: 12))
                                .foregroundStyle(e.done == true ? DS.up : DS.caution)
                                .frame(width: 18)
                            TCell {
                                Text(e.tradeId ?? "—").font(DSFont.mono(12))
                                    .foregroundStyle(DS.text)
                            }
                            TCell {
                                Text(e.symbol ?? "").font(DSFont.semibold(13))
                                    .foregroundStyle(DS.text)
                            }
                            TCell {
                                if let out = e.outcome {
                                    Tag(out, tone: out == "WIN" ? .up : .down, size: 10)
                                }
                            }
                            TCell(align: .trailing) {
                                Text(e.pnl ?? "").font(DSFont.num(12))
                                    .foregroundStyle(DS.textMuted)
                            }
                            TCell(align: .trailing, weight: 2) {
                                Text(e.done == true ? "processed by skill_5"
                                     : "queued for skill_5 rewrite")
                                    .font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                                    .lineLimit(1)
                            }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var verdictsCard: some View {
        let verdicts = p?.verdicts ?? []
        if !verdicts.isEmpty {
            Card("Per-trade source verdicts", subtitle: "which source was right about which trade") {
                VStack(alignment: .leading, spacing: DS.s3) {
                    ForEach(Array(verdicts.enumerated()), id: \.offset) { _, v in
                        VStack(alignment: .leading, spacing: 5) {
                            Text(v.file ?? "—").font(DSFont.mono(11))
                                .foregroundStyle(DS.textFaint)
                            SourceChips(sources: v.sources ?? [:])
                        }
                    }
                }
            }
        }
    }

    private var analysesCard: some View {
        Card("All analyses", subtitle: "postmortems and victories, newest first") {
            let pms = p?.postmortems ?? []
            if pms.isEmpty {
                EmptyNote(text: "No postmortems or victories yet.", icon: "books.vertical")
            } else {
                VStack(spacing: 0) {
                    ForEach(pms) { pm in
                        TRow(showsDivider: pm.id != pms.last?.id, onTap: { document = pm }) {
                            Image(systemName: pm.kind == "victory" ? "trophy.fill" : "stethoscope")
                                .font(.system(size: 12))
                                .foregroundStyle(pm.kind == "victory" ? DS.up : DS.down)
                                .frame(width: 18)
                            TCell(weight: 4) {
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(pm.title ?? pm.file).font(DSFont.body(13))
                                        .foregroundStyle(DS.text).lineLimit(1)
                                    Text("\(pm.file) · \((pm.tradeIds ?? []).joined(separator: ", "))")
                                        .font(DSFont.mono(10)).foregroundStyle(DS.textFaint)
                                        .lineLimit(1)
                                }
                            }
                            TCell(align: .trailing) {
                                Image(systemName: "chevron.right")
                                    .font(.system(size: 10, weight: .bold))
                                    .foregroundStyle(DS.textFaint)
                            }
                        }
                    }
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
    @State private var kind: LibraryKind = .all

    enum LibraryKind: String, CaseIterable, Hashable {
        case all = "All", picks = "Research", reviews = "Midweek",
             postmortems = "Postmortems", victories = "Victories"
    }

    private var p: LibraryPayload? { model.libraryPayload }

    private var researchFiles: [ResearchFileMeta] {
        (p?.research ?? []).filter { f in
            (kind == .all || kind == .picks || kind == .reviews)
            && (kind != .picks || f.kind == "picks")
            && (kind != .reviews || f.kind != "picks")
            && (search.isEmpty || f.file.lowercased().contains(search.lowercased()))
        }
    }
    private var analyses: [PostmortemMeta] {
        (p?.postmortems ?? []).filter { pm in
            (kind == .all || kind == .postmortems || kind == .victories)
            && (kind != .postmortems || pm.kind != "victory")
            && (kind != .victories || pm.kind == "victory")
            && (search.isEmpty
                || pm.file.lowercased().contains(search.lowercased())
                || (pm.title ?? "").lowercased().contains(search.lowercased()))
        }
    }

    var body: some View {
        Group {
            if p == nil {
                LoadingScreen(title: "Loading library…", detail: "research files and analyses")
                    .background(DS.bg)
            } else {
                Screen(title: "Library", subtitle: AppTab.library.blurb,
                       refresh: { await model.load(.library) }) {
                    if let err = model.tabErrors[.library] { ErrorBox(text: err) }
                    Card {
                        WidthReader(compactBelow: 640) { compact in
                            let pills = FilterPills(
                                options: LibraryKind.allCases.map { ($0, $0.rawValue) },
                                selection: $kind)
                            let field = OrganicField(prompt: "filename / title",
                                                     text: $search, width: 260)
                            if compact {
                                VStack(alignment: .leading, spacing: DS.s3) { pills; field }
                            } else {
                                HStack(spacing: DS.s3) { pills; Spacer(minLength: DS.s2); field }
                            }
                        }
                    }
                    researchCard
                    analysesCard
                }
            }
        }
        .task { if p == nil { await model.load(.library) } }
        .sheet(item: $research) { f in
            DocumentSheet(title: f.file, loader: { await model.researchText(f.file) })
        }
        .sheet(item: $analysis) { pm in
            DocumentSheet(title: pm.file, loader: { await model.postmortemText(pm.file) })
        }
    }

    @ViewBuilder
    private var researchCard: some View {
        if !researchFiles.isEmpty || kind == .all {
            Card("Research (\(researchFiles.count))",
                 subtitle: "weekend picks and midweek reviews") {
                if researchFiles.isEmpty {
                    EmptyNote(text: "No research files match.", icon: "lightbulb")
                } else {
                    VStack(spacing: 0) {
                        ForEach(researchFiles) { f in
                            TRow(showsDivider: f.id != researchFiles.last?.id,
                                 onTap: { research = f }) {
                                Image(systemName: f.kind == "picks"
                                      ? "lightbulb.fill" : "stethoscope.circle")
                                    .font(.system(size: 12))
                                    .foregroundStyle(f.kind == "picks" ? DS.caution : DS.accent)
                                    .frame(width: 18)
                                TCell(weight: 4) {
                                    Text(f.file).font(DSFont.mono(12))
                                        .foregroundStyle(DS.text).lineLimit(1)
                                }
                                TCell(align: .trailing, weight: 2) {
                                    Tag(f.kind == "picks" ? "research picks" : "midweek review",
                                        tone: f.kind == "picks" ? .caution : .accent, size: 10)
                                }
                                TCell(align: .trailing) {
                                    Text(f.date ?? "").font(DSFont.num(11))
                                        .foregroundStyle(DS.textFaint)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var analysesCard: some View {
        if !analyses.isEmpty || kind == .all {
            Card("Analyses (\(analyses.count))", subtitle: "postmortems and victories") {
                if analyses.isEmpty {
                    EmptyNote(text: "No analyses match.", icon: "stethoscope")
                } else {
                    VStack(spacing: 0) {
                        ForEach(analyses) { pm in
                            TRow(showsDivider: pm.id != analyses.last?.id,
                                 onTap: { analysis = pm }) {
                                Image(systemName: pm.kind == "victory"
                                      ? "trophy.fill" : "stethoscope")
                                    .font(.system(size: 12))
                                    .foregroundStyle(pm.kind == "victory" ? DS.up : DS.down)
                                    .frame(width: 18)
                                TCell(weight: 4) {
                                    Text(pm.title ?? pm.file).font(DSFont.body(13))
                                        .foregroundStyle(DS.text).lineLimit(1)
                                }
                                TCell(align: .trailing, weight: 2) {
                                    Text(pm.file).font(DSFont.mono(11))
                                        .foregroundStyle(DS.textFaint).lineLimit(1)
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

// MARK: - RX-3 rotation

struct RX3View: View {
    @Environment(AppModel.self) private var model
    @State private var showRawConfig = false

    private var p: RX3Payload? { model.rx3Payload }

    var body: some View {
        Group {
            if p == nil {
                LoadingScreen(title: "Loading RX-3 paper track…",
                              detail: "rotation × vol throttle × RISKX")
                    .background(DS.bg)
            } else {
                Screen(title: "RX-3 Rotation", subtitle: AppTab.rx3.blurb,
                       refresh: { await model.load(.rx3) }) {
                    if let err = model.tabErrors[.rx3] { ErrorBox(text: err) }
                    statTiles
                    promotionCard
                    curveCard
                    AdaptivePair(compactBelow: 860, ratio: 1.4) {
                        bookCard
                    } second: {
                        gatesCard
                    }
                    historyCard
                    rawConfigCard
                }
            }
        }
        .task { if p == nil { await model.load(.rx3) } }
    }

    private var statTiles: some View {
        let m = p?.meta
        return StatRow {
            StatCard(label: "Paper equity", value: Fmt.usd(m?.equity),
                     delta: Fmt.pct(m?.returnPct) + " since start",
                     deltaTone: .forValue(m?.returnPct),
                     sublabel: "started \(Fmt.when(m?.startDate))")
            StatCard(label: "Cash parked", value: Fmt.usd(m?.cash),
                     delta: p?.latest?.cashW.map {
                        Fmt.pct($0 * 100, dp: 0, signed: false) + " weight" } ?? nil,
                     deltaTone: .neutral,
                     sublabel: "vol throttle + RISKX")
            StatCard(label: "RISKX gate",
                     value: Fmt.pct((p?.latest?.riskx ?? 0) * 100, dp: 0, signed: false),
                     delta: "vol scale \(Fmt.pct((p?.latest?.volScale ?? 0) * 100, dp: 0, signed: false))",
                     deltaTone: (p?.latest?.riskx ?? 0) >= 0.6 ? .up : .caution,
                     sublabel: "5-signal cross-asset risk appetite")
            StatCard(label: "Paper days",
                     value: "\(p?.paperDays ?? 0)",
                     delta: "gate at \(p?.promotionGateDays ?? 10)",
                     deltaTone: (p?.paperDays ?? 0) >= (p?.promotionGateDays ?? 10) ? .up : .caution,
                     sublabel: "last run \(Fmt.when(m?.lastRun))")
        }
    }

    private var promotionCard: some View {
        let days = p?.paperDays ?? 0
        let gate = p?.promotionGateDays ?? 10
        return Card("Promotion ladder — paper phase",
                    subtitle: "≥\(gate) matching paper days → 2 weeks half size → full") {
            VStack(alignment: .leading, spacing: DS.s2) {
                HStack {
                    Text("Auto-rotate")
                        .font(DSFont.body(13)).foregroundStyle(DS.textMuted)
                    Tag(p?.config?["paper_enabled"]?.boolValue == true ? "ON (paper)" : "off",
                        tone: p?.config?["paper_enabled"]?.boolValue == true ? .sage : .neutral)
                    Spacer()
                    Text("\(days)/\(gate) days")
                        .font(DSFont.num(14, .bold))
                        .foregroundStyle(days >= gate ? DS.up : DS.caution)
                }
                MeterBar(value: Double(min(days, gate)) / Double(max(1, gate)),
                         tint: days >= gate ? DS.up : DS.caution, height: 10)
                Text("Flipping this sleeve to live capital is an operator action — the app never does it for you.")
                    .font(DSFont.body(12)).foregroundStyle(DS.textMuted)
            }
        }
    }

    private struct CurvePoint: Identifiable {
        let id = UUID()
        let date: Date
        let series: String
        let value: Double
    }

    private var curveData: [CurvePoint] {
        var out: [CurvePoint] = []
        let curve = p?.curve ?? []
        if let base = curve.first?.value, base > 0 {
            for pt in curve {
                if let v = pt.value, let d = Fmt.date(pt.ts) {
                    out.append(CurvePoint(date: d, series: "RX-3", value: v / base * 100))
                }
            }
        }
        let spy = p?.spySinceStart ?? []
        if let base = spy.first?.close, base > 0 {
            for pt in spy {
                if let c = pt.close, let d = Fmt.date(pt.ts) {
                    out.append(CurvePoint(date: d, series: "SPY", value: c / base * 100))
                }
            }
        }
        return out.sorted { $0.date < $1.date }
    }

    private var curveCard: some View {
        Card("Paper equity vs SPY", subtitle: "indexed to 100 at the paper start") {
            let data = curveData
            if data.count < 3 {
                EmptyNote(text: "Curve appears after a few paper days.",
                          icon: "chart.xyaxis.line")
                    .frame(minHeight: 80)
            } else {
                Chart(data) { pt in
                    LineMark(x: .value("Day", pt.date), y: .value("Indexed", pt.value))
                        .foregroundStyle(by: .value("Series", pt.series))
                        .interpolationMethod(.monotone)
                        .lineStyle(StrokeStyle(lineWidth: pt.series == "RX-3" ? 2.4 : 1.4,
                                               lineCap: .round))
                }
                .chartForegroundStyleScale(["RX-3": DS.accent, "SPY": DS.neutral(5)])
                .chartYScale(domain: .automatic(includesZero: false))
                .chartXAxis {
                    AxisMarks(values: .automatic(desiredCount: 5)) { value in
                        AxisGridLine().foregroundStyle(DS.divider.opacity(0.5))
                        AxisValueLabel {
                            if let d = value.as(Date.self) {
                                Text(d, format: .dateTime.month(.abbreviated).day())
                                    .font(DSFont.num(10))
                                    .foregroundStyle(DS.textMuted)
                            }
                        }
                    }
                }
                .chartYAxis {
                    AxisMarks(position: .leading) { _ in
                        AxisGridLine().foregroundStyle(DS.divider)
                        AxisValueLabel().font(DSFont.num(10))
                    }
                }
                .frame(height: 220)
            }
        }
    }

    private var bookCard: some View {
        Card("Current paper book", subtitle: "top-2 momentum leaders + defensive sleeve") {
            VStack(alignment: .leading, spacing: DS.s3) {
                HStack(spacing: 7) {
                    Text("Leaders").font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                    ForEach(p?.latest?.leaders ?? p?.leaders ?? [], id: \.self) { s in
                        Tag(s, tone: .up, size: 11)
                    }
                    if let def = p?.latest?.defensive, !def.isEmpty {
                        Text("Defensive").font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                        ForEach(def, id: \.self) { s in Tag(s, tone: .caution, size: 11) }
                    }
                    Spacer()
                }
                let positions = p?.positions ?? []
                if positions.isEmpty {
                    EmptyNote(text: "No paper positions — fully in cash.", icon: "banknote")
                } else {
                    VStack(spacing: 0) {
                        THeader(columns: ["Ticker", "Shares", "Price", "Change", "Value"],
                                alignments: [.leading, .trailing, .trailing, .trailing, .trailing])
                        Rectangle().fill(DS.divider).frame(height: 1)
                        ForEach(positions) { pos in
                            TRow(showsDivider: pos.id != positions.last?.id) {
                                TCell {
                                    Text(pos.symbol).font(DSFont.bold(14))
                                        .foregroundStyle(DS.text)
                                }
                                TCell(align: .trailing) {
                                    Text(Fmt.num(pos.shares, dp: 4)).font(DSFont.num(12))
                                        .foregroundStyle(DS.textMuted)
                                }
                                TCell(align: .trailing) {
                                    Text(Fmt.usd(pos.price ?? pos.lastPx))
                                        .font(DSFont.num(13)).foregroundStyle(DS.text)
                                }
                                TCell(align: .trailing) {
                                    Text(Fmt.pct(pos.changePct))
                                        .font(DSFont.num(12, .semibold))
                                        .foregroundStyle(pos.changePct.pnlColor)
                                }
                                TCell(align: .trailing) {
                                    Text(Fmt.usd(pos.value)).font(DSFont.num(13, .semibold))
                                        .foregroundStyle(DS.text)
                                }
                            }
                        }
                    }
                }
                if let latest = p?.latest {
                    Rectangle().fill(DS.divider).frame(height: 1)
                    HStack(spacing: DS.s4) {
                        gauge("RISKX", latest.riskx, "risk-appetite gate")
                        gauge("Vol scale", latest.volScale, "vol throttle")
                        gauge("Cash", latest.cashW, "weight parked")
                    }
                }
            }
        }
    }

    private func gauge(_ label: String, _ value: Double?, _ help: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label.uppercased()).font(DSFont.label(10)).kerning(0.5)
                .foregroundStyle(DS.textFaint)
            MeterBar(value: min(max(value ?? 0, 0), 1), tint: DS.accent)
            Text(Fmt.pct((value ?? 0) * 100, dp: 0, signed: false))
                .font(DSFont.num(12, .bold)).foregroundStyle(DS.text)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .help(help)
    }

    private var gatesCard: some View {
        Card("Rotation config", subtitle: "what the deterministic engine is running with") {
            VStack(spacing: DS.s2) {
                KeyValueRow(key: "Mode", value: p?.config?["mode"]?.stringValue ?? "paper")
                KeyValueRow(key: "Paper enabled",
                            value: p?.config?["paper_enabled"]?.boolValue == true ? "yes" : "no")
                KeyValueRow(key: "Top N", value: p?.config?["top_n"]?.compact ?? "—")
                KeyValueRow(key: "Target vol", value: p?.config?["target_vol"]?.compact ?? "—")
                KeyValueRow(key: "Universe",
                            value: "\((p?.config?["universe"]?.arrayValue ?? []).count) names")
                if let note = p?.meta?.note {
                    Rectangle().fill(DS.divider).frame(height: 1).padding(.vertical, 2)
                    Text(note).font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }

    @ViewBuilder
    private var historyCard: some View {
        let hist = p?.history ?? []
        if !hist.isEmpty {
            Card("Daily decisions", subtitle: "one row per paper day, straight from the engine") {
                VStack(spacing: 0) {
                    THeader(columns: ["Date", "Equity", "Leaders", "Defensive",
                                      "RISKX", "Vol", "Cash"],
                            weights: [1, 1, 1.4, 1.2, 1, 1, 1],
                            alignments: [.leading, .trailing, .leading, .leading,
                                         .trailing, .trailing, .trailing])
                    Rectangle().fill(DS.divider).frame(height: 1)
                    ForEach(hist) { day in
                        TRow(showsDivider: day.id != hist.last?.id) {
                            TCell {
                                Text(day.date ?? "—").font(DSFont.num(12))
                                    .foregroundStyle(DS.text)
                            }
                            TCell(align: .trailing) {
                                Text(Fmt.usd(day.equity)).font(DSFont.num(12, .semibold))
                                    .foregroundStyle(DS.text)
                            }
                            TCell(weight: 1.4) {
                                Text((day.leaders ?? []).joined(separator: " + "))
                                    .font(DSFont.num(12)).foregroundStyle(DS.up).lineLimit(1)
                            }
                            TCell(weight: 1.2) {
                                Text((day.defensive ?? []).joined(separator: " + "))
                                    .font(DSFont.num(12)).foregroundStyle(DS.caution).lineLimit(1)
                            }
                            TCell(align: .trailing) {
                                Text(Fmt.num(day.riskx, dp: 2)).font(DSFont.num(12))
                                    .foregroundStyle(DS.textMuted)
                            }
                            TCell(align: .trailing) {
                                Text(Fmt.num(day.volScale, dp: 2)).font(DSFont.num(12))
                                    .foregroundStyle(DS.textMuted)
                            }
                            TCell(align: .trailing) {
                                Text(Fmt.pct((day.cashW ?? 0) * 100, dp: 0, signed: false))
                                    .font(DSFont.num(12)).foregroundStyle(DS.textMuted)
                            }
                        }
                    }
                }
            }
        }
    }

    private var rawConfigCard: some View {
        Card {
            VStack(alignment: .leading, spacing: DS.s3) {
                Button { showRawConfig.toggle() } label: {
                    HStack(spacing: 8) {
                        Image(systemName: showRawConfig ? "chevron.down" : "chevron.right")
                            .font(.system(size: 10, weight: .bold))
                        Text("Raw rotation config").font(DSFont.semibold(14))
                        Spacer()
                    }
                    .foregroundStyle(DS.text)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                if showRawConfig {
                    ScrollView {
                        Text(p?.config?.pretty ?? "—")
                            .font(DSFont.mono(11))
                            .foregroundStyle(DS.consoleText)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .textSelection(.enabled)
                            .padding(DS.s3)
                    }
                    .frame(maxHeight: 360)
                    .background(DS.consoleBG,
                                in: RoundedRectangle(cornerRadius: DS.rMd, style: .continuous))
                }
            }
        }
    }
}
