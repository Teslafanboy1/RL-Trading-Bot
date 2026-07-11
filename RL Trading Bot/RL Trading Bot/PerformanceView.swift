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
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let err = model.tabErrors[.performance] { ErrorBox(text: err) }
                equityCard
                AdaptivePair(compactBelow: 660) {
                    realizedCard
                } second: {
                    drawdownCard
                }
                perTradeCard
                scoreboardRow
            }
            .padding()
        }
        .opacity(p == nil ? 0 : 1)
        .refreshable { await model.load(.performance) }
        .task { if p == nil { await model.load(.performance) } }
        .overlay {
            if p == nil {
                ContentUnavailableView("Loading performance…", systemImage: "chart.xyaxis.line")
            }
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
        GroupBox {
            let pts = equitySeries
            if pts.isEmpty {
                Text("No equity snapshots yet — they accrue automatically from broker reads (logs/equity_curve.jsonl).")
                    .font(.callout).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 120, alignment: .center)
            } else {
                Chart(pts) { pt in
                    LineMark(x: .value("Date", pt.date), y: .value("$", pt.value))
                        .foregroundStyle(by: .value("Series", pt.series))
                        .interpolationMethod(.monotone)
                }
                .chartForegroundStyleScale([
                    "Account": Color.accentColor,
                    "SPY (scaled)": Color.secondary,
                    "RX-3 paper (scaled)": Color.purple,
                ])
                .chartYScale(domain: .automatic(includesZero: false))
                .chartYAxis {
                    AxisMarks(position: .leading) { v in
                        AxisGridLine()
                        AxisValueLabel {
                            if let d = v.as(Double.self) { Text(Fmt.usd(d, dp: 0)) }
                        }
                    }
                }
                .frame(height: 240)
            }
        } label: {
            Label("Equity curve — account vs SPY vs RX-3 paper",
                  systemImage: "chart.line.uptrend.xyaxis")
        }
    }

    private var realizedCard: some View {
        GroupBox {
            let pts = (p?.realized ?? []).compactMap { r -> (Date, Double)? in
                guard let d = Fmt.date(r.ts), let v = r.cumPnl else { return nil }
                return (d, v)
            }
            if pts.isEmpty {
                Text("no closed trades yet").font(.callout).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 100, alignment: .center)
            } else {
                Chart(Array(pts.enumerated()), id: \.offset) { item in
                    AreaMark(x: .value("Date", item.element.0),
                             y: .value("$", item.element.1))
                        .foregroundStyle(Color.up.opacity(0.2))
                    LineMark(x: .value("Date", item.element.0),
                             y: .value("$", item.element.1))
                        .foregroundStyle(Color.up)
                }
                .frame(height: 170)
            }
        } label: { Label("Cumulative realized P&L", systemImage: "sum") }
    }

    private var drawdownCard: some View {
        GroupBox {
            let pts = (p?.drawdown ?? []).compactMap { r -> (Date, Double)? in
                guard let d = Fmt.date(r.ts), let v = r.dd else { return nil }
                return (d, v)
            }
            if pts.isEmpty {
                Text("starts with equity recording").font(.callout).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 100, alignment: .center)
            } else {
                Chart(Array(pts.enumerated()), id: \.offset) { item in
                    AreaMark(x: .value("Date", item.element.0),
                             y: .value("%", item.element.1))
                        .foregroundStyle(Color.down.opacity(0.25))
                    LineMark(x: .value("Date", item.element.0),
                             y: .value("%", item.element.1))
                        .foregroundStyle(Color.down)
                }
                .frame(height: 170)
            }
        } label: { Label("Drawdown (recorded equity)", systemImage: "arrow.down.right") }
    }

    private var perTradeCard: some View {
        GroupBox {
            let trades = Array((p?.perTrade ?? []).suffix(24))
            if trades.isEmpty {
                Text("no closed trades yet").font(.callout).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 80, alignment: .center)
            } else {
                Chart(trades, id: \.stableId) { t in
                    BarMark(x: .value("Trade", "\(t.id ?? "") \(t.symbol ?? "")"),
                            y: .value("$", t.pnl ?? 0))
                        .foregroundStyle((t.pnl ?? 0) >= 0 ? Color.up : Color.down)
                }
                .chartXAxis {
                    AxisMarks { v in
                        AxisValueLabel(orientation: .verticalReversed) {
                            if let s = v.as(String.self) {
                                Text(s.split(separator: " ").last.map(String.init) ?? s)
                                    .font(.caption2)
                            }
                        }
                    }
                }
                .frame(height: 190)
            }
        } label: { Label("P&L by trade (last 24)", systemImage: "chart.bar.fill") }
    }

    private var scoreboardRow: some View {
        let s = p?.summary
        let prog = p?.progress
        // adaptive grid: 3-up on wide windows, stacked when narrow
        return LazyVGrid(columns: [GridItem(.adaptive(minimum: 250), spacing: 14)],
                         alignment: .leading, spacing: 14) {
            GroupBox {
                VStack(spacing: 4) {
                    KeyValueRow(key: "trades", value: "\(s?.totalTrades ?? 0)")
                    KeyValueRow(key: "wins / losses", value: "\(s?.wins ?? 0) / \(s?.losses ?? 0)")
                    KeyValueRow(key: "win rate",
                                value: Fmt.pct((s?.winRate ?? 0) * 100, dp: 1, signed: false))
                    KeyValueRow(key: "total P&L", value: Fmt.usdSigned(s?.totalPnl),
                                valueColor: (s?.totalPnl ?? 0).pnlColor)
                }
            } label: { Label("Scoreboard", systemImage: "trophy") }

            GroupBox {
                VStack(spacing: 4) {
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
            } label: { Label("Month vs goal", systemImage: "flag.checkered") }

            GroupBox {
                VStack(spacing: 4) {
                    ForEach((p?.exitReasons ?? [:]).sorted(by: { $0.value > $1.value }),
                            id: \.key) { k, v in
                        KeyValueRow(key: k, value: "\(v)")
                    }
                }
            } label: { Label("Exit reasons", systemImage: "door.right.hand.open") }
        }
    }
}
