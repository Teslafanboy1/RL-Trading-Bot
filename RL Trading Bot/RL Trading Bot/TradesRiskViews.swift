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
    @State private var postmortem: PostmortemMeta?

    enum SortKey: String, CaseIterable { case date = "Closed", pnl = "P&L", symbol = "Symbol" }

    private var trades: [ClosedTrade] {
        var rows = model.tradesPayload?.trades ?? []
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
        List {
            if let err = model.tabErrors[.trades] { ErrorBox(text: err) }
            Section {
                ForEach(trades, id: \.stableId) { t in
                    TradeRow(trade: t) {
                        if let file = t.analysisFile {
                            postmortem = PostmortemMeta(file: file, kind: nil, title: nil,
                                                        tradeIds: nil)
                        }
                    }
                }
            } header: {
                HStack {
                    Text("\(trades.count) closed trades")
                    Spacer()
                    Picker("Sort", selection: $sortKey) {
                        ForEach(SortKey.allCases, id: \.self) { Text($0.rawValue) }
                    }
                    .pickerStyle(.menu)
                    Button {
                        sortNewestFirst.toggle()
                    } label: {
                        Image(systemName: sortNewestFirst ? "arrow.down" : "arrow.up")
                    }
                    .buttonStyle(.borderless)
                }
            }
            if let pms = model.tradesPayload?.postmortems, !pms.isEmpty {
                Section("All analyses") {
                    ForEach(pms) { pm in
                        Button {
                            postmortem = pm
                        } label: {
                            Label(pm.file, systemImage: pm.kind == "victory" ? "trophy.fill" : "stethoscope")
                                .font(.callout.monospaced())
                        }
                    }
                }
            }
        }
        .searchable(text: $search, prompt: "symbol / reason / outcome")
        .refreshable { await model.load(.trades) }
        .task { if model.tradesPayload == nil { await model.load(.trades) } }
        .sheet(item: $postmortem) { pm in
            DocumentSheet(title: pm.file, loader: { await model.postmortemText(pm.file) })
        }
        .overlay {
            if model.tradesPayload == nil {
                ContentUnavailableView("Loading trades…", systemImage: "list.bullet")
            }
        }
    }
}

struct TradeRow: View {
    let trade: ClosedTrade
    let openAnalysis: () -> Void

    var body: some View {
        let t = trade
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(t.symbol ?? "—").font(.headline.monospaced())
                    if t.stopLoss == true {
                        Text("STOP").font(.caption2.bold())
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Color.down.opacity(0.18), in: Capsule())
                            .foregroundStyle(Color.down)
                    }
                }
                Text("\(t.id ?? "") · \(t.exitReason ?? (t.stopLoss == true ? "stop_loss" : "—")) · \(Fmt.when(t.exitDate))")
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                PnLText(value: t.pnlDollar, pct: t.pnlPct, font: .callout)
                Text("\(Fmt.usd(t.entryPrice)) → \(Fmt.usd(t.exitPrice))")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            if t.analysisFile != nil {
                Button {
                    openAnalysis()
                } label: {
                    Image(systemName: t.outcome == "WIN" ? "trophy.fill" : "stethoscope")
                }
                .buttonStyle(.bordered).controlSize(.small)
                .help("Open \(t.outcome == "WIN" ? "victory" : "postmortem") analysis")
            }
        }
        .padding(.vertical, 2)
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
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let err = model.tabErrors[.risk] { ErrorBox(text: err) }
                if let err = actionError { ErrorBox(text: err) }
                killswitchCard
                cashFlowCard
                stopsCard
                dntCard
                watchdogCard
            }
            .padding()
        }
        .opacity(r == nil ? 0 : 1)
        .refreshable { await model.load(.risk) }
        .task { if r == nil { await model.load(.risk) } }
        .sheet(item: $overrideRow) { row in StopOverrideSheet(row: row) }
        .sheet(isPresented: $showHalt) { HaltSheet() }
        .sheet(isPresented: $showClearHalt) { ClearHaltSheet() }
        .sheet(isPresented: $showCashFlow) {
            CashFlowSheet()
                .onDisappear { Task { await model.load(.risk) } }
        }
        .overlay {
            if r == nil { ContentUnavailableView("Loading risk…", systemImage: "shield") }
        }
    }

    private var killswitchCard: some View {
        GroupBox {
            let ks = r?.killswitch
            let dd = (ks?.drawdown ?? 0) * 100
            let halted = r?.bot?.halt?.halted == true
            VStack(alignment: .leading, spacing: 8) {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 220), spacing: 20)],
                          alignment: .leading, spacing: 4) {
                    KeyValueRow(key: "month peak", value: Fmt.usd(ks?.peak))
                    KeyValueRow(key: "current", value: Fmt.usd(ks?.current))
                    KeyValueRow(key: "halts at", value: Fmt.usd(ks?.haltAtValue))
                    KeyValueRow(key: "drawdown", value: Fmt.pct(dd, dp: 1, signed: false),
                                valueColor: dd > 15 ? .down : .caution)
                    KeyValueRow(key: "headroom", value: Fmt.pct(25 - dd, dp: 1, signed: false))
                }
                Gauge(value: min(dd, 25), in: 0...25) { EmptyView() }
                    .gaugeStyle(.accessoryLinearCapacity)
                    .tint(dd > 15 ? .down : .caution)
                if halted {
                    ErrorBox(text: "HALTED — the bot will not trade; its stop enforcement is OFF (watchdog alerts only). Detail: \(r?.bot?.halt?.detail?.prefix(200) ?? "")")
                    Button { showClearHalt = true } label: {
                        Label("Clear HALT", systemImage: "checkmark.shield.fill")
                    }
                    .buttonStyle(.borderedProminent).tint(.up)
                } else {
                    Button(role: .destructive) { showHalt = true } label: {
                        Label("Force HALT now", systemImage: "octagon.fill")
                    }
                    .buttonStyle(.bordered).tint(.down)
                }
            }
        } label: { Label("Kill-switch — monthly −25% drawdown", systemImage: "bolt.shield.fill") }
    }

    private var cashFlowCard: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                Button { showCashFlow = true } label: {
                    Label("Log deposit / withdrawal", systemImage: "plus.forwardslash.minus")
                }
                .buttonStyle(.bordered)
                let flows = r?.cashFlows ?? []
                if flows.isEmpty {
                    Text("no declared deposits/withdrawals yet")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    VStack(spacing: 0) {
                        ForEach(flows.prefix(6)) { f in
                            HStack {
                                Text((f.ts ?? "").prefix(16))
                                    .font(.caption2.monospaced()).foregroundStyle(.secondary)
                                if let note = f.note, !note.isEmpty {
                                    Text(note).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                                }
                                Spacer()
                                Text(Fmt.usdSigned(f.amount))
                                    .font(.caption.monospacedDigit().bold())
                                    .foregroundStyle((f.amount ?? 0) >= 0 ? Color.up : Color.down)
                                Text(f.applied == true ? "applied" : "pending")
                                    .font(.caption2)
                                    .foregroundStyle(f.applied == true ? .secondary : Color.caution)
                            }
                            .padding(.vertical, 4)
                            if f.id != flows.prefix(6).last?.id { Divider() }
                        }
                    }
                }
            }
        } label: {
            Label("Deposits & withdrawals — keeps performance % honest", systemImage: "banknote.fill")
        }
    }

    private var stopsCard: some View {
        GroupBox {
            VStack(spacing: 0) {
                ForEach(r?.positions ?? []) { row in
                    riskRow(row)
                        .padding(.vertical, 6)
                    if row.id != r?.positions?.last?.id { Divider() }
                }
                if (r?.positions ?? []).isEmpty {
                    Text("no open positions").foregroundStyle(.secondary).padding(.vertical, 10)
                }
            }
            Text("Overrides are honored by the bot next cycle and mirrored into the watchdog's stops.json. Leveraged ETFs already get a tighter effective stop.")
                .font(.caption2).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        } label: {
            let rm = r?.riskManagement
            let stop = (rm?["stop_loss_pct"]?.numberValue ?? 0.1) * 100
            let trail = (rm?["trailing_stop_pct"]?.numberValue ?? 0) * 100
            let ribbon = rm?["exit_on_ribbon_sell"]?.boolValue == true
            Label("Per-position stops — hard \(Int(stop))% · trail \(Int(trail))% · ribbon-exit \(ribbon ? "ON" : "off (advisory)")",
                  systemImage: "target")
                .lineLimit(1)
                .truncationMode(.tail)
        }
    }

    @ViewBuilder
    private func riskRow(_ row: RiskRow) -> some View {
        let symbolBlock = VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Text(row.symbol).font(.subheadline.bold().monospaced())
                if row.override_ != nil && row.override_ != JSONValue.null {
                    Text("OVERRIDE").font(.caption2.bold())
                        .padding(.horizontal, 5).padding(.vertical, 2)
                        .background(Color.caution.opacity(0.18), in: Capsule())
                        .foregroundStyle(Color.caution)
                }
            }
            Text("entry \(Fmt.usd(row.entry)) · peak \(Fmt.usd(row.peak)) · now \(Fmt.usd(row.price))")
                .font(.caption2.monospaced()).foregroundStyle(.secondary)
                .lineLimit(1)
        }
        let cells = HStack(spacing: 12) {
            stopCell("stop", row.stopPrice, row.distStopPct, dangerBelow: 3)
            stopCell("trail", row.trailPrice, row.distTrailPct, dangerBelow: 3)
            Button("Edit") { overrideRow = row }
                .buttonStyle(.bordered).controlSize(.small)
        }
        WidthReader(compactBelow: 540) { compact in
            if compact {
                VStack(alignment: .leading, spacing: 6) {
                    symbolBlock
                    cells
                }
            } else {
                HStack(spacing: 12) {
                    symbolBlock
                    Spacer(minLength: 8)
                    cells
                }
            }
        }
    }

    private func stopCell(_ label: String, _ price: Double?, _ dist: Double?,
                          dangerBelow: Double) -> some View {
        VStack(alignment: .trailing, spacing: 2) {
            Text(Fmt.usd(price)).font(.caption.monospacedDigit())
            Text(dist != nil ? "\(label) \(Fmt.pct(dist, dp: 1, signed: false))" : label)
                .font(.caption2.monospacedDigit())
                .foregroundStyle((dist ?? 99) < dangerBelow ? Color.down : Color.secondary)
        }
        .frame(minWidth: 76, alignment: .trailing)
    }

    private var dntCard: some View {
        GroupBox {
            HStack {
                TextField("SYMBOL", text: $dntSymbol)
                    .font(.body.monospaced())
                    .textCase(.uppercase)
                    .frame(maxWidth: 140)
                    #if !os(macOS)
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                    #endif
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
                .buttonStyle(.bordered)
                Spacer()
            }
            if (r?.dnt ?? []).isEmpty {
                Text("empty — the bot may buy anything its strategy allows")
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack {
                        ForEach(r?.dnt ?? [], id: \.self) { sym in
                            Button {
                                model.requireArm {
                                    Task {
                                        actionError = await model.setDNT(symbol: sym, blocked: false)
                                        await model.load(.risk)
                                    }
                                }
                            } label: {
                                Label(sym, systemImage: "xmark.circle.fill")
                                    .font(.caption.bold().monospaced())
                            }
                            .buttonStyle(.bordered).controlSize(.small)
                            .help("Unblock \(sym)")
                        }
                    }
                }
            }
        } label: {
            Label("Do-not-trade list — blocks bot BUYs; sells still allowed",
                  systemImage: "nosign")
        }
    }

    private var watchdogCard: some View {
        GroupBox {
            let lines = r?.watchdogLog ?? []
            if lines.isEmpty {
                Text("(no watchdog log lines yet)").font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                ScrollView {
                    Text(lines.joined(separator: "\n"))
                        .font(.caption2.monospaced())
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
                .frame(maxHeight: 180)
            }
        } label: {
            Label("Watchdog (independent, launchd) — stops.json updated \((r?.stopsFile?["updated"]?.stringValue ?? "—").prefix(16))",
                  systemImage: "dog.fill")
        }
    }
}
