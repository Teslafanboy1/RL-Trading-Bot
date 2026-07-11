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
        HStack(spacing: 4) {
            ForEach(Self.order, id: \.self) { tf in
                let v = returns?[tf]?.numberValue
                VStack(spacing: 0) {
                    Text(tf).font(.system(size: 8, weight: .bold))
                        .foregroundStyle(.secondary)
                    Text(v == nil ? "—" : Fmt.pct(v, dp: 0))
                        .font(.caption2.weight(.semibold).monospacedDigit())
                        .foregroundStyle(v == nil ? Color.secondary : v!.pnlColor)
                }
                .frame(minWidth: 34)
                .padding(.vertical, 2)
                .background((v ?? 0).pnlColor.opacity(v == nil ? 0.04 : 0.09),
                            in: RoundedRectangle(cornerRadius: 5))
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
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let err = model.tabErrors[.analyzer] { ErrorBox(text: err) }
                controls
                quoteHeader
                chartCard
                AdaptivePair(compactBelow: 700) {
                    statsCard
                } second: {
                    ribbonCard
                }
                pastTradesCard
                newsCard
            }
            .padding()
        }
        .refreshable { await model.load(.analyzer) }
        .task {
            symbolField = model.analyzerSymbol
            if p == nil { await model.load(.analyzer) }
        }
    }

    private var controls: some View {
        @Bindable var model = model
        return GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    TextField("Symbol", text: $symbolField)
                        .font(.body.monospaced())
                        .textFieldStyle(.roundedBorder)
                        .frame(maxWidth: 120)
                        .onSubmit { go() }
                        #if !os(macOS)
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                        #endif
                    Picker("", selection: $model.analyzerInterval) {
                        Text("Daily").tag("1d")
                        Text("1h (bot)").tag("1h")
                        Text("30m").tag("30m")
                    }
                    .pickerStyle(.segmented)
                    .frame(maxWidth: 260)
                    .onChange(of: model.analyzerInterval) { _, _ in
                        Task { await model.load(.analyzer) }
                    }
                    Button { go() } label: { Label("Load", systemImage: "magnifyingglass") }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.small)
                    Spacer()
                }
                quickChips
            }
        }
    }

    private var quickChips: some View {
        let held = (model.overview?.positions ?? []).map(\.symbol)
        let extras = ["SPY", "QQQ", "IWM", "NVDA", "MU", "LABU"]
        let all = Array(NSOrderedSet(array: held + extras)) as? [String] ?? extras
        return ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(all, id: \.self) { s in
                    Button {
                        symbolField = s
                        go()
                    } label: {
                        Text(s)
                            .font(.caption.weight(.semibold).monospaced())
                            .padding(.horizontal, 8).padding(.vertical, 3)
                            .background(s == model.analyzerSymbol
                                        ? Color.accentColor.opacity(0.25)
                                        : Color.secondary.opacity(0.12),
                                        in: Capsule())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private func go() {
        let s = symbolField.trimmingCharacters(in: .whitespaces).uppercased()
        guard !s.isEmpty else { return }
        symbolField = s
        model.analyzerSymbol = s
        Task { await model.load(.analyzer) }
    }

    @ViewBuilder
    private var quoteHeader: some View {
        if let p {
            HStack(spacing: 12) {
                Text(p.symbol ?? "—").font(.title2.bold().monospaced())
                Text(Fmt.usd(p.quote?.price)).font(.title3.monospacedDigit())
                Text(Fmt.pct(p.quote?.changePct))
                    .font(.headline.monospacedDigit())
                    .foregroundStyle((p.quote?.changePct ?? 0).pnlColor)
                RibbonBadge(state: p.ribbon?.state)
                if p.held == true { StatusDot(color: .up, label: "HELD") }
                if p.dnt == true { StatusDot(color: .down, label: "DNT") }
                Spacer()
                HStack(spacing: 8) {
                    Button {
                        model.orderPrefill = .init(symbol: p.symbol ?? "", side: "buy")
                        model.showOrderSheet = true
                    } label: { Label("Buy", systemImage: "plus.circle") }
                    Button {
                        model.orderPrefill = .init(symbol: p.symbol ?? "", side: "sell")
                        model.showOrderSheet = true
                    } label: { Label("Sell", systemImage: "minus.circle") }
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
        }
    }

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
            if let c = b.close { out.append(SeriesPoint(idx: i, series: "close", value: c)) }
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
        GroupBox {
            let data = chartData
            if data.isEmpty {
                ContentUnavailableView("No chart data",
                                       systemImage: "chart.line.downtrend.xyaxis",
                                       description: Text("Load a symbol — intraday intervals need 60+ bars."))
                    .frame(height: 220)
            } else {
                Chart {
                    ForEach(data) { pt in
                        LineMark(x: .value("Bar", pt.idx),
                                 y: .value("Price", pt.value))
                            .foregroundStyle(by: .value("Series", pt.series))
                            .lineStyle(StrokeStyle(lineWidth: pt.series == "close" ? 2 : 1.2))
                    }
                    if let stop = p?.stops?.stopPrice {
                        RuleMark(y: .value("Stop", stop))
                            .foregroundStyle(Color.down.opacity(0.7))
                            .lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 3]))
                            .annotation(position: .trailing, alignment: .leading) {
                                Text("stop").font(.caption2).foregroundStyle(Color.down)
                            }
                    }
                    if let trail = p?.stops?.trailPrice {
                        RuleMark(y: .value("Trail", trail))
                            .foregroundStyle(Color.caution.opacity(0.7))
                            .lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 3]))
                            .annotation(position: .trailing, alignment: .leading) {
                                Text("trail").font(.caption2).foregroundStyle(Color.caution)
                            }
                    }
                    if let entry = p?.position?["entry_price"]?.numberValue {
                        RuleMark(y: .value("Entry", entry))
                            .foregroundStyle(Color.secondary.opacity(0.7))
                            .lineStyle(StrokeStyle(lineWidth: 1, dash: [2, 3]))
                            .annotation(position: .trailing, alignment: .leading) {
                                Text("entry").font(.caption2).foregroundStyle(.secondary)
                            }
                    }
                }
                .chartForegroundStyleScale([
                    "close": Color.primary,
                    "EMA 8": Color.blue,
                    "EMA 13": Color.green,
                    "EMA 21": Color.yellow,
                    "EMA 55": Color.red,
                ])
                .chartXAxis(.hidden)
                .chartYScale(domain: .automatic(includesZero: false))
                .frame(height: 260)
                HStack {
                    Text(p?.bars?.first?.ts ?? "")
                    Spacer()
                    Text("\(p?.bars?.count ?? 0) bars · \(p?.interval ?? "")")
                    Spacer()
                    Text(p?.bars?.last?.ts ?? "")
                }
                .font(.caption2.monospaced())
                .foregroundStyle(.secondary)
            }
        } label: {
            Label("Price + bot ribbon (EMA 8/13/21/55)", systemImage: "chart.xyaxis.line")
        }
    }

    private var statsCard: some View {
        GroupBox {
            VStack(spacing: 4) {
                KeyValueRow(key: "52w high", value: Fmt.usd(p?.stats?.high52w))
                KeyValueRow(key: "52w low", value: Fmt.usd(p?.stats?.low52w))
                KeyValueRow(key: "Off high", value: Fmt.pct(p?.stats?.offHighPct),
                            valueColor: (p?.stats?.offHighPct ?? 0).pnlColor)
                KeyValueRow(key: "Momentum score",
                            value: p?.stats?.momentumScore.map { Fmt.num($0, dp: 0) + "/100" } ?? "—")
                Divider().padding(.vertical, 2)
                HStack {
                    Text("Returns").foregroundStyle(.secondary)
                    Spacer()
                    ReturnsPills(returns: p?.stats?.returns)
                }
            }
        } label: { Label("Momentum profile", systemImage: "speedometer") }
    }

    private var ribbonCard: some View {
        GroupBox {
            VStack(spacing: 4) {
                HStack {
                    Text("State").foregroundStyle(.secondary)
                    Spacer()
                    RibbonBadge(state: p?.ribbon?.state)
                    Text(p?.ribbon?.transition ?? "—")
                        .font(.caption.monospaced()).foregroundStyle(.secondary)
                }
                KeyValueRow(key: "EMA 8 (blue)", value: Fmt.usd(p?.ribbon?.lines?.blue))
                KeyValueRow(key: "EMA 13 (green)", value: Fmt.usd(p?.ribbon?.lines?.green))
                KeyValueRow(key: "EMA 21 (yellow)", value: Fmt.usd(p?.ribbon?.lines?.yellow))
                KeyValueRow(key: "EMA 55 (red)", value: Fmt.usd(p?.ribbon?.lines?.red))
                KeyValueRow(key: "Bars / source",
                            value: "\(p?.ribbon?.bars ?? 0) · \(p?.ribbon?.source ?? "—")")
                if let note = p?.ribbon?.note, !note.isEmpty {
                    Text(note).font(.caption2).foregroundStyle(Color.caution)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        } label: { Label("Live ribbon (bot signal layer)", systemImage: "dot.radiowaves.left.and.right") }
    }

    @ViewBuilder
    private var pastTradesCard: some View {
        if let trades = p?.pastTrades, !trades.isEmpty {
            GroupBox {
                VStack(spacing: 6) {
                    ForEach(trades, id: \.stableId) { t in
                        HStack {
                            Text(t.id ?? "—").font(.caption.monospaced())
                            Text(t.outcome ?? "").font(.caption.bold())
                                .foregroundStyle(t.outcome == "WIN" ? Color.up : Color.down)
                            Text(t.exitReason ?? "").font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            Text(Fmt.pct(t.pnlPct))
                                .font(.caption.monospacedDigit())
                                .foregroundStyle((t.pnlPct ?? 0).pnlColor)
                            Text(Fmt.when(t.exitDate)).font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            } label: { Label("Bot's past trades here", systemImage: "clock.arrow.circlepath") }
        }
    }

    @ViewBuilder
    private var newsCard: some View {
        if let news = p?.news, !news.isEmpty {
            GroupBox {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(news.enumerated()), id: \.offset) { _, h in
                        VStack(alignment: .leading, spacing: 1) {
                            if let link = h.link, let url = URL(string: link) {
                                Link(h.title ?? "—", destination: url)
                                    .font(.callout)
                            } else {
                                Text(h.title ?? "—").font(.callout)
                            }
                            Text("\(h.source ?? "") · \(h.published ?? "")")
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } label: { Label("Symbol news", systemImage: "newspaper") }
        }
    }
}

// MARK: - Signals

struct SignalsView: View {
    @Environment(AppModel.self) private var model

    private var payload: SignalsPayload? { model.signalsPayload }
    private var rows: [SignalRow] { payload?.rows ?? [] }

    var body: some View {
        List {
            if let err = model.tabErrors[.signals] { ErrorBox(text: err) }
            Section {
                summary
            }
            Section("Every symbol the bot watches — tap to analyze") {
                ForEach(rows) { row in
                    Button { model.analyze(row.symbol) } label: {
                        SignalRowView(row: row)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .refreshable { await model.load(.signals) }
        .task { if payload == nil { await model.load(.signals) } }
        .overlay {
            if payload == nil {
                ContentUnavailableView("Computing ribbons…", systemImage: "dot.radiowaves.left.and.right")
            }
        }
    }

    private var summary: some View {
        let buys = rows.filter { $0.state == "BUY" }.count
        let sells = rows.filter { $0.state == "SELL" }.count
        let neutral = rows.filter { $0.state == "NEUTRAL" }.count
        return GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 10) {
                    StatusDot(color: .up, label: "BUY \(buys)")
                    StatusDot(color: .down, label: "SELL \(sells)")
                    StatusDot(color: .secondary, label: "NEUTRAL \(neutral)")
                    Spacer()
                    Text("interval \(payload?.interval ?? "—")")
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                }
                Text("Plain EMA 8/13/21/55 — the exact lines the bot trades. Margin = how far red(55) sits below the fast pack (+ = BUY room, − = SELL).")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct SignalRowView: View {
    let row: SignalRow

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                if row.held == true {
                    Circle().fill(Color.up).frame(width: 6, height: 6)
                }
                Text(row.symbol).font(.headline.monospaced())
                RibbonBadge(state: row.state)
                if let t = row.transition, t != "NO_ACTION", t != "HOLD" {
                    Text(t).font(.caption2.bold().monospaced())
                        .foregroundStyle(t == "ENTER_LONG" ? Color.up : Color.caution)
                }
                if row.dnt == true {
                    Text("DNT").font(.caption2.bold()).foregroundStyle(Color.down)
                }
                Spacer()
                Sparkline(points: row.spark ?? [])
                VStack(alignment: .trailing, spacing: 1) {
                    Text(Fmt.usd(row.price)).font(.callout.monospacedDigit())
                    Text(Fmt.pct(row.changePct))
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle((row.changePct ?? 0).pnlColor)
                }
            }
            HStack(spacing: 10) {
                if let m = row.redMarginPct {
                    Text("margin \(Fmt.pct(m, dp: 2))")
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(m > 0 ? Color.up : Color.down)
                }
                if let l = row.lines {
                    Text("8:\(Fmt.num(l.blue, dp: 2)) 13:\(Fmt.num(l.green, dp: 2)) 21:\(Fmt.num(l.yellow, dp: 2)) 55:\(Fmt.num(l.red, dp: 2))")
                        .font(.caption2.monospaced())
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                if let note = row.note, !note.isEmpty {
                    Text(note).font(.caption2).foregroundStyle(Color.caution).lineLimit(1)
                }
                Spacer()
            }
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())
    }
}

// MARK: - Screener

struct ScreenerView: View {
    @Environment(AppModel.self) private var model

    private var payload: ScreenPayload? { model.screenPayload }

    var body: some View {
        List {
            if let err = model.tabErrors[.screener] { ErrorBox(text: err) }
            Section {
                header
            }
            Section("Ranked by momentum score — tap to analyze") {
                ForEach(Array((payload?.rows ?? []).enumerated()), id: \.element.id) { i, row in
                    Button { model.analyze(row.symbol) } label: {
                        ScreenRowView(rank: i + 1, row: row)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .refreshable { await model.load(.screener) }
        .task { if payload == nil { await model.load(.screener) } }
        .overlay {
            if payload == nil, model.loadingTabs.contains(.screener) {
                ContentUnavailableView("Screening…", systemImage: "line.3.horizontal.decrease.circle")
            }
        }
    }

    private var header: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("The bot's own multi-timeframe momentum screen (momentum_screen.py) over held + picks + watchlist + universe.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Text("anchor \(payload?.anchor ?? "1m") · min score \(Fmt.num(payload?.minScore, dp: 0)) · \(Fmt.when(payload?.generatedAt))")
                            .font(.caption2.monospaced())
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button {
                        Task { await model.rerunScreen() }
                    } label: {
                        if model.loadingTabs.contains(.screener) {
                            ProgressView().controlSize(.small)
                        } else {
                            Label("Re-screen", systemImage: "arrow.triangle.2.circlepath")
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                }
            }
        }
    }
}

struct ScreenRowView: View {
    let rank: Int
    let row: ScreenRow

    var body: some View {
        HStack(spacing: 10) {
            Text("#\(rank)")
                .font(.caption.bold().monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 28, alignment: .trailing)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(row.symbol).font(.headline.monospaced())
                    if row.qualified == true {
                        Label("QUALIFIED", systemImage: "checkmark.seal.fill")
                            .font(.caption2.bold())
                            .foregroundStyle(Color.up)
                            .labelStyle(.titleAndIcon)
                    }
                    if row.held == true { chip("HELD", .up) }
                    if row.pick == true { chip("PICK", .accentColor) }
                }
                HStack(spacing: 8) {
                    Gauge(value: min(max(row.score ?? 0, 0), 100), in: 0...100) { EmptyView() }
                        .gaugeStyle(.accessoryLinearCapacity)
                        .tint(row.qualified == true ? Color.up : Color.secondary)
                        .frame(width: 90)
                    Text(Fmt.num(row.score, dp: 0))
                        .font(.caption.bold().monospacedDigit())
                    if row.qualified != true {
                        Text(row.reason ?? "")
                            .font(.caption2.monospaced())
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                }
            }
            Spacer()
            ReturnsPills(returns: row.returns)
            VStack(alignment: .trailing, spacing: 1) {
                Text(Fmt.usd(row.lastPrice)).font(.callout.monospacedDigit())
                Text(Fmt.pct(row.changePct))
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle((row.changePct ?? 0).pnlColor)
            }
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())
    }

    private func chip(_ text: String, _ color: Color) -> some View {
        Text(text).font(.caption2.bold())
            .padding(.horizontal, 5).padding(.vertical, 1)
            .background(color.opacity(0.15), in: Capsule())
            .foregroundStyle(color)
    }
}
