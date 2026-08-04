//
//  AnalyzerSignalsViews.swift — the market-data trio:
//  Analyzer (any-symbol chart with the bot's EMA ribbon overlaid + stats),
//  Signals (ribbon lab across every symbol of interest), and
//  Screener (the bot's own multi-timeframe momentum screen).
//

import SwiftUI
import Charts

// MARK: - Shared bits

/// 1w/1m/6m/1y return pills, colored by sign. Values arrive as percent.
struct ReturnsPills: View {
    let returns: [String: JSONValue]?
    private static let order = ["1w", "1m", "6m", "1y"]

    var body: some View {
        HStack(spacing: 5) {
            ForEach(Self.order, id: \.self) { tf in
                let v = returns?[tf]?.numberValue
                VStack(spacing: 0) {
                    Text(tf)
                        .font(DSFont.label(9))
                        .foregroundStyle(DS.textFaint)
                    Text(v == nil ? "—" : Fmt.pct(v, dp: 0))
                        .font(DSFont.num(12, .semibold))
                        .foregroundStyle(v == nil ? DS.textFaint : v.pnlColor)
                }
                .frame(minWidth: 38)
                .padding(.vertical, 4)
                .background(v.pnlColor.opacity(v == nil ? 0.05 : 0.12),
                            in: RoundedRectangle(cornerRadius: DS.rSm, style: .continuous))
            }
        }
    }
}

// MARK: - Analyzer

struct AnalyzerView: View {
    @Environment(AppModel.self) private var model
    @State private var symbolField = ""

    private var p: SymbolPayload? { model.symbolPayload }

    var body: some View {
        Screen(title: "Analyzer", subtitle: AppTab.analyzer.blurb,
               refresh: { await model.load(.analyzer) }) {
            if let err = model.tabErrors[.analyzer] { ErrorBox(text: err) }
            controls
            AdaptivePair(compactBelow: 900, ratio: 1.7) {
                chartCard
            } second: {
                signalReadCard
            }
            AdaptivePair(compactBelow: 760) {
                statsCard
            } second: {
                ribbonCard
            }
            pastTradesCard
            newsCard
        }
        .task {
            symbolField = model.analyzerSymbol
            if p == nil { await model.load(.analyzer) }
        }
    }

    // MARK: controls

    private var controls: some View {
        @Bindable var model = model
        return VStack(alignment: .leading, spacing: DS.s3) {
            HStack(spacing: DS.s2) {
                OrganicField(prompt: "Symbol", text: $symbolField, mono: true,
                             width: 140, onSubmit: go)
                Button { go() } label: { Label("Load", systemImage: "magnifyingglass") }
                    .buttonStyle(.organicPrimary)
                Spacer(minLength: DS.s2)
                FilterPills(options: [("1d", "Daily"), ("1h", "1h (bot)"), ("30m", "30m")],
                            selection: $model.analyzerInterval)
                    .onChange(of: model.analyzerInterval) { _, _ in
                        Task { await model.load(.analyzer) }
                    }
                    .frame(maxWidth: 280)
            }
            quickChips
        }
    }

    private var quickChips: some View {
        let held = (model.overview?.positions ?? []).map(\.symbol)
        let extras = ["SPY", "QQQ", "IWM", "NVDA", "MU", "LABU"]
        let all = Array(NSOrderedSet(array: held + extras)) as? [String] ?? extras
        return ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: DS.s2) {
                ForEach(all, id: \.self) { s in
                    Button {
                        symbolField = s
                        go()
                    } label: {
                        Text(s)
                            .font(DSFont.semibold(13))
                            .padding(.horizontal, 16).padding(.vertical, 7)
                            .background(s == model.analyzerSymbol ? DS.accent : DS.neutral(2),
                                        in: Capsule())
                            .foregroundStyle(s == model.analyzerSymbol ? .white : DS.text)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.vertical, 1)
        }
    }

    private func go() {
        let s = symbolField.trimmingCharacters(in: .whitespaces).uppercased()
        guard !s.isEmpty else { return }
        symbolField = s
        model.analyzerSymbol = s
        Task { await model.load(.analyzer) }
    }

    // MARK: chart

    private struct SeriesPoint: Identifiable {
        let id = UUID()
        let idx: Int
        let series: String
        let value: Double
    }

    private var chartData: [SeriesPoint] {
        guard let p, let bars = p.bars else { return [] }
        var out: [SeriesPoint] = []
        for (i, b) in bars.enumerated() {
            if let c = b.close { out.append(SeriesPoint(idx: i, series: "Close", value: c)) }
        }
        // ribbon overlay: EMA(8/13/21/55) — same math the bot trades on
        let nameMap = [("blue", "EMA 8"), ("green", "EMA 13"),
                       ("yellow", "EMA 21"), ("red", "EMA 55")]
        for (key, label) in nameMap {
            guard let series = p.emaSeries?[key] else { continue }
            for (i, v) in series.enumerated() {
                out.append(SeriesPoint(idx: i, series: label, value: v))
            }
        }
        return out
    }

    private var chartCard: some View {
        Card("Price & ribbon",
             subtitle: "plain EMA 8 / 13 / 21 / 55 — the exact lines the bot trades") {
            VStack(alignment: .leading, spacing: DS.s3) {
                quoteHeader
                let data = chartData
                if data.isEmpty {
                    EmptyNote(text: "No chart data — intraday intervals need 60+ bars.",
                              icon: "chart.line.downtrend.xyaxis")
                        .frame(height: 200)
                } else {
                    Chart {
                        ForEach(data) { pt in
                            LineMark(x: .value("Bar", pt.idx), y: .value("Price", pt.value))
                                .foregroundStyle(by: .value("Series", pt.series))
                                .lineStyle(StrokeStyle(lineWidth: pt.series == "Close" ? 2.4 : 1.2,
                                                       lineCap: .round, lineJoin: .round))
                        }
                        if let stop = p?.stops?.stopPrice {
                            RuleMark(y: .value("Stop", stop))
                                .foregroundStyle(DS.down.opacity(0.75))
                                .lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 3]))
                                .annotation(position: .trailing, alignment: .leading) {
                                    Text("stop").font(DSFont.label(9)).foregroundStyle(DS.down)
                                }
                        }
                        if let trail = p?.stops?.trailPrice {
                            RuleMark(y: .value("Trail", trail))
                                .foregroundStyle(DS.caution.opacity(0.75))
                                .lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 3]))
                                .annotation(position: .trailing, alignment: .leading) {
                                    Text("trail").font(DSFont.label(9)).foregroundStyle(DS.caution)
                                }
                        }
                        if let entry = p?.position?["entry_price"]?.numberValue {
                            RuleMark(y: .value("Entry", entry))
                                .foregroundStyle(DS.textMuted.opacity(0.8))
                                .lineStyle(StrokeStyle(lineWidth: 1, dash: [2, 3]))
                                .annotation(position: .trailing, alignment: .leading) {
                                    Text("entry").font(DSFont.label(9))
                                        .foregroundStyle(DS.textMuted)
                                }
                        }
                    }
                    .chartForegroundStyleScale([
                        "Close": DS.text,
                        "EMA 8": DS.accent(4),
                        "EMA 13": DS.accent2,
                        "EMA 21": DS.caution,
                        "EMA 55": DS.down,
                    ])
                    .chartXAxis(.hidden)
                    .chartYScale(domain: .automatic(includesZero: false))
                    .chartYAxis {
                        AxisMarks(position: .leading) { v in
                            AxisGridLine().foregroundStyle(DS.divider)
                            AxisValueLabel {
                                if let d = v.as(Double.self) {
                                    Text(Fmt.num(d, dp: 0)).font(DSFont.num(10))
                                        .foregroundStyle(DS.textMuted)
                                }
                            }
                        }
                    }
                    .frame(height: 280)
                    HStack {
                        Text(p?.bars?.first?.ts ?? "")
                        Spacer()
                        Text("\(p?.bars?.count ?? 0) bars · \(p?.interval ?? "")")
                        Spacer()
                        Text(p?.bars?.last?.ts ?? "")
                    }
                    .font(DSFont.mono(10))
                    .foregroundStyle(DS.textFaint)
                }
            }
        }
    }

    @ViewBuilder
    private var quoteHeader: some View {
        if let p {
            WidthReader(compactBelow: 520) { compact in
                let left = HStack(spacing: DS.s3) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(p.symbol ?? "—")
                            .font(DSFont.heading(22)).foregroundStyle(DS.text)
                        HStack(spacing: 8) {
                            Text(Fmt.usd(p.quote?.price))
                                .font(DSFont.num(20, .bold)).foregroundStyle(DS.text)
                            Text(Fmt.pct(p.quote?.changePct))
                                .font(DSFont.num(15, .semibold))
                                .foregroundStyle(p.quote?.changePct.pnlColor ?? DS.flat)
                        }
                    }
                    RibbonBadge(state: p.ribbon?.state)
                    if p.held == true { StatusDot(color: DS.up, label: "HELD") }
                    if p.dnt == true { StatusDot(color: DS.down, label: "DNT") }
                }
                let right = HStack(spacing: DS.s2) {
                    Button {
                        model.orderPrefill = .init(symbol: p.symbol ?? "", side: "buy")
                        model.showOrderSheet = true
                    } label: { Label("Buy", systemImage: "plus.circle") }
                        .buttonStyle(SoftButtonStyle(tint: DS.up))
                    Button {
                        model.orderPrefill = .init(symbol: p.symbol ?? "", side: "sell")
                        model.showOrderSheet = true
                    } label: { Label("Sell", systemImage: "minus.circle") }
                        .buttonStyle(SoftButtonStyle(tint: DS.down))
                }
                if compact {
                    VStack(alignment: .leading, spacing: DS.s2) { left; right }
                } else {
                    HStack { left; Spacer(minLength: DS.s2); right }
                }
            }
        }
    }

    // MARK: signal read

    private var signalReadCard: some View {
        Card("Signal Read", subtitle: "what the bot's ribbon says right now") {
            let state = p?.ribbon?.state
            let score = p?.stats?.momentumScore
            VStack(alignment: .leading, spacing: DS.s3) {
                if state == nil {
                    EmptyNote(text: "No active signal for this symbol yet.", icon: "wifi.slash")
                } else {
                    RibbonBadge(state: state)
                    VStack(alignment: .leading, spacing: 5) {
                        Text("Momentum score")
                            .font(DSFont.body(13)).foregroundStyle(DS.textMuted)
                        MeterBar(value: (score ?? 0) / 100,
                                 tint: (score ?? 0) >= 60 ? DS.accent : DS.neutral(5))
                        Text("\(Fmt.num(score, dp: 0)) / 100")
                            .font(DSFont.num(12, .semibold))
                            .foregroundStyle(DS.textMuted)
                            .frame(maxWidth: .infinity, alignment: .trailing)
                    }
                    Text(reasoning)
                        .font(DSFont.body(14))
                        .foregroundStyle(DS.text)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("Source: plain EMA 8/13/21/55 · \(p?.ribbon?.bars ?? 0) bars · \(p?.ribbon?.source ?? "—")")
                        .font(DSFont.body(11))
                        .foregroundStyle(DS.textFaint)
                    if let note = p?.ribbon?.note, !note.isEmpty {
                        WarningBox(text: note)
                    }
                }
            }
        }
    }

    private var reasoning: String {
        guard let r = p?.ribbon else { return "" }
        let t = r.transition ?? "NO_ACTION"
        switch r.state {
        case "BUY":
            return "Red EMA(55) sits below the fast pack — the bot's long gate is open"
                + (t == "ENTER_LONG" ? " and it just crossed this bar." : ".")
        case "SELL":
            return "Red EMA(55) is above the fast pack. Under let-winners-run this is advisory — the trailing stop owns the exit."
        default:
            return "Ribbon is tangled (neutral). No entry edge; existing positions ride their stops."
        }
    }

    // MARK: stats / ribbon

    private var statsCard: some View {
        Card("Momentum profile", subtitle: "the numbers the screener ranks on") {
            VStack(spacing: DS.s2) {
                KeyValueRow(key: "52w high", value: Fmt.usd(p?.stats?.high52w))
                KeyValueRow(key: "52w low", value: Fmt.usd(p?.stats?.low52w))
                KeyValueRow(key: "Off high", value: Fmt.pct(p?.stats?.offHighPct),
                            valueColor: p?.stats?.offHighPct.pnlColor)
                KeyValueRow(key: "Momentum score",
                            value: p?.stats?.momentumScore.map { Fmt.num($0, dp: 0) + "/100" } ?? "—")
                Rectangle().fill(DS.divider).frame(height: 1).padding(.vertical, 2)
                HStack {
                    Text("Returns").font(DSFont.body(13)).foregroundStyle(DS.textMuted)
                    Spacer()
                    ReturnsPills(returns: p?.stats?.returns)
                }
            }
        }
    }

    private var ribbonCard: some View {
        Card("Live ribbon", subtitle: "the bot's signal layer, line by line") {
            VStack(spacing: DS.s2) {
                HStack {
                    Text("State").font(DSFont.body(13)).foregroundStyle(DS.textMuted)
                    Spacer()
                    RibbonBadge(state: p?.ribbon?.state)
                    Tag(p?.ribbon?.transition ?? "—", tone: .neutral, size: 10)
                }
                KeyValueRow(key: "EMA 8 (blue)", value: Fmt.usd(p?.ribbon?.lines?.blue))
                KeyValueRow(key: "EMA 13 (green)", value: Fmt.usd(p?.ribbon?.lines?.green))
                KeyValueRow(key: "EMA 21 (yellow)", value: Fmt.usd(p?.ribbon?.lines?.yellow))
                KeyValueRow(key: "EMA 55 (red)", value: Fmt.usd(p?.ribbon?.lines?.red))
                KeyValueRow(key: "Bars / source",
                            value: "\(p?.ribbon?.bars ?? 0) · \(p?.ribbon?.source ?? "—")")
                if p?.ribbon?.warmupOk == false {
                    WarningBox(text: "Not fully seeded — fewer than 165 bars of warmup.")
                }
            }
        }
    }

    @ViewBuilder
    private var pastTradesCard: some View {
        if let trades = p?.pastTrades, !trades.isEmpty {
            Card("The bot's past trades here", subtitle: "closed positions in this name") {
                VStack(spacing: 0) {
                    ForEach(trades, id: \.stableId) { t in
                        TRow(showsDivider: t.stableId != trades.last?.stableId) {
                            TCell {
                                Text(t.id ?? "—").font(DSFont.mono(11))
                                    .foregroundStyle(DS.textFaint)
                            }
                            TCell {
                                Tag(t.outcome ?? "—", tone: t.outcome == "WIN" ? .up : .down,
                                    size: 10)
                            }
                            TCell(weight: 2) {
                                Text(t.exitReason ?? "").font(DSFont.body(12))
                                    .foregroundStyle(DS.textMuted).lineLimit(1)
                            }
                            TCell(align: .trailing) {
                                Text(Fmt.pct(t.pnlPct))
                                    .font(DSFont.num(13, .semibold))
                                    .foregroundStyle(t.pnlPct.pnlColor)
                            }
                            TCell(align: .trailing) {
                                Text(Fmt.when(t.exitDate)).font(DSFont.num(11))
                                    .foregroundStyle(DS.textFaint)
                            }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var newsCard: some View {
        if let news = p?.news, !news.isEmpty {
            headlinesCard(news, title: "Symbol news",
                          subtitle: "live headlines for \(model.analyzerSymbol)")
        }
    }
}

// MARK: - Signals

struct SignalsView: View {
    @Environment(AppModel.self) private var model
    @State private var filter: SignalFilter = .all

    enum SignalFilter: String, Hashable, CaseIterable {
        case all = "All", buy = "Buy", sell = "Sell", neutral = "Neutral", held = "Held"
    }

    private var payload: SignalsPayload? { model.signalsPayload }
    private var allRows: [SignalRow] { payload?.rows ?? [] }

    private var rows: [SignalRow] {
        switch filter {
        case .all: allRows
        case .buy: allRows.filter { $0.state == "BUY" }
        case .sell: allRows.filter { $0.state == "SELL" }
        case .neutral: allRows.filter { $0.state != "BUY" && $0.state != "SELL" }
        case .held: allRows.filter { $0.held == true }
        }
    }

    var body: some View {
        Group {
            if payload == nil {
                LoadingScreen(title: "Computing ribbons…",
                              detail: "plain EMA 8/13/21/55 over every watched symbol")
                    .background(DS.bg)
            } else {
                Screen(title: "Signals", subtitle: AppTab.signals.blurb,
                       refresh: { await model.load(.signals) }) {
                    if let err = model.tabErrors[.signals] { ErrorBox(text: err) }
                    summary
                    FilterPills(options: SignalFilter.allCases.map { ($0, $0.rawValue) },
                                selection: $filter)
                    grid
                }
            }
        }
        .task { if payload == nil { await model.load(.signals) } }
    }

    private var summary: some View {
        let buys = allRows.filter { $0.state == "BUY" }.count
        let sells = allRows.filter { $0.state == "SELL" }.count
        let neutral = allRows.count - buys - sells
        return StatRow(minWidth: 175) {
            StatCard(label: "BUY zone", value: "\(buys)",
                     delta: "red(55) under the fast pack", deltaTone: .up)
            StatCard(label: "SELL zone", value: "\(sells)",
                     delta: "advisory — trail owns the exit", deltaTone: .down)
            StatCard(label: "Neutral", value: "\(neutral)", sublabel: "ribbon tangled")
            StatCard(label: "Interval", value: payload?.interval ?? "—",
                     sublabel: "generated \(Fmt.when(payload?.generatedAt))")
        }
    }

    private var grid: some View {
        Card(padding: DS.s4) {
            if rows.isEmpty {
                EmptyNote(text: "No symbols match this filter.", icon: "wifi.slash")
            } else {
                VStack(spacing: 0) {
                    ForEach(rows) { row in
                        SignalRowView(row: row, isLast: row.id == rows.last?.id)
                            .contentShape(Rectangle())
                            .onTapGesture { model.analyze(row.symbol) }
                    }
                }
                Text("Margin = how far red(55) sits below the fast pack (+ = BUY room, − = SELL).")
                    .font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                    .padding(.top, DS.s2)
            }
        }
    }
}

struct SignalRowView: View {
    let row: SignalRow
    let isLast: Bool

    var body: some View {
        VStack(spacing: 0) {
            WidthReader(compactBelow: 640) { compact in
                if compact { compactBody } else { wideBody }
            }
            .padding(.vertical, 12)
            if !isLast { Rectangle().fill(DS.divider).frame(height: 1) }
        }
    }

    private var wideBody: some View {
        HStack(spacing: DS.s3) {
            identity.frame(minWidth: 160, alignment: .leading)
            Spacer(minLength: DS.s2)
            lines
            Spacer(minLength: DS.s2)
            margin
            Sparkline(points: row.spark ?? [])
            price
        }
    }

    private var compactBody: some View {
        VStack(alignment: .leading, spacing: DS.s2) {
            HStack { identity; Spacer(); price }
            HStack(spacing: DS.s2) { margin; Sparkline(points: row.spark ?? [], width: 64); Spacer() }
            lines
        }
    }

    private var identity: some View {
        HStack(spacing: 8) {
            if row.held == true { Circle().fill(DS.up).frame(width: 7, height: 7) }
            Text(row.symbol).font(DSFont.bold(15)).foregroundStyle(DS.text)
            RibbonBadge(state: row.state)
            if let t = row.transition, t != "NO_ACTION", t != "HOLD" {
                Tag(t, tone: t == "ENTER_LONG" ? .up : .caution, size: 9)
            }
            if row.dnt == true { Tag("DNT", tone: .down, size: 9) }
        }
    }

    @ViewBuilder
    private var lines: some View {
        if let l = row.lines {
            Text("8: \(Fmt.num(l.blue, dp: 2))   13: \(Fmt.num(l.green, dp: 2))   21: \(Fmt.num(l.yellow, dp: 2))   55: \(Fmt.num(l.red, dp: 2))")
                .font(DSFont.num(11))
                .foregroundStyle(DS.textFaint)
                .lineLimit(1)
        }
    }

    @ViewBuilder
    private var margin: some View {
        if let m = row.redMarginPct {
            Tag("margin \(Fmt.pct(m, dp: 2))", tone: m > 0 ? .up : .down, size: 10)
        }
        if let note = row.note, !note.isEmpty {
            Tag(note, tone: .caution, size: 10)
        }
    }

    private var price: some View {
        VStack(alignment: .trailing, spacing: 2) {
            Text(Fmt.usd(row.price)).font(DSFont.num(14, .semibold)).foregroundStyle(DS.text)
            Text(Fmt.pct(row.changePct))
                .font(DSFont.num(11, .semibold))
                .foregroundStyle(row.changePct.pnlColor)
        }
        .frame(minWidth: 78, alignment: .trailing)
    }
}

// MARK: - Screener

struct ScreenerView: View {
    @Environment(AppModel.self) private var model
    @State private var query = ""

    private var payload: ScreenPayload? { model.screenPayload }

    private var rows: [ScreenRow] {
        let all = payload?.rows ?? []
        guard !query.isEmpty else { return all }
        let q = query.lowercased()
        return all.filter { $0.symbol.lowercased().contains(q)
            || ($0.reason ?? "").lowercased().contains(q) }
    }

    var body: some View {
        Group {
            if payload == nil, model.loadingTabs.contains(.screener) {
                LoadingScreen(title: "Screening…",
                              detail: "multi-timeframe momentum over held + picks + universe")
                    .background(DS.bg)
            } else {
                Screen(title: "Screener", subtitle: AppTab.screener.blurb,
                       refresh: { await model.load(.screener) }) {
                    if let err = model.tabErrors[.screener] { ErrorBox(text: err) }
                    header
                    tableCard
                }
            }
        }
        .task { if payload == nil { await model.load(.screener) } }
    }

    private var header: some View {
        Card {
            WidthReader(compactBelow: 620) { compact in
                let left = VStack(alignment: .leading, spacing: 4) {
                    Text("momentum_screen.py over held + picks + watchlist + universe")
                        .font(DSFont.body(13)).foregroundStyle(DS.textMuted)
                    Text("anchor \(payload?.anchor ?? "1m") · min score \(Fmt.num(payload?.minScore, dp: 0)) · \(Fmt.when(payload?.generatedAt))")
                        .font(DSFont.mono(11)).foregroundStyle(DS.textFaint)
                }
                let right = HStack(spacing: DS.s2) {
                    OrganicField(prompt: "Filter ticker / reason", text: $query, width: 220)
                    Button {
                        Task { await model.rerunScreen() }
                    } label: {
                        if model.loadingTabs.contains(.screener) {
                            ProgressView().controlSize(.small)
                        } else {
                            Label("Re-screen", systemImage: "arrow.triangle.2.circlepath")
                        }
                    }
                    .buttonStyle(.organicPrimary)
                }
                if compact {
                    VStack(alignment: .leading, spacing: DS.s3) { left; right }
                } else {
                    HStack(spacing: DS.s3) { left; Spacer(minLength: DS.s2); right }
                }
            }
        }
    }

    private var tableCard: some View {
        Card(padding: DS.s4) {
            if rows.isEmpty {
                EmptyNote(text: "No names match.", icon: "line.3.horizontal.decrease")
            } else {
                VStack(spacing: 0) {
                    THeader(columns: ["#", "Ticker", "Score", "Returns", "Matched on", "Price"],
                            weights: [0.35, 1.4, 1.4, 1.6, 1.6, 1],
                            alignments: [.leading, .leading, .leading, .trailing,
                                         .leading, .trailing])
                    Rectangle().fill(DS.divider).frame(height: 1)
                    ForEach(Array(rows.enumerated()), id: \.element.id) { i, row in
                        ScreenRowView(rank: i + 1, row: row, isLast: i == rows.count - 1)
                            .contentShape(Rectangle())
                            .onTapGesture { model.analyze(row.symbol) }
                    }
                }
            }
        }
    }
}

struct ScreenRowView: View {
    let rank: Int
    let row: ScreenRow
    let isLast: Bool

    var body: some View {
        TRow(showsDivider: !isLast) {
            TCell(weight: 0.35) {
                Text("\(rank)")
                    .font(DSFont.num(12, .bold))
                    .foregroundStyle(DS.textFaint)
            }
            TCell(weight: 1.4) {
                HStack(spacing: 6) {
                    Text(row.symbol).font(DSFont.bold(14)).foregroundStyle(DS.text)
                    if row.qualified == true {
                        Tag("QUALIFIED", tone: .up, size: 9, icon: "checkmark.seal.fill")
                    }
                    if row.held == true { Tag("HELD", tone: .sage, size: 9) }
                    if row.pick == true { Tag("PICK", tone: .accent, size: 9) }
                }
            }
            TCell(weight: 1.4) {
                HStack(spacing: 8) {
                    MeterBar(value: min(max(row.score ?? 0, 0), 100) / 100,
                             tint: row.qualified == true ? DS.accent2 : DS.neutral(5))
                        .frame(width: 90)
                    Text(Fmt.num(row.score, dp: 0))
                        .font(DSFont.num(13, .bold)).foregroundStyle(DS.text)
                }
            }
            TCell(align: .trailing, weight: 1.6) {
                ReturnsPills(returns: row.returns)
            }
            TCell(weight: 1.6) {
                Text(row.qualified == true
                     ? "clears anchor + score gates"
                     : (row.reason ?? row.affordableReason ?? "—"))
                    .font(DSFont.body(12))
                    .foregroundStyle(DS.textMuted)
                    .lineLimit(2)
            }
            TCell(align: .trailing) {
                VStack(alignment: .trailing, spacing: 2) {
                    Text(Fmt.usd(row.lastPrice)).font(DSFont.num(13, .semibold))
                        .foregroundStyle(DS.text)
                    Text(Fmt.pct(row.changePct))
                        .font(DSFont.num(11))
                        .foregroundStyle(row.changePct.pnlColor)
                }
            }
        }
    }
}
