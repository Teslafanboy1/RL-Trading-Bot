//
//  OverviewView.swift — the home screen: account hero, bot status,
//  live positions, today's orders, kill-switch budget, indices.
//  Every section adapts to the available width (WidthReader/AdaptivePair)
//  so the window can be resized freely without clipping.
//

import SwiftUI

struct OverviewView: View {
    @Environment(AppModel.self) private var model
    @State private var showHalt = false
    @State private var showClearHalt = false
    @State private var actionError: String?

    private var ov: Overview? { model.overview }

    var body: some View {
        Group {
            if ov == nil {
                // no skeleton behind the placeholder — one clear state
                ContentUnavailableView(
                    model.connectionError == nil ? "Loading…" : "Not connected",
                    systemImage: "antenna.radiowaves.left.and.right",
                    description: Text(model.connectionError ?? "Fetching bot state"))
            } else {
                ScrollView {
                    VStack(spacing: 14) {
                        heroCard
                        if let err = actionError { ErrorBox(text: err) }
                        if let err = model.tabErrors[.overview] { ErrorBox(text: err) }
                        positionsCard
                        AdaptivePair(compactBelow: 660) {
                            ordersCard
                        } second: {
                            killswitchCard
                        }
                        indicesCard
                    }
                    .padding()
                }
                .refreshable { await model.refreshTick() }
            }
        }
        .sheet(isPresented: $showHalt) { HaltSheet() }
        .sheet(isPresented: $showClearHalt) { ClearHaltSheet() }
    }

    // MARK: hero

    private var heroCard: some View {
        GroupBox {
            WidthReader(compactBelow: 620) { compact in
                if compact {
                    VStack(alignment: .leading, spacing: 10) {
                        totalBlock
                        dayBlock
                        botBadge
                    }
                } else {
                    HStack(alignment: .top, spacing: 24) {
                        totalBlock
                        dayBlock
                        Spacer(minLength: 8)
                        botBadge
                    }
                }
            }
            statusChipsRow
                .padding(.top, 6)
            if let err = ov?.account?.error {
                Text("⚠ \(err)")
                    .font(.caption2.monospaced())
                    .foregroundStyle(Color.caution)
                    .lineLimit(2)
                    .truncationMode(.tail)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, 2)
            }
            Divider().padding(.vertical, 6)
            actionsRow
        }
    }

    private var totalBlock: some View {
        let a = ov?.account
        let total = a?.liveTotal ?? a?.total
        return VStack(alignment: .leading, spacing: 2) {
            if let total {
                Text(Fmt.usd(total))
                    .font(.system(size: 34, weight: .bold, design: .rounded).monospacedDigit())
                    .contentTransition(.numericText())
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
            } else {
                Text("$ —")
                    .font(.system(size: 34, weight: .bold, design: .rounded))
                    .foregroundStyle(.tertiary)
            }
            Text(a?.updated == nil
                 ? "no broker read yet — tap Broker refresh"
                 : "broker \(Fmt.usd(a?.total)) · cash \(Fmt.usd(a?.cash)) (\(Fmt.num(a?.cashPct, dp: 1))%) · BP \(Fmt.usd(a?.buyingPower))")
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .lineLimit(2)
        }
    }

    private var dayBlock: some View {
        let day = (ov?.dayPnlPositions ?? 0) + (ov?.realizedToday ?? 0)
        return VStack(alignment: .leading, spacing: 2) {
            PnLText(value: ov == nil ? nil : day,
                    font: .system(size: 26, weight: .semibold, design: .rounded))
                .lineLimit(1)
                .minimumScaleFactor(0.6)
            Text("today · pos \(Fmt.usdSigned(ov?.dayPnlPositions)) · realized \(Fmt.usdSigned(ov?.realizedToday))")
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .lineLimit(2)
        }
    }

    private var botBadge: some View {
        let bot = ov?.bot
        let state = bot?.state ?? "unknown"
        let (color, label, icon): (Color, String, String) = switch state {
        case "running": (.up, "RUNNING", "checkmark.circle.fill")
        case "paused": (.caution, "PAUSED", "pause.circle.fill")
        case "halted": (.down, "HALTED", "octagon.fill")
        case "stale": (.down, "STALE / DOWN", "exclamationmark.triangle.fill")
        default: (.secondary, state.uppercased(), "questionmark.circle")
        }
        return VStack(alignment: .leading, spacing: 4) {
            Label(label, systemImage: icon)
                .font(.headline)
                .lineLimit(1)
                .padding(.horizontal, 12).padding(.vertical, 6)
                .background(color.opacity(0.14), in: Capsule())
                .foregroundStyle(color)
            Text("heartbeat \(Fmt.ago(bot?.heartbeat?.ageSeconds))"
                 + (bot?.cycle?.runningFresh == true ? " · CYCLE RUNNING" : ""))
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
    }

    /// Market / broker chips — horizontal scroll so they can never clip.
    private var statusChipsRow: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                if let mkt = model.meta?.marketOpen {
                    StatusDot(color: mkt ? .up : .secondary,
                              label: mkt ? "MKT OPEN" : "mkt closed")
                }
                if let mode = model.meta?.broker?.mode {
                    StatusDot(color: mode == "direct-mcp" ? .up : .caution, label: mode)
                }
                if let acct = model.meta?.broker?.account {
                    StatusDot(color: .secondary, label: "acct \(acct)")
                }
                if let asof = ov?.account?.updated {
                    StatusDot(color: .secondary, label: "broker read \(Fmt.when(asof))")
                }
            }
        }
    }

    private var actionsRow: some View {
        let paused = ov?.bot?.pause?.paused == true
        let halted = ov?.bot?.halt?.halted == true
        // adaptive grid: buttons wrap to new rows instead of clipping
        return LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 8)],
                         spacing: 8) {
            Button {
                model.orderPrefill = .init()
                model.showOrderSheet = true
            } label: {
                Label("New order", systemImage: "bolt.fill")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)

            Button {
                Task { actionError = await model.brokerRefresh() }
            } label: {
                Label("Broker refresh", systemImage: "arrow.triangle.2.circlepath")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.bordered)

            if paused {
                Button {
                    model.requireArm { Task { actionError = await model.resumeBot() } }
                } label: {
                    Label("Resume bot", systemImage: "play.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered).tint(.up)
            } else {
                Button {
                    model.requireArm { Task { actionError = await model.pauseBot() } }
                } label: {
                    Label("Pause bot", systemImage: "pause.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
            }

            if halted {
                Button {
                    showClearHalt = true
                } label: {
                    Label("Clear HALT", systemImage: "checkmark.shield.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered).tint(.up)
            } else {
                Button(role: .destructive) {
                    showHalt = true
                } label: {
                    Label("Halt", systemImage: "octagon.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered).tint(.down)
            }
        }
    }

    // MARK: positions

    private var positionsCard: some View {
        GroupBox {
            VStack(spacing: 0) {
                ForEach(ov?.positions ?? [], id: \.stableId) { pos in
                    PositionRow(position: pos)
                    if pos.stableId != ov?.positions?.last?.stableId { Divider() }
                }
                if (ov?.positions ?? []).isEmpty {
                    Text("no open positions").foregroundStyle(.secondary).padding(.vertical, 12)
                }
            }
        } label: {
            HStack {
                Label("Open positions (\((ov?.positions ?? []).count))",
                      systemImage: "briefcase.fill")
                Spacer()
                if let s = ov?.summary {
                    Text("win rate \(Fmt.pct((s.winRate ?? 0) * 100, dp: 1, signed: false)) · P&L \(Fmt.usdSigned(s.totalPnl))")
                        .font(.caption.monospaced()).foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
        }
    }

    // MARK: orders / killswitch / indices

    private var ordersCard: some View {
        GroupBox {
            let orders = ov?.ordersToday ?? []
            if orders.isEmpty {
                Text("none seen — Broker refresh updates")
                    .font(.callout).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                VStack(spacing: 6) {
                    ForEach(orders, id: \.stableId) { o in
                        HStack {
                            VStack(alignment: .leading) {
                                Text(o.symbol ?? "—").font(.subheadline.bold().monospaced())
                                Text("\(o.side ?? "") \(o.type ?? "") · \(o.state ?? "")")
                                    .font(.caption2).foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                            Spacer()
                            Text(o.quantity != nil ? "\(Fmt.num(o.quantity, dp: 4)) sh" : Fmt.usd(o.notional))
                                .font(.callout.monospacedDigit())
                            if ["new", "queued", "confirmed", "unconfirmed",
                                "partially_filled"].contains(o.state ?? "") {
                                Button("Cancel", role: .destructive) {
                                    model.requireArm {
                                        Task { actionError = await model.cancelOrder(id: o.id ?? "") }
                                    }
                                }
                                .buttonStyle(.bordered).controlSize(.small)
                            }
                        }
                    }
                }
            }
        } label: { Label("Today's orders", systemImage: "clock.fill") }
    }

    private var killswitchCard: some View {
        GroupBox {
            let ks = ov?.killswitch
            let dd = (ks?.drawdown ?? 0) * 100
            VStack(alignment: .leading, spacing: 6) {
                KeyValueRow(key: "month peak", value: Fmt.usd(ks?.peak))
                KeyValueRow(key: "current", value: Fmt.usd(ks?.current))
                KeyValueRow(key: "drawdown", value: Fmt.pct(dd, dp: 1, signed: false),
                            valueColor: dd > 15 ? .down : dd > 8 ? .caution : .up)
                KeyValueRow(key: "halts at", value: "\(Fmt.usd(ks?.haltAtValue)) (−25%)")
                Gauge(value: min(dd, 25), in: 0...25) { EmptyView() }
                    .gaugeStyle(.accessoryLinearCapacity)
                    .tint(dd > 15 ? .down : dd > 8 ? .caution : .up)
            }
        } label: { Label("Kill-switch budget", systemImage: "shield.lefthalf.filled") }
    }

    private var indicesCard: some View {
        GroupBox {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 168), spacing: 10)],
                      alignment: .leading, spacing: 8) {
                ForEach(ov?.indices ?? []) { ix in
                    HStack {
                        Text(ix.label ?? ix.symbol)
                            .font(.caption.weight(.medium))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                        Spacer(minLength: 6)
                        if let price = ix.price {
                            VStack(alignment: .trailing, spacing: 0) {
                                Text(Fmt.num(price)).font(.callout.monospacedDigit())
                                Text(Fmt.pct(ix.changePct))
                                    .font(.caption2.monospacedDigit())
                                    .foregroundStyle((ix.changePct ?? 0).pnlColor)
                            }
                        } else {
                            Text("—").foregroundStyle(.tertiary)
                        }
                    }
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
                }
            }
        } label: { Label("Indices", systemImage: "globe") }
    }
}

// MARK: - Position row (wide = one line, compact = two lines)

struct PositionRow: View {
    @Environment(AppModel.self) private var model
    let position: Position

    private var stopDist: Double? {
        guard let px = position.price, let sp = position.stop?.stopPrice, px > 0
        else { return nil }
        return (px - sp) / px * 100
    }

    var body: some View {
        WidthReader(compactBelow: 620) { compact in
            Group {
                if compact { compactRow } else { wideRow }
            }
            .padding(.vertical, 8)
        }
        .contextMenu {
            Button(position.dnt == true ? "Remove from do-not-trade" : "Add to do-not-trade") {
                model.requireArm {
                    Task { _ = await model.setDNT(symbol: position.symbol,
                                                  blocked: !(position.dnt == true)) }
                }
            }
            if let thesis = position.thesis, !thesis.isEmpty {
                Section("Thesis") { Text(thesis) }
            }
        }
    }

    private var wideRow: some View {
        HStack(spacing: 12) {
            symbolBlock
                .frame(minWidth: 120, alignment: .leading)
            Spacer(minLength: 4)
            priceBlock
            PnLText(value: position.unrealizedPnl, pct: position.unrealizedPct, font: .callout)
                .lineLimit(1)
                .layoutPriority(1)
            ribbonBlock
            Sparkline(points: position.spark ?? [])
            buttons
        }
    }

    private var compactRow: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                symbolBlock
                Spacer(minLength: 4)
                priceBlock
                PnLText(value: position.unrealizedPnl, pct: position.unrealizedPct,
                        font: .callout)
                    .lineLimit(1)
                    .layoutPriority(1)
            }
            HStack(spacing: 10) {
                ribbonBlock
                Sparkline(points: position.spark ?? [])
                Spacer(minLength: 4)
                buttons
            }
        }
    }

    private var symbolBlock: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 5) {
                Text(position.symbol).font(.headline.monospaced())
                if position.dnt == true {
                    Text("DNT").font(.caption2.bold())
                        .padding(.horizontal, 4).padding(.vertical, 1)
                        .background(Color.caution.opacity(0.2), in: Capsule())
                        .foregroundStyle(Color.caution)
                }
                if position.locked == true { Image(systemName: "lock.fill").font(.caption2) }
            }
            Text("\(Fmt.num(position.shares, dp: 4)) sh @ \(Fmt.usd(position.entryPrice)) · conf \(position.confidence.map(String.init) ?? "—")")
                .font(.caption).foregroundStyle(.secondary)
                .lineLimit(1)
        }
    }

    private var priceBlock: some View {
        VStack(alignment: .trailing, spacing: 2) {
            Text(Fmt.usd(position.price)).font(.callout.monospacedDigit())
            if let prev = position.prevClose, let px = position.price, prev > 0 {
                Text(Fmt.pct((px / prev - 1) * 100))
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle((px - prev).pnlColor)
            }
        }
    }

    private var ribbonBlock: some View {
        VStack(alignment: .trailing, spacing: 3) {
            RibbonBadge(state: position.emaState)
            if let d = stopDist {
                Text("stop \(Fmt.pct(d, dp: 1, signed: false))")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(d < 3 ? Color.down : Color.secondary)
                    .lineLimit(1)
            }
        }
    }

    private var buttons: some View {
        HStack(spacing: 6) {
            Button("Close") {
                model.orderPrefill = .init(symbol: position.symbol, side: "sell",
                                           quantity: position.shares, type: "market")
                model.showOrderSheet = true
            }
            .buttonStyle(.bordered).controlSize(.small).tint(.down)
            Button("Add") {
                model.orderPrefill = .init(symbol: position.symbol, side: "buy")
                model.showOrderSheet = true
            }
            .buttonStyle(.bordered).controlSize(.small)
        }
    }
}
