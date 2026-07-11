//
//  PositionsOrdersViews.swift — deep position manager (stops, giveback,
//  per-position actions) and the order center (working orders, history,
//  cancel, journal).
//

import SwiftUI
import Charts

// MARK: - Positions

struct PositionsView: View {
    @Environment(AppModel.self) private var model
    @State private var expanded: Set<String> = []
    @State private var overrideRow: RiskRow?
    @State private var actionError: String?

    private var positions: [Position] { model.overview?.positions ?? [] }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let err = model.tabErrors[.positions] { ErrorBox(text: err) }
                if let err = actionError { ErrorBox(text: err) }
                statStrip
                if positions.isEmpty {
                    ContentUnavailableView("No open positions",
                                           systemImage: "tray",
                                           description: Text("The bot is flat — nothing held right now."))
                        .frame(maxWidth: .infinity)
                } else {
                    ForEach(positions, id: \.stableId) { pos in
                        PositionManagerCard(
                            position: pos,
                            riskRow: riskRow(for: pos.symbol),
                            isExpanded: expanded.contains(pos.stableId),
                            toggle: {
                                if expanded.contains(pos.stableId) { expanded.remove(pos.stableId) }
                                else { expanded.insert(pos.stableId) }
                            },
                            editStops: { overrideRow = riskRow(for: pos.symbol) ?? syntheticRiskRow(pos) },
                            onError: { actionError = $0 })
                    }
                }
            }
            .padding()
        }
        .refreshable { await model.load(.positions) }
        .task { if model.overview == nil { await model.load(.positions) } }
        .sheet(item: $overrideRow) { row in
            StopOverrideSheet(row: row)
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

    private var statStrip: some View {
        let value = positions.reduce(0.0) { $0 + (($1.price ?? 0) * ($1.shares ?? 0)) }
        let unreal = positions.compactMap(\.unrealizedPnl).reduce(0, +)
        let day = model.overview?.dayPnlPositions
        let cash = model.overview?.account?.cash
        return GroupBox {
            WidthReader(compactBelow: 620) { compact in
                let cols = [GridItem(.adaptive(minimum: compact ? 130 : 150), spacing: 12)]
                LazyVGrid(columns: cols, alignment: .leading, spacing: 10) {
                    stat("Positions", "\(positions.count)")
                    stat("Market value", Fmt.usd(value))
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Day P&L").font(.caption).foregroundStyle(.secondary)
                        PnLText(value: day, font: .headline)
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Unrealized").font(.caption).foregroundStyle(.secondary)
                        PnLText(value: unreal, font: .headline)
                    }
                    stat("Settled cash", Fmt.usd(cash))
                }
            }
        }
    }

    private func stat(_ key: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(key).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.headline.monospacedDigit())
        }
    }
}

struct PositionManagerCard: View {
    @Environment(AppModel.self) private var model
    let position: Position
    let riskRow: RiskRow?
    let isExpanded: Bool
    let toggle: () -> Void
    let editStops: () -> Void
    let onError: (String?) -> Void

    var body: some View {
        let p = position
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                headerRow
                if isExpanded {
                    Divider()
                    detailGrid
                    if let thesis = p.thesis, !thesis.isEmpty {
                        Text(thesis)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    actionRow
                }
            }
        }
    }

    private var headerRow: some View {
        let p = position
        return HStack(spacing: 10) {
            Button(action: toggle) {
                Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                    .font(.caption.bold())
                    .foregroundStyle(.secondary)
                    .frame(width: 16)
            }
            .buttonStyle(.plain)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(p.symbol).font(.headline.monospaced())
                    RibbonBadge(state: p.emaState)
                    if p.dnt == true { tag("DNT", .down) }
                    if p.locked == true { tag("LOCKED", .caution) }
                    if p.adopted == true { tag("ADOPTED", .secondary) }
                }
                Text("\(Fmt.num(p.shares, dp: 4)) sh @ \(Fmt.usd(p.entryPrice)) · \(Fmt.when(p.entryDate))")
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Sparkline(points: p.spark ?? [])
            VStack(alignment: .trailing, spacing: 2) {
                Text(Fmt.usd(p.price)).font(.headline.monospacedDigit())
                PnLText(value: p.unrealizedPnl, pct: p.unrealizedPct, font: .caption)
            }
        }
    }

    private var detailGrid: some View {
        let p = position
        let peak = p.peakPrice ?? p.entryPrice ?? 0
        let giveback = (peak > 0 && p.price != nil)
            ? max(0, (peak - p.price!) / peak * 100) : nil
        return WidthReader(compactBelow: 560) { compact in
            let cols = [GridItem(.adaptive(minimum: compact ? 140 : 165), spacing: 10)]
            LazyVGrid(columns: cols, alignment: .leading, spacing: 8) {
                kv("Day P&L", Fmt.usdSigned(p.dayPnl), (p.dayPnl ?? 0).pnlColor)
                kv("Peak", Fmt.usd(p.peakPrice))
                kv("Giveback off peak", giveback.map { Fmt.pct($0, dp: 1, signed: false) } ?? "—",
                   (giveback ?? 0) > 15 ? .caution : .primary)
                kv("Hard stop", Fmt.usd(p.stop?.stopPrice), .down)
                kv("Trailing stop", Fmt.usd(p.stop?.trailPrice), .caution)
                kv("Dist. to stop",
                   riskRow?.distStopPct.map { Fmt.pct($0, dp: 1, signed: false) } ?? "—")
                kv("Dist. to trail",
                   riskRow?.distTrailPct.map { Fmt.pct($0, dp: 1, signed: false) } ?? "—")
                kv("Confidence", p.confidence.map { "\($0)/100" } ?? "—")
                kv("Signal", "\(p.emaState ?? "—") / \(p.emaTransition ?? "—")")
            }
        }
    }

    private var actionRow: some View {
        let p = position
        return HStack(spacing: 8) {
            Button {
                model.orderPrefill = .init(symbol: p.symbol, side: "buy")
                model.showOrderSheet = true
            } label: { Label("Buy", systemImage: "plus.circle") }
            Button {
                model.orderPrefill = .init(symbol: p.symbol, side: "sell",
                                           quantity: p.shares)
                model.showOrderSheet = true
            } label: { Label("Sell", systemImage: "minus.circle") }
            Button {
                editStops()
            } label: { Label("Stops", systemImage: "shield.lefthalf.filled") }
            Button {
                let blocking = p.dnt != true
                model.requireArm {
                    Task { onError(await model.setDNT(symbol: p.symbol, blocked: blocking)) }
                }
            } label: {
                Label(p.dnt == true ? "Allow buys" : "Block buys",
                      systemImage: p.dnt == true ? "checkmark.circle" : "nosign")
            }
            Spacer()
            Button {
                model.analyze(p.symbol)
            } label: { Label("Analyze", systemImage: "waveform.and.magnifyingglass") }
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
    }

    private func kv(_ key: String, _ value: String, _ color: Color = .primary) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(key).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.callout.monospacedDigit()).foregroundStyle(color)
        }
    }

    private func tag(_ text: String, _ color: Color) -> some View {
        Text(text).font(.caption2.bold())
            .padding(.horizontal, 5).padding(.vertical, 1)
            .background(color.opacity(0.15), in: Capsule())
            .foregroundStyle(color)
    }
}

// MARK: - Orders

struct OrdersView: View {
    @Environment(AppModel.self) private var model
    @State private var cancelTarget: BrokerOrder?
    @State private var actionError: String?

    private var payload: OrdersPayload? { model.ordersPayload }

    private static let terminal: Set<String> = ["filled", "cancelled", "canceled",
                                                "rejected", "failed", "expired"]

    private var working: [BrokerOrder] {
        (payload?.today ?? []).filter {
            !Self.terminal.contains(($0.state ?? "").lowercased())
        }
    }
    private var doneToday: [BrokerOrder] {
        (payload?.today ?? []).filter {
            Self.terminal.contains(($0.state ?? "").lowercased())
        }
    }

    var body: some View {
        @Bindable var model = model
        List {
            if let err = model.tabErrors[.orders] { ErrorBox(text: err) }
            if let err = actionError { ErrorBox(text: err) }
            Section {
                headerCard
            }
            Section("Working orders (\(working.count))") {
                if working.isEmpty {
                    Text("No working orders.").foregroundStyle(.secondary).font(.callout)
                }
                ForEach(working, id: \.stableId) { o in
                    OrderRow(order: o) { cancelTarget = o }
                }
            }
            Section("Executed / terminal today (\(doneToday.count))") {
                if doneToday.isEmpty {
                    Text("Nothing filled today.").foregroundStyle(.secondary).font(.callout)
                }
                ForEach(doneToday, id: \.stableId) { o in
                    OrderRow(order: o, cancel: nil)
                }
            }
            if model.orderHistoryDays > 1 {
                Section("History — last \(model.orderHistoryDays) days") {
                    if let err = payload?.historyError {
                        WarningBox(text: err)
                    } else if let hist = payload?.history {
                        ForEach(hist, id: \.stableId) { o in
                            OrderRow(order: o, cancel: nil)
                        }
                    } else {
                        Text("Loading…").foregroundStyle(.secondary)
                    }
                }
            }
            if let journal = payload?.journal, !journal.isEmpty {
                Section("Dashboard order journal") {
                    ForEach(Array(journal.enumerated()), id: \.offset) { _, a in
                        JournalRow(action: a)
                    }
                }
            }
        }
        .refreshable { await model.load(.orders) }
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
        return GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 10) {
                    StatusDot(color: payload?.broker?.mode == "direct-mcp" ? .up : .caution,
                              label: payload?.broker?.mode ?? "broker?")
                    Text("updated \(Fmt.when(payload?.updated))")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button {
                        Task { actionError = await model.brokerRefresh(); await model.load(.orders) }
                    } label: { Label("Refresh", systemImage: "arrow.clockwise") }
                        .controlSize(.small)
                    Button {
                        model.orderPrefill = .init()
                        model.showOrderSheet = true
                    } label: { Label("New order", systemImage: "plus.circle.fill") }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.small)
                }
                Picker("Lookback", selection: $model.orderHistoryDays) {
                    Text("Today").tag(1)
                    Text("7 days").tag(7)
                    Text("30 days").tag(30)
                }
                .pickerStyle(.segmented)
                .onChange(of: model.orderHistoryDays) { _, _ in
                    Task { await model.load(.orders) }
                }
                if model.orderHistoryDays > 1 && payload?.broker?.mode != "direct-mcp" {
                    Text("Deeper history needs the direct broker connection (rh_login) — the claude-cli path only snapshots today.")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
        }
    }
}

struct OrderRow: View {
    let order: BrokerOrder
    var cancel: (() -> Void)?

    var body: some View {
        let o = order
        let side = (o.side ?? "").lowercased()
        HStack(spacing: 10) {
            Image(systemName: side == "buy" ? "arrow.down.circle.fill" : "arrow.up.circle.fill")
                .foregroundStyle(side == "buy" ? Color.up : Color.down)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(o.symbol ?? "—").font(.headline.monospaced())
                    Text(side.uppercased())
                        .font(.caption2.bold())
                        .foregroundStyle(side == "buy" ? Color.up : Color.down)
                    Text(o.type ?? "").font(.caption2).foregroundStyle(.secondary)
                }
                Text("\(Fmt.when(o.createdAt)) · id \(String((o.id ?? "—").prefix(12)))")
                    .font(.caption2.monospaced()).foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(o.quantity != nil ? "\(Fmt.num(o.quantity, dp: 5)) sh"
                     : Fmt.usd(o.notional))
                    .font(.callout.monospacedDigit())
                Text("\(o.state ?? "—")\(o.price != nil ? " @ \(Fmt.usd(o.price))" : "")")
                    .font(.caption2.monospaced())
                    .foregroundStyle(stateColor(o.state))
            }
            if let cancel {
                Button(role: .destructive, action: cancel) {
                    Image(systemName: "xmark.circle")
                }
                .buttonStyle(.bordered).controlSize(.small)
                .help("Cancel this order")
            }
        }
        .padding(.vertical, 2)
    }

    private func stateColor(_ s: String?) -> Color {
        switch (s ?? "").lowercased() {
        case "filled": .up
        case "cancelled", "canceled", "rejected", "failed": .secondary
        default: .caution
        }
    }
}

struct JournalRow: View {
    let action: ManualAction

    var body: some View {
        let ok = action.result?["ok"]?.boolValue != false
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Image(systemName: ok ? "checkmark.circle" : "xmark.octagon.fill")
                    .foregroundStyle(ok ? Color.secondary : Color.down)
                    .font(.caption)
                Text(action.action ?? "—").font(.callout.monospaced())
                Spacer()
                Text(Fmt.when(action.ts)).font(.caption2).foregroundStyle(.secondary)
            }
            if let params = action.params?.objectValue, !params.isEmpty {
                Text(JSONValue.object(params).compact)
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
        }
        .padding(.vertical, 1)
    }
}
