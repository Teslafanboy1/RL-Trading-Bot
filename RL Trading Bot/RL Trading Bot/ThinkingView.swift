//
//  ThinkingView.swift — the bot's latest research as native cards:
//  ranked picks, momentum options watch, self-rewrite queue, midweek review.
//

import SwiftUI

struct ThinkingView: View {
    @Environment(AppModel.self) private var model
    @State private var rawFile: ResearchFileMeta?
    @State private var openSection: String?

    private var t: Thinking? { model.thinking }

    var body: some View {
        Group {
            if t == nil {
                LoadingScreen(title: "Loading research…",
                              detail: "the latest weekend picks and midweek review")
                    .background(DS.bg)
            } else {
                Screen(title: "Thinking", subtitle: AppTab.thinking.blurb,
                       refresh: { await model.load(.thinking) }) {
                    if let err = model.tabErrors[.thinking] { ErrorBox(text: err) }
                    header
                    picksGrid
                    momentumCard
                    queueCard
                    midweekCard
                }
            }
        }
        .task { if t == nil { await model.load(.thinking) } }
        .sheet(item: $rawFile) { meta in
            DocumentSheet(title: meta.file, loader: { await model.researchText(meta.file) })
        }
    }

    private var header: some View {
        Card {
            WidthReader(compactBelow: 560) { compact in
                let left = VStack(alignment: .leading, spacing: 4) {
                    Text(t?.picksFile?.file ?? "no research file found")
                        .font(DSFont.mono(13))
                        .foregroundStyle(DS.text)
                    if let hdr = t?.picks?.header, !hdr.isEmpty {
                        Text(hdr.prefix(3).joined(separator: "  ·  ")
                            .replacingOccurrences(of: "**", with: ""))
                            .font(DSFont.body(13))
                            .foregroundStyle(DS.textMuted)
                            .lineLimit(3)
                    }
                }
                let right = Group {
                    if let meta = t?.picksFile {
                        Button("Raw file") { rawFile = meta }
                            .buttonStyle(.organicSoft)
                    }
                }
                if compact {
                    VStack(alignment: .leading, spacing: DS.s2) { left; right }
                } else {
                    HStack { left; Spacer(minLength: DS.s2); right }
                }
            }
        }
    }

    private var picksGrid: some View {
        let picks = t?.picks?.picks ?? []
        return Group {
            if picks.isEmpty {
                Card("Ranked picks", subtitle: "from the morning research run") {
                    EmptyNote(text: "No parseable picks in the latest research file.",
                              icon: "lightbulb")
                }
            } else {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 360), spacing: DS.s4)],
                          alignment: .leading, spacing: DS.s4) {
                    ForEach(picks) { pick in PickCard(pick: pick) }
                }
            }
        }
    }

    @ViewBuilder
    private var momentumCard: some View {
        if let mw = t?.momentumWatch, !mw.isEmpty {
            Card("Momentum options watch",
                 subtitle: "the catalyst feed the paper options sleeve reuses") {
                VStack(spacing: 0) {
                    let keys = mw.keys.sorted()
                    ForEach(keys, id: \.self) { sym in
                        TRow(showsDivider: sym != keys.last) {
                            TCell {
                                Text(sym).font(DSFont.bold(14)).foregroundStyle(DS.text)
                            }
                            TCell(align: .leading) {
                                Tag("conf \(mw[sym]?.confidence ?? 0)",
                                    tone: (mw[sym]?.confidence ?? 0) >= 75 ? .up : .caution,
                                    size: 10)
                            }
                            TCell(weight: 5) {
                                Text(mw[sym]?.catalyst ?? "")
                                    .font(DSFont.body(13))
                                    .foregroundStyle(DS.text)
                                    .lineLimit(3)
                            }
                        }
                    }
                }
            }
        }
    }

    private var queueCard: some View {
        let queue = Array((t?.rewriteQueue ?? []).reversed().prefix(12))
        let pending = (t?.rewriteQueue ?? []).filter { $0.done != true }.count
        return Card("Self-rewrite queue",
                    subtitle: "\(pending) pending · skill_5 consumes one entry per cycle") {
            if queue.isEmpty {
                EmptyNote(text: "Queue empty — nothing waiting for a strategy rewrite.",
                          icon: "checkmark.circle")
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(queue.enumerated()), id: \.offset) { i, q in
                        TRow(showsDivider: i < queue.count - 1) {
                            Image(systemName: q.done == true ? "checkmark.circle.fill" : "circle.dotted")
                                .font(.system(size: 13))
                                .foregroundStyle(q.done == true ? DS.up : DS.caution)
                                .frame(width: 18)
                            TCell(weight: 5) {
                                Text(q.tradeId != nil
                                     ? "\(q.tradeId!)  \(q.symbol ?? "")  \(q.outcome ?? "")  \(q.pnl ?? "")"
                                     : String((q.raw ?? "").prefix(100)))
                                    .font(DSFont.mono(12))
                                    .foregroundStyle(q.done == true ? DS.textMuted : DS.text)
                                    .lineLimit(2)
                            }
                            TCell(align: .trailing) {
                                Tag(q.done == true ? "processed" : "queued",
                                    tone: q.done == true ? .neutral : .caution, size: 10)
                            }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var midweekCard: some View {
        if let mid = t?.midweek, let sections = mid.sections, !sections.isEmpty {
            Card("Midweek review", subtitle: t?.midweekFile?.file ?? "") {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(sections.prefix(8).enumerated()), id: \.offset) { i, s in
                        let title = s.title ?? "section"
                        let open = openSection == title
                        VStack(alignment: .leading, spacing: DS.s2) {
                            Button {
                                openSection = open ? nil : title
                            } label: {
                                HStack {
                                    Image(systemName: open ? "chevron.down" : "chevron.right")
                                        .font(.system(size: 10, weight: .bold))
                                        .foregroundStyle(DS.textFaint)
                                    Text(title).font(DSFont.semibold(14))
                                        .foregroundStyle(DS.text)
                                    Spacer()
                                }
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            if open {
                                MarkdownLite(text: s.body ?? "", scrolls: false)
                                    .padding(.leading, 20)
                                    .padding(.bottom, DS.s2)
                            }
                        }
                        .padding(.vertical, 10)
                        if i < min(sections.count, 8) - 1 {
                            Rectangle().fill(DS.divider).frame(height: 1)
                        }
                    }
                }
            }
        }
    }
}

// MARK: - Pick card

struct PickCard: View {
    @Environment(AppModel.self) private var model
    let pick: Pick

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: DS.s2) {
                HStack(spacing: 8) {
                    Tag("#\(pick.rank ?? 0)", tone: .accent, size: 11)
                    Text(pick.symbol).font(DSFont.heading(20)).foregroundStyle(DS.text)
                    RibbonBadge(state: pick.emaState)
                    if pick.held == true { Tag("HELD", tone: .up, size: 10) }
                    Spacer()
                    Text("\(pick.confidence.map(String.init) ?? "—")/100")
                        .font(DSFont.num(16, .bold))
                        .foregroundStyle(DS.text)
                }
                if let name = pick.name, !name.isEmpty {
                    Text(name).font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                }
                ConfidenceMeter(confidence: pick.confidence)

                VStack(alignment: .leading, spacing: 5) {
                    field("EMA", pick.fields?["ema"])
                    field("Momentum", pick.fields?["momentum"])
                    field("Targets", pick.fields?["targets"])
                    field("Allocation", pick.fields?["allocation"])
                    field("Sources", pick.fields?["sources"])
                }

                if let inv = pick.fields?["invalidates"], !inv.isEmpty {
                    WarningBox(text: inv)
                }

                HStack(spacing: DS.s2) {
                    Button {
                        model.orderPrefill = .init(symbol: pick.symbol, side: "buy")
                        model.showOrderSheet = true
                    } label: { Label("Buy", systemImage: "cart.badge.plus") }
                        .buttonStyle(SoftButtonStyle(tint: DS.up))
                    Button { model.analyze(pick.symbol) } label: {
                        Label("Analyze", systemImage: "waveform.and.magnifyingglass")
                    }
                    .buttonStyle(.organicSoft)
                    Spacer()
                    if let px = pick.price {
                        Text("now \(Fmt.usd(px))")
                            .font(DSFont.num(12)).foregroundStyle(DS.textMuted)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func field(_ label: String, _ value: String?) -> some View {
        if let value, !value.isEmpty {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(label.uppercased())
                    .font(DSFont.label(10)).kerning(0.5)
                    .foregroundStyle(DS.textFaint)
                    .frame(width: 76, alignment: .leading)
                Text(value).font(DSFont.body(12)).foregroundStyle(DS.text)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

// MARK: - Raw document sheet (research files, postmortems)

struct DocumentSheet: View {
    @Environment(\.dismiss) private var dismiss
    let title: String
    let loader: () async -> String
    @State private var text: String?

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(title).font(DSFont.heading(17)).foregroundStyle(DS.text)
                    .lineLimit(1).truncationMode(.middle)
                Spacer()
                Button("Done") { dismiss() }
                    .buttonStyle(.organicPrimary)
            }
            .padding(DS.s4)
            .background(DS.surface)
            Rectangle().fill(DS.divider).frame(height: 1)
            Group {
                if let text {
                    MarkdownLite(text: text)
                } else {
                    LoadingScreen(title: "Loading…")
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(DS.bg)
        }
        .background(DS.bg)
        .task { text = await loader() }
        #if os(macOS)
        .frame(width: 700, height: 680)
        #endif
    }
}
