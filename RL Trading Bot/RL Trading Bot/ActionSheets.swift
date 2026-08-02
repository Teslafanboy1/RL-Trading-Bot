//
//  ActionSheets.swift — PIN arming, the order ticket, halt/clear-halt,
//  stop-override editor, and Settings.
//

import SwiftUI

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
        NavigationStack {
            VStack(spacing: 18) {
                Image(systemName: "lock.shield.fill")
                    .font(.system(size: 44))
                    .foregroundStyle(Color.accentColor)
                Text("Arm control actions")
                    .font(.title3.bold())
                Text("Money-moving and bot-control actions need your PIN. Arms this device for 5 minutes.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)

                SecureField("PIN", text: $pin)
                    .textFieldStyle(.roundedBorder)
                    .font(.title2.monospaced())
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 220)
                    .focused($focused)
                    .onSubmit { submit() }
                    #if !os(macOS)
                    .keyboardType(.numberPad)
                    #endif

                if let error { ErrorBox(text: error) }

                if offerSave {
                    VStack(spacing: 10) {
                        Text("Save this PIN for Face ID / Touch ID quick-arm?")
                            .font(.callout)
                        HStack {
                            Button("Not now") { dismiss() }
                            Button("Enable quick-arm") {
                                try? PINStore.save(pin: pin)
                                dismiss()
                            }
                            .buttonStyle(.borderedProminent)
                        }
                    }
                } else {
                    Button {
                        submit()
                    } label: {
                        if busy { ProgressView().controlSize(.small) }
                        else { Text("Arm").frame(maxWidth: 220) }
                    }
                    .buttonStyle(.borderedProminent)
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
                            Label("Quick-arm with Face ID / Touch ID",
                                  systemImage: "faceid")
                        }
                        .buttonStyle(.bordered)
                    }
                }
            }
            .padding(28)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") {
                        model.pendingArmedAction = nil
                        dismiss()
                    }
                }
            }
        }
        .onAppear { focused = true }
        #if os(macOS)
        .frame(width: 380, height: offerSave ? 380 : 360)
        #else
        .presentationDetents([.medium])
        #endif
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
        NavigationStack {
            Form {
                Section {
                    HStack {
                        TextField("Symbol", text: $symbol)
                            .font(.title3.weight(.bold).monospaced())
                            .textCase(.uppercase)
                            #if !os(macOS)
                            .textInputAutocapitalization(.characters)
                            .autocorrectionDisabled()
                            #endif
                        Picker("", selection: $side) {
                            Text("BUY").tag("buy")
                            Text("SELL").tag("sell")
                        }
                        .pickerStyle(.segmented)
                        .frame(maxWidth: 160)
                    }
                    Picker("Size by", selection: $useDollars) {
                        Text("$ amount").tag(true)
                        Text("# shares").tag(false)
                    }
                    .pickerStyle(.segmented)
                    if useDollars {
                        TextField("USD notional (e.g. 50)", text: $dollarsText)
                            .font(.body.monospaced())
                    } else {
                        TextField("Shares (fractional OK)", text: $quantityText)
                            .font(.body.monospaced())
                    }
                    Picker("Type", selection: $orderType) {
                        ForEach(["market", "limit", "stop_market", "stop_limit"], id: \.self) {
                            Text($0).tag($0)
                        }
                    }
                    if orderType.contains("limit") {
                        TextField("Limit price", text: $limitText).font(.body.monospaced())
                    }
                    if orderType.hasPrefix("stop") {
                        TextField("Stop trigger price", text: $stopText).font(.body.monospaced())
                    }
                    Picker("Time in force", selection: $tif) {
                        Text("Day (gfd)").tag("gfd")
                        Text("GTC").tag("gtc")
                    }
                } header: {
                    Label("Real-money order · account \(model.meta?.broker?.account ?? "—")",
                          systemImage: "bolt.fill")
                }

                switch phase {
                case .form:
                    Section {
                        Button {
                            preview()
                        } label: {
                            Text("Preview order").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(symbol.isEmpty || (useDollars ? dollarsText.isEmpty : quantityText.isEmpty))
                    }
                case .previewing:
                    Section { HStack { Spacer(); ProgressView("Previewing…"); Spacer() } }
                case .preview(let p):
                    previewSection(p)
                case .placing:
                    Section { HStack { Spacer(); ProgressView("Placing…"); Spacer() } }
                case .placed(let ref):
                    Section {
                        Label("Order placed (\(ref))", systemImage: "checkmark.circle.fill")
                            .foregroundStyle(Color.up)
                    }
                }

                if let errorText {
                    Section { ErrorBox(text: errorText) }
                }
            }
            .formStyle(.grouped)
            .navigationTitle("Order ticket")
            #if !os(macOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
            }
        }
        #if os(macOS)
        .frame(width: 480, height: 620)
        #endif
    }

    @ViewBuilder
    private func previewSection(_ p: OrderPreview) -> some View {
        Section("Confirm") {
            let params = p.params
            KeyValueRow(key: "Order",
                        value: "\(params?.side?.uppercased() ?? "") \(params?.symbol ?? "") · \(params?.type ?? "")")
            if let q = params?.quantity { KeyValueRow(key: "Shares", value: Fmt.num(q, dp: 6)) }
            if let d = params?.dollarAmount { KeyValueRow(key: "Notional", value: Fmt.usd(d)) }
            if let lp = params?.limitPrice { KeyValueRow(key: "Limit", value: Fmt.usd(lp)) }
            if let px = p.quote?.price { KeyValueRow(key: "Last price", value: Fmt.usd(px)) }
            if let est = p.estCost {
                KeyValueRow(key: params?.side == "buy" ? "Est. cost" : "Est. proceeds",
                            value: Fmt.usd(est))
            }
            ForEach(p.warnings ?? [], id: \.self) { WarningBox(text: $0) }
            HoldToConfirmButton(
                title: "HOLD to \(params?.side?.uppercased() ?? "PLACE") \(params?.symbol ?? "")",
                tint: params?.side == "buy" ? .up : .down
            ) {
                place(previewId: p.previewId ?? "")
            }
            .listRowInsets(EdgeInsets(top: 6, leading: 6, bottom: 6, trailing: 6))
            if let review = p.brokerReview {
                DisclosureGroup("Broker pre-trade review") {
                    Text(review.pretty)
                        .font(.caption2.monospaced())
                        .frame(maxWidth: .infinity, alignment: .leading)
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
        NavigationStack {
            Form {
                Section {
                    WarningBox(text: "Writes the HALT file. The bot will NOT trade — including its own stop-loss enforcement — until you clear it. Positions are NOT auto-sold; the independent watchdog keeps alerting.")
                    TextField("Reason (logged)", text: $reason)
                    HoldToConfirmButton(title: "HOLD to HALT the bot") {
                        model.requireArm {
                            Task {
                                error = await model.haltBot(reason: reason)
                                if error == nil { dismiss() }
                            }
                        }
                    }
                    if let error { ErrorBox(text: error) }
                } header: {
                    Label("Force kill-switch HALT", systemImage: "octagon.fill")
                        .foregroundStyle(Color.down)
                }
            }
            .formStyle(.grouped)
            .navigationTitle("Halt bot")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
        }
        #if os(macOS)
        .frame(width: 420, height: 330)
        #else
        .presentationDetents([.medium])
        #endif
    }
}

struct ClearHaltSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var confirm = ""
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    WarningBox(text: "Re-arms the bot to trade on its next cycle. Understand why it halted first — the HALT reason is on the Risk tab.")
                    TextField("Type CLEAR to confirm", text: $confirm)
                        .font(.body.monospaced())
                        #if !os(macOS)
                        .textInputAutocapitalization(.characters)
                        #endif
                    HoldToConfirmButton(title: "HOLD to CLEAR halt", tint: .up) {
                        model.requireArm {
                            Task {
                                error = await model.clearHalt(confirm: confirm.trimmingCharacters(in: .whitespaces))
                                if error == nil { dismiss() }
                            }
                        }
                    }
                    if let error { ErrorBox(text: error) }
                } header: {
                    Label("Clear HALT", systemImage: "checkmark.shield.fill")
                }
            }
            .formStyle(.grouped)
            .navigationTitle("Clear halt")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
        }
        #if os(macOS)
        .frame(width: 420, height: 320)
        #else
        .presentationDetents([.medium])
        #endif
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
        NavigationStack {
            Form {
                Section {
                    KeyValueRow(key: "Current stop", value: Fmt.usd(row.stopPrice))
                    KeyValueRow(key: "Current trail", value: Fmt.usd(row.trailPrice))
                    KeyValueRow(key: "Price", value: Fmt.usd(row.price))
                } header: {
                    Text("\(row.symbol) — bot reads overrides next cycle; the watchdog snapshot follows")
                }
                Section("Override") {
                    TextField("Absolute stop $ (e.g. 550)", text: $stopPriceText)
                        .font(.body.monospaced())
                    TextField("Stop % as fraction (0.10 = 10%)", text: $stopPctText)
                        .font(.body.monospaced())
                    TextField("Trailing % as fraction (0.25 = 25%)", text: $trailPctText)
                        .font(.body.monospaced())
                }
                Section {
                    Button("Save override") { save(clear: false) }
                        .buttonStyle(.borderedProminent)
                    Button("Clear override", role: .destructive) { save(clear: true) }
                    if let error { ErrorBox(text: error) }
                }
            }
            .formStyle(.grouped)
            .navigationTitle("Stop override")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
        }
        #if os(macOS)
        .frame(width: 440, height: 460)
        #endif
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

    enum Kind: String, CaseIterable, Identifiable {
        case deposit = "Deposit", withdrawal = "Withdrawal"
        var id: String { rawValue }
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    WarningBox(text: "Tells the bot exactly how much cash moved in/out so it never mistakes a deposit or withdrawal for trading performance (or vice versa). Applied next cycle to month_start_value, current_value, and the kill-switch peak — the exact amount you enter, not a guess from a broker read.")
                    Picker("Type", selection: $kind) {
                        ForEach(Kind.allCases) { Text($0.rawValue).tag($0) }
                    }
                    .pickerStyle(.segmented)
                    TextField("Amount ($)", text: $amountText)
                        .font(.body.monospaced())
                        #if !os(macOS)
                        .keyboardType(.decimalPad)
                        #endif
                    TextField("Note (optional)", text: $note)
                } header: {
                    Label("Declare cash movement", systemImage: "arrow.left.arrow.right.circle.fill")
                }
                Section {
                    Button("Record \(kind.rawValue.lowercased())") { save() }
                        .buttonStyle(.borderedProminent)
                        .disabled(Double(amountText) == nil || Double(amountText) == 0)
                    if let error { ErrorBox(text: error) }
                }
            }
            .formStyle(.grouped)
            .navigationTitle("Deposit / withdrawal")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
        }
        #if os(macOS)
        .frame(width: 440, height: 380)
        #else
        .presentationDetents([.medium])
        #endif
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
        NavigationStack {
            Form {
                Section("Server") {
                    TextField("URL", text: $model.serverURLString,
                              prompt: Text("https://your-mac.tailnet.ts.net"))
                        .font(.body.monospaced())
                        #if !os(macOS)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        #endif
                    Button("Test connection") {
                        Task {
                            do {
                                let m: Meta = try await model.api.get("/api/meta")
                                testResult = "✓ connected — broker \(m.broker?.mode ?? "?"), market \(m.marketOpen == true ? "open" : "closed")"
                                await model.refreshTick()
                            } catch {
                                testResult = "✗ " + ((error as? APIError)?.errorDescription ?? error.localizedDescription)
                            }
                        }
                    }
                    if let testResult {
                        Text(testResult)
                            .font(.callout)
                            .foregroundStyle(testResult.hasPrefix("✓") ? Color.up : Color.down)
                    }
                    #if os(macOS)
                    Text("Local server: run `bash run_dashboard.sh` in the bot repo. On iPhone, use the Tailscale HTTPS URL (`tailscale serve --bg --https=443 http://127.0.0.1:8787`).")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    #endif
                }
                Section("Quick-arm") {
                    Toggle("Face ID / Touch ID quick-arm", isOn: $quickArmEnabled)
                        .onChange(of: quickArmEnabled) { _, on in
                            if !on { PINStore.delete() }
                            // enabling happens from the arm sheet after a
                            // successful PIN entry (we never store an unverified PIN)
                        }
                    Text(quickArmEnabled
                         ? "PIN is stored in the Keychain behind biometrics."
                         : "Enable by arming once with your PIN — you'll be offered to save it.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Section("Status") {
                    KeyValueRow(key: "Broker path", value: model.meta?.broker?.mode ?? "—")
                    if let reason = model.meta?.broker?.reason, !reason.isEmpty {
                        Text(reason).font(.caption).foregroundStyle(.secondary)
                    }
                    Button("Retry direct broker connection") {
                        Task { await model.retryDirectBroker() }
                    }
                    KeyValueRow(key: "Server PIN configured",
                                value: model.meta?.pinConfigured == true ? "yes" : "NO — run --set-pin")
                    KeyValueRow(key: "Last refresh",
                                value: model.lastRefreshed?.formatted(date: .omitted, time: .standard) ?? "—")
                }
            }
            .formStyle(.grouped)
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
        #if os(macOS)
        .frame(width: 500, height: 520)
        #endif
    }
}
