//
//  Support.swift — JSONValue, formatters, theme, shared UI components.
//

import SwiftUI
import Charts

// MARK: - JSONValue (free-form API payloads: config, earnings, broker review…)

enum JSONValue: Codable, Hashable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else if let o = try? c.decode([String: JSONValue].self) { self = .object(o) }
        else { self = .null }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let b): try c.encode(b)
        case .number(let n): try c.encode(n)
        case .string(let s): try c.encode(s)
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }

    var stringValue: String? { if case .string(let s) = self { return s }; return nil }
    var numberValue: Double? {
        if case .number(let n) = self { return n }
        if case .string(let s) = self { return Double(s) }
        return nil
    }
    var boolValue: Bool? { if case .bool(let b) = self { return b }; return nil }
    var objectValue: [String: JSONValue]? { if case .object(let o) = self { return o }; return nil }
    var arrayValue: [JSONValue]? { if case .array(let a) = self { return a }; return nil }
    subscript(key: String) -> JSONValue? { objectValue?[key] }

    /// Compact single-line description for tables/logs.
    var compact: String {
        switch self {
        case .null: return "—"
        case .bool(let b): return b ? "true" : "false"
        case .number(let n):
            return n == n.rounded() && abs(n) < 1e12
                ? String(Int(n)) : String(format: "%.4g", n)
        case .string(let s): return s
        case .array(let a): return "[" + a.map(\.compact).joined(separator: ", ") + "]"
        case .object(let o):
            return "{" + o.keys.sorted().map { "\($0): \(o[$0]!.compact)" }
                .joined(separator: ", ") + "}"
        }
    }

    /// Pretty multi-line JSON for detail sheets.
    var pretty: String {
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let d = try? enc.encode(self), let s = String(data: d, encoding: .utf8)
        else { return compact }
        return s
    }
}

// MARK: - Formatting

enum Fmt {
    static func usd(_ v: Double?, dp: Int = 2) -> String {
        guard let v, v.isFinite else { return "—" }
        let sign = v < 0 ? "-" : ""
        return sign + "$" + abs(v).formatted(.number.precision(.fractionLength(dp)).grouping(.automatic))
    }
    static func usdSigned(_ v: Double?, dp: Int = 2) -> String {
        guard let v, v.isFinite else { return "—" }
        return (v > 0 ? "+" : v < 0 ? "-" : "") + "$"
            + abs(v).formatted(.number.precision(.fractionLength(dp)))
    }
    static func num(_ v: Double?, dp: Int = 2) -> String {
        guard let v, v.isFinite else { return "—" }
        return v.formatted(.number.precision(.fractionLength(0...dp)))
    }
    static func pct(_ v: Double?, dp: Int = 2, signed: Bool = true) -> String {
        guard let v, v.isFinite else { return "—" }
        let s = signed && v > 0 ? "+" : ""
        return s + v.formatted(.number.precision(.fractionLength(dp))) + "%"
    }
    static func ago(_ seconds: Double?) -> String {
        guard let s = seconds else { return "never" }
        if s < 90 { return "\(Int(s))s ago" }
        if s < 5400 { return "\(Int(s / 60))m ago" }
        if s < 172_800 { return String(format: "%.1fh ago", s / 3600) }
        return String(format: "%.1fd ago", s / 86400)
    }
    /// Parse the bot's timestamps: ISO8601 w/ offset, date-only, or naive.
    static func date(_ ts: String?) -> Date? {
        guard let ts, !ts.isEmpty else { return nil }
        if let d = isoFrac.date(from: ts) { return d }
        if let d = iso.date(from: ts) { return d }
        if let d = dateOnly.date(from: ts) { return d }
        if let d = naive.date(from: ts) { return d }
        return nil
    }
    static func when(_ ts: String?) -> String {
        guard let d = date(ts) else { return ts.map { String($0.prefix(16)) } ?? "—" }
        return d.formatted(.dateTime.month(.abbreviated).day().hour().minute())
    }
    private static let iso: ISO8601DateFormatter = { ISO8601DateFormatter() }()
    private static let isoFrac: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let dateOnly: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"; f.locale = Locale(identifier: "en_US_POSIX")
        return f
    }()
    private static let naive: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"; f.locale = Locale(identifier: "en_US_POSIX")
        return f
    }()
}

// MARK: - Theme

extension Color {
    static let up = Color.green
    static let down = Color.red
    static let caution = Color.orange
}

extension Double {
    var pnlColor: Color { self > 0 ? .up : self < 0 ? .down : .secondary }
}

// MARK: - Shared components

/// Colored EMA ribbon state badge (BUY / SELL / NEUTRAL / …).
struct RibbonBadge: View {
    let state: String?
    var body: some View {
        // long diagnostic states (INSUFFICIENT_DATA, ERROR) must not blow up
        // row layouts — shorten them and keep the full text as a tooltip
        let raw = state ?? "—"
        let s: String = switch raw {
        case "INSUFFICIENT_DATA": "NO DATA"
        case "ERROR": "ERR"
        default: raw
        }
        let color: Color = raw == "BUY" ? .up : raw == "SELL" ? .down : .secondary
        Text(s)
            .font(.caption2.weight(.bold).monospaced())
            .lineLimit(1)
            .fixedSize()
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(color.opacity(0.15), in: Capsule())
            .foregroundStyle(color)
            .help(raw)
    }
}

/// Measures the width actually offered to it and hands `isCompact` to the
/// content builder. The reliable way to adapt card pairs / dense rows to
/// window resizing — ViewThatFits can't help here because GroupBoxes and
/// charts are infinitely compressible, so the wide variant always "fits".
struct WidthReader<Content: View>: View {
    var compactBelow: CGFloat
    @ViewBuilder let content: (_ isCompact: Bool) -> Content
    @State private var width: CGFloat = .infinity

    var body: some View {
        content(width < compactBelow)
            .frame(maxWidth: .infinity, alignment: .leading)
            .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
    }
}

/// Two cards side by side when there's room, stacked when there isn't.
struct AdaptivePair<A: View, B: View>: View {
    var compactBelow: CGFloat = 680
    @ViewBuilder let first: () -> A
    @ViewBuilder let second: () -> B

    var body: some View {
        WidthReader(compactBelow: compactBelow) { compact in
            if compact {
                VStack(alignment: .leading, spacing: 14) { first(); second() }
            } else {
                HStack(alignment: .top, spacing: 14) {
                    first().frame(maxWidth: .infinity, alignment: .topLeading)
                    second().frame(maxWidth: .infinity, alignment: .topLeading)
                }
            }
        }
    }
}

struct ConfidenceMeter: View {
    let confidence: Int?
    var body: some View {
        let c = confidence ?? 0
        let color: Color = c >= 75 ? .up : c >= 60 ? .caution : .down
        Gauge(value: Double(c), in: 0...100) { EmptyView() }
            .gaugeStyle(.accessoryLinearCapacity)
            .tint(color)
            .scaleEffect(y: 0.7)
    }
}

/// P&L text with sign coloring and monospaced digits.
struct PnLText: View {
    let value: Double?
    var pct: Double? = nil
    var font: Font = .body
    var body: some View {
        let str = Fmt.usdSigned(value) + (pct != nil ? " (\(Fmt.pct(pct)))" : "")
        Text(value == nil ? "—" : str)
            .font(font.monospacedDigit())
            .foregroundStyle((value ?? 0).pnlColor)
    }
}

struct KeyValueRow: View {
    let key: String
    let value: String
    var valueColor: Color? = nil
    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(key).foregroundStyle(.secondary)
            Spacer(minLength: 12)
            Text(value)
                .font(.body.monospacedDigit())
                .foregroundStyle(valueColor ?? .primary)
                .multilineTextAlignment(.trailing)
        }
    }
}

struct WarningBox: View {
    let text: String
    var body: some View {
        Label(text, systemImage: "exclamationmark.triangle.fill")
            .font(.callout)
            .foregroundStyle(Color.caution)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(Color.caution.opacity(0.1), in: RoundedRectangle(cornerRadius: 8))
    }
}

struct ErrorBox: View {
    let text: String
    var body: some View {
        Label(text, systemImage: "xmark.octagon.fill")
            .font(.callout)
            .foregroundStyle(Color.down)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(Color.down.opacity(0.1), in: RoundedRectangle(cornerRadius: 8))
    }
}

/// Press-and-hold confirm button for real-money / bot-control actions.
struct HoldToConfirmButton: View {
    let title: String
    var tint: Color = .down
    var duration: Double = 1.2
    let action: () -> Void

    @State private var progress: Double = 0
    @State private var timer: Timer?
    @State private var fired = false

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 10)
                .fill(tint.opacity(0.12))
            GeometryReader { geo in
                Rectangle()
                    .fill(tint.opacity(0.4))
                    .frame(width: geo.size.width * progress)
                    .animation(.linear(duration: 0.05), value: progress)
            }
            .clipShape(RoundedRectangle(cornerRadius: 10))
            RoundedRectangle(cornerRadius: 10)
                .strokeBorder(tint, lineWidth: 1.5)
            Label(title, systemImage: "hand.tap.fill")
                .font(.headline)
                .foregroundStyle(tint)
                .padding(.vertical, 12)
        }
        .frame(height: 48)
        .contentShape(RoundedRectangle(cornerRadius: 10))
        .onLongPressGesture(minimumDuration: duration, maximumDistance: 60) {
            if !fired { fired = true; stop(); progress = 1; action() }
        } onPressingChanged: { pressing in
            if pressing {
                fired = false
                progress = 0
                timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { _ in
                    MainActor.assumeIsolated {
                        progress = min(1, progress + 0.05 / duration)
                    }
                }
            } else if !fired {
                stop(); progress = 0
            }
        }
        .accessibilityHint("Press and hold to confirm")
    }

    private func stop() { timer?.invalidate(); timer = nil }
}

/// Tiny inline sparkline via Swift Charts.
struct Sparkline: View {
    let points: [Double]
    var body: some View {
        if points.count > 1 {
            let up = (points.last ?? 0) >= (points.first ?? 0)
            Chart(Array(points.enumerated()), id: \.offset) { item in
                LineMark(x: .value("i", item.offset), y: .value("v", item.element))
                    .lineStyle(StrokeStyle(lineWidth: 1.5))
                    .foregroundStyle(up ? Color.up : Color.down)
            }
            .chartXAxis(.hidden)
            .chartYAxis(.hidden)
            .chartYScale(domain: (points.min() ?? 0)...(points.max() ?? 1))
            .frame(width: 84, height: 24)
        } else {
            Color.clear.frame(width: 84, height: 24)
        }
    }
}

/// Very small markdown-ish renderer for research/postmortem text.
struct MarkdownLite: View {
    let text: String
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                    blockView(block)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding()
        }
    }

    private enum Block { case h(Int, String), li(String), code(String), p(String) }

    private var blocks: [Block] {
        var out: [Block] = []
        var inCode = false
        var code: [String] = []
        for raw in text.components(separatedBy: "\n") {
            if raw.hasPrefix("```") {
                if inCode { out.append(.code(code.joined(separator: "\n"))); code = [] }
                inCode.toggle(); continue
            }
            if inCode { code.append(raw); continue }
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("### ") { out.append(.h(3, String(line.dropFirst(4)))) }
            else if line.hasPrefix("## ") { out.append(.h(2, String(line.dropFirst(3)))) }
            else if line.hasPrefix("# ") { out.append(.h(1, String(line.dropFirst(2)))) }
            else if line.hasPrefix("- ") || line.hasPrefix("* ") { out.append(.li(String(line.dropFirst(2)))) }
            else if !line.isEmpty { out.append(.p(line)) }
        }
        if !code.isEmpty { out.append(.code(code.joined(separator: "\n"))) }
        return out
    }

    @ViewBuilder
    private func blockView(_ b: Block) -> some View {
        switch b {
        case .h(let level, let s):
            styled(s)
                .font(level == 1 ? .title3.bold() : level == 2 ? .headline : .subheadline.bold())
                .foregroundStyle(level == 2 ? Color.accentColor : Color.primary)
                .padding(.top, 6)
        case .li(let s):
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("•").foregroundStyle(.secondary)
                styled(s)
            }
        case .code(let s):
            Text(s)
                .font(.caption.monospaced())
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(8)
                .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6))
        case .p(let s):
            styled(s)
        }
    }

    private func styled(_ s: String) -> Text {
        if let attr = try? AttributedString(
            markdown: s, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)) {
            return Text(attr)
        }
        return Text(s)
    }
}

/// Age-tinted dot + text for heartbeat-style freshness.
struct StatusDot: View {
    let color: Color
    let label: String
    var body: some View {
        HStack(spacing: 5) {
            Circle().fill(color).frame(width: 8, height: 8)
            Text(label).font(.caption.weight(.semibold).monospaced())
        }
        .padding(.horizontal, 8).padding(.vertical, 4)
        .background(color.opacity(0.12), in: Capsule())
        .foregroundStyle(color)
    }
}
