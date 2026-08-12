//
//  Shell.swift — the Organic app shell: a fixed 272pt sidebar (4 sections,
//  21 screens, live console badge) beside the scrolling screen area, with a
//  persistent agent-status bar across the top of the content column.
//
//  On iPhone the same screens are reached through a tab bar + More list;
//  everything else about them is identical.
//

import SwiftUI

// MARK: - Nav icons (Lucide-ish line art, drawn with SF Symbols)

extension AppTab {
    /// Stroke-style glyph matching the design's line-art icon set.
    var lineSymbol: String {
        switch self {
        case .overview: "gauge.open.with.lines.needle.33percent"
        case .positions: "clock"
        case .orders: "dollarsign.circle"
        case .market: "globe"
        case .analyzer: "chart.bar"
        case .thinking: "brain"
        case .signals: "wifi"
        case .screener: "line.3.horizontal.decrease"
        case .news: "newspaper"
        case .calendar: "calendar"
        case .strategy: "gearshape"
        case .learning: "graduationcap"
        case .library: "chart.bar.doc.horizontal"
        case .shadow: "moon.stars"
        case .rx3: "arrow.triangle.2.circlepath"
        case .rx4: "bolt.horizontal.circle"
        case .performance: "chart.line.uptrend.xyaxis"
        case .trades: "list.bullet.rectangle"
        case .risk: "shield"
        case .activity: "clock.arrow.circlepath"
        case .health: "waveform.path.ecg"
        case .console: "chevron.left.forwardslash.chevron.right"
        }
    }

    /// One-line description shown under each screen's H1.
    var blurb: String {
        switch self {
        case .overview: "Your portfolio, at a glance."
        case .positions: "Every open position, its stops and its thesis."
        case .orders: "Working orders, fills, history and the manual journal."
        case .market: "Indices, your watchlist, earnings and headlines."
        case .analyzer: "Deep dive on a single ticker."
        case .thinking: "What the bot researched and why."
        case .signals: "The EMA ribbon across every symbol the bot watches."
        case .screener: "The bot's own multi-timeframe momentum screen."
        case .news: "Headlines, citations and which sources earn their weight."
        case .calendar: "Bot schedule, blackout windows and earnings dates."
        case .strategy: "Live config, learned rules and the version timeline."
        case .learning: "The post-trade learning loop, end to end."
        case .library: "Every research file, postmortem and victory."
        case .shadow: "Paper options sleeve and its go-live checklist."
        case .rx3: "Deterministic rotation × vol throttle × RISKX — the live book."
        case .rx4: "Same engine, full-deploy variant — paper only, for comparison."
        case .performance: "Equity curve, drawdown and per-trade P&L."
        case .trades: "Full closed-trade history."
        case .risk: "Stops, overrides, kill-switch and the watchdog."
        case .activity: "Every stream of bot and operator activity."
        case .health: "Heartbeat, control plane and state-file freshness."
        case .console: "Alerts and the raw manual-action journal."
        }
    }
}

// MARK: - Root

struct ContentView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.scenePhase) private var scenePhase
    #if !os(macOS)
    @Environment(\.horizontalSizeClass) private var hSize
    #endif
    @State private var showSettings = false

    var body: some View {
        @Bindable var model = model
        Group {
            #if os(macOS)
            desktopShell
            #else
            if hSize == .compact { phoneShell } else { desktopShell }
            #endif
        }
        .tint(DS.accent)
        .background(DS.bg)
        .preferredColorScheme(model.appearance.colorScheme)
        .sheet(isPresented: $model.showArmSheet) { ArmSheet() }
        .sheet(isPresented: $model.showOrderSheet) {
            OrderTicketSheet(prefill: model.orderPrefill ?? .init())
        }
        .sheet(isPresented: $showSettings) { SettingsSheet() }
        .overlay(alignment: .bottom) { toastView }
        .task { model.start() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { model.start(); Task { await model.refreshTick() } }
            else if phase == .background { model.stop() }
        }
        .onChange(of: model.selectedTab) { _, tab in
            Task { await model.load(tab) }
        }
    }

    // MARK: desktop (Mac / iPad)

    private var desktopShell: some View {
        HStack(spacing: 0) {
            Sidebar(showSettings: $showSettings)
            contentColumn
        }
        .background(DS.bg)
        .ignoresSafeArea(.container, edges: .top)
    }

    private var contentColumn: some View {
        VStack(spacing: 0) {
            TopBar(showSettings: $showSettings)
            alertBanner
            Divider().overlay(DS.divider)
            ZStack {
                DS.bg
                screen(for: model.selectedTab)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(DS.bg)
    }

    // MARK: phone

    private static let phonePrimary: [AppTab] = [.overview, .positions, .thinking, .risk]

    private var phoneShell: some View {
        @Bindable var model = model
        return TabView(selection: $model.selectedTab) {
            ForEach(Self.phonePrimary, id: \.self) { tab in
                NavigationStack {
                    phoneScreen(tab)
                }
                .tabItem { Label(tab.title, systemImage: tab.lineSymbol) }
                .tag(tab)
            }
            NavigationStack { moreList }
                .tabItem { Label("More", systemImage: "ellipsis.circle") }
                .tag(AppTab.console)
        }
    }

    private func phoneScreen(_ tab: AppTab) -> some View {
        VStack(spacing: 0) {
            TopBar(showSettings: $showSettings, compact: true)
            alertBanner
            screen(for: tab)
        }
        .background(DS.bg)
        .navigationTitle(tab.title)
        #if !os(macOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
    }

    private var moreList: some View {
        List {
            ForEach(AppTab.SidebarSection.allCases) { section in
                let tabs = section.tabs.filter { !Self.phonePrimary.contains($0) }
                if !tabs.isEmpty {
                    Section(section.rawValue) {
                        ForEach(tabs) { tab in
                            NavigationLink {
                                phoneScreen(tab)
                                    .onAppear { Task { await model.load(tab) } }
                            } label: {
                                Label(tab.title, systemImage: tab.lineSymbol)
                                    .badge(tab == .console ? urgentAlertCount : 0)
                            }
                        }
                    }
                }
            }
            Section {
                Button { showSettings = true } label: {
                    Label("Settings", systemImage: "slider.horizontal.3")
                }
            }
        }
        .navigationTitle("More")
    }

    // MARK: screen routing

    @ViewBuilder
    private func screen(for tab: AppTab) -> some View {
        switch tab {
        case .overview: OverviewView()
        case .positions: PositionsView()
        case .orders: OrdersView()
        case .market: MarketView()
        case .analyzer: AnalyzerView()
        case .thinking: ThinkingView()
        case .signals: SignalsView()
        case .screener: ScreenerView()
        case .news: NewsView()
        case .calendar: CalendarView()
        case .strategy: StrategyView()
        case .learning: LearningView()
        case .library: LibraryView()
        case .shadow: ShadowView()
        case .rx3: RX3View()
        case .rx4: RX4View()
        case .performance: PerformanceView()
        case .trades: TradesView()
        case .risk: RiskView()
        case .activity: ActivityView()
        case .health: HealthView()
        case .console: ConsoleView()
        }
    }

    // MARK: chrome

    private var urgentAlertCount: Int {
        model.alerts.filter { $0.level != "info" }.count
    }

    @ViewBuilder
    private var alertBanner: some View {
        if urgentAlertCount > 0, let first = model.alerts.first(where: { $0.level != "info" }) {
            Button {
                model.selectedTab = .console
            } label: {
                Label("\(urgentAlertCount) alert\(urgentAlertCount == 1 ? "" : "s") — \(first.title ?? "")",
                      systemImage: "exclamationmark.triangle.fill")
                    .font(DSFont.semibold(13))
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, DS.s6).padding(.vertical, 9)
                    .background(DS.down.opacity(0.14))
                    .foregroundStyle(DS.down)
            }
            .buttonStyle(.plain)
        } else if let err = model.connectionError {
            Label(err, systemImage: "wifi.slash")
                .font(DSFont.body(13))
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, DS.s6).padding(.vertical, 9)
                .background(DS.caution.opacity(0.14))
                .foregroundStyle(DS.caution)
        }
    }

    @ViewBuilder
    private var toastView: some View {
        if let toast = model.toast {
            Text(toast)
                .font(DSFont.semibold(14))
                .padding(.horizontal, 20).padding(.vertical, 11)
                .background(DS.surface, in: Capsule())
                .overlay(Capsule().strokeBorder(DS.divider, lineWidth: 1))
                .foregroundStyle(DS.text)
                .shadow(color: DS.shadowColor, radius: 14, y: 4)
                .padding(.bottom, 26)
                .transition(.move(edge: .bottom).combined(with: .opacity))
                .task {
                    try? await Task.sleep(for: .seconds(3))
                    model.toast = nil
                }
        }
    }
}

// MARK: - Sidebar

struct Sidebar: View {
    @Environment(AppModel.self) private var model
    @Binding var showSettings: Bool

    private var urgentAlertCount: Int {
        model.alerts.filter { $0.level != "info" }.count
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // pinned: the brand mark must not scroll away on short windows
            brand
                .padding(.horizontal, 14)
                .padding(.top, topInset)
            navList
        }
        .frame(width: DS.sidebarWidth)
        .background(DS.neutral(1))
        .overlay(alignment: .trailing) {
            Rectangle().fill(DS.neutral(3)).frame(width: 1)
        }
    }

    private var navList: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                ForEach(AppTab.SidebarSection.allCases) { section in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(section.rawValue.uppercased())
                            .font(DSFont.label(11))
                            .kerning(0.66)
                            .foregroundStyle(DS.textFaint)
                            .padding(.horizontal, 10)
                            .padding(.bottom, 8)
                        ForEach(section.tabs) { tab in
                            navItem(tab)
                        }
                    }
                }
                Spacer(minLength: DS.s4)
                footer
            }
            .padding(.horizontal, 14)
            .padding(.bottom, 20)
        }
    }

    private var topInset: CGFloat {
        #if os(macOS)
        34   // clears the overlaid traffic lights (hidden title bar)
        #else
        20
        #endif
    }

    private var brand: some View {
        HStack(spacing: 10) {
            ZStack {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(DS.accent)
                Image(systemName: "chart.line.uptrend.xyaxis")
                    .font(.system(size: 15, weight: .bold))
                    .foregroundStyle(.white)
            }
            .frame(width: 34, height: 34)
            VStack(alignment: .leading, spacing: 1) {
                Text("RX-3 Desk")
                    .font(DSFont.heading(16))
                    .foregroundStyle(DS.text)
                Text("RL Trading Command Center")
                    .font(DSFont.body(11))
                    .foregroundStyle(DS.textMuted)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
        }
        .padding(.horizontal, 8)
        .padding(.bottom, 14)
    }

    private func navItem(_ tab: AppTab) -> some View {
        let active = model.selectedTab == tab
        return Button {
            model.selectedTab = tab
        } label: {
            HStack(spacing: 11) {
                Image(systemName: tab.lineSymbol)
                    .font(.system(size: 16, weight: .semibold))
                    .frame(width: 20)
                Text(tab.title)
                    .font(active ? DSFont.bold(15) : DSFont.body(15))
                    .lineLimit(1)
                Spacer(minLength: 4)
                if tab == .console, urgentAlertCount > 0 {
                    Text("\(urgentAlertCount)")
                        .font(DSFont.semibold(11))
                        .padding(.horizontal, 8).padding(.vertical, 2)
                        .background(active ? Color.white.opacity(0.24) : DS.neutral(3),
                                    in: Capsule())
                        .foregroundStyle(active ? .white : DS.text)
                }
            }
            .padding(.horizontal, 14).padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(active ? DS.accent : .clear,
                        in: RoundedRectangle(cornerRadius: DS.rLg, style: .continuous))
            .foregroundStyle(active ? .white : DS.text)
            .contentShape(RoundedRectangle(cornerRadius: DS.rLg, style: .continuous))
        }
        .buttonStyle(.plain)
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button { showSettings = true } label: {
                HStack(spacing: 11) {
                    Image(systemName: "slider.horizontal.3")
                        .font(.system(size: 15, weight: .semibold))
                        .frame(width: 20)
                    Text("Settings").font(DSFont.body(14))
                    Spacer()
                }
                .padding(.horizontal, 14).padding(.vertical, 9)
                .foregroundStyle(DS.textMuted)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            Text(model.lastRefreshed.map { "synced \($0.formatted(date: .omitted, time: .standard))" }
                 ?? "not synced yet")
                .font(DSFont.body(11))
                .foregroundStyle(DS.textFaint)
                .padding(.horizontal, 14)
        }
    }
}

// MARK: - Top bar

struct TopBar: View {
    @Environment(AppModel.self) private var model
    @Binding var showSettings: Bool
    var compact: Bool = false

    @State private var pulse = false

    private var bot: BotStatus? { model.overview?.bot }

    private var status: (color: Color, label: String) {
        switch bot?.state {
        case "running": (DS.accent2, "Agent Active")
        case "paused": (DS.caution, "Agent Paused")
        case "halted": (DS.down, "Agent HALTED")
        case "stale": (DS.down, "Agent Stale")
        case .none: (DS.textFaint, model.connectionError == nil ? "Connecting…" : "Offline")
        case .some(let s): (DS.textFaint, "Agent \(s.capitalized)")
        }
    }

    private var modeText: String {
        if bot?.cycle?.runningFresh == true {
            return "· cycle running\(bot?.cycle?.task.map { " (\($0))" } ?? "")"
        }
        if model.meta?.marketOpen == true { return "· market open · RX-3 rotation mode" }
        return "· market closed · RX-3 rotation mode"
    }

    var body: some View {
        HStack(alignment: .center, spacing: DS.s3) {
            HStack(spacing: 10) {
                Circle()
                    .fill(status.color)
                    .frame(width: 10, height: 10)
                    .overlay(
                        Circle().stroke(status.color.opacity(0.28), lineWidth: 4)
                            .scaleEffect(pulse ? 1.55 : 1)
                            .opacity(pulse ? 0 : 1)
                    )
                    .animation(.easeOut(duration: 1.6).repeatForever(autoreverses: false),
                               value: pulse)
                Text(status.label)
                    .font(DSFont.bold(14))
                    .foregroundStyle(DS.text)
                if !compact {
                    Text(modeText)
                        .font(DSFont.body(14))
                        .foregroundStyle(DS.textMuted)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: DS.s2)
            controls
        }
        .padding(.horizontal, compact ? DS.s4 : DS.s8)
        .padding(.top, compact ? DS.s3 : 26)
        .padding(.bottom, DS.s3)
        .background(DS.bg)
        .onAppear { pulse = true }
    }

    @ViewBuilder
    private var controls: some View {
        HStack(spacing: DS.s2) {
            if !compact {
                Tag(model.meta?.broker?.mode == "direct-mcp" ? "Live capital" : "CLI broker",
                    tone: model.meta?.broker?.mode == "direct-mcp" ? .sage : .caution)
            }
            ArmButton()
            iconButton("plus", help: "New order") {
                model.orderPrefill = .init()
                model.showOrderSheet = true
            }
            iconButton("arrow.clockwise", help: "Refresh (⌘R)") {
                Task { await model.refreshTick() }
            }
            .keyboardShortcut("r", modifiers: .command)
            #if os(macOS)
            iconButton("slider.horizontal.3", help: "Settings") { showSettings = true }
            #endif
            if !compact {
                Text(nowText)
                    .font(DSFont.body(13))
                    .foregroundStyle(DS.textMuted)
                    .lineLimit(1)
                    .padding(.leading, 2)
            }
        }
    }

    private var nowText: String {
        let now = Date()
        return now.formatted(.dateTime.month(.abbreviated).day().year()) + " · "
            + now.formatted(.dateTime.hour().minute())
    }

    private func iconButton(_ symbol: String, help: String,
                            action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 13, weight: .semibold))
                .frame(width: 30, height: 30)
                .background(DS.neutral(2), in: Circle())
                .foregroundStyle(DS.text)
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .help(help)
    }
}

// MARK: - Arm button (lock chip with countdown)

struct ArmButton: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Button {
            if model.isArmed {
                Task { await model.disarm() }
            } else {
                model.showArmSheet = true
            }
        } label: {
            Group {
                if model.isArmed, let until = model.armedUntil {
                    TimelineView(.periodic(from: .now, by: 1)) { _ in
                        let left = max(0, Int(until.timeIntervalSinceNow))
                        Label("\(left / 60):\(String(format: "%02d", left % 60))",
                              systemImage: "lock.open.fill")
                            .font(DSFont.num(12, .bold))
                    }
                } else {
                    Label("Locked", systemImage: "lock.fill")
                        .font(DSFont.semibold(12))
                }
            }
            .padding(.horizontal, 11).padding(.vertical, 6)
            .background((model.isArmed ? DS.caution : DS.neutral(3)).opacity(model.isArmed ? 0.18 : 1),
                        in: Capsule())
            .foregroundStyle(model.isArmed ? DS.caution : DS.text)
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .help(model.isArmed ? "Armed — click to disarm" : "Arm control actions (PIN)")
    }
}

#Preview {
    ContentView().environment(AppModel())
}
