//
//  DesignKit.swift — the Organic component set every screen is built from:
//  cards, stat tiles, tags, filter pills, meters, tables, buttons, and the
//  restyled trading primitives (ribbon badge, P&L text, hold-to-confirm…).
//

import SwiftUI
import Charts

// MARK: - Card

/// The base container: surface fill, 32pt corners, soft warm shadow.
struct Card<Content: View>: View {
    var padding: CGFloat = DS.cardPadding
    var title: String? = nil
    var subtitle: String? = nil
    /// Optional trailing control rendered on the title line.
    var accessory: AnyView? = nil
    @ViewBuilder var content: () -> Content

    init(_ title: String? = nil,
         subtitle: String? = nil,
         padding: CGFloat = DS.cardPadding,
         @ViewBuilder content: @escaping () -> Content) {
        self.title = title
        self.subtitle = subtitle
        self.padding = padding
        self.content = content
    }

    init<A: View>(_ title: String? = nil,
                  subtitle: String? = nil,
                  padding: CGFloat = DS.cardPadding,
                  @ViewBuilder accessory: () -> A,
                  @ViewBuilder content: @escaping () -> Content) {
        self.title = title
        self.subtitle = subtitle
        self.padding = padding
        self.accessory = AnyView(accessory())
        self.content = content
    }

    var body: some View {
        VStack(alignment: .leading, spacing: DS.s3) {
            if title != nil || accessory != nil {
                HStack(alignment: .firstTextBaseline, spacing: DS.s2) {
                    if let title {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(title)
                                .font(DSFont.heading(17))
                                .foregroundStyle(DS.text)
                            if let subtitle {
                                Text(subtitle)
                                    .font(DSFont.body(12))
                                    .foregroundStyle(DS.textMuted)
                            }
                        }
                    }
                    Spacer(minLength: DS.s2)
                    if let accessory { accessory }
                }
            }
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(padding)
        .background(DS.surface, in: RoundedRectangle(cornerRadius: DS.rCard, style: .continuous))
        .shadow(color: DS.shadowColor, radius: DS.shadowRadius, y: DS.shadowY)
    }
}

/// A recessed tile used *inside* cards (index mini-tiles, shadow positions…).
struct Tile<Content: View>: View {
    var padding: CGFloat = 12
    @ViewBuilder var content: () -> Content
    var body: some View {
        content()
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(DS.neutral(1), in: RoundedRectangle(cornerRadius: DS.rMd, style: .continuous))
    }
}

// MARK: - Stat card

enum DeltaTone {
    case up, down, neutral, caution
    var color: Color {
        switch self {
        case .up: DS.up
        case .down: DS.down
        case .caution: DS.caution
        case .neutral: DS.textMuted
        }
    }
    static func forValue(_ v: Double?) -> DeltaTone {
        guard let v else { return .neutral }
        return v > 0 ? .up : v < 0 ? .down : .neutral
    }
}

/// The 4-up stat tile used across Overview / Performance / Risk / Learning.
struct StatCard: View {
    let label: String
    let value: String
    var delta: String? = nil
    var deltaTone: DeltaTone = .neutral
    var sublabel: String? = nil
    var sublabelTone: Color = DS.textMuted
    var valueSize: CGFloat = 26

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label.uppercased())
                .font(DSFont.semibold(13))
                .kerning(0.5)
                .foregroundStyle(DS.textMuted)
                .lineLimit(1)
                .truncationMode(.tail)
            Text(value)
                .font(DSFont.heading(valueSize))
                .foregroundStyle(DS.text)
                .lineLimit(2)
                .minimumScaleFactor(0.55)
                .fixedSize(horizontal: false, vertical: true)
            if let delta {
                Text(delta)
                    .font(DSFont.semibold(14))
                    .foregroundStyle(deltaTone.color)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let sublabel {
                Text(sublabel)
                    .font(DSFont.body(13))
                    .foregroundStyle(sublabelTone)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 118, alignment: .topLeading)
        .padding(DS.s4)
        .background(DS.surface, in: RoundedRectangle(cornerRadius: DS.rCard, style: .continuous))
        .shadow(color: DS.shadowColor, radius: DS.shadowRadius, y: DS.shadowY)
    }
}

/// Responsive row of stat cards — 4-up wide, 2-up medium, 1-up narrow.
struct StatRow<Content: View>: View {
    var minWidth: CGFloat = 190
    @ViewBuilder var content: () -> Content
    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: minWidth), spacing: DS.s4)],
                  spacing: DS.s4) {
            content()
        }
    }
}

// MARK: - Tag

enum TagTone {
    case neutral, accent, sage, up, down, caution, ink

    var fg: Color {
        switch self {
        case .neutral: DS.neutral(7)
        case .accent: DS.accent(7)
        case .sage: DS.accent2(7)
        case .up: DS.up
        case .down: DS.down
        case .caution: DS.caution
        case .ink: .white
        }
    }
    var bg: Color {
        switch self {
        case .neutral: DS.neutral(3)
        case .accent: DS.accent(2)
        case .sage: DS.accent2(2)
        case .up: DS.up.opacity(0.16)
        case .down: DS.down.opacity(0.16)
        case .caution: DS.caution.opacity(0.16)
        case .ink: DS.accent
        }
    }
}

/// Pill tag — the design's universal label chip.
struct Tag: View {
    let text: String
    var tone: TagTone = .neutral
    var size: CGFloat = 11
    var icon: String? = nil

    init(_ text: String, tone: TagTone = .neutral, size: CGFloat = 11, icon: String? = nil) {
        self.text = text; self.tone = tone; self.size = size; self.icon = icon
    }

    var body: some View {
        HStack(spacing: 4) {
            if let icon { Image(systemName: icon).font(.system(size: size - 1, weight: .bold)) }
            Text(text)
        }
        .font(DSFont.semibold(size))
        .lineLimit(1)
        .fixedSize()
        .padding(.horizontal, 9)
        .padding(.vertical, 3)
        .background(tone.bg, in: Capsule())
        .foregroundStyle(tone.fg)
    }
}

/// Status dot + label chip (market open, broker mode, freshness…).
struct StatusDot: View {
    let color: Color
    let label: String
    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 8, height: 8)
            Text(label).font(DSFont.semibold(11))
        }
        .lineLimit(1)
        .fixedSize()
        .padding(.horizontal, 9).padding(.vertical, 4)
        .background(color.opacity(0.14), in: Capsule())
        .foregroundStyle(color)
    }
}

/// A ticker "avatar" chip — rounded square with the symbol inside.
struct SymbolChip: View {
    let symbol: String
    var size: CGFloat = 36
    var body: some View {
        Text(symbol.prefix(4))
            .font(DSFont.bold(size <= 28 ? 10 : 12))
            .lineLimit(1)
            .minimumScaleFactor(0.6)
            .frame(width: size, height: size)
            .background(DS.accent(2), in: RoundedRectangle(cornerRadius: size * 0.28,
                                                           style: .continuous))
            .foregroundStyle(DS.accent(8))
    }
}

// MARK: - Filter pills

/// Horizontal pill filter row (Orders / Signals / Console / Activity).
struct FilterPills<T: Hashable>: View {
    let options: [(value: T, label: String)]
    @Binding var selection: T

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: DS.s2) {
                ForEach(options, id: \.value) { opt in
                    Button {
                        selection = opt.value
                    } label: {
                        Text(opt.label)
                            .font(DSFont.semibold(13))
                            .padding(.horizontal, 16).padding(.vertical, 7)
                            .background(selection == opt.value ? DS.accent : DS.neutral(2),
                                        in: Capsule())
                            .foregroundStyle(selection == opt.value ? .white : DS.text)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.vertical, 1)
        }
    }
}

/// A small pill toggle (Active↔Paused, Shadow On↔Off, …).
struct PillToggle: View {
    let onLabel: String
    let offLabel: String
    let isOn: Bool
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(isOn ? onLabel : offLabel)
                .font(DSFont.semibold(13))
                .padding(.horizontal, 16).padding(.vertical, 7)
                .background(isOn ? DS.accent2 : DS.neutral(3), in: Capsule())
                .foregroundStyle(isOn ? .white : DS.text)
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Buttons

/// Filled terracotta action.
struct PrimaryButtonStyle: ButtonStyle {
    var tint: Color = DS.accent
    var size: CGFloat = 13
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(DSFont.semibold(size))
            .padding(.horizontal, 16).padding(.vertical, 8)
            .background(tint.opacity(configuration.isPressed ? 0.8 : 1), in: Capsule())
            .foregroundStyle(.white)
            .contentShape(Capsule())
    }
}

/// Outlined / tinted secondary action.
struct SoftButtonStyle: ButtonStyle {
    var tint: Color = DS.text
    var size: CGFloat = 13
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(DSFont.semibold(size))
            .padding(.horizontal, 14).padding(.vertical, 7)
            .background(tint.opacity(configuration.isPressed ? 0.24 : 0.12), in: Capsule())
            .foregroundStyle(tint)
            .contentShape(Capsule())
    }
}

extension ButtonStyle where Self == PrimaryButtonStyle {
    static var organicPrimary: PrimaryButtonStyle { PrimaryButtonStyle() }
    static func organicPrimary(_ tint: Color) -> PrimaryButtonStyle {
        PrimaryButtonStyle(tint: tint)
    }
}
extension ButtonStyle where Self == SoftButtonStyle {
    static var organicSoft: SoftButtonStyle { SoftButtonStyle() }
    static func organicSoft(_ tint: Color) -> SoftButtonStyle { SoftButtonStyle(tint: tint) }
}

/// Pill text field matching the design's input treatment.
struct OrganicField: View {
    let prompt: String
    @Binding var text: String
    var mono: Bool = false
    var width: CGFloat? = nil
    var onSubmit: (() -> Void)? = nil

    var body: some View {
        TextField(prompt, text: $text)
            .textFieldStyle(.plain)
            .font(mono ? DSFont.mono(13) : DSFont.body(14))
            .foregroundStyle(DS.text)
            .padding(.horizontal, 14).padding(.vertical, 8)
            .background(DS.neutral(1), in: Capsule())
            .overlay(Capsule().strokeBorder(DS.divider, lineWidth: 1))
            .frame(maxWidth: width)
            .onSubmit { onSubmit?() }
            #if !os(macOS)
            .autocorrectionDisabled()
            #endif
    }
}

// MARK: - Meters

/// Horizontal progress/strength bar — the design's 8pt rounded track.
struct MeterBar: View {
    /// 0…1
    let value: Double
    var tint: Color = DS.accent
    var height: CGFloat = 8

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(DS.neutral(3))
                Capsule().fill(tint)
                    .frame(width: max(0, min(1, value.isFinite ? value : 0)) * geo.size.width)
            }
        }
        .frame(height: height)
    }
}

/// Labelled sector/exposure bar row.
struct BarRow: View {
    let label: String
    let valueText: String
    /// 0…1
    let fraction: Double
    var tint: Color = DS.accent

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text(label).font(DSFont.body(13)).foregroundStyle(DS.text).lineLimit(1)
                Spacer(minLength: 8)
                Text(valueText).font(DSFont.semibold(13)).foregroundStyle(DS.text)
            }
            MeterBar(value: fraction, tint: tint)
        }
    }
}

/// Confidence 0–100 with the band coloring the bot uses.
struct ConfidenceMeter: View {
    let confidence: Int?
    var body: some View {
        let c = Double(confidence ?? 0)
        MeterBar(value: c / 100, tint: c >= 75 ? DS.up : c >= 60 ? DS.caution : DS.down)
    }
}

// MARK: - Trading primitives

/// Colored EMA ribbon state badge (BUY / SELL / NEUTRAL / …).
struct RibbonBadge: View {
    let state: String?
    var body: some View {
        // long diagnostic states (INSUFFICIENT_DATA, ERROR) must not blow up
        // row layouts — shorten them and keep the full text as a tooltip
        let raw = state ?? "—"
        let short: String = switch raw {
        case "INSUFFICIENT_DATA": "NO DATA"
        case "ERROR": "ERR"
        default: raw
        }
        let tone: TagTone = raw == "BUY" ? .up : raw == "SELL" ? .down : .neutral
        Tag(short, tone: tone).help(raw)
    }
}

/// P&L text with sign coloring and column-aligned digits.
struct PnLText: View {
    let value: Double?
    var pct: Double? = nil
    var size: CGFloat = 14
    var weight: Font.Weight = .semibold

    var body: some View {
        let str = Fmt.usdSigned(value) + (pct != nil ? " · \(Fmt.pct(pct))" : "")
        Text(value == nil ? "—" : str)
            .font(DSFont.num(size, weight))
            .foregroundStyle(value.pnlColor)
    }
}

/// Percent-first P&L (the design's "+20.61% · $2,928.00" ordering).
struct PnLPair: View {
    let dollars: Double?
    let pct: Double?
    var size: CGFloat = 13

    var body: some View {
        let color = (pct ?? dollars ?? 0).pnlColor
        VStack(alignment: .trailing, spacing: 1) {
            Text(pct == nil ? Fmt.usdSigned(dollars) : Fmt.pct(pct))
                .font(DSFont.num(size, .semibold))
            if pct != nil, dollars != nil {
                Text(Fmt.usdSigned(dollars))
                    .font(DSFont.num(size - 1))
            }
        }
        .foregroundStyle(color)
    }
}

struct KeyValueRow: View {
    let key: String
    let value: String
    var valueColor: Color? = nil
    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(key).font(DSFont.body(13)).foregroundStyle(DS.textMuted)
            Spacer(minLength: 12)
            Text(value)
                .font(DSFont.num(13, .medium))
                .foregroundStyle(valueColor ?? DS.text)
                .multilineTextAlignment(.trailing)
        }
    }
}

/// Small stacked label/value pair used in detail grids.
struct MicroStat: View {
    let key: String
    let value: String
    var color: Color? = nil
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(key.uppercased())
                .font(DSFont.label(10)).kerning(0.5)
                .foregroundStyle(DS.textFaint)
            Text(value)
                .font(DSFont.num(14, .medium))
                .foregroundStyle(color ?? DS.text)
                .lineLimit(1).minimumScaleFactor(0.7)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct WarningBox: View {
    let text: String
    var body: some View {
        Label(text, systemImage: "exclamationmark.triangle.fill")
            .font(DSFont.body(13))
            .foregroundStyle(DS.caution)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(12)
            .background(DS.caution.opacity(0.10),
                        in: RoundedRectangle(cornerRadius: DS.rMd, style: .continuous))
    }
}

struct ErrorBox: View {
    let text: String
    var body: some View {
        Label(text, systemImage: "xmark.octagon.fill")
            .font(DSFont.body(13))
            .foregroundStyle(DS.down)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(12)
            .background(DS.down.opacity(0.10),
                        in: RoundedRectangle(cornerRadius: DS.rMd, style: .continuous))
    }
}

/// Empty / loading placeholder in the Organic voice.
struct EmptyNote: View {
    let text: String
    var icon: String? = nil
    var body: some View {
        HStack(spacing: 8) {
            if let icon {
                Image(systemName: icon).font(.system(size: 13)).foregroundStyle(DS.textFaint)
            }
            Text(text).font(DSFont.body(13)).foregroundStyle(DS.textMuted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 6)
    }
}

struct LoadingScreen: View {
    let title: String
    var detail: String? = nil
    var body: some View {
        VStack(spacing: DS.s3) {
            ProgressView().controlSize(.large).tint(DS.accent)
            Text(title).font(DSFont.heading(20)).foregroundStyle(DS.text)
            if let detail {
                Text(detail).font(DSFont.body(13)).foregroundStyle(DS.textMuted)
                    .multilineTextAlignment(.center)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 260)
    }
}

/// Press-and-hold confirm button for real-money / bot-control actions.
struct HoldToConfirmButton: View {
    let title: String
    var tint: Color = DS.down
    var duration: Double = 1.2
    let action: () -> Void

    @State private var progress: Double = 0
    @State private var timer: Timer?
    @State private var fired = false

    var body: some View {
        ZStack {
            Capsule().fill(tint.opacity(0.14))
            GeometryReader { geo in
                Capsule()
                    .fill(tint.opacity(0.42))
                    .frame(width: geo.size.width * progress)
                    .animation(.linear(duration: 0.05), value: progress)
            }
            .clipShape(Capsule())
            Capsule().strokeBorder(tint, lineWidth: 1.5)
            Label(title, systemImage: "hand.tap.fill")
                .font(DSFont.bold(15))
                .foregroundStyle(tint)
                .padding(.horizontal, 12)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
        .frame(height: 48)
        .contentShape(Capsule())
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
    var width: CGFloat = 84
    var height: CGFloat = 24

    var body: some View {
        if points.count > 1 {
            let up = (points.last ?? 0) >= (points.first ?? 0)
            Chart(Array(points.enumerated()), id: \.offset) { item in
                LineMark(x: .value("i", item.offset), y: .value("v", item.element))
                    .lineStyle(StrokeStyle(lineWidth: 1.6, lineCap: .round, lineJoin: .round))
                    .foregroundStyle(up ? DS.up : DS.down)
                    .interpolationMethod(.monotone)
            }
            .chartXAxis(.hidden)
            .chartYAxis(.hidden)
            .chartYScale(domain: (points.min() ?? 0)...(points.max() ?? 1))
            .frame(width: width, height: height)
        } else {
            Color.clear.frame(width: width, height: height)
        }
    }
}

// MARK: - Tables

/// Per-cell width weight. `0` means "size to content" — that's the default so
/// icons, buttons and fixed-width stamps dropped straight into a `TRow` keep
/// their intrinsic size, and only explicit `TCell`s share the leftover width.
private struct CellWeightKey: LayoutValueKey {
    static let defaultValue: CGFloat = 0
}

/// Divides a row's width by weight instead of by layout priority.
///
/// Priority is the wrong tool here: every cell wants infinite width, so the
/// highest-priority one takes *all* the slack and the rest collapse to zero
/// (which is how the watchlist lost its price column). This measures the
/// zero-weight children at their ideal size and splits what's left.
struct WeightedRow: Layout {
    var spacing: CGFloat = DS.s3
    var verticalPadding: CGFloat = 0

    private func widths(total: CGFloat, subviews: Subviews) -> [CGFloat] {
        let n = subviews.count
        guard n > 0 else { return [] }
        var out = [CGFloat](repeating: 0, count: n)
        var flexible = max(0, total - spacing * CGFloat(n - 1))
        var weightSum: CGFloat = 0
        for (i, sv) in subviews.enumerated() {
            let w = sv[CellWeightKey.self]
            if w <= 0 {
                let ideal = sv.sizeThatFits(.unspecified).width
                out[i] = ideal
                flexible -= ideal
            } else {
                weightSum += w
            }
        }
        flexible = max(0, flexible)
        if weightSum > 0 {
            for (i, sv) in subviews.enumerated() {
                let w = sv[CellWeightKey.self]
                if w > 0 { out[i] = flexible * w / weightSum }
            }
        }
        return out
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews,
                      cache: inout ()) -> CGSize {
        let total = proposal.width ?? subviews.reduce(0) {
            $0 + $1.sizeThatFits(.unspecified).width
        }
        let ws = widths(total: total, subviews: subviews)
        let h = zip(subviews, ws).map {
            $0.sizeThatFits(ProposedViewSize(width: $1, height: nil)).height
        }.max() ?? 0
        return CGSize(width: total, height: h + verticalPadding * 2)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize,
                       subviews: Subviews, cache: inout ()) {
        let ws = widths(total: bounds.width, subviews: subviews)
        var x = bounds.minX
        for (sv, w) in zip(subviews, ws) {
            sv.place(at: CGPoint(x: x, y: bounds.midY), anchor: .leading,
                     proposal: ProposedViewSize(width: w, height: nil))
            x += w + spacing
        }
    }
}

/// Uppercase column header row. Pass `weights` matching the body row's `TCell`
/// weights so the header stays glued to its columns.
struct THeader: View {
    let columns: [String]
    /// Per-column relative widths; nil = equal.
    var weights: [CGFloat]? = nil
    var alignments: [HorizontalAlignment]? = nil
    /// Width of a fixed leading gutter in the body rows (e.g. a rank stamp).
    var fixedLeading: CGFloat? = nil

    var body: some View {
        WeightedRow {
            if let fixedLeading {
                Color.clear.frame(width: fixedLeading, height: 1)
            }
            ForEach(Array(columns.enumerated()), id: \.offset) { i, c in
                Text(c.uppercased())
                    .font(DSFont.semibold(11))
                    .kerning(0.6)
                    .foregroundStyle(DS.textMuted)
                    .lineLimit(1)
                    .frame(maxWidth: .infinity,
                           alignment: Alignment(horizontal: alignments?[safe: i] ?? .leading,
                                                vertical: .center))
                    .layoutValue(key: CellWeightKey.self, value: weights?[safe: i] ?? 1)
            }
        }
        .padding(.vertical, 10)
    }
}

/// A table body row with the design's hairline separator.
struct TRow<Content: View>: View {
    var showsDivider: Bool = true
    var onTap: (() -> Void)? = nil
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(spacing: 0) {
            WeightedRow { content() }
                .padding(.vertical, 11)
                .contentShape(Rectangle())
                .onTapGesture { onTap?() }
            if showsDivider {
                Rectangle().fill(DS.divider).frame(height: 1)
            }
        }
    }
}

/// A flexible table cell: takes a share of the row's leftover width.
struct TCell<Content: View>: View {
    var align: HorizontalAlignment = .leading
    var weight: CGFloat = 1
    @ViewBuilder var content: () -> Content
    var body: some View {
        content()
            .frame(maxWidth: .infinity,
                   alignment: Alignment(horizontal: align, vertical: .center))
            .layoutValue(key: CellWeightKey.self, value: weight)
    }
}

extension Array {
    subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}

// MARK: - Layout helpers

/// Measures the width actually offered to it and hands `isCompact` to the
/// content builder. The reliable way to adapt card pairs / dense rows to
/// window resizing — ViewThatFits can't help here because cards and charts
/// are infinitely compressible, so the wide variant always "fits".
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
/// `ratio` is a real width ratio (first : second), not a layout priority —
/// cards are infinitely compressible, so priorities alone give lopsided
/// columns whenever one side happens to hold longer text.
struct AdaptivePair<A: View, B: View>: View {
    var compactBelow: CGFloat = 680
    /// Width of the first column relative to the second.
    var ratio: CGFloat = 1
    @ViewBuilder let first: () -> A
    @ViewBuilder let second: () -> B

    @State private var width: CGFloat = 0

    var body: some View {
        Group {
            if width > 0, width >= compactBelow {
                let usable = width - DS.s4
                let firstWidth = usable * ratio / (ratio + 1)
                HStack(alignment: .top, spacing: DS.s4) {
                    first().frame(width: max(180, firstWidth), alignment: .topLeading)
                    second().frame(width: max(180, usable - firstWidth), alignment: .topLeading)
                }
            } else {
                VStack(alignment: .leading, spacing: DS.s4) { first(); second() }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
    }
}

/// Wrapping chip cloud (symbol lists, source verdicts).
struct ChipCloud<T: Hashable, Content: View>: View {
    let items: [T]
    var minWidth: CGFloat = 78
    @ViewBuilder let chip: (T) -> Content

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: minWidth), spacing: 6)],
                  alignment: .leading, spacing: 6) {
            ForEach(items, id: \.self) { chip($0) }
        }
    }
}

// MARK: - Screen scaffolding

/// The per-screen H1 + one-line subtitle.
struct ScreenHeader: View {
    let title: String
    let subtitle: String
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(DSFont.heading(32))
                .foregroundStyle(DS.text)
            Text(subtitle)
                .font(DSFont.body(14))
                .foregroundStyle(DS.textMuted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Every screen's outer scroll container: header, then stacked sections.
struct Screen<Content: View>: View {
    let title: String
    let subtitle: String
    var refresh: (() async -> Void)? = nil
    @ViewBuilder var content: () -> Content

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DS.s6) {
                ScreenHeader(title: title, subtitle: subtitle)
                content()
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, DS.s8)
            .padding(.top, DS.s6)
            .padding(.bottom, 70)
        }
        .background(DS.bg)
        .scrollContentBackground(.hidden)
        .refreshable { await refresh?() }
    }
}
