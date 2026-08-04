//
//  MarketNewsViews.swift — market context (indices, watchlist, earnings,
//  headlines) and the news feed with the source-accuracy leaderboard.
//

import SwiftUI

// MARK: - Market

struct MarketView: View {
    @Environment(AppModel.self) private var model
    @State private var watchlistText = ""
    @State private var saveError: String?

    private var m: MarketPayload? { model.marketPayload }

    var body: some View {
        Group {
            if m == nil {
                LoadingScreen(title: "Loading market…", detail: "indices, watchlist, earnings")
                    .background(DS.bg)
            } else {
                Screen(title: "Market", subtitle: AppTab.market.blurb,
                       refresh: { await model.load(.market) }) {
                    if let err = model.tabErrors[.market] { ErrorBox(text: err) }
                    indexTiles
                    watchlistCard
                    AdaptivePair(compactBelow: 820) {
                        earningsCard
                    } second: {
                        headlinesCard(m?.generalNews ?? [], title: "Headlines",
                                      subtitle: "general market tape")
                    }
                }
            }
        }
        .task {
            if m == nil { await model.load(.market) }
            if watchlistText.isEmpty {
                watchlistText = (m?.watchlistSymbols ?? []).joined(separator: ", ")
            }
        }
        .onChange(of: m?.watchlistSymbols) { _, syms in
            if watchlistText.isEmpty { watchlistText = (syms ?? []).joined(separator: ", ") }
        }
    }

    private var indexTiles: some View {
        StatRow(minWidth: 200) {
            ForEach(m?.indices ?? []) { ix in
                VStack(alignment: .leading, spacing: 6) {
                    Text((ix.label ?? ix.symbol).uppercased())
                        .font(DSFont.semibold(13)).kerning(0.5)
                        .foregroundStyle(DS.textMuted).lineLimit(1)
                    Text(ix.price == nil ? "—" : Fmt.num(ix.price))
                        .font(DSFont.heading(24)).foregroundStyle(DS.text)
                        .lineLimit(1).minimumScaleFactor(0.6)
                    HStack {
                        Text(Fmt.pct(ix.changePct))
                            .font(DSFont.num(14, .semibold))
                            .foregroundStyle(ix.changePct.pnlColor)
                        Spacer()
                        Sparkline(points: ix.spark ?? [], width: 70, height: 22)
                    }
                }
                .frame(maxWidth: .infinity, minHeight: 118, alignment: .topLeading)
                .padding(DS.s4)
                .background(DS.surface,
                            in: RoundedRectangle(cornerRadius: DS.rCard, style: .continuous))
                .shadow(color: DS.shadowColor, radius: DS.shadowRadius, y: DS.shadowY)
            }
        }
    }

    private var watchlistCard: some View {
        Card("My watchlist", subtitle: "the symbols the bot polls every cycle") {
            VStack(alignment: .leading, spacing: DS.s3) {
                HStack(spacing: DS.s2) {
                    OrganicField(prompt: "SPY, QQQ, NVDA…", text: $watchlistText, mono: true)
                    Button("Save") {
                        let syms = watchlistText.split(separator: ",")
                            .map { $0.trimmingCharacters(in: .whitespaces).uppercased() }
                            .filter { !$0.isEmpty }
                        Task { saveError = await model.saveWatchlist(syms) }
                    }
                    .buttonStyle(.organicPrimary)
                }
                if let saveError { ErrorBox(text: saveError) }
                let rows = m?.watchlist ?? []
                if rows.isEmpty {
                    EmptyNote(text: "Watchlist is empty.", icon: "star")
                } else {
                    VStack(spacing: 0) {
                        THeader(columns: ["Ticker", "Price", "Change", "Signal", "Trend", ""],
                                weights: [1.3, 1, 1, 1, 1, 1],
                                alignments: [.leading, .trailing, .trailing, .trailing,
                                             .trailing, .trailing])
                        Rectangle().fill(DS.divider).frame(height: 1)
                        ForEach(rows) { row in
                            TRow(showsDivider: row.id != rows.last?.id,
                                 onTap: { model.analyze(row.symbol) }) {
                                TCell(weight: 1.3) {
                                    HStack(spacing: 7) {
                                        Text(row.symbol).font(DSFont.bold(14))
                                            .foregroundStyle(DS.text)
                                        if row.held == true { Tag("held", tone: .up, size: 9) }
                                    }
                                }
                                TCell(align: .trailing) {
                                    Text(Fmt.usd(row.price)).font(DSFont.num(13))
                                        .foregroundStyle(DS.text)
                                }
                                TCell(align: .trailing) {
                                    Text(Fmt.pct(row.changePct))
                                        .font(DSFont.num(13, .semibold))
                                        .foregroundStyle(row.changePct.pnlColor)
                                }
                                TCell(align: .trailing) { RibbonBadge(state: row.emaState) }
                                TCell(align: .trailing) {
                                    Sparkline(points: row.spark ?? [], width: 70, height: 22)
                                }
                                TCell(align: .trailing) {
                                    Button("Trade") {
                                        model.orderPrefill = .init(symbol: row.symbol, side: "buy")
                                        model.showOrderSheet = true
                                    }
                                    .buttonStyle(SoftButtonStyle(tint: DS.accent, size: 12))
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    private var earningsCard: some View {
        Card("Earnings — next 14 days",
             subtitle: "held names inside the window are blackout candidates") {
            earningsBody
        }
    }

    @ViewBuilder
    private var earningsBody: some View {
        let e = m?.earnings
        if let note = e?["unavailable"]?.stringValue {
            EmptyNote(text: note, icon: "calendar")
        } else if let err = e?["error"]?.stringValue {
            WarningBox(text: err)
        } else {
            let items = e?["earnings"]?.arrayValue ?? e?["results"]?.arrayValue
                ?? e?["calendar"]?.arrayValue ?? e?.arrayValue ?? []
            if items.isEmpty {
                EmptyNote(text: "Nothing in the window.", icon: "calendar")
            } else {
                let held = Set(m?.held ?? [])
                VStack(spacing: 0) {
                    ForEach(Array(items.prefix(30).enumerated()), id: \.offset) { i, item in
                        let sym = (item["symbol"]?.stringValue
                                   ?? item["ticker"]?.stringValue ?? "").uppercased()
                        TRow(showsDivider: i < min(items.count, 30) - 1) {
                            TCell {
                                HStack(spacing: 7) {
                                    Text(sym).font(DSFont.bold(13)).foregroundStyle(DS.text)
                                    if held.contains(sym) {
                                        Tag("HELD — blackout?", tone: .caution, size: 9)
                                    }
                                }
                            }
                            TCell(align: .trailing) {
                                Text(item["report_date"]?.stringValue
                                     ?? item["date"]?.stringValue ?? "—")
                                    .font(DSFont.num(12)).foregroundStyle(DS.text)
                            }
                            TCell(align: .trailing) {
                                Text(item["timing"]?.stringValue ?? item["time"]?.stringValue ?? "")
                                    .font(DSFont.body(11)).foregroundStyle(DS.textMuted)
                            }
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
        Group {
            if n == nil {
                LoadingScreen(title: "Loading news…", detail: "headlines, citations, verdicts")
                    .background(DS.bg)
            } else {
                Screen(title: "News", subtitle: AppTab.news.blurb,
                       refresh: { await model.load(.news) }) {
                    if let err = model.tabErrors[.news] { ErrorBox(text: err) }
                    leaderboardCard
                    AdaptivePair(compactBelow: 860, ratio: 1.3) {
                        citationsCard
                    } second: {
                        verdictsCard
                    }
                    ForEach((n?.symbolNews ?? [:]).keys.sorted(), id: \.self) { sym in
                        headlinesCard(n?.symbolNews?[sym] ?? [],
                                      title: "Live headlines — \(sym)",
                                      subtitle: "pulled for a held or watched name")
                    }
                    headlinesCard(n?.general ?? [], title: "General market news",
                                  subtitle: "broad tape the research run reads")
                }
            }
        }
        .task { if n == nil { await model.load(.news) } }
    }

    private var leaderboardCard: some View {
        Card("Source accuracy leaderboard",
             subtitle: "these numbers drive the weights the bot actually scores with") {
            let rows = n?.leaderboard ?? []
            if rows.isEmpty {
                EmptyNote(text: "No source data yet — it accrues from postmortems.",
                          icon: "chart.bar")
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
                                Tag("w \(Fmt.pct((row.weight ?? 0) * 100, dp: 1, signed: false))",
                                    tone: .neutral, size: 10)
                            }
                            MeterBar(value: row.accuracy ?? 0,
                                     tint: (row.accuracy ?? 0) >= 0.5 ? DS.accent2 : DS.down)
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var verdictsCard: some View {
        let verdicts = n?.verdicts ?? []
        if !verdicts.isEmpty {
            Card("Per-trade source verdicts", subtitle: "extracted from each postmortem") {
                VStack(alignment: .leading, spacing: DS.s3) {
                    ForEach(Array(verdicts.prefix(10).enumerated()), id: \.offset) { _, v in
                        VStack(alignment: .leading, spacing: 5) {
                            Text("\(v.file ?? "") \(v.tradeIds?.joined(separator: ", ") ?? "")")
                                .font(DSFont.mono(11)).foregroundStyle(DS.textFaint)
                            SourceChips(sources: v.sources ?? [:])
                        }
                    }
                }
            }
        }
    }

    private var citationsCard: some View {
        Card("What the bot cited", subtitle: "research + review claims, with their kill-switch") {
            let cites = Array((n?.citations ?? []).prefix(25))
            if cites.isEmpty {
                EmptyNote(text: "No citations parsed yet.", icon: "quote.opening")
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(cites.enumerated()), id: \.offset) { i, c in
                        VStack(alignment: .leading, spacing: 4) {
                            HStack(spacing: 7) {
                                Text(c.symbol ?? "—").font(DSFont.bold(14))
                                    .foregroundStyle(DS.text)
                                if let conf = c.confidence {
                                    Tag("conf \(conf)",
                                        tone: conf >= 75 ? .up : conf >= 60 ? .caution : .neutral,
                                        size: 10)
                                }
                                Spacer()
                                Text(c.when ?? c.file ?? "")
                                    .font(DSFont.body(11)).foregroundStyle(DS.textFaint)
                                    .lineLimit(1)
                            }
                            if let s = c.sources, !s.isEmpty {
                                Text("sources: \(s)")
                                    .font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                            }
                            if let note = c.note, !note.isEmpty {
                                Text(note).font(DSFont.body(13)).foregroundStyle(DS.text)
                                    .lineLimit(3)
                            }
                            if let inv = c.invalidates, !inv.isEmpty {
                                Label(inv, systemImage: "exclamationmark.triangle.fill")
                                    .font(DSFont.body(12))
                                    .foregroundStyle(DS.caution)
                                    .lineLimit(2)
                            }
                        }
                        .padding(.vertical, 9)
                        if i < cites.count - 1 {
                            Rectangle().fill(DS.divider).frame(height: 1)
                        }
                    }
                }
            }
        }
    }
}

/// Wrapping right/wrong chips for a postmortem's source verdicts.
struct SourceChips: View {
    let sources: [String: Bool]
    var body: some View {
        let items = sources.sorted { $0.key < $1.key }
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 108), spacing: 6)],
                  alignment: .leading, spacing: 6) {
            ForEach(items, id: \.key) { name, ok in
                Tag(name, tone: ok ? .up : .down, size: 10,
                    icon: ok ? "checkmark" : "xmark")
            }
        }
    }
}

// MARK: - shared headline list

@ViewBuilder
func headlinesCard(_ items: [Headline], title: String, subtitle: String) -> some View {
    if !items.isEmpty {
        Card(title, subtitle: subtitle) {
            VStack(spacing: 0) {
                ForEach(Array(items.prefix(15).enumerated()), id: \.offset) { i, h in
                    VStack(alignment: .leading, spacing: 4) {
                        if let link = h.link, let url = URL(string: link) {
                            Link(destination: url) {
                                Text(h.title ?? link)
                                    .font(DSFont.semibold(14))
                                    .foregroundStyle(DS.text)
                                    .multilineTextAlignment(.leading)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            .buttonStyle(.plain)
                        } else {
                            Text(h.title ?? "")
                                .font(DSFont.semibold(14))
                                .foregroundStyle(DS.text)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        HStack(spacing: 8) {
                            if let src = h.source, !src.isEmpty {
                                Tag(src, tone: .neutral, size: 10)
                            }
                            if let pub = h.published, !pub.isEmpty {
                                Text(pub).font(DSFont.body(11)).foregroundStyle(DS.textFaint)
                            }
                        }
                    }
                    .padding(.vertical, 9)
                    if i < min(items.count, 15) - 1 {
                        Rectangle().fill(DS.divider).frame(height: 1)
                    }
                }
            }
        }
    }
}
