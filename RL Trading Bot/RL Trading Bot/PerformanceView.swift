//
//  PerformanceView.swift — equity curve vs SPY vs RX-3, drawdown,
//  per-trade P&L, scoreboard, month-vs-goal. Swift Charts.
//

import SwiftUI
import Charts

struct PerformanceView: View {
    @Environment(AppModel.self) private var model
    private var p: Performance? { model.performance }

    struct SeriesPoint: Identifiable {
        let id = UUID()
        let date: Date
        let value: Double
        let series: String
    }

    var body: some View {
        Group {
            if p == nil {
                LoadingScreen(title: "Loading performance…",
                              detail: "equity curve, drawdown, per-trade P&L")
                    .background(DS.bg)
            } else {
                Screen(title: "Performance", subtitle: AppTab.performance.blurb,
                       refresh: { await model.load(.performance) }) {
                    if let err = model.tabErrors[.performance] { ErrorBox(text: err) }
                    statTiles
                    equityCard
                    AdaptivePair(compactBelow: 820) {
                        realizedCard
                    } second: {
                        drawdownCard
                    }
                    perTradeCard
                    scoreboardRow
                }
            }
        }
        .task { if p == nil { await model.load(.performance) } }
    }

    // MARK: stat tiles

    private var maxDrawdown: Double? {
        let dds = (p?.drawdown ?? []).compactMap(\.dd)
        return dds.isEmpty ? nil : dds.map { abs($0) }.max()
    }

    private var statTiles: some View {
        let s = p?.summary
        let prog = p?.progress
        let ret = prog?["current_return"]?.stringValue ?? Fmt.pct(s?.monthlyReturnPct)
        return StatRow {
            StatCard(label: "Total return",
                     value: ret,
                     delta: "goal \(prog?["monthly_goal"]?.stringValue ?? s?.monthlyGoal ?? "—")",
                     deltaTone: s?.onTrack == true ? .up : .neutral,
                     sublabel: "month start \(Fmt.usd(prog?["month_start_value"]?.numberValue ?? s?.monthStartValue))")
            StatCard(label: "Max drawdown",
                     value: maxDrawdown.map { Fmt.pct($0, dp: 1, signed: false) } ?? "—",
                     delta: "kill-switch fires at 25%",
                     deltaTone: (maxDrawdown ?? 0) > 15 ? .down : .neutral,
                     sublabel: "from recorded equity")
            StatCard(label: "Win rate",
                     value: Fmt.pct((s?.winRate ?? 0) * 100, dp: 1, signed: false),
                     delta: "\(s?.wins ?? 0)W / \(s?.losses ?? 0)L",
                     deltaTone: (s?.winRate ?? 0) >= 0.5 ? .up : .down,
                     sublabel: "\(s?.totalTrades ?? 0) closed trades")
            StatCard(label: "Total P&L",
                     value: Fmt.usdSigned(s?.totalPnl),
                     deltaTone: .forValue(s?.totalPnl),
                     sublabel: "realized, all time")
        }
    }

    // MARK: equity vs benchmarks

    private var equitySeries: [SeriesPoint] {
        var out: [SeriesPoint] = []
        let curve = (p?.equityCurve ?? []).compactMap { pt -> (Date, Double)? in
            guard let d = Fmt.date(pt.ts), let v = pt.total else { return nil }
            return (d, v)
        }
        guard curve.count > 1, let first = curve.first else { return [] }
        out += curve.map { SeriesPoint(date: $0.0, value: $0.1, series: "Account") }

        let spy = (p?.benchmarkSpy ?? []).compactMap { pt -> (Date, Double)? in
            guard let d = Fmt.date(pt.ts), let v = pt.close else { return nil }
            return (d, v)
        }.filter { $0.0 >= first.0.addingTimeInterval(-86400) }
        if let sFirst = spy.first, sFirst.1 > 0 {
            let k = first.1 / sFirst.1
            out += spy.map { SeriesPoint(date: $0.0, value: $0.1 * k, series: "SPY (scaled)") }
        }

        let rx3 = (p?.rx3Curve ?? []).compactMap { pt -> (Date, Double)? in
            guard let d = Fmt.date(pt.ts), let v = pt.value else { return nil }
            return (d, v)
        }
        if rx3.count > 1, let rFirst = rx3.first, rFirst.1 > 0 {
            let k = first.1 / rFirst.1
            out += rx3.map { SeriesPoint(date: $0.0, value: $0.1 * k, series: "RX-3 paper (scaled)") }
        }
        return out
    }

    private var equityCard: some View {
        Card("Equity curve", subtitle: "account vs SPY vs RX-3 paper, scaled to the same start") {
            let pts = equitySeries
            if pts.isEmpty {
                EmptyNote(text: "No equity snapshots yet — they accrue automatically from broker reads (logs/equity_curve.jsonl).",
                          icon: "chart.line.uptrend.xyaxis")
                    .frame(minHeight: 120)
            } else {
                Chart(pts) { pt in
                    LineMark(x: .value("Date", pt.date), y: .value("$", pt.value))
                        .foregroundStyle(by: .value("Series", pt.series))
                        .interpolationMethod(.monotone)
                        .lineStyle(StrokeStyle(lineWidth: pt.series == "Account" ? 2.4 : 1.4,
                                               lineCap: .round))
                }
                .chartForegroundStyleScale([
                    "Account": DS.accent,
                    "SPY (scaled)": DS.neutral(5),
                    "RX-3 paper (scaled)": DS.accent2,
                ])
                .chartYScale(domain: .automatic(includesZero: false))
                .chartYAxis {
                    AxisMarks(position: .leading) { v in
                        AxisGridLine().foregroundStyle(DS.divider)
                        AxisValueLabel {
                            if let d = v.as(Double.self) {
                                Text(Fmt.usd(d, dp: 0)).font(DSFont.num(10))
                                    .foregroundStyle(DS.textMuted)
                            }
                        }
                    }
                }
                .chartXAxis {
                    AxisMarks { _ in
                        AxisGridLine().foregroundStyle(DS.divider.opacity(0.5))
                        AxisValueLabel().font(DSFont.num(10))
                    }
                }
                .frame(height: 260)
            }
        }
    }

    private var realizedCard: some View {
        Card("Cumulative realized P&L", subtitle: "closed trades only") {
            let pts = (p?.realized ?? []).compactMap { r -> (Date, Double)? in
                guard let d = Fmt.date(r.ts), let v = r.cumPnl else { return nil }
                return (d, v)
            }
            if pts.isEmpty {
                EmptyNote(text: "No closed trades yet.", icon: "sum").frame(minHeight: 100)
            } else {
                Chart(Array(pts.enumerated()), id: \.offset) { item in
                    AreaMark(x: .value("Date", item.element.0), y: .value("$", item.element.1))
                        .foregroundStyle(DS.accent2.opacity(0.22))
                    LineMark(x: .value("Date", item.element.0), y: .value("$", item.element.1))
                        .foregroundStyle(DS.accent2)
                        .lineStyle(StrokeStyle(lineWidth: 2, lineCap: .round))
                }
                .chartYAxis {
                    AxisMarks(position: .leading) { _ in
                        AxisGridLine().foregroundStyle(DS.divider)
                        AxisValueLabel().font(DSFont.num(10))
                    }
                }
                .frame(height: 180)
            }
        }
    }

    private var drawdownCard: some View {
        Card("Drawdown", subtitle: "from recorded equity") {
            let pts = (p?.drawdown ?? []).compactMap { r -> (Date, Double)? in
                guard let d = Fmt.date(r.ts), let v = r.dd else { return nil }
                return (d, v)
            }
            if pts.isEmpty {
                EmptyNote(text: "Starts with equity recording.", icon: "arrow.down.right")
                    .frame(minHeight: 100)
            } else {
                Chart(Array(pts.enumerated()), id: \.offset) { item in
                    AreaMark(x: .value("Date", item.element.0), y: .value("%", item.element.1))
                        .foregroundStyle(DS.down.opacity(0.22))
                    LineMark(x: .value("Date", item.element.0), y: .value("%", item.element.1))
                        .foregroundStyle(DS.down)
                        .lineStyle(StrokeStyle(lineWidth: 2, lineCap: .round))
                }
                .chartYAxis {
                    AxisMarks(position: .leading) { _ in
                        AxisGridLine().foregroundStyle(DS.divider)
                        AxisValueLabel().font(DSFont.num(10))
                    }
                }
                .frame(height: 180)
            }
        }
    }

    private var perTradeCard: some View {
        Card("P&L by trade", subtitle: "last 24 closes") {
            let trades = Array((p?.perTrade ?? []).suffix(24))
            if trades.isEmpty {
                EmptyNote(text: "No closed trades yet.", icon: "chart.bar").frame(minHeight: 80)
            } else {
                Chart(trades, id: \.stableId) { t in
                    BarMark(x: .value("Trade", "\(t.id ?? "") \(t.symbol ?? "")"),
                            y: .value("$", t.pnl ?? 0))
                        .foregroundStyle((t.pnl ?? 0) >= 0 ? DS.accent2 : DS.down)
                        .cornerRadius(4)
                }
                .chartXAxis {
                    AxisMarks { v in
                        AxisValueLabel(orientation: .verticalReversed) {
                            if let s = v.as(String.self) {
                                Text(s.split(separator: " ").last.map(String.init) ?? s)
                                    .font(DSFont.num(10))
                                    .foregroundStyle(DS.textMuted)
                            }
                        }
                    }
                }
                .chartYAxis {
                    AxisMarks(position: .leading) { _ in
                        AxisGridLine().foregroundStyle(DS.divider)
                        AxisValueLabel().font(DSFont.num(10))
                    }
                }
                .frame(height: 200)
            }
        }
    }

    private var scoreboardRow: some View {
        let s = p?.summary
        let prog = p?.progress
        return LazyVGrid(columns: [GridItem(.adaptive(minimum: 280), spacing: DS.s4)],
                         alignment: .leading, spacing: DS.s4) {
            Card("Scoreboard", subtitle: "all-time trade record") {
                VStack(spacing: DS.s2) {
                    KeyValueRow(key: "trades", value: "\(s?.totalTrades ?? 0)")
                    KeyValueRow(key: "wins / losses", value: "\(s?.wins ?? 0) / \(s?.losses ?? 0)")
                    KeyValueRow(key: "win rate",
                                value: Fmt.pct((s?.winRate ?? 0) * 100, dp: 1, signed: false))
                    KeyValueRow(key: "total P&L", value: Fmt.usdSigned(s?.totalPnl),
                                valueColor: s?.totalPnl.pnlColor)
                }
            }
            Card("Month vs goal", subtitle: "the denominator is deposit/withdrawal aware") {
                VStack(spacing: DS.s2) {
                    KeyValueRow(key: "month start",
                                value: Fmt.usd(prog?["month_start_value"]?.numberValue ?? s?.monthStartValue))
                    KeyValueRow(key: "current",
                                value: Fmt.usd(prog?["current_value"]?.numberValue ?? s?.currentValue))
                    KeyValueRow(key: "return",
                                value: prog?["current_return"]?.stringValue
                                    ?? Fmt.pct(s?.monthlyReturnPct))
                    KeyValueRow(key: "goal",
                                value: prog?["monthly_goal"]?.stringValue ?? s?.monthlyGoal ?? "—")
                }
            }
            Card("Exit reasons", subtitle: "how positions actually leave the book") {
                let reasons = (p?.exitReasons ?? [:]).sorted { $0.value > $1.value }
                if reasons.isEmpty {
                    EmptyNote(text: "Nothing closed yet.")
                } else {
                    let total = max(1, reasons.reduce(0) { $0 + $1.value })
                    VStack(spacing: DS.s2) {
                        ForEach(reasons, id: \.key) { k, v in
                            BarRow(label: k, valueText: "\(v)",
                                   fraction: Double(v) / Double(total),
                                   tint: k.contains("stop") ? DS.down : DS.accent)
                        }
                    }
                }
            }
        }
    }
}
