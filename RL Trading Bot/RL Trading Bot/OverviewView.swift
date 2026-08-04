//
//  OverviewView.swift — the home screen: stat tiles, agent status + controls,
//  open positions, agent thinking, exposure, kill-switch, market snapshot,
//  today's orders and the recent-activity feed.
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
                LoadingScreen(title: model.connectionError == nil ? "Loading…" : "Not connected",
                              detail: model.connectionError ?? "Fetching bot state")
                    .frame(maxHeight: .infinity)
                    .background(DS.bg)
            } else {
                Screen(title: "Overview", subtitle: AppTab.overview.blurb,
                       refresh: { await model.refreshTick() }) {
                    statTiles
                    if let err = actionError { ErrorBox(text: err) }
                    if let err = model.tabErrors[.overview] { ErrorBox(text: err) }
                    agentCard
                    AdaptivePair(compactBelow: 860, ratio: 1.4) {
                        positionsCard
                    } second: {
                        thinkingCard
                    }
                    AdaptivePair(compactBelow: 780) {
                        exposureCard
                    } second: {
                        marketCard
                    }
                    AdaptivePair(compactBelow: 780) {
                        ordersCard
                    } second: {
                        killswitchCard
                    }
                    activityCard
                }
            }
        }
        .sheet(isPresented: $showHalt) { HaltSheet() }
        .sheet(isPresented: $showClearHalt) { ClearHaltSheet() }
    }

    // MARK: stat tiles

    private var statTiles: some View {
        let a = ov?.account
        let total = a?.liveTotal ?? a?.total
        let day = (ov?.dayPnlPositions ?? 0) + (ov?.realizedToday ?? 0)
        let dayPct = (total ?? 0) > 0 ? day / (total! - day) * 100 : nil
        let s = ov?.summary
        return StatRow {
            StatCard(label: "Portfolio value",
                     value: total == nil ? "$ —" : Fmt.usd(total),
                     delta: "\(Fmt.usdSigned(day)) today"
                        + (dayPct != nil ? " · \(Fmt.pct(dayPct, dp: 2))" : ""),
                     deltaTone: .forValue(day),
                     sublabel: a?.updated == nil
                        ? "no broker read yet — hit Broker refresh"
                        : "broker read \(Fmt.when(a?.updated))")
            StatCard(label: "Total P&L",
                     value: Fmt.usdSigned(s?.totalPnl),
                     delta: "win rate \(Fmt.pct((s?.winRate ?? 0) * 100, dp: 1, signed: false))",
                     deltaTone: .forValue(s?.totalPnl),
                     sublabel: "\(s?.wins ?? 0)W / \(s?.losses ?? 0)L over \(s?.totalTrades ?? 0) trades")
            StatCard(label: "Cash",
                     value: Fmt.usd(a?.cash),
                     delta: a?.cashPct != nil
                        ? "\(Fmt.num(a?.cashPct, dp: 1))% of book" : nil,
                     deltaTone: .neutral,
                     sublabel: "Settled — available for new positions")
            StatCard(label: "Buying power",
                     value: Fmt.usd(a?.buyingPower),
                     sublabel: "Account \(model.meta?.broker?.account ?? "—")")
        }
    }

    // MARK: agent status + controls

    private var agentCard: some View {
        let bot = ov?.bot
        let state = bot?.state ?? "unknown"
        let (tone, label, icon): (TagTone, String, String) = switch state {
        case "running": (.up, "RUNNING", "checkmark.circle.fill")
        case "paused": (.caution, "PAUSED", "pause.circle.fill")
        case "halted": (.down, "HALTED", "octagon.fill")
        case "stale": (.down, "STALE / DOWN", "exclamationmark.triangle.fill")
        default: (.neutral, state.uppercased(), "questionmark.circle")
        }
        return Card("Agent status", subtitle: "Heartbeat \(Fmt.ago(bot?.heartbeat?.ageSeconds))"
                    + (bot?.cycle?.runningFresh == true ? " · cycle running" : "")) {
            VStack(alignment: .leading, spacing: DS.s3) {
                WidthReader(compactBelow: 620) { compact in
                    let cols = [GridItem(.adaptive(minimum: compact ? 150 : 170), spacing: DS.s3)]
                    LazyVGrid(columns: cols, alignment: .leading, spacing: DS.s2) {
                        Tag(label, tone: tone, size: 12, icon: icon)
                        if let mkt = model.meta?.marketOpen {
                            StatusDot(color: mkt ? DS.up : DS.textFaint,
                                      label: mkt ? "MARKET OPEN" : "MARKET CLOSED")
                        }
                        if let mode = model.meta?.broker?.mode {
                            StatusDot(color: mode == "direct-mcp" ? DS.up : DS.caution, label: mode)
                        }
                        if let acct = model.meta?.broker?.account {
                            StatusDot(color: DS.textFaint, label: "acct \(acct)")
                        }
                    }
                }
                if let err = ov?.account?.error {
                    WarningBox(text: err)
                }
                if ov?.bot?.halt?.halted == true {
                    ErrorBox(text: "HALT active — the bot will not trade, including its own stop enforcement. \(ov?.bot?.halt?.detail?.prefix(180) ?? "")")
                }
                if ov?.bot?.pause?.paused == true {
                    WarningBox(text: "Paused by operator — protective exits and broker bookkeeping still run.")
                }
                actionsRow
            }
        }
    }

    private var actionsRow: some View {
        let paused = ov?.bot?.pause?.paused == true
        let halted = ov?.bot?.halt?.halted == true
        return LazyVGrid(columns: [GridItem(.adaptive(minimum: 155), spacing: DS.s2)],
                         spacing: DS.s2) {
            Button {
                model.orderPrefill = .init()
                model.showOrderSheet = true
            } label: {
                Label("New order", systemImage: "bolt.fill").frame(maxWidth: .infinity)
            }
            .buttonStyle(.organicPrimary)

            Button {
                Task { actionError = await model.brokerRefresh() }
            } label: {
                Label("Broker refresh", systemImage: "arrow.triangle.2.circlepath")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.organicSoft)

            if paused {
                Button {
                    model.requireArm { Task { actionError = await model.resumeBot() } }
                } label: {
                    Label("Resume bot", systemImage: "play.fill").frame(maxWidth: .infinity)
                }
                .buttonStyle(.organicSoft(DS.up))
            } else {
                Button {
                    model.requireArm { Task { actionError = await model.pauseBot() } }
                } label: {
                    Label("Pause bot", systemImage: "pause.fill").frame(maxWidth: .infinity)
                }
                .buttonStyle(.organicSoft(DS.caution))
            }

            if halted {
                Button { showClearHalt = true } label: {
                    Label("Clear HALT", systemImage: "checkmark.shield.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.organicSoft(DS.up))
            } else {
                Button { showHalt = true } label: {
                    Label("Halt", systemImage: "octagon.fill").frame(maxWidth: .infinity)
                }
                .buttonStyle(.organicSoft(DS.down))
            }
        }
    }

    // MARK: positions

    private var positionsCard: some View {
        let positions = ov?.positions ?? []
        return Card("Open Positions",
                    subtitle: positions.isEmpty
                        ? "flat — nothing held"
                        : "\(positions.count) held · unrealized \(Fmt.usdSigned(positions.compactMap(\.unrealizedPnl).reduce(0, +)))") {
            if positions.isEmpty {
                EmptyNote(text: "No open positions.", icon: "tray")
            } else {
                VStack(spacing: 0) {
                    ForEach(positions, id: \.stableId) { pos in
                        OverviewPositionRow(position: pos,
                                            isLast: pos.stableId == positions.last?.stableId)
                    }
                }
            }
        }
    }

    // MARK: agent thinking

    private var thinkingCard: some View {
        let picks = model.thinking?.picks?.picks ?? []
        let queue = (model.thinking?.rewriteQueue ?? []).filter { $0.done != true }
        return Card("Agent Thinking",
                    subtitle: model.thinking?.picksFile?.file ?? "latest research") {
            VStack(alignment: .leading, spacing: DS.s3) {
                if picks.isEmpty && queue.isEmpty {
                    EmptyNote(text: "No research parsed yet.", icon: "brain")
                }
                ForEach(picks.prefix(3)) { pick in
                    VStack(alignment: .leading, spacing: 5) {
                        HStack(spacing: 8) {
                            Tag("#\(pick.rank ?? 0) pick", tone: .accent)
                            RibbonBadge(state: pick.emaState)
                            Spacer()
                            Text("\(pick.confidence.map(String.init) ?? "—")/100")
                                .font(DSFont.num(12, .semibold))
                                .foregroundStyle(DS.textMuted)
                        }
                        Text("\(pick.symbol) — \(pick.fields?["ema"] ?? pick.name ?? "")")
                            .font(DSFont.body(14))
                            .foregroundStyle(DS.text)
                            .lineLimit(3)
                    }
                    .padding(.bottom, 2)
                    Rectangle().fill(DS.divider).frame(height: 1)
                }
                ForEach(queue.prefix(2)) { q in
                    HStack(spacing: 8) {
                        Tag("rewrite", tone: .caution)
                        Text("\(q.tradeId ?? "") \(q.symbol ?? "") \(q.outcome ?? "") \(q.pnl ?? "")")
                            .font(DSFont.body(13))
                            .foregroundStyle(DS.text)
                            .lineLimit(2)
                    }
                }
                Button {
                    model.selectedTab = .thinking
                } label: {
                    Text("View full reasoning log →")
                        .font(DSFont.semibold(13))
                        .foregroundStyle(DS.accent(7))
                }
                .buttonStyle(.plain)
                .padding(.top, 2)
            }
        }
    }

    // MARK: exposure

    /// One exposure bar: a position's (or cash's) share of the book.
    private struct ExposureSlice: Identifiable {
        let id: String
        let value: Double
        let gaining: Bool
    }

    private var exposureTotal: Double {
        if let t = ov?.account?.liveTotal ?? ov?.account?.total, t > 0 { return t }
        let positions: [Position] = ov?.positions ?? []
        var sum = 0.0
        for p in positions { sum += (p.price ?? 0) * (p.shares ?? 0) }
        return sum
    }

    private var exposureSlices: [ExposureSlice] {
        let positions: [Position] = ov?.positions ?? []
        var out: [ExposureSlice] = positions.map { p in
            ExposureSlice(id: p.symbol,
                          value: (p.price ?? 0) * (p.shares ?? 0),
                          gaining: (p.unrealizedPct ?? 0) >= 0)
        }
        out.sort { $0.value > $1.value }
        if let cash = ov?.account?.cash, cash > 0 {
            out.append(ExposureSlice(id: "Cash", value: cash, gaining: true))
        }
        return out
    }

    private var exposureCard: some View {
        let total = exposureTotal
        let slices = exposureSlices
        return Card("Risk Exposure", subtitle: "share of book by position") {
            VStack(alignment: .leading, spacing: DS.s2) {
                if slices.isEmpty {
                    EmptyNote(text: "Nothing deployed — 100% cash.", icon: "banknote")
                } else {
                    ForEach(slices) { slice in
                        let frac: Double = total > 0 ? slice.value / total : 0
                        let tint: Color = slice.id == "Cash"
                            ? DS.neutral(5) : (slice.gaining ? DS.accent2 : DS.down)
                        BarRow(label: slice.id,
                               valueText: Fmt.pct(frac * 100, dp: 1, signed: false),
                               fraction: frac,
                               tint: tint)
                    }
                }
            }
        }
    }

    // MARK: market snapshot

    private var marketCard: some View {
        Card("Market Snapshot", subtitle: "indices the bot reads for regime") {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 140), spacing: DS.s2)],
                      spacing: DS.s2) {
                ForEach(ov?.indices ?? []) { ix in
                    Tile {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(ix.label ?? ix.symbol)
                                .font(DSFont.body(12))
                                .foregroundStyle(DS.textMuted)
                                .lineLimit(1)
                            Text(ix.price == nil ? "—" : Fmt.num(ix.price))
                                .font(DSFont.num(16, .bold))
                                .foregroundStyle(DS.text)
                            Text(Fmt.pct(ix.changePct))
                                .font(DSFont.num(12, .semibold))
                                .foregroundStyle(ix.changePct.pnlColor)
                        }
                    }
                }
                if (ov?.indices ?? []).isEmpty {
                    EmptyNote(text: "No index quotes yet.", icon: "globe")
                }
            }
        }
    }

    // MARK: today's orders

    private var ordersCard: some View {
        let orders = ov?.ordersToday ?? []
        return Card("Today's orders", subtitle: "broker-confirmed, this session") {
            if orders.isEmpty {
                EmptyNote(text: "None seen — Broker refresh updates this.", icon: "clock")
            } else {
                VStack(spacing: 0) {
                    ForEach(orders, id: \.stableId) { o in
                        TRow(showsDivider: o.stableId != orders.last?.stableId) {
                            TCell(weight: 1.2) {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(o.symbol ?? "—").font(DSFont.bold(14))
                                        .foregroundStyle(DS.text)
                                    Text("\(o.side ?? "") \(o.type ?? "")")
                                        .font(DSFont.body(11))
                                        .foregroundStyle(DS.textMuted)
                                        .lineLimit(1)
                                }
                            }
                            TCell(align: .trailing) {
                                Text(o.quantity != nil ? "\(Fmt.num(o.quantity, dp: 4)) sh"
                                     : Fmt.usd(o.notional))
                                    .font(DSFont.num(13))
                                    .foregroundStyle(DS.text)
                            }
                            TCell(align: .trailing) {
                                Tag(o.state ?? "—", tone: orderTone(o.state))
                            }
                            if ["new", "queued", "confirmed", "unconfirmed",
                                "partially_filled"].contains(o.state ?? "") {
                                Button("Cancel") {
                                    model.requireArm {
                                        Task { actionError = await model.cancelOrder(id: o.id ?? "") }
                                    }
                                }
                                .buttonStyle(.organicSoft(DS.down))
                                .fixedSize()
                            }
                        }
                    }
                }
            }
        }
    }

    private func orderTone(_ s: String?) -> TagTone {
        switch (s ?? "").lowercased() {
        case "filled": .up
        case "cancelled", "canceled", "rejected", "failed": .neutral
        default: .caution
        }
    }

    // MARK: kill-switch

    private var killswitchCard: some View {
        let ks = ov?.killswitch
        let dd = (ks?.drawdown ?? 0) * 100
        return Card("Kill-switch budget", subtitle: "monthly −25% drawdown halt") {
            VStack(alignment: .leading, spacing: DS.s2) {
                KeyValueRow(key: "month peak", value: Fmt.usd(ks?.peak))
                KeyValueRow(key: "current", value: Fmt.usd(ks?.current))
                KeyValueRow(key: "drawdown", value: Fmt.pct(dd, dp: 1, signed: false),
                            valueColor: dd > 15 ? DS.down : dd > 8 ? DS.caution : DS.up)
                KeyValueRow(key: "halts at", value: "\(Fmt.usd(ks?.haltAtValue)) (−25%)")
                MeterBar(value: min(dd, 25) / 25,
                         tint: dd > 15 ? DS.down : dd > 8 ? DS.caution : DS.up)
                    .padding(.top, 2)
                Text("Headroom \(Fmt.pct(max(0, 25 - dd), dp: 1, signed: false)) before the bot flattens and refuses to trade.")
                    .font(DSFont.body(12))
                    .foregroundStyle(DS.textMuted)
            }
        }
    }

    // MARK: recent activity

    private var activityCard: some View {
        let events = Array((model.logsPayload?.events ?? []).prefix(8))
        return Card("Recent Activity", subtitle: "every stream, newest first") {
            if events.isEmpty {
                EmptyNote(text: "Nothing logged yet.", icon: "clock.arrow.circlepath")
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(events.enumerated()), id: \.offset) { i, ev in
                        TRow(showsDivider: i < events.count - 1) {
                            Text(Fmt.clock(ev.ts))
                                .font(DSFont.num(12))
                                .foregroundStyle(DS.textFaint)
                                .frame(width: 66, alignment: .leading)
                            Tag(ev.kind ?? "system", tone: activityTone(ev))
                            TCell(weight: 4) {
                                Text(ev.title ?? "—")
                                    .font(DSFont.body(13))
                                    .foregroundStyle(DS.text)
                                    .lineLimit(2)
                            }
                            if let d = ev.detail, !d.isEmpty {
                                TCell(align: .trailing, weight: 2) {
                                    Text(d)
                                        .font(DSFont.body(12))
                                        .foregroundStyle(DS.textMuted)
                                        .lineLimit(1)
                                }
                            }
                        }
                    }
                }
                Button { model.selectedTab = .activity } label: {
                    Text("Open the full activity feed →")
                        .font(DSFont.semibold(13))
                        .foregroundStyle(DS.accent(7))
                }
                .buttonStyle(.plain)
                .padding(.top, DS.s2)
            }
        }
    }

    private func activityTone(_ ev: LogEvent) -> TagTone {
        if ev.ok == false || ev.severity == "MAJOR" { return .down }
        switch ev.kind {
        case "manual": return .accent
        case "change": return .caution
        case "agent": return .sage
        default: return .neutral
        }
    }
}

// MARK: - Overview position row

struct OverviewPositionRow: View {
    @Environment(AppModel.self) private var model
    let position: Position
    let isLast: Bool

    private var stopDist: Double? {
        guard let px = position.price, let sp = position.stop?.stopPrice, px > 0
        else { return nil }
        return (px - sp) / px * 100
    }

    var body: some View {
        VStack(spacing: 0) {
            WidthReader(compactBelow: 560) { compact in
                if compact { compactBody } else { wideBody }
            }
            .padding(.vertical, 11)
            if !isLast { Rectangle().fill(DS.divider).frame(height: 1) }
        }
        .contextMenu {
            Button(position.dnt == true ? "Remove from do-not-trade" : "Add to do-not-trade") {
                model.requireArm {
                    Task { _ = await model.setDNT(symbol: position.symbol,
                                                  blocked: !(position.dnt == true)) }
                }
            }
            Button("Analyze \(position.symbol)") { model.analyze(position.symbol) }
            if let thesis = position.thesis, !thesis.isEmpty {
                Section("Thesis") { Text(thesis) }
            }
        }
    }

    private var wideBody: some View {
        HStack(spacing: DS.s3) {
            identity
            Spacer(minLength: DS.s2)
            Sparkline(points: position.spark ?? [])
            ribbon
            price
            value
            buttons
        }
    }

    private var compactBody: some View {
        VStack(alignment: .leading, spacing: DS.s2) {
            HStack(spacing: DS.s3) {
                identity
                Spacer(minLength: 4)
                value
            }
            HStack(spacing: DS.s2) {
                ribbon
                Sparkline(points: position.spark ?? [], width: 64)
                Spacer(minLength: 4)
                buttons
            }
        }
    }

    private var identity: some View {
        HStack(spacing: DS.s2) {
            SymbolChip(symbol: position.symbol)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 5) {
                    Text(position.symbol).font(DSFont.semibold(14)).foregroundStyle(DS.text)
                    if position.dnt == true { Tag("DNT", tone: .caution, size: 9) }
                    if position.locked == true {
                        Image(systemName: "lock.fill").font(.system(size: 9))
                            .foregroundStyle(DS.textMuted)
                    }
                }
                Text("\(Fmt.num(position.shares, dp: 4)) sh @ \(Fmt.usd(position.entryPrice))"
                     + (position.confidence.map { " · conf \($0)" } ?? ""))
                    .font(DSFont.body(12))
                    .foregroundStyle(DS.textMuted)
                    .lineLimit(1)
            }
        }
        .frame(minWidth: 150, alignment: .leading)
    }

    private var ribbon: some View {
        VStack(alignment: .trailing, spacing: 3) {
            RibbonBadge(state: position.emaState)
            if let d = stopDist {
                Text("stop \(Fmt.pct(d, dp: 1, signed: false))")
                    .font(DSFont.num(10))
                    .foregroundStyle(d < 3 ? DS.down : DS.textFaint)
            }
        }
    }

    private var price: some View {
        VStack(alignment: .trailing, spacing: 2) {
            Text(Fmt.usd(position.price)).font(DSFont.num(13)).foregroundStyle(DS.text)
            if let prev = position.prevClose, let px = position.price, prev > 0 {
                Text(Fmt.pct((px / prev - 1) * 100))
                    .font(DSFont.num(11))
                    .foregroundStyle((px - prev).pnlColor)
            }
        }
    }

    private var value: some View {
        VStack(alignment: .trailing, spacing: 2) {
            Text(Fmt.usd((position.price ?? 0) * (position.shares ?? 0)))
                .font(DSFont.num(14, .bold))
                .foregroundStyle(DS.text)
            PnLPair(dollars: position.unrealizedPnl, pct: position.unrealizedPct)
        }
        .frame(minWidth: 92, alignment: .trailing)
    }

    private var buttons: some View {
        HStack(spacing: 6) {
            Button("Close") {
                model.orderPrefill = .init(symbol: position.symbol, side: "sell",
                                           quantity: position.shares, type: "market")
                model.showOrderSheet = true
            }
            .buttonStyle(SoftButtonStyle(tint: DS.down, size: 12))
            Button("Add") {
                model.orderPrefill = .init(symbol: position.symbol, side: "buy")
                model.showOrderSheet = true
            }
            .buttonStyle(SoftButtonStyle(tint: DS.accent, size: 12))
        }
    }
}
