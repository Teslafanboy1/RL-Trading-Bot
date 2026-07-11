//
//  MarketNewsViews.swift — market context (indices, watchlist, earnings,
//  headlines) and the news/sources feed with the accuracy leaderboard.
//

import SwiftUI

// MARK: - Market

struct MarketView: View {
    @Environment(AppModel.self) private var model
    @State private var watchlistText = ""
    @State private var saveError: String?

    private var m: MarketPayload? { model.marketPayload }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let err = model.tabErrors[.market] { ErrorBox(text: err) }
                indicesCard
                watchlistCard
                earningsCard
                headlinesCard(m?.generalNews ?? [], title: "Headlines")
            }
            .padding()
        }
        .opacity(m == nil ? 0 : 1)
        .refreshable { await model.load(.market) }
        .task {
            if m == nil { await model.load(.market) }
            watchlistText = (m?.watchlistSymbols ?? []).joined(separator: ", ")
        }
        .onChange(of: m?.watchlistSymbols) { _, syms in
            if watchlistText.isEmpty { watchlistText = (syms ?? []).joined(separator: ", ") }
        }
        .overlay {
            if m == nil { ContentUnavailableView("Loading market…", systemImage: "globe") }
        }
    }

    private var indicesCard: some View {
        GroupBox {
            let cols = [GridItem(.adaptive(minimum: 210), spacing: 12)]
            LazyVGrid(columns: cols, spacing: 10) {
                ForEach(m?.indices ?? []) { ix in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(ix.label ?? ix.symbol).font(.callout.weight(.medium))
                            Spacer()
                            VStack(alignment: .trailing, spacing: 0) {
                                Text(Fmt.num(ix.price)).font(.callout.monospacedDigit())
                                Text(Fmt.pct(ix.changePct))
                                    .font(.caption2.monospacedDigit())
                                    .foregroundStyle((ix.changePct ?? 0).pnlColor)
                            }
                        }
                        Sparkline(points: ix.spark ?? [])
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .padding(8)
                    .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
                }
            }
        } label: { Label("Indices", systemImage: "chart.xyaxis.line") }
    }

    private var watchlistCard: some View {
        GroupBox {
            HStack {
                TextField("SPY, QQQ, NVDA…", text: $watchlistText)
                    .font(.callout.monospaced())
                    #if !os(macOS)
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                    #endif
                Button("Save") {
                    let syms = watchlistText.split(separator: ",")
                        .map { $0.trimmingCharacters(in: .whitespaces).uppercased() }
                        .filter { !$0.isEmpty }
                    Task { saveError = await model.saveWatchlist(syms) }
                }
                .buttonStyle(.bordered)
            }
            if let saveError { ErrorBox(text: saveError) }
            VStack(spacing: 0) {
                ForEach(m?.watchlist ?? []) { row in
                    watchRow(row)
                        .padding(.vertical, 6)
                    if row.id != m?.watchlist?.last?.id { Divider() }
                }
            }
        } label: { Label("My watchlist", systemImage: "star.fill") }
    }

    @ViewBuilder
    private func watchRow(_ row: WatchRow) -> some View {
        let symbolBlock = HStack(spacing: 6) {
            Text(row.symbol).font(.subheadline.bold().monospaced())
            if row.held == true {
                Text("held").font(.caption2)
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(Color.up.opacity(0.15), in: Capsule())
                    .foregroundStyle(Color.up)
            }
        }
        let priceBlock = HStack(spacing: 10) {
            Text(Fmt.usd(row.price)).font(.callout.monospacedDigit())
            Text(Fmt.pct(row.changePct))
                .font(.caption.monospacedDigit())
                .foregroundStyle((row.changePct ?? 0).pnlColor)
        }
        let extras = HStack(spacing: 10) {
            RibbonBadge(state: row.emaState)
            Sparkline(points: row.spark ?? [])
            Button("Trade") {
                model.orderPrefill = .init(symbol: row.symbol, side: "buy")
                model.showOrderSheet = true
            }
            .buttonStyle(.bordered).controlSize(.small)
        }
        WidthReader(compactBelow: 560) { compact in
            if compact {
                VStack(alignment: .leading, spacing: 6) {
                    HStack { symbolBlock; Spacer(); priceBlock }
                    extras
                }
            } else {
                HStack(spacing: 10) {
                    symbolBlock.frame(minWidth: 90, alignment: .leading)
                    Spacer(minLength: 8)
                    priceBlock
                    extras
                }
            }
        }
    }

    private var earningsCard: some View {
        GroupBox {
            earningsBody
        } label: { Label("Earnings — next 14 days", systemImage: "calendar") }
    }

    @ViewBuilder
    private var earningsBody: some View {
        let e = m?.earnings
        if let note = e?["unavailable"]?.stringValue {
            Text(note).font(.caption).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        } else if let err = e?["error"]?.stringValue {
            WarningBox(text: err)
        } else {
            let items = e?["earnings"]?.arrayValue ?? e?["results"]?.arrayValue
                ?? e?["calendar"]?.arrayValue ?? e?.arrayValue ?? []
            if items.isEmpty {
                Text("nothing in the window").font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                let held = Set(m?.held ?? [])
                VStack(spacing: 4) {
                    ForEach(Array(items.prefix(30).enumerated()), id: \.offset) { _, item in
                        let sym = (item["symbol"]?.stringValue ?? item["ticker"]?.stringValue ?? "").uppercased()
                        HStack {
                            Text(sym).font(.callout.bold().monospaced())
                            if held.contains(sym) {
                                Text("HELD — blackout?").font(.caption2.bold())
                                    .foregroundStyle(Color.caution)
                            }
                            Spacer()
                            Text(item["report_date"]?.stringValue ?? item["date"]?.stringValue ?? "—")
                                .font(.caption.monospaced())
                            Text(item["timing"]?.stringValue ?? item["time"]?.stringValue ?? "")
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }
}

// MARK: - News

struct NewsView: View {
    @Environment(AppModel.self) private var model
    private var n: NewsPayload? { model.newsPayload }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let err = model.tabErrors[.news] { ErrorBox(text: err) }
                leaderboardCard
                verdictsCard
                citationsCard
                symbolNewsCards
                headlinesCard(n?.general ?? [], title: "General market news")
            }
            .padding()
        }
        .opacity(n == nil ? 0 : 1)
        .refreshable { await model.load(.news) }
        .task { if n == nil { await model.load(.news) } }
        .overlay {
            if n == nil { ContentUnavailableView("Loading news…", systemImage: "newspaper") }
        }
    }

    private var leaderboardCard: some View {
        GroupBox {
            VStack(spacing: 8) {
                ForEach(n?.leaderboard ?? []) { row in
                    HStack(spacing: 10) {
                        Text(row.source).font(.subheadline.bold().monospaced())
                            .frame(width: 110, alignment: .leading)
                        Text(row.weight != nil ? "w \(Fmt.pct((row.weight ?? 0) * 100, dp: 1, signed: false))" : "—")
                            .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                        Gauge(value: row.accuracy ?? 0, in: 0...1) { EmptyView() }
                            .gaugeStyle(.accessoryLinearCapacity)
                            .tint((row.accuracy ?? 0) >= 0.5 ? Color.up : Color.down)
                        Text("\(row.wins ?? 0)W/\(row.losses ?? 0)L")
                            .font(.caption.monospacedDigit())
                            .frame(width: 64, alignment: .trailing)
                    }
                }
            }
        } label: {
            Label("Source accuracy leaderboard (drives the bot's weights)",
                  systemImage: "chart.bar.fill")
        }
    }

    @ViewBuilder
    private var verdictsCard: some View {
        if let verdicts = n?.verdicts, !verdicts.isEmpty {
            GroupBox {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(verdicts.prefix(10).enumerated()), id: \.offset) { _, v in
                        VStack(alignment: .leading, spacing: 3) {
                            Text("\(v.file ?? "") \(v.tradeIds?.joined(separator: ",") ?? "")")
                                .font(.caption.monospaced()).foregroundStyle(.secondary)
                            HStack(spacing: 5) {
                                ForEach((v.sources ?? [:]).keys.sorted(), id: \.self) { src in
                                    let ok = v.sources?[src] == true
                                    Label(src, systemImage: ok ? "checkmark" : "xmark")
                                        .font(.caption2.bold())
                                        .padding(.horizontal, 6).padding(.vertical, 2)
                                        .background((ok ? Color.up : Color.down).opacity(0.14),
                                                    in: Capsule())
                                        .foregroundStyle(ok ? Color.up : Color.down)
                                }
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } label: {
                Label("Per-trade source verdicts (from postmortems)", systemImage: "checklist")
            }
        }
    }

    private var citationsCard: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(Array((n?.citations ?? []).prefix(25).enumerated()), id: \.offset) { _, c in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(c.symbol ?? "—").font(.subheadline.bold().monospaced())
                            if let conf = c.confidence {
                                Text("conf \(conf)").font(.caption.monospaced())
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text(c.when ?? c.file ?? "").font(.caption2).foregroundStyle(.tertiary)
                        }
                        if let s = c.sources, !s.isEmpty {
                            Text("sources: \(s)").font(.caption).foregroundStyle(.secondary)
                        }
                        if let note = c.note, !note.isEmpty {
                            Text(note).font(.caption).lineLimit(3)
                        }
                        if let inv = c.invalidates, !inv.isEmpty {
                            Text("invalidates if: \(inv)")
                                .font(.caption).foregroundStyle(Color.caution).lineLimit(2)
                        }
                    }
                    .padding(.vertical, 2)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } label: {
            Label("What the bot cited (research + reviews)", systemImage: "quote.opening")
        }
    }

    @ViewBuilder
    private var symbolNewsCards: some View {
        ForEach((n?.symbolNews ?? [:]).keys.sorted(), id: \.self) { sym in
            headlinesCard(n?.symbolNews?[sym] ?? [], title: "Live headlines — \(sym)")
        }
    }
}

// MARK: - shared headline list

@ViewBuilder
func headlinesCard(_ items: [Headline], title: String) -> some View {
    if !items.isEmpty {
        GroupBox {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(items.prefix(15).enumerated()), id: \.offset) { _, h in
                    HStack(alignment: .firstTextBaseline) {
                        if let link = h.link, let url = URL(string: link) {
                            Link(h.title ?? link, destination: url)
                                .font(.callout)
                        } else {
                            Text(h.title ?? "").font(.callout)
                        }
                        Spacer()
                        Text(h.source ?? "").font(.caption2).foregroundStyle(.tertiary)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } label: { Label(title, systemImage: "newspaper") }
    }
}
