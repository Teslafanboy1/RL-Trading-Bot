//
//  Support.swift — JSONValue, formatters, and the small markdown renderer.
//  Visual components live in DesignKit.swift; tokens live in Theme.swift.
//

import SwiftUI

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
    /// Clock-only rendering for log/thinking timestamps (`10:02:14`).
    static func clock(_ ts: String?) -> String {
        guard let d = date(ts) else {
            guard let ts, ts.count >= 19 else { return ts ?? "—" }
            return String(ts.dropFirst(11).prefix(8))
        }
        return d.formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute(.twoDigits)
            .second(.twoDigits))
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

// MARK: - Legacy color aliases (map onto the Organic tokens)

extension Color {
    static let up = DS.up
    static let down = DS.down
    static let caution = DS.caution
}

// MARK: - Markdown-ish renderer for research / postmortem text

struct MarkdownLite: View {
    let text: String
    var scrolls: Bool = true

    var body: some View {
        if scrolls {
            ScrollView { stack.padding(DS.s4) }
        } else {
            stack
        }
    }

    private var stack: some View {
        VStack(alignment: .leading, spacing: 7) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                blockView(block)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .textSelection(.enabled)
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
                .font(DSFont.heading(level == 1 ? 22 : level == 2 ? 18 : 15))
                .foregroundStyle(level == 2 ? DS.accent : DS.text)
                .padding(.top, 8)
        case .li(let s):
            HStack(alignment: .firstTextBaseline, spacing: 7) {
                Text("•").foregroundStyle(DS.accent)
                styled(s).font(DSFont.body(14)).foregroundStyle(DS.text)
            }
        case .code(let s):
            Text(s)
                .font(DSFont.mono(11))
                .foregroundStyle(DS.text)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
                .background(DS.neutral(2),
                            in: RoundedRectangle(cornerRadius: DS.rSm, style: .continuous))
        case .p(let s):
            styled(s).font(DSFont.body(14)).foregroundStyle(DS.text)
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
