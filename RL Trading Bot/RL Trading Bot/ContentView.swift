//
//  ContentView.swift — adaptive shell: sidebar on Mac/iPad, tab bar on iPhone.
//

import SwiftUI

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
            splitShell
            #else
            if hSize == .compact { tabShell } else { splitShell }
            #endif
        }
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

    // MARK: shells

    private var splitShell: some View {
        let selection = Binding<AppTab?>(
            get: { model.selectedTab },
            set: { if let tab = $0 { model.selectedTab = tab } })
        return NavigationSplitView {
            List(selection: selection) {
                ForEach(AppTab.SidebarSection.allCases) { section in
                    Section(section.rawValue) {
                        ForEach(section.tabs) { tab in
                            Label(tab.title, systemImage: tab.symbol)
                                .badge(tab == .console ? urgentAlertCount : 0)
                                .tag(tab)
                        }
                    }
                }
            }
            .navigationTitle("RL Trading Bot")
            #if os(macOS)
            .navigationSplitViewColumnWidth(min: 195, ideal: 215)
            #endif
        } detail: {
            detailView(for: model.selectedTab)
        }
    }

    private var tabShell: some View {
        @Bindable var model = model
        // iPhone: 4 primary tabs + More (holds the rest)
        return TabView(selection: $model.selectedTab) {
            ForEach([AppTab.overview, .thinking, .trades, .risk], id: \.self) { tab in
                NavigationStack { detailView(for: tab) }
                    .tabItem { Label(tab.title, systemImage: tab.symbol) }
                    .tag(tab)
            }
            NavigationStack { moreList }
                .tabItem { Label("More", systemImage: "ellipsis.circle.fill") }
                .tag(AppTab.console)
        }
    }

    /// Tabs pinned to the iPhone tab bar; everything else lives under More.
    private static let phonePrimary: [AppTab] = [.overview, .thinking, .trades, .risk]

    private var moreList: some View {
        List {
            ForEach(AppTab.SidebarSection.allCases) { section in
                let tabs = section.tabs.filter { !Self.phonePrimary.contains($0) }
                if !tabs.isEmpty {
                    Section(section.rawValue) {
                        ForEach(tabs) { tab in
                            NavigationLink {
                                detailView(for: tab)
                                    .onAppear { Task { await model.load(tab) } }
                            } label: {
                                Label(tab.title, systemImage: tab.symbol)
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

    // MARK: detail

    @ViewBuilder
    private func detailView(for tab: AppTab) -> some View {
        Group {
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
            case .performance: PerformanceView()
            case .trades: TradesView()
            case .risk: RiskView()
            case .activity: ActivityView()
            case .health: HealthView()
            case .console: ConsoleView()
            }
        }
        .navigationTitle(tab.title)
        #if !os(macOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .toolbar { toolbarContent }
        .safeAreaInset(edge: .top, spacing: 0) { alertBanner }
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
                    .font(.callout.weight(.semibold))
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 14).padding(.vertical, 8)
                    .background(Color.down.opacity(0.15))
                    .foregroundStyle(Color.down)
            }
            .buttonStyle(.plain)
        } else if let err = model.connectionError {
            Label(err, systemImage: "wifi.slash")
                .font(.callout)
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 14).padding(.vertical, 8)
                .background(Color.caution.opacity(0.15))
                .foregroundStyle(Color.caution)
        }
    }

    // Keep the toolbar lean — status chips live in the Overview hero, where
    // they can wrap. Cramming them into the toolbar clipped badly on narrow
    // windows and crowded the iPhone nav bar.
    @ToolbarContentBuilder
    private var toolbarContent: some ToolbarContent {
        ToolbarItemGroup(placement: .primaryAction) {
            ArmButton()
            Button {
                Task { await model.refreshTick() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .keyboardShortcut("r", modifiers: .command)
            #if os(macOS)
            Button {
                showSettings = true
            } label: {
                Label("Settings", systemImage: "slider.horizontal.3")
            }
            #endif
        }
    }

    @ViewBuilder
    private var toastView: some View {
        if let toast = model.toast {
            Text(toast)
                .font(.callout.weight(.medium))
                .padding(.horizontal, 16).padding(.vertical, 10)
                .background(.regularMaterial, in: Capsule())
                .shadow(radius: 8)
                .padding(.bottom, 20)
                .transition(.move(edge: .bottom).combined(with: .opacity))
                .task {
                    try? await Task.sleep(for: .seconds(3))
                    model.toast = nil
                }
        }
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
            if model.isArmed, let until = model.armedUntil {
                TimelineView(.periodic(from: .now, by: 1)) { _ in
                    let left = max(0, Int(until.timeIntervalSinceNow))
                    Label("\(left / 60):\(String(format: "%02d", left % 60))",
                          systemImage: "lock.open.fill")
                        .font(.caption.weight(.bold).monospacedDigit())
                        .foregroundStyle(Color.caution)
                }
            } else {
                Label("Locked", systemImage: "lock.fill")
                    .font(.caption.weight(.semibold))
            }
        }
        .help(model.isArmed ? "Armed — click to disarm" : "Arm control actions (PIN)")
    }
}

#Preview {
    ContentView().environment(AppModel())
}
