//
//  TradesRiskViews.swift — closed-trade history (sortable/searchable) and the
//  risk & safety panel (stops, overrides, DNT, kill-switch, watchdog).
//

import SwiftUI

// MARK: - Trades

struct TradesView: View {
    @Environment(AppModel.self) private var model
    @State private var search = ""
    @State private var sortNewestFirst = true
    @State private var sortKey: SortKey = .date
    @State private var outcomeFilter: OutcomeFilter = .all
    @State private var postmortem: PostmortemMeta?

    enum SortKey: String, CaseIterable, Hashable {
        case date = "Closed", pnl = "P&L", symbol = "Symbol"
    }
    enum OutcomeFilter: String, CaseIterable, Hashable {
        case all = "All", wins = "Wins", losses = "Losses", stops = "Stopped out"
    }

    private var trades: [ClosedTrade] {
        var rows = model.tradesPayload?.trades ?? []
        switch outcomeFilter {
        case .all: break
        case .wins: rows = rows.filter { $0.outcome == "WIN" }
        case .losses: rows = rows.filter { $0.outcome != "WIN" }
        case .stops: rows = rows.filter { $0.stopLoss == true }
        }
        if !search.isEmpty {
            let q = search.lowercased()
            rows = rows.filter {
                "\($0.symbol ?? "") \($0.outcome ?? "") \($0.exitReason ?? "") \($0.id ?? "")"
                    .lowercased().contains(q)
            }
        }
        rows.sort { a, b in
            let r: Bool = switch sortKey {
            case .date: (a.exitDate ?? "") < (b.exitDate ?? "")
            case .pnl: (a.pnlDollar ?? 0) < (b.pnlDollar ?? 0)
            case .symbol: (a.symbol ?? "") < (b.symbol ?? "")
            }
            return sortNewestFirst ? !r : r
        }
        return rows
    }

    var body: some View {
        Group {
            if model.tradesPayload == nil {
                LoadingScreen(title: "Loading trades…", detail: "closed positions and analyses")
                    .background(DS.bg)
            } else {
                Screen(title: "Trades",
                       subtitle: "\(trades.count) closed trades · every loss and win has an analysis",
                       refresh: { await model.load(.trades) }) {
                    if let err = model.tabErrors[.trades] { ErrorBox(text: err) }
                    controls
                    tableCard
                    analysesCard
                }
            }
        }
        .task { if model.tradesPayload == nil { await model.load(.trades) } }
        .sheet(item: $postmortem) { pm in
            DocumentSheet(title: pm.file, loader: { await model.postmortemText(pm.file) })
        }
    }

    private var controls: some View {
        Card {
            WidthReader(compactBelow: 700) { compact in
                let filters = FilterPills(
                    options: OutcomeFilter.allCases.map { ($0, $0.rawValue) },
                    selection: $outcomeFilter)
                let tools = HStack(spacing: DS.s2) {
                    OrganicField(prompt: "symbol / reason / outcome", text: $search, width: 230)
                    FilterPills(options: SortKey.allCases.map { ($0, $0.rawValue) },
                                selection: $sortKey)
                        .frame(maxWidth: 240)
                    Button {
                        sortNewestFirst.toggle()
                    } label: {
                        Image(systemName: sortNewestFirst ? "arrow.down" : "arrow.up")
                            .font(.system(size: 12, weight: .bold))
                    }
                    .buttonStyle(.organicSoft)
                    .help(sortNewestFirst ? "Descending" : "Ascending")
                }
                if compact {
                    VStack(alignment: .leading, spacing: DS.s3) { filters; tools }
                } else {
                    HStack(spacing: DS.s3) { filters; Spacer(minLength: DS.s2); tools }
                }
            }
        }
    }

    private var tableCard: some View {
        Card(padding: DS.s4) {
            if trades.isEmpty {
                EmptyNote(text: "No trades match.", icon: "list.bullet.rectangle")
            } else {
                VStack(spacing: 0) {
                    THeader(columns: ["Closed", "Ticker", "Trade", "Entry → exit",
                                      "P&L", "Exit reason", ""],
                            weights: [1.2, 1, 1.2, 1.2, 1, 1.4, 0.7],
                            alignments: [.leading, .leading, .leading, .trailing,
                                         .trailing, .leading, .trailing])
                    Rectangle().fill(DS.divider).frame(height: 1)
                    ForEach(trades, id: \.stableId) { t in
                        TradeRow(trade: t, isLast: t.stableId == trades.last?.stableId) {
                            if let file = t.analysisFile {
                                postmortem = PostmortemMeta(file: file, kind: nil,
                                                            title: nil, tradeIds: nil)
                            }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var analysesCard: some View {
        let pms = model.tradesPayload?.postmortems ?? []
        if !pms.isEmpty {
            Card("All analyses", subtitle: "\(pms.count) postmortems and victories on disk") {
                VStack(spacing: 0) {
                    ForEach(pms) { pm in
                        TRow(showsDivider: pm.id != pms.last?.id, onTap: { postmortem = pm }) {
                            Image(systemName: pm.kind == "victory" ? "trophy.fill" : "stethoscope")
                                .font(.system(size: 12))
                                .foregroundStyle(pm.kind == "victory" ? DS.up : DS.down)
                                .frame(width: 18)
                            TCell(weight: 4) {
                                Text(pm.title ?? pm.file)
                                    .font(DSFont.body(13)).foregroundStyle(DS.text).lineLimit(1)
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

struct TradeRow: View {
    let trade: ClosedTrade
    let isLast: Bool
    let openAnalysis: () -> Void

    var body: some View {
        let t = trade
        TRow(showsDivider: !isLast) {
            TCell(weight: 1.2) {
                Text(Fmt.when(t.exitDate)).font(DSFont.num(12))
                    .foregroundStyle(DS.textMuted).lineLimit(1)
            }
            TCell {
                HStack(spacing: 6) {
                    Text(t.symbol ?? "—").font(DSFont.bold(14)).foregroundStyle(DS.text)
                    if t.stopLoss == true { Tag("STOP", tone: .down, size: 9) }
                }
            }
            TCell(weight: 1.2) {
                VStack(alignment: .leading, spacing: 2) {
                    Tag(t.outcome ?? "—", tone: t.outcome == "WIN" ? .up : .down, size: 10)
                    Text("\(Fmt.num(t.shares, dp: 4)) sh · \(t.id ?? "")")
                        .font(DSFont.num(10)).foregroundStyle(DS.textFaint).lineLimit(1)
                }
            }
            TCell(align: .trailing, weight: 1.2) {
                Text("\(Fmt.usd(t.entryPrice)) → \(Fmt.usd(t.exitPrice))")
                    .font(DSFont.num(12)).foregroundStyle(DS.text).lineLimit(1)
            }
            TCell(align: .trailing) {
                PnLPair(dollars: t.pnlDollar, pct: t.pnlPct)
            }
            TCell(weight: 1.4) {
                Text(t.exitReason ?? (t.stopLoss == true ? "stop_loss" : "—"))
                    .font(DSFont.body(12)).foregroundStyle(DS.textMuted).lineLimit(1)
            }
            TCell(align: .trailing, weight: 0.7) {
                if t.analysisFile != nil {
                    Button(action: openAnalysis) {
                        Image(systemName: t.outcome == "WIN" ? "trophy.fill" : "stethoscope")
                            .font(.system(size: 11, weight: .semibold))
                    }
                    .buttonStyle(SoftButtonStyle(tint: t.outcome == "WIN" ? DS.up : DS.down,
                                                 size: 11))
                    .help("Open \(t.outcome == "WIN" ? "victory" : "postmortem") analysis")
                }
            }
        }
    }
}

// MARK: - Risk

struct RiskView: View {
    @Environment(AppModel.self) private var model
    @State private var overrideRow: RiskRow?
    @State private var dntSymbol = ""
    @State private var showHalt = false
    @State private var showClearHalt = false
    @State private var showCashFlow = false
    @State private var actionError: String?

    private var r: RiskPayload? { model.riskPayload }

    var body: some View {
        Group {
            if r == nil {
                LoadingScreen(title: "Loading risk…", detail: "stops, kill-switch, watchdog")
                    .background(DS.bg)
            } else {
                Screen(title: "Risk", subtitle: AppTab.risk.blurb,
                       refresh: { await model.load(.risk) }) {
                    if let err = model.tabErrors[.risk] { ErrorBox(text: err) }
                    if let err = actionError { ErrorBox(text: err) }
                    statTiles
                    killswitchCard
                    stopsCard
                    AdaptivePair(compactBelow: 820) {
                        dntCard
                    } second: {
                        cashFlowCard
                    }
                    watchdogCard
                }
            }
        }
        .task { if r == nil { await model.load(.risk) } }
        .sheet(item: $overrideRow) { row in StopOverrideSheet(row: row) }
        .sheet(isPresented: $showHalt) { HaltSheet() }
        .sheet(isPresented: $showClearHalt) { ClearHaltSheet() }
        .sheet(isPresented: $showCashFlow) {
            CashFlowSheet().onDisappear { Task { await model.load(.risk) } }
        }
    }

    // MARK: tiles

    private var statTiles: some View {
        let ks = r?.killswitch
        let dd = (ks?.drawdown ?? 0) * 100
        let rows = r?.positions ?? []
        let atRisk = rows.filter { ($0.distStopPct ?? 99) < 5 || ($0.distTrailPct ?? 99) < 5 }
        let deployed = rows.reduce(0.0) { $0 + (($1.price ?? 0) * ($1.shares ?? 0)) }
        let total = ks?.current ?? model.overview?.account?.total
        let rm = r?.riskManagement
        return StatRow {
            StatCard(label: "Portfolio exposure",
                     value: total.map { $0 > 0 ? Fmt.pct(deployed / $0 * 100, dp: 1, signed: false) : "—" } ?? "—",
                     delta: Fmt.usd(deployed) + " deployed",
                     deltaTone: .neutral,
                     sublabel: "\(rows.count) positions")
            StatCard(label: "Kill-switch room",
                     value: Fmt.pct(max(0, 25 - dd), dp: 1, signed: false),
                     delta: "drawdown \(Fmt.pct(dd, dp: 1, signed: false))",
                     deltaTone: dd > 15 ? .down : dd > 8 ? .caution : .up,
                     sublabel: "halts at \(Fmt.usd(ks?.haltAtValue))")
            StatCard(label: "Near a stop",
                     value: "\(atRisk.count)",
                     delta: atRisk.isEmpty ? "nothing within 5%" : atRisk.map(\.symbol).joined(separator: ", "),
                     deltaTone: atRisk.isEmpty ? .up : .caution,
                     sublabel: "hard \(Fmt.pct((rm?["stop_loss_pct"]?.numberValue ?? 0.1) * 100, dp: 0, signed: false)) · trail \(Fmt.pct((rm?["trailing_stop_pct"]?.numberValue ?? 0) * 100, dp: 0, signed: false))")
            StatCard(label: "Blocked names",
                     value: "\((r?.dnt ?? []).count)",
                     delta: (r?.dnt ?? []).isEmpty ? "no do-not-trade entries" : (r?.dnt ?? []).joined(separator: ", "),
                     deltaTone: .neutral,
                     sublabel: "blocks BUYs; sells still allowed")
        }
    }

    // MARK: kill-switch

    private var killswitchCard: some View {
        let ks = r?.killswitch
        let dd = (ks?.drawdown ?? 0) * 100
        let halted = r?.bot?.halt?.halted == true
        return Card("Kill-switch — monthly −25% drawdown",
                    subtitle: "account-level dead-man, checked every cycle") {
            VStack(alignment: .leading, spacing: DS.s3) {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 160), spacing: DS.s4)],
                          alignment: .leading, spacing: DS.s2) {
                    MicroStat(key: "Month peak", value: Fmt.usd(ks?.peak))
                    MicroStat(key: "Current", value: Fmt.usd(ks?.current))
                    MicroStat(key: "Halts at", value: Fmt.usd(ks?.haltAtValue), color: DS.down)
                    MicroStat(key: "Drawdown", value: Fmt.pct(dd, dp: 1, signed: false),
                              color: dd > 15 ? DS.down : DS.caution)
                    MicroStat(key: "Headroom", value: Fmt.pct(max(0, 25 - dd), dp: 1, signed: false),
                              color: DS.up)
                }
                MeterBar(value: min(dd, 25) / 25, tint: dd > 15 ? DS.down : DS.caution, height: 10)
                if halted {
                    ErrorBox(text: "HALTED — the bot will not trade; its stop enforcement is OFF (watchdog alerts only). \(r?.bot?.halt?.detail?.prefix(200) ?? "")")
                    Button { showClearHalt = true } label: {
                        Label("Clear HALT", systemImage: "checkmark.shield.fill")
                    }
                    .buttonStyle(.organicPrimary(DS.up))
                } else {
                    Button { showHalt = true } label: {
                        Label("Force HALT now", systemImage: "octagon.fill")
                    }
                    .buttonStyle(.organicSoft(DS.down))
                }
            }
        }
    }

    // MARK: stops

    private var stopsCard: some View {
        let rm = r?.riskManagement
        let stop = (rm?["stop_loss_pct"]?.numberValue ?? 0.1) * 100
        let trail = (rm?["trailing_stop_pct"]?.numberValue ?? 0) * 100
        let ribbon = rm?["exit_on_ribbon_sell"]?.boolValue == true
        let rows = r?.positions ?? []
        return Card("Per-position stops",
                    subtitle: "hard \(Int(stop))% · trail \(Int(trail))% · ribbon-exit \(ribbon ? "ON" : "off (advisory)")") {
            if rows.isEmpty {
                EmptyNote(text: "No open positions.", icon: "shield")
            } else {
                VStack(spacing: 0) {
                    THeader(columns: ["Ticker", "Entry", "Peak", "Now",
                                      "Hard stop", "Trailing stop", ""],
                            weights: [1.2, 1, 1, 1, 1, 1, 0.8],
                            alignments: [.leading, .trailing, .trailing, .trailing,
                                         .trailing, .trailing, .trailing])
                    Rectangle().fill(DS.divider).frame(height: 1)
                    ForEach(rows) { row in
                        TRow(showsDivider: row.id != rows.last?.id) {
                            TCell(weight: 1.2) {
                                HStack(spacing: 6) {
                                    Text(row.symbol).font(DSFont.bold(14))
                                        .foregroundStyle(DS.text)
                                    if row.override_ != nil, row.override_ != JSONValue.null {
                                        Tag("OVERRIDE", tone: .caution, size: 9)
                                    }
                                }
                            }
                            TCell(align: .trailing) {
                                Text(Fmt.usd(row.entry)).font(DSFont.num(12))
                                    .foregroundStyle(DS.textMuted)
                            }
                            TCell(align: .trailing) {
                                Text(Fmt.usd(row.peak)).font(DSFont.num(12))
                                    .foregroundStyle(DS.textMuted)
                            }
                            TCell(align: .trailing) {
                                Text(Fmt.usd(row.price)).font(DSFont.num(13, .semibold))
                                    .foregroundStyle(DS.text)
                            }
                            TCell(align: .trailing) {
                                stopCell(row.stopPrice, row.distStopPct)
                            }
                            TCell(align: .trailing) {
                                stopCell(row.trailPrice, row.distTrailPct)
                            }
                            TCell(align: .trailing, weight: 0.8) {
                                Button("Edit") { overrideRow = row }
                                    .buttonStyle(SoftButtonStyle(tint: DS.accent, size: 12))
                            }
                        }
                    }
                }
                Text("Overrides are honored by the bot next cycle and mirrored into the watchdog's stops.json. Leveraged ETFs already get a tighter effective stop.")
                    .font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                    .padding(.top, DS.s2)
            }
        }
    }

    private func stopCell(_ price: Double?, _ dist: Double?) -> some View {
        VStack(alignment: .trailing, spacing: 2) {
            Text(Fmt.usd(price)).font(DSFont.num(13)).foregroundStyle(DS.text)
            Text(dist != nil ? Fmt.pct(dist, dp: 1, signed: false) + " away" : "—")
                .font(DSFont.num(10))
                .foregroundStyle((dist ?? 99) < 3 ? DS.down : DS.textFaint)
        }
    }

    // MARK: do-not-trade

    private var dntCard: some View {
        Card("Do-not-trade list", subtitle: "blocks bot BUYs · sells are never blocked") {
            VStack(alignment: .leading, spacing: DS.s3) {
                HStack(spacing: DS.s2) {
                    OrganicField(prompt: "SYMBOL", text: $dntSymbol, mono: true, width: 150)
                    Button("Block") {
                        let sym = dntSymbol.trimmingCharacters(in: .whitespaces).uppercased()
                        guard !sym.isEmpty else { return }
                        model.requireArm {
                            Task {
                                actionError = await model.setDNT(symbol: sym, blocked: true)
                                dntSymbol = ""
                                await model.load(.risk)
                            }
                        }
                    }
                    .buttonStyle(.organicPrimary(DS.down))
                    Spacer()
                }
                if (r?.dnt ?? []).isEmpty {
                    EmptyNote(text: "Empty — the bot may buy anything its strategy allows.",
                              icon: "nosign")
                } else {
                    ChipCloud(items: r?.dnt ?? [], minWidth: 96) { sym in
                        Button {
                            model.requireArm {
                                Task {
                                    actionError = await model.setDNT(symbol: sym, blocked: false)
                                    await model.load(.risk)
                                }
                            }
                        } label: {
                            Tag(sym, tone: .down, size: 11, icon: "xmark.circle.fill")
                        }
                        .buttonStyle(.plain)
                        .help("Unblock \(sym)")
                    }
                }
            }
        }
    }

    // MARK: cash flow

    private var cashFlowCard: some View {
        Card("Deposits & withdrawals", subtitle: "keeps the performance % honest") {
            VStack(alignment: .leading, spacing: DS.s3) {
                Button { showCashFlow = true } label: {
                    Label("Log deposit / withdrawal", systemImage: "plus.forwardslash.minus")
                }
                .buttonStyle(.organicSoft)
                let flows = r?.cashFlows ?? []
                if flows.isEmpty {
                    EmptyNote(text: "No declared deposits or withdrawals yet.", icon: "banknote")
                } else {
                    VStack(spacing: 0) {
                        let shown = Array(flows.prefix(6))
                        ForEach(shown) { f in
                            TRow(showsDivider: f.id != shown.last?.id) {
                                TCell(weight: 1.4) {
                                    Text((f.ts ?? "").prefix(16))
                                        .font(DSFont.num(11)).foregroundStyle(DS.textFaint)
                                }
                                TCell(weight: 2) {
                                    Text(f.note ?? "").font(DSFont.body(12))
                                        .foregroundStyle(DS.textMuted).lineLimit(1)
                                }
                                TCell(align: .trailing) {
                                    Text(Fmt.usdSigned(f.amount))
                                        .font(DSFont.num(13, .bold))
                                        .foregroundStyle((f.amount ?? 0) >= 0 ? DS.up : DS.down)
                                }
                                TCell(align: .trailing) {
                                    Tag(f.applied == true ? "applied" : "pending",
                                        tone: f.applied == true ? .neutral : .caution, size: 10)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // MARK: watchdog

    private var watchdogCard: some View {
        let lines = r?.watchdogLog ?? []
        return Card("Watchdog",
                    subtitle: "independent launchd dead-man · stops.json updated \((r?.stopsFile?["updated"]?.stringValue ?? "—").prefix(16))") {
            if lines.isEmpty {
                EmptyNote(text: "No watchdog log lines yet.", icon: "dog")
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                            Text(line)
                                .font(DSFont.mono(11))
                                .foregroundStyle(line.contains("ALERT") ? DS.down : DS.consoleText)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    .padding(DS.s3)
                }
                .frame(maxHeight: 200)
                .background(DS.consoleBG,
                            in: RoundedRectangle(cornerRadius: DS.rMd, style: .continuous))
            }
        }
    }
}
