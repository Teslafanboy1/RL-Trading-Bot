//
//  PositionsOrdersViews.swift — the deep position manager (stops, giveback,
//  per-position actions) and the order center (working orders, history,
//  cancel, journal).
//

import SwiftUI

// MARK: - Positions

struct PositionsView: View {
    @Environment(AppModel.self) private var model
    @State private var expanded: Set<String> = []
    @State private var overrideRow: RiskRow?
    @State private var actionError: String?

    private var positions: [Position] { model.overview?.positions ?? [] }

    var body: some View {
        Screen(title: "Positions",
               subtitle: positions.isEmpty
                    ? "The bot is flat — nothing held right now."
                    : "\(positions.count) open positions · click a row for the agent's thesis",
               refresh: { await model.load(.positions) }) {
            if let err = model.tabErrors[.positions] { ErrorBox(text: err) }
            if let err = actionError { ErrorBox(text: err) }
            statTiles
            tableCard
        }
        .task { if model.overview == nil { await model.load(.positions) } }
        .sheet(item: $overrideRow) { row in StopOverrideSheet(row: row) }
    }

    private var statTiles: some View {
        let value = positions.reduce(0.0) { $0 + (($1.price ?? 0) * ($1.shares ?? 0)) }
        let unreal = positions.compactMap(\.unrealizedPnl).reduce(0, +)
        let day = model.overview?.dayPnlPositions
        let cash = model.overview?.account?.cash
        return StatRow {
            StatCard(label: "Positions", value: "\(positions.count)",
                     sublabel: "held at the broker")
            StatCard(label: "Market value", value: Fmt.usd(value),
                     sublabel: "at last quote")
            StatCard(label: "Day P&L", value: Fmt.usdSigned(day),
                     deltaTone: .forValue(day), sublabel: "open positions only")
            StatCard(label: "Unrealized", value: Fmt.usdSigned(unreal),
                     deltaTone: .forValue(unreal),
                     sublabel: "settled cash \(Fmt.usd(cash))")
        }
    }

    private var tableCard: some View {
        Card(padding: DS.s4) {
            if positions.isEmpty {
                EmptyNote(text: "No open positions — the bot is flat.", icon: "tray")
            } else {
                VStack(spacing: 0) {
                    THeader(columns: ["Ticker", "Qty", "Avg price", "Current",
                                      "Market value", "P&L", "Signal"],
                            weights: [1.4, 1, 1, 1, 1, 1, 1.2],
                            alignments: [.leading, .trailing, .trailing, .trailing,
                                         .trailing, .trailing, .trailing])
                    Rectangle().fill(DS.divider).frame(height: 1)
                    ForEach(positions, id: \.stableId) { pos in
                        PositionTableRow(
                            position: pos,
                            riskRow: riskRow(for: pos.symbol),
                            isExpanded: expanded.contains(pos.stableId),
                            isLast: pos.stableId == positions.last?.stableId,
                            toggle: {
                                if expanded.contains(pos.stableId) { expanded.remove(pos.stableId) }
                                else { expanded.insert(pos.stableId) }
                            },
                            editStops: {
                                overrideRow = riskRow(for: pos.symbol) ?? syntheticRiskRow(pos)
                            },
                            onError: { actionError = $0 })
                    }
                }
            }
        }
    }

    private func riskRow(for symbol: String) -> RiskRow? {
        model.riskPayload?.positions?.first { $0.symbol == symbol }
    }

    /// Risk payload may lag the overview (separate fetch) — build a usable
    /// stand-in from what the overview already knows so the sheet still opens.
    private func syntheticRiskRow(_ p: Position) -> RiskRow {
        RiskRow(symbol: p.symbol, shares: p.shares, entry: p.entryPrice,
                peak: p.peakPrice, price: p.price,
                stopPrice: p.stop?.stopPrice, trailPrice: p.stop?.trailPrice,
                distStopPct: nil, distTrailPct: nil,
                override_: p.stop?.override_, watchdogStop: nil)
    }
}

struct PositionTableRow: View {
    @Environment(AppModel.self) private var model
    let position: Position
    let riskRow: RiskRow?
    let isExpanded: Bool
    let isLast: Bool
    let toggle: () -> Void
    let editStops: () -> Void
    let onError: (String?) -> Void

    var body: some View {
        VStack(spacing: 0) {
            summaryRow
            if isExpanded { detail }
            if !isLast || isExpanded {
                Rectangle().fill(DS.divider).frame(height: 1)
            }
        }
    }

    private var summaryRow: some View {
        let p = position
        return WeightedRow {
            TCell(weight: 1.4) {
                HStack(spacing: 8) {
                    Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(DS.textFaint)
                        .frame(width: 12)
                    Text(p.symbol).font(DSFont.bold(14)).foregroundStyle(DS.text)
                    if p.dnt == true { Tag("DNT", tone: .caution, size: 9) }
                    if p.locked == true { Tag("LOCKED", tone: .caution, size: 9) }
                    if p.adopted == true { Tag("ADOPTED", tone: .neutral, size: 9) }
                }
            }
            TCell(align: .trailing) {
                Text(Fmt.num(p.shares, dp: 4)).font(DSFont.num(13)).foregroundStyle(DS.text)
            }
            TCell(align: .trailing) {
                Text(Fmt.usd(p.entryPrice)).font(DSFont.num(13)).foregroundStyle(DS.text)
            }
            TCell(align: .trailing) {
                Text(Fmt.usd(p.price)).font(DSFont.num(13)).foregroundStyle(DS.text)
            }
            TCell(align: .trailing) {
                Text(Fmt.usd((p.price ?? 0) * (p.shares ?? 0)))
                    .font(DSFont.num(13, .semibold)).foregroundStyle(DS.text)
            }
            TCell(align: .trailing) {
                PnLPair(dollars: p.unrealizedPnl, pct: p.unrealizedPct)
            }
            TCell(align: .trailing, weight: 1.2) {
                HStack(spacing: 6) {
                    Sparkline(points: p.spark ?? [], width: 56, height: 20)
                    RibbonBadge(state: p.emaState)
                }
            }
        }
        .padding(.vertical, 12)
        .contentShape(Rectangle())
        .onTapGesture(perform: toggle)
    }

    private var detail: some View {
        let p = position
        let peak = p.peakPrice ?? p.entryPrice ?? 0
        let giveback = (peak > 0 && p.price != nil)
            ? max(0, (peak - p.price!) / peak * 100) : nil
        return VStack(alignment: .leading, spacing: DS.s3) {
            WidthReader(compactBelow: 620) { compact in
                LazyVGrid(columns: [GridItem(.adaptive(minimum: compact ? 130 : 155),
                                             spacing: DS.s3)],
                          alignment: .leading, spacing: DS.s3) {
                    MicroStat(key: "Opened", value: Fmt.when(p.entryDate))
                    MicroStat(key: "Day P&L", value: Fmt.usdSigned(p.dayPnl),
                              color: p.dayPnl.pnlColor)
                    MicroStat(key: "Peak", value: Fmt.usd(p.peakPrice))
                    MicroStat(key: "Giveback off peak",
                              value: giveback.map { Fmt.pct($0, dp: 1, signed: false) } ?? "—",
                              color: (giveback ?? 0) > 15 ? DS.caution : nil)
                    MicroStat(key: "Hard stop", value: Fmt.usd(p.stop?.stopPrice), color: DS.down)
                    MicroStat(key: "Trailing stop", value: Fmt.usd(p.stop?.trailPrice),
                              color: DS.caution)
                    MicroStat(key: "Dist. to stop",
                              value: riskRow?.distStopPct.map { Fmt.pct($0, dp: 1, signed: false) } ?? "—")
                    MicroStat(key: "Dist. to trail",
                              value: riskRow?.distTrailPct.map { Fmt.pct($0, dp: 1, signed: false) } ?? "—")
                    MicroStat(key: "Confidence", value: p.confidence.map { "\($0)/100" } ?? "—")
                    MicroStat(key: "Signal",
                              value: "\(p.emaState ?? "—") / \(p.emaTransition ?? "—")")
                }
            }
            if let thesis = p.thesis, !thesis.isEmpty {
                Text(thesis)
                    .font(DSFont.body(13))
                    .foregroundStyle(DS.textMuted)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            actionRow
        }
        .padding(DS.s4)
        .background(DS.neutral(1),
                    in: RoundedRectangle(cornerRadius: DS.rMd, style: .continuous))
        .padding(.bottom, DS.s3)
    }

    private var actionRow: some View {
        let p = position
        return LazyVGrid(columns: [GridItem(.adaptive(minimum: 118), spacing: DS.s2)],
                         spacing: DS.s2) {
            Button {
                model.orderPrefill = .init(symbol: p.symbol, side: "buy")
                model.showOrderSheet = true
            } label: { Label("Buy", systemImage: "plus.circle").frame(maxWidth: .infinity) }
                .buttonStyle(SoftButtonStyle(tint: DS.up, size: 12))
            Button {
                model.orderPrefill = .init(symbol: p.symbol, side: "sell", quantity: p.shares)
                model.showOrderSheet = true
            } label: { Label("Sell", systemImage: "minus.circle").frame(maxWidth: .infinity) }
                .buttonStyle(SoftButtonStyle(tint: DS.down, size: 12))
            Button(action: editStops) {
                Label("Stops", systemImage: "shield.lefthalf.filled").frame(maxWidth: .infinity)
            }
            .buttonStyle(SoftButtonStyle(tint: DS.caution, size: 12))
            Button {
                let blocking = p.dnt != true
                model.requireArm {
                    Task { onError(await model.setDNT(symbol: p.symbol, blocked: blocking)) }
                }
            } label: {
                Label(p.dnt == true ? "Allow buys" : "Block buys",
                      systemImage: p.dnt == true ? "checkmark.circle" : "nosign")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(SoftButtonStyle(tint: DS.text, size: 12))
            Button { model.analyze(p.symbol) } label: {
                Label("Analyze", systemImage: "waveform.and.magnifyingglass")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(SoftButtonStyle(tint: DS.accent, size: 12))
        }
    }
}

// MARK: - Orders

struct OrdersView: View {
    @Environment(AppModel.self) private var model
    @State private var cancelTarget: BrokerOrder?
    @State private var actionError: String?
    @State private var filter: OrderFilter = .all

    enum OrderFilter: String, Hashable, CaseIterable {
        case all = "All", working = "Working", filled = "Filled", cancelled = "Cancelled"
    }

    private var payload: OrdersPayload? { model.ordersPayload }

    private static let terminal: Set<String> = ["filled", "cancelled", "canceled",
                                                "rejected", "failed", "expired"]

    private var allOrders: [BrokerOrder] {
        (payload?.today ?? []) + (model.orderHistoryDays > 1 ? (payload?.history ?? []) : [])
    }

    private var visible: [BrokerOrder] {
        allOrders.filter { o in
            let s = (o.state ?? "").lowercased()
            switch filter {
            case .all: return true
            case .working: return !Self.terminal.contains(s)
            case .filled: return s == "filled"
            case .cancelled: return ["cancelled", "canceled", "rejected", "failed",
                                     "expired"].contains(s)
            }
        }
    }

    private var workingCount: Int {
        allOrders.filter { !Self.terminal.contains(($0.state ?? "").lowercased()) }.count
    }

    var body: some View {
        Screen(title: "Orders",
               subtitle: "\(workingCount) working · \(allOrders.count) in view · cancel needs an armed session",
               refresh: { await model.load(.orders) }) {
            if let err = model.tabErrors[.orders] { ErrorBox(text: err) }
            if let err = actionError { ErrorBox(text: err) }
            headerCard
            FilterPills(options: OrderFilter.allCases.map { ($0, $0.rawValue) },
                        selection: $filter)
            tableCard
            journalCard
        }
        .task { if payload == nil { await model.load(.orders) } }
        .confirmationDialog("Cancel this order?", isPresented: .init(
            get: { cancelTarget != nil },
            set: { if !$0 { cancelTarget = nil } })) {
            Button("Cancel order", role: .destructive) {
                if let o = cancelTarget, let id = o.id {
                    model.requireArm {
                        Task { actionError = await model.cancelOrder(id: id) }
                    }
                }
                cancelTarget = nil
            }
            Button("Keep order", role: .cancel) { cancelTarget = nil }
        } message: {
            Text("\(cancelTarget?.side ?? "") \(cancelTarget?.symbol ?? "") — this sends a broker cancel request.")
        }
    }

    private var headerCard: some View {
        @Bindable var model = model
        return Card {
            VStack(alignment: .leading, spacing: DS.s3) {
                WidthReader(compactBelow: 620) { compact in
                    let status = HStack(spacing: DS.s2) {
                        StatusDot(color: payload?.broker?.mode == "direct-mcp" ? DS.up : DS.caution,
                                  label: payload?.broker?.mode ?? "broker?")
                        Text("updated \(Fmt.when(payload?.updated))")
                            .font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                            .lineLimit(1)
                    }
                    let actions = HStack(spacing: DS.s2) {
                        Button {
                            Task {
                                actionError = await model.brokerRefresh()
                                await model.load(.orders)
                            }
                        } label: { Label("Refresh", systemImage: "arrow.clockwise") }
                            .buttonStyle(.organicSoft)
                        Button {
                            model.orderPrefill = .init()
                            model.showOrderSheet = true
                        } label: { Label("New order", systemImage: "plus.circle.fill") }
                            .buttonStyle(.organicPrimary)
                    }
                    if compact {
                        VStack(alignment: .leading, spacing: DS.s2) { status; actions }
                    } else {
                        HStack(spacing: DS.s2) { status; Spacer(minLength: DS.s2); actions }
                    }
                }
                HStack(spacing: DS.s2) {
                    Text("Lookback").font(DSFont.body(13)).foregroundStyle(DS.textMuted)
                    FilterPills(options: [(1, "Today"), (7, "7 days"), (30, "30 days")],
                                selection: $model.orderHistoryDays)
                        .onChange(of: model.orderHistoryDays) { _, _ in
                            Task { await model.load(.orders) }
                        }
                }
                if model.orderHistoryDays > 1, payload?.broker?.mode != "direct-mcp" {
                    Text("Deeper history needs the direct broker connection (rh_login) — the claude-cli path only snapshots today.")
                        .font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                }
                if let err = payload?.historyError { WarningBox(text: err) }
            }
        }
    }

    private var tableCard: some View {
        Card(padding: DS.s4) {
            if visible.isEmpty {
                EmptyNote(text: "No orders match this filter.", icon: "tray")
            } else {
                VStack(spacing: 0) {
                    THeader(columns: ["Order", "Ticker", "Side", "Type", "Qty",
                                      "Price", "Status", "Time"],
                            weights: [1.2, 1, 1, 1, 1, 1, 1, 1.4],
                            alignments: [.leading, .leading, .leading, .leading,
                                         .trailing, .trailing, .trailing, .trailing])
                    Rectangle().fill(DS.divider).frame(height: 1)
                    ForEach(visible, id: \.stableId) { o in
                        orderRow(o, isLast: o.stableId == visible.last?.stableId)
                    }
                }
            }
        }
    }

    private func orderRow(_ o: BrokerOrder, isLast: Bool) -> some View {
        let side = (o.side ?? "").lowercased()
        let cancellable = !Self.terminal.contains((o.state ?? "").lowercased())
        return TRow(showsDivider: !isLast) {
            TCell(weight: 1.2) {
                Text(String((o.id ?? "—").prefix(10)))
                    .font(DSFont.mono(11)).foregroundStyle(DS.textFaint).lineLimit(1)
            }
            TCell {
                Text(o.symbol ?? "—").font(DSFont.bold(14)).foregroundStyle(DS.text)
            }
            TCell {
                Text(side.uppercased())
                    .font(DSFont.semibold(12))
                    .foregroundStyle(side == "buy" ? DS.up : DS.down)
            }
            TCell {
                Text(o.type ?? "—").font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                    .lineLimit(1)
            }
            TCell(align: .trailing) {
                Text(o.quantity != nil ? Fmt.num(o.quantity, dp: 5) : Fmt.usd(o.notional))
                    .font(DSFont.num(13)).foregroundStyle(DS.text)
            }
            TCell(align: .trailing) {
                Text(o.price != nil ? Fmt.usd(o.price) : "—")
                    .font(DSFont.num(13)).foregroundStyle(DS.text)
            }
            TCell(align: .trailing) {
                Tag(o.state ?? "—", tone: stateTone(o.state))
            }
            TCell(align: .trailing, weight: 1.4) {
                HStack(spacing: 6) {
                    Text(Fmt.when(o.createdAt))
                        .font(DSFont.num(11)).foregroundStyle(DS.textMuted).lineLimit(1)
                    if cancellable {
                        Button { cancelTarget = o } label: {
                            Image(systemName: "xmark")
                                .font(.system(size: 10, weight: .bold))
                        }
                        .buttonStyle(SoftButtonStyle(tint: DS.down, size: 11))
                        .help("Cancel this order")
                    }
                }
            }
        }
    }

    private func stateTone(_ s: String?) -> TagTone {
        switch (s ?? "").lowercased() {
        case "filled": .up
        case "cancelled", "canceled", "rejected", "failed", "expired": .neutral
        default: .caution
        }
    }

    @ViewBuilder
    private var journalCard: some View {
        let journal = payload?.journal ?? []
        if !journal.isEmpty {
            Card("Dashboard order journal",
                 subtitle: "every action this app took — \u{201C}did I do that, or the bot?\u{201D}") {
                VStack(spacing: 0) {
                    ForEach(Array(journal.enumerated()), id: \.offset) { i, a in
                        let ok = a.result?["ok"]?.boolValue != false
                        TRow(showsDivider: i < journal.count - 1) {
                            Image(systemName: ok ? "checkmark.circle" : "xmark.octagon.fill")
                                .font(.system(size: 12))
                                .foregroundStyle(ok ? DS.up : DS.down)
                                .frame(width: 16)
                            TCell {
                                Text(a.action ?? "—")
                                    .font(DSFont.semibold(13)).foregroundStyle(DS.text)
                            }
                            TCell(weight: 3) {
                                Text(a.params?.compact ?? "")
                                    .font(DSFont.mono(11)).foregroundStyle(DS.textMuted)
                                    .lineLimit(2)
                            }
                            TCell(align: .trailing) {
                                Text(Fmt.when(a.ts))
                                    .font(DSFont.num(11)).foregroundStyle(DS.textFaint)
                            }
                        }
                    }
                }
            }
        }
    }
}
