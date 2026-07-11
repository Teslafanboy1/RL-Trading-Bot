//
//  ThinkingView.swift — the bot's latest research as native cards:
//  ranked picks, momentum options watch, self-rewrite queue, midweek review.
//

import SwiftUI

struct ThinkingView: View {
    @Environment(AppModel.self) private var model
    @State private var rawFile: ResearchFileMeta?

    private var t: Thinking? { model.thinking }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let err = model.tabErrors[.thinking] { ErrorBox(text: err) }
                header
                picksGrid
                momentumCard
                queueCard
                midweekCard
            }
            .padding()
        }
        .opacity(t == nil ? 0 : 1)
        .refreshable { await model.load(.thinking) }
        .task { if t == nil { await model.load(.thinking) } }
        .sheet(item: $rawFile) { meta in
            DocumentSheet(title: meta.file, loader: { await model.researchText(meta.file) })
        }
        .overlay {
            if t == nil { ContentUnavailableView("Loading research…", systemImage: "brain") }
        }
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(t?.picksFile?.file ?? "no research file found")
                    .font(.headline.monospaced())
                if let hdr = t?.picks?.header, !hdr.isEmpty {
                    Text(hdr.prefix(3).joined(separator: "  ·  ")
                        .replacingOccurrences(of: "**", with: ""))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
            Spacer()
            if let meta = t?.picksFile {
                Button("Raw") { rawFile = meta }
                    .buttonStyle(.bordered).controlSize(.small)
            }
        }
    }

    private var picksGrid: some View {
        let cols = [GridItem(.adaptive(minimum: 330), spacing: 12)]
        return LazyVGrid(columns: cols, alignment: .leading, spacing: 12) {
            ForEach(t?.picks?.picks ?? []) { pick in
                PickCard(pick: pick)
            }
        }
    }

    @ViewBuilder
    private var momentumCard: some View {
        if let mw = t?.momentumWatch, !mw.isEmpty {
            GroupBox {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(mw.keys.sorted(), id: \.self) { sym in
                        HStack(alignment: .firstTextBaseline) {
                            Text(sym).font(.subheadline.bold().monospaced())
                            Text("conf \(mw[sym]?.confidence ?? 0)")
                                .font(.caption.monospaced()).foregroundStyle(.secondary)
                            Text(mw[sym]?.catalyst ?? "")
                                .font(.callout).foregroundStyle(.primary)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } label: {
                Label("Momentum options watch (paper sleeve feed)", systemImage: "sparkles")
            }
        }
    }

    private var queueCard: some View {
        GroupBox {
            let queue = (t?.rewriteQueue ?? []).reversed()
            if queue.isEmpty {
                Text("queue empty").foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(queue.prefix(12))) { q in
                        HStack(spacing: 8) {
                            Image(systemName: q.done == true ? "checkmark.circle.fill" : "circle.dotted")
                                .foregroundStyle(q.done == true ? Color.up : Color.caution)
                            Text(q.tradeId != nil
                                 ? "\(q.tradeId!) \(q.symbol ?? "") \(q.outcome ?? "") \(q.pnl ?? "")"
                                 : String((q.raw ?? "").prefix(80)))
                                .font(.callout.monospaced())
                                .foregroundStyle(q.done == true ? .secondary : .primary)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        } label: {
            let pending = (t?.rewriteQueue ?? []).filter { $0.done != true }.count
            Label("Self-rewrite queue (\(pending) pending)", systemImage: "arrow.triangle.2.circlepath.circle")
        }
    }

    @ViewBuilder
    private var midweekCard: some View {
        if let mid = t?.midweek, let sections = mid.sections, !sections.isEmpty {
            GroupBox {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(sections.prefix(6)) { s in
                        DisclosureGroup(s.title ?? "section") {
                            MarkdownLite(text: s.body ?? "")
                                .frame(maxHeight: 320)
                        }
                        .font(.subheadline.weight(.semibold))
                    }
                }
            } label: {
                Label("Midweek review — \(t?.midweekFile?.file ?? "")",
                      systemImage: "calendar.badge.checkmark")
            }
        }
    }
}

// MARK: - Pick card

struct PickCard: View {
    @Environment(AppModel.self) private var model
    let pick: Pick

    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    Text("#\(pick.rank ?? 0) \(pick.symbol)")
                        .font(.title3.bold().monospaced())
                    RibbonBadge(state: pick.emaState)
                    if pick.held == true {
                        Text("HELD").font(.caption2.bold())
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(Color.up.opacity(0.15), in: Capsule())
                            .foregroundStyle(Color.up)
                    }
                    Spacer()
                    Text("\(pick.confidence.map(String.init) ?? "—")/100")
                        .font(.headline.monospacedDigit())
                }
                if let name = pick.name {
                    Text(name).font(.caption).foregroundStyle(.secondary)
                }
                ConfidenceMeter(confidence: pick.confidence)

                field("EMA", pick.fields?["ema"])
                field("Momentum", pick.fields?["momentum"])
                field("Targets", pick.fields?["targets"])
                field("Allocation", pick.fields?["allocation"])
                field("Sources", pick.fields?["sources"])

                if let inv = pick.fields?["invalidates"], !inv.isEmpty {
                    Label(inv, systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundStyle(Color.caution)
                        .padding(8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.caution.opacity(0.08),
                                    in: RoundedRectangle(cornerRadius: 6))
                }

                HStack {
                    Button {
                        model.orderPrefill = .init(symbol: pick.symbol, side: "buy")
                        model.showOrderSheet = true
                    } label: { Label("Buy", systemImage: "cart.badge.plus") }
                        .buttonStyle(.bordered).controlSize(.small).tint(.up)
                    if let px = pick.price {
                        Text("now \(Fmt.usd(px))")
                            .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    }
                    Spacer()
                }
            }
        }
    }

    @ViewBuilder
    private func field(_ label: String, _ value: String?) -> some View {
        if let value, !value.isEmpty {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(label).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    .frame(width: 74, alignment: .leading)
                Text(value).font(.caption)
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
        NavigationStack {
            Group {
                if let text {
                    MarkdownLite(text: text)
                        .textSelection(.enabled)
                } else {
                    ProgressView()
                }
            }
            .navigationTitle(title)
            #if !os(macOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
        .task { text = await loader() }
        #if os(macOS)
        .frame(width: 640, height: 640)
        #endif
    }
}
