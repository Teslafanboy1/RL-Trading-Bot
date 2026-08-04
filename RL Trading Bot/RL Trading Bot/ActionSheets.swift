//
//  ActionSheets.swift — PIN arming, the order ticket, halt/clear-halt,
//  stop-override editor, cash flow, and Settings — all in the Organic shell.
//

import SwiftUI

// MARK: - Sheet scaffolding

/// Shared chrome for every modal: warm surface header, scrolling body,
/// consistent sizing on Mac and iPhone.
struct SheetShell<Content: View>: View {
    let title: String
    var subtitle: String? = nil
    var icon: String? = nil
    var iconTint: Color = DS.accent
    var width: CGFloat = 470
    var height: CGFloat = 560
    let onClose: () -> Void
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .center, spacing: DS.s3) {
                if let icon {
                    Image(systemName: icon)
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(.white)
                        .frame(width: 36, height: 36)
                        .background(iconTint,
                                    in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(DSFont.heading(19)).foregroundStyle(DS.text)
                    if let subtitle {
                        Text(subtitle).font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                            .lineLimit(2)
                    }
                }
                Spacer(minLength: DS.s2)
                Button("Close") { onClose() }
                    .buttonStyle(.organicSoft)
            }
            .padding(DS.s4)
            .background(DS.surface)
            Rectangle().fill(DS.divider).frame(height: 1)
            ScrollView {
                VStack(alignment: .leading, spacing: DS.s4) { content() }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(DS.s4)
            }
            .background(DS.bg)
        }
        .background(DS.bg)
        .tint(DS.accent)
        #if os(macOS)
        .frame(width: width, height: height)
        #else
        .presentationDetents([.large])
        #endif
    }
}

/// A labelled field group inside a sheet.
struct SheetField<Content: View>: View {
    let label: String
    var hint: String? = nil
    @ViewBuilder var content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label.uppercased())
                .font(DSFont.label(10)).kerning(0.5)
                .foregroundStyle(DS.textFaint)
            content()
            if let hint {
                Text(hint).font(DSFont.body(11)).foregroundStyle(DS.textMuted)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Arm sheet

struct ArmSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var pin = ""
    @State private var error: String?
    @State private var busy = false
    @State private var offerSave = false
    @FocusState private var focused: Bool

    var body: some View {
        SheetShell(title: "Arm control actions",
                   subtitle: "Money-moving and bot-control actions need your PIN.",
                   icon: "lock.shield.fill", width: 430, height: 470,
                   onClose: { model.pendingArmedAction = nil; dismiss() }) {
            Text("Arms this device for 5 minutes. The token is bound to this IP and expires on its own.")
                .font(DSFont.body(14)).foregroundStyle(DS.textMuted)
                .fixedSize(horizontal: false, vertical: true)

            SheetField(label: "PIN") {
                SecureField("••••", text: $pin)
                    .textFieldStyle(.plain)
                    .font(DSFont.mono(20))
                    .multilineTextAlignment(.center)
                    .padding(.vertical, 12)
                    .frame(maxWidth: .infinity)
                    .background(DS.neutral(1), in: Capsule())
                    .overlay(Capsule().strokeBorder(DS.divider, lineWidth: 1))
                    .focused($focused)
                    .onSubmit { submit() }
                    #if !os(macOS)
                    .keyboardType(.numberPad)
                    #endif
            }

            if let error { ErrorBox(text: error) }

            if offerSave {
                Card {
                    VStack(alignment: .leading, spacing: DS.s3) {
                        Text("Save this PIN for Face ID / Touch ID quick-arm?")
                            .font(DSFont.semibold(14)).foregroundStyle(DS.text)
                        Text("It's stored in the Keychain behind biometrics — never in the app.")
                            .font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                        HStack(spacing: DS.s2) {
                            Button("Not now") { dismiss() }
                                .buttonStyle(.organicSoft)
                            Button("Enable quick-arm") {
                                try? PINStore.save(pin: pin)
                                dismiss()
                            }
                            .buttonStyle(.organicPrimary)
                        }
                    }
                }
            } else {
                VStack(spacing: DS.s2) {
                    Button {
                        submit()
                    } label: {
                        if busy { ProgressView().controlSize(.small) }
                        else { Text("Arm").frame(maxWidth: .infinity) }
                    }
                    .buttonStyle(.organicPrimary)
                    .disabled(pin.count < 4 || busy)

                    if model.quickArmAvailable {
                        Button {
                            Task {
                                busy = true
                                error = await model.quickArm()
                                busy = false
                                if error == nil { dismiss() }
                            }
                        } label: {
                            Label("Quick-arm with Face ID / Touch ID", systemImage: "faceid")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.organicSoft)
                    }
                }
            }
        }
        .onAppear { focused = true }
    }

    private func submit() {
        guard pin.count >= 4, !busy else { return }
        Task {
            busy = true
            error = await model.arm(pin: pin)
            busy = false
            if error == nil {
                if !model.quickArmAvailable { offerSave = true } else { dismiss() }
            }
        }
    }
}

// MARK: - Order ticket

struct OrderTicketSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    @State private var symbol: String
    @State private var side: String
    @State private var useDollars: Bool
    @State private var quantityText: String
    @State private var dollarsText = ""
    @State private var orderType: String
    @State private var limitText = ""
    @State private var stopText = ""
    @State private var tif = "gfd"
    @State private var showBrokerReview = false

    @State private var phase: Phase = .form
    @State private var errorText: String?

    enum Phase { case form, previewing, preview(OrderPreview), placing, placed(String) }

    init(prefill: AppModel.OrderPrefill) {
        _symbol = State(initialValue: prefill.symbol)
        _side = State(initialValue: prefill.side)
        _orderType = State(initialValue: prefill.type)
        _useDollars = State(initialValue: prefill.quantity == nil)
        _quantityText = State(initialValue: prefill.quantity.map { Fmt.num($0, dp: 6) } ?? "")
    }

    var body: some View {
        SheetShell(title: "Order ticket",
                   subtitle: "Real money · account \(model.meta?.broker?.account ?? "—")",
                   icon: "bolt.fill", width: 500, height: 680,
                   onClose: { dismiss() }) {
            ticketForm
            switch phase {
            case .form:
                Button { preview() } label: {
                    Text("Preview order").frame(maxWidth: .infinity)
                }
                .buttonStyle(.organicPrimary)
                .disabled(symbol.isEmpty || (useDollars ? dollarsText.isEmpty : quantityText.isEmpty))
            case .previewing:
                HStack { Spacer(); ProgressView("Previewing…"); Spacer() }
            case .preview(let p):
                previewSection(p)
            case .placing:
                HStack { Spacer(); ProgressView("Placing…"); Spacer() }
            case .placed(let ref):
                Label("Order placed (\(ref))", systemImage: "checkmark.circle.fill")
                    .font(DSFont.semibold(15))
                    .foregroundStyle(DS.up)
            }
            if let errorText { ErrorBox(text: errorText) }
        }
    }

    private var ticketForm: some View {
        Card {
            VStack(alignment: .leading, spacing: DS.s3) {
                HStack(spacing: DS.s2) {
                    TextField("Symbol", text: $symbol)
                        .textFieldStyle(.plain)
                        .font(DSFont.mono(18, .bold))
                        .foregroundStyle(DS.text)
                        .padding(.horizontal, 14).padding(.vertical, 8)
                        .background(DS.neutral(1), in: Capsule())
                        .overlay(Capsule().strokeBorder(DS.divider, lineWidth: 1))
                        #if !os(macOS)
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                        #endif
                    FilterPills(options: [("buy", "BUY"), ("sell", "SELL")], selection: $side)
                        .frame(maxWidth: 170)
                }
                SheetField(label: "Size by") {
                    FilterPills(options: [(true, "$ amount"), (false, "# shares")],
                                selection: $useDollars)
                }
                if useDollars {
                    SheetField(label: "USD notional",
                               hint: "Fractional dollar orders — the broker converts to shares.") {
                        OrganicField(prompt: "e.g. 50", text: $dollarsText, mono: true)
                    }
                } else {
                    SheetField(label: "Shares", hint: "Fractional quantities are allowed.") {
                        OrganicField(prompt: "e.g. 1.25", text: $quantityText, mono: true)
                    }
                }
                SheetField(label: "Order type") {
                    FilterPills(options: [("market", "Market"), ("limit", "Limit"),
                                          ("stop_market", "Stop"), ("stop_limit", "Stop limit")],
                                selection: $orderType)
                }
                if orderType.contains("limit") {
                    SheetField(label: "Limit price") {
                        OrganicField(prompt: "0.00", text: $limitText, mono: true)
                    }
                }
                if orderType.hasPrefix("stop") {
                    SheetField(label: "Stop trigger price") {
                        OrganicField(prompt: "0.00", text: $stopText, mono: true)
                    }
                }
                SheetField(label: "Time in force") {
                    FilterPills(options: [("gfd", "Day (gfd)"), ("gtc", "GTC")], selection: $tif)
                }
            }
        }
    }

    @ViewBuilder
    private func previewSection(_ p: OrderPreview) -> some View {
        Card("Confirm", subtitle: "reviewed by the broker before anything is sent") {
            VStack(alignment: .leading, spacing: DS.s3) {
                let params = p.params
                VStack(spacing: DS.s2) {
                    KeyValueRow(key: "Order",
                                value: "\(params?.side?.uppercased() ?? "") \(params?.symbol ?? "") · \(params?.type ?? "")")
                    if let q = params?.quantity {
                        KeyValueRow(key: "Shares", value: Fmt.num(q, dp: 6))
                    }
                    if let d = params?.dollarAmount {
                        KeyValueRow(key: "Notional", value: Fmt.usd(d))
                    }
                    if let lp = params?.limitPrice {
                        KeyValueRow(key: "Limit", value: Fmt.usd(lp))
                    }
                    if let sp = params?.stopPrice {
                        KeyValueRow(key: "Stop trigger", value: Fmt.usd(sp))
                    }
                    if let px = p.quote?.price {
                        KeyValueRow(key: "Last price", value: Fmt.usd(px))
                    }
                    if let est = p.estCost {
                        KeyValueRow(key: params?.side == "buy" ? "Est. cost" : "Est. proceeds",
                                    value: Fmt.usd(est))
                    }
                    KeyValueRow(key: "Time in force",
                                value: (params?.timeInForce ?? tif).uppercased())
                }
                ForEach(p.warnings ?? [], id: \.self) { WarningBox(text: $0) }
                HoldToConfirmButton(
                    title: "HOLD to \(params?.side?.uppercased() ?? "PLACE") \(params?.symbol ?? "")",
                    tint: params?.side == "buy" ? DS.up : DS.down
                ) {
                    place(previewId: p.previewId ?? "")
                }
                if let review = p.brokerReview {
                    Button { showBrokerReview.toggle() } label: {
                        HStack(spacing: 8) {
                            Image(systemName: showBrokerReview ? "chevron.down" : "chevron.right")
                                .font(.system(size: 10, weight: .bold))
                            Text("Broker pre-trade review").font(DSFont.semibold(13))
                            Spacer()
                        }
                        .foregroundStyle(DS.textMuted)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    if showBrokerReview {
                        ScrollView {
                            Text(review.pretty)
                                .font(DSFont.mono(11))
                                .foregroundStyle(DS.consoleText)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(DS.s3)
                        }
                        .frame(maxHeight: 200)
                        .background(DS.consoleBG,
                                    in: RoundedRectangle(cornerRadius: DS.rMd, style: .continuous))
                    }
                }
            }
        }
    }

    private func preview() {
        errorText = nil
        phase = .previewing
        let qty = useDollars ? nil : Double(quantityText.replacingOccurrences(of: ",", with: ""))
        let dollars = useDollars ? Double(dollarsText.replacingOccurrences(of: ",", with: "")) : nil
        let sym = symbol.trimmingCharacters(in: .whitespaces).uppercased()
        model.requireArm {
            Task {
                do {
                    let p = try await model.previewOrder(
                        symbol: sym, side: side, type: orderType,
                        quantity: qty, dollarAmount: dollars,
                        limitPrice: Double(limitText), stopPrice: Double(stopText),
                        tif: tif)
                    phase = .preview(p)
                } catch {
                    phase = .form
                    errorText = (error as? APIError)?.errorDescription ?? error.localizedDescription
                }
            }
        }
        if !model.isArmed { phase = .form }  // wait for the arm sheet round-trip
    }

    private func place(previewId: String) {
        phase = .placing
        model.requireArm {
            Task {
                do {
                    let r = try await model.placeOrder(previewId: previewId)
                    if r.ok == true {
                        phase = .placed(r.result?["ref_id"]?.stringValue ?? "ok")
                        try? await Task.sleep(for: .seconds(1.5))
                        dismiss()
                    } else {
                        phase = .form
                        errorText = r.error ?? "order not placed"
                    }
                } catch {
                    phase = .form
                    errorText = (error as? APIError)?.errorDescription ?? error.localizedDescription
                }
            }
        }
    }
}

// MARK: - Halt / clear-halt

struct HaltSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var reason = ""
    @State private var error: String?

    var body: some View {
        SheetShell(title: "Force kill-switch HALT",
                   subtitle: "Writes the HALT file the bot reads every cycle",
                   icon: "octagon.fill", iconTint: DS.down,
                   width: 460, height: 430, onClose: { dismiss() }) {
            WarningBox(text: "The bot will NOT trade — including its own stop-loss enforcement — until you clear this. Positions are NOT auto-sold; the independent watchdog keeps alerting.")
            SheetField(label: "Reason", hint: "Logged with the halt so the next session knows why.") {
                OrganicField(prompt: "e.g. broker feed looks wrong", text: $reason)
            }
            HoldToConfirmButton(title: "HOLD to HALT the bot", tint: DS.down) {
                model.requireArm {
                    Task {
                        error = await model.haltBot(reason: reason)
                        if error == nil { dismiss() }
                    }
                }
            }
            if let error { ErrorBox(text: error) }
        }
    }
}

struct ClearHaltSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var confirm = ""
    @State private var error: String?

    var body: some View {
        SheetShell(title: "Clear HALT",
                   subtitle: "Re-arms the bot on its next cycle",
                   icon: "checkmark.shield.fill", iconTint: DS.up,
                   width: 460, height: 430, onClose: { dismiss() }) {
            WarningBox(text: "Understand why it halted first — the HALT reason is on the Risk tab.")
            SheetField(label: "Type CLEAR to confirm") {
                OrganicField(prompt: "CLEAR", text: $confirm, mono: true)
            }
            HoldToConfirmButton(title: "HOLD to CLEAR halt", tint: DS.up) {
                model.requireArm {
                    Task {
                        error = await model.clearHalt(
                            confirm: confirm.trimmingCharacters(in: .whitespaces))
                        if error == nil { dismiss() }
                    }
                }
            }
            if let error { ErrorBox(text: error) }
        }
    }
}

// MARK: - Stop override editor

struct StopOverrideSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let row: RiskRow
    @State private var stopPriceText = ""
    @State private var stopPctText = ""
    @State private var trailPctText = ""
    @State private var error: String?

    var body: some View {
        SheetShell(title: "Stop override — \(row.symbol)",
                   subtitle: "Honored by the bot next cycle; the watchdog snapshot follows",
                   icon: "shield.lefthalf.filled", iconTint: DS.caution,
                   width: 470, height: 560, onClose: { dismiss() }) {
            Card("Current levels") {
                VStack(spacing: DS.s2) {
                    KeyValueRow(key: "Price", value: Fmt.usd(row.price))
                    KeyValueRow(key: "Entry", value: Fmt.usd(row.entry))
                    KeyValueRow(key: "Peak", value: Fmt.usd(row.peak))
                    KeyValueRow(key: "Hard stop", value: Fmt.usd(row.stopPrice),
                                valueColor: DS.down)
                    KeyValueRow(key: "Trailing stop", value: Fmt.usd(row.trailPrice),
                                valueColor: DS.caution)
                }
            }
            SheetField(label: "Absolute stop $",
                       hint: "Overrides the computed hard stop for this symbol only.") {
                OrganicField(prompt: "e.g. 550", text: $stopPriceText, mono: true)
            }
            SheetField(label: "Stop % as fraction", hint: "0.10 = 10% below entry.") {
                OrganicField(prompt: "0.10", text: $stopPctText, mono: true)
            }
            SheetField(label: "Trailing % as fraction", hint: "0.25 = 25% giveback from peak.") {
                OrganicField(prompt: "0.25", text: $trailPctText, mono: true)
            }
            HStack(spacing: DS.s2) {
                Button("Save override") { save(clear: false) }
                    .buttonStyle(.organicPrimary)
                Button("Clear override") { save(clear: true) }
                    .buttonStyle(.organicSoft(DS.down))
            }
            if let error { ErrorBox(text: error) }
        }
        .onAppear {
            if let ov = row.override_?.objectValue {
                if let v = ov["stop_price"]?.numberValue { stopPriceText = Fmt.num(v, dp: 4) }
                if let v = ov["stop_pct"]?.numberValue { stopPctText = String(v) }
                if let v = ov["trail_pct"]?.numberValue { trailPctText = String(v) }
            }
        }
    }

    private func save(clear: Bool) {
        model.requireArm {
            Task {
                error = await model.setStopOverride(
                    symbol: row.symbol,
                    stopPrice: Double(stopPriceText), stopPct: Double(stopPctText),
                    trailPct: Double(trailPctText), clear: clear)
                if error == nil { dismiss() }
            }
        }
    }
}

// MARK: - Cash flow (deposit / withdrawal)

struct CashFlowSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var kind: Kind = .deposit
    @State private var amountText = ""
    @State private var note = ""
    @State private var error: String?

    enum Kind: String, CaseIterable, Identifiable, Hashable {
        case deposit = "Deposit", withdrawal = "Withdrawal"
        var id: String { rawValue }
    }

    var body: some View {
        SheetShell(title: "Declare cash movement",
                   subtitle: "Keeps the performance % and kill-switch honest",
                   icon: "arrow.left.arrow.right.circle.fill",
                   width: 470, height: 500, onClose: { dismiss() }) {
            WarningBox(text: "Tells the bot exactly how much cash moved in/out so it never mistakes a deposit or withdrawal for trading performance. Applied next cycle to month_start_value, current_value and the kill-switch peak — the exact amount you enter, not a guess from a broker read.")
            SheetField(label: "Type") {
                FilterPills(options: Kind.allCases.map { ($0, $0.rawValue) }, selection: $kind)
            }
            SheetField(label: "Amount ($)") {
                OrganicField(prompt: "0.00", text: $amountText, mono: true)
            }
            SheetField(label: "Note (optional)") {
                OrganicField(prompt: "e.g. monthly top-up", text: $note)
            }
            Button("Record \(kind.rawValue.lowercased())") { save() }
                .buttonStyle(.organicPrimary)
                .disabled(Double(amountText) == nil || Double(amountText) == 0)
            if let error { ErrorBox(text: error) }
        }
    }

    private func save() {
        guard let raw = Double(amountText), raw != 0 else { return }
        let amount = kind == .withdrawal ? -abs(raw) : abs(raw)
        model.requireArm {
            Task {
                error = await model.recordCashFlow(amount: amount, note: note)
                if error == nil { dismiss() }
            }
        }
    }
}

// MARK: - Settings

struct SettingsSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var testResult: String?
    @State private var quickArmEnabled = PINStore.isEnabled

    var body: some View {
        @Bindable var model = model
        return SheetShell(title: "Settings",
                          subtitle: "Server, quick-arm and connection status",
                          icon: "slider.horizontal.3",
                          width: 520, height: 600, onClose: { dismiss() }) {
            Card("Server", subtitle: "where the TradeCommand dashboard is listening") {
                VStack(alignment: .leading, spacing: DS.s3) {
                    OrganicField(prompt: "https://your-mac.tailnet.ts.net",
                                 text: $model.serverURLString, mono: true)
                    Button("Test connection") {
                        Task {
                            do {
                                let m: Meta = try await model.api.get("/api/meta")
                                testResult = "✓ connected — broker \(m.broker?.mode ?? "?"), market \(m.marketOpen == true ? "open" : "closed")"
                                await model.refreshTick()
                            } catch {
                                testResult = "✗ " + ((error as? APIError)?.errorDescription
                                                     ?? error.localizedDescription)
                            }
                        }
                    }
                    .buttonStyle(.organicSoft)
                    if let testResult {
                        Text(testResult)
                            .font(DSFont.body(13))
                            .foregroundStyle(testResult.hasPrefix("✓") ? DS.up : DS.down)
                    }
                    #if os(macOS)
                    Text("Local server: run `bash run_dashboard.sh` in the bot repo. On iPhone, use the Tailscale HTTPS URL (`tailscale serve --bg --https=443 http://127.0.0.1:8787`).")
                        .font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                    #endif
                }
            }

            Card("Appearance", subtitle: "the cream \u{201C}Organic\u{201D} palette is the design default") {
                FilterPills(options: AppModel.Appearance.allCases.map { ($0, $0.label) },
                            selection: $model.appearance)
            }

            Card("Quick-arm", subtitle: "biometric shortcut for the 5-minute arm token") {
                VStack(alignment: .leading, spacing: DS.s2) {
                    Toggle("Face ID / Touch ID quick-arm", isOn: $quickArmEnabled)
                        .font(DSFont.body(14))
                        .tint(DS.accent2)
                        .onChange(of: quickArmEnabled) { _, on in
                            if !on { PINStore.delete() }
                            // enabling happens from the arm sheet after a
                            // successful PIN entry (we never store an unverified PIN)
                        }
                    Text(quickArmEnabled
                         ? "PIN is stored in the Keychain behind biometrics."
                         : "Enable by arming once with your PIN — you'll be offered to save it.")
                        .font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            Card("Status", subtitle: "what this app currently sees") {
                VStack(spacing: DS.s2) {
                    KeyValueRow(key: "Broker path", value: model.meta?.broker?.mode ?? "—",
                                valueColor: model.meta?.broker?.mode == "direct-mcp"
                                    ? DS.up : DS.caution)
                    if let reason = model.meta?.broker?.reason, !reason.isEmpty {
                        Text(reason).font(DSFont.body(12)).foregroundStyle(DS.textMuted)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    KeyValueRow(key: "Server PIN configured",
                                value: model.meta?.pinConfigured == true
                                    ? "yes" : "NO — run --set-pin",
                                valueColor: model.meta?.pinConfigured == true ? nil : DS.down)
                    KeyValueRow(key: "Market",
                                value: model.meta?.marketOpen == true ? "open" : "closed")
                    KeyValueRow(key: "Poll interval",
                                value: "\(Int(model.meta?.pollSeconds ?? 30))s")
                    KeyValueRow(key: "Last refresh",
                                value: model.lastRefreshed?
                                    .formatted(date: .omitted, time: .standard) ?? "—")
                    Button("Retry direct broker connection") {
                        Task { await model.retryDirectBroker() }
                    }
                    .buttonStyle(.organicSoft)
                    .padding(.top, DS.s1)
                }
            }
        }
    }
}
