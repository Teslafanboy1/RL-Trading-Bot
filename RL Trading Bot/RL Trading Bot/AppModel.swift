//
//  AppModel.swift — the app's observable store: connection, polling,
//  per-tab payloads, PIN arming, and every control action.
//

import SwiftUI
import Observation

enum AppTab: String, CaseIterable, Identifiable, Hashable {
    // Trading
    case overview, positions, orders, market, analyzer
    // Intelligence
    case thinking, signals, screener, news, calendar
    // Strategy & learning
    case strategy, learning, library, shadow, rx3
    // Operations
    case performance, trades, risk, activity, health, console

    var id: String { rawValue }

    enum SidebarSection: String, CaseIterable, Identifiable {
        case trading = "Trading"
        case intelligence = "Intelligence"
        case strategyLearning = "Strategy & Learning"
        case operations = "Operations"
        var id: String { rawValue }
        var tabs: [AppTab] { AppTab.allCases.filter { $0.section == self } }
    }

    var section: SidebarSection {
        switch self {
        case .overview, .positions, .orders, .market, .analyzer: .trading
        case .thinking, .signals, .screener, .news, .calendar: .intelligence
        case .strategy, .learning, .library, .shadow, .rx3: .strategyLearning
        case .performance, .trades, .risk, .activity, .health, .console: .operations
        }
    }

    var title: String {
        switch self {
        case .overview: "Overview"
        case .positions: "Positions"
        case .orders: "Orders"
        case .market: "Market"
        case .analyzer: "Analyzer"
        case .thinking: "Thinking"
        case .signals: "Signals"
        case .screener: "Screener"
        case .news: "News"
        case .calendar: "Calendar"
        case .strategy: "Strategy"
        case .learning: "Learning"
        case .library: "Library"
        case .shadow: "Shadow"
        case .rx3: "RX-3 Rotation"
        case .performance: "Performance"
        case .trades: "Trades"
        case .risk: "Risk"
        case .activity: "Activity"
        case .health: "Health"
        case .console: "Console"
        }
    }
    var symbol: String {
        switch self {
        case .overview: "gauge.with.dots.needle.67percent"
        case .positions: "chart.pie.fill"
        case .orders: "arrow.left.arrow.right.circle.fill"
        case .market: "globe.americas.fill"
        case .analyzer: "waveform.and.magnifyingglass"
        case .thinking: "brain.head.profile"
        case .signals: "dot.radiowaves.left.and.right"
        case .screener: "line.3.horizontal.decrease.circle.fill"
        case .news: "newspaper.fill"
        case .calendar: "calendar.badge.clock"
        case .strategy: "gearshape.2.fill"
        case .learning: "graduationcap.fill"
        case .library: "books.vertical.fill"
        case .shadow: "moon.stars.fill"
        case .rx3: "arrow.triangle.2.circlepath"
        case .performance: "chart.line.uptrend.xyaxis"
        case .trades: "list.bullet.rectangle.portrait"
        case .risk: "shield.lefthalf.filled"
        case .activity: "clock.arrow.circlepath"
        case .health: "heart.text.square.fill"
        case .console: "terminal.fill"
        }
    }
}

@Observable
final class AppModel {
    // MARK: connection
    let api = APIClient()
    var serverURLString: String {
        didSet {
            UserDefaults.standard.set(serverURLString, forKey: "serverURL")
            api.baseURL = URL(string: serverURLString)
        }
    }
    var connectionError: String?
    var lastRefreshed: Date?

    // MARK: payloads
    var meta: Meta?
    var overview: Overview?
    var alerts: [AlertItem] = []
    var thinking: Thinking?
    var performance: Performance?
    var tradesPayload: TradesPayload?
    var riskPayload: RiskPayload?
    var newsPayload: NewsPayload?
    var marketPayload: MarketPayload?
    var strategyPayload: StrategyPayload?
    var shadowPayload: ShadowPayload?
    var actions: [ManualAction] = []
    var signalsPayload: SignalsPayload?
    var symbolPayload: SymbolPayload?
    var screenPayload: ScreenPayload?
    var ordersPayload: OrdersPayload?
    var rx3Payload: RX3Payload?
    var healthPayload: HealthPayload?
    var learningPayload: LearningPayload?
    var calendarPayload: CalendarPayload?
    var logsPayload: LogsPayload?
    var libraryPayload: LibraryPayload?
    var tabErrors: [AppTab: String] = [:]
    var loadingTabs: Set<AppTab> = []

    // Analyzer / Orders tab parameters (persisted across tab switches)
    var analyzerSymbol: String = "SPY"
    var analyzerInterval: String = "1d"
    var orderHistoryDays: Int = 1

    // MARK: navigation + sheets
    var selectedTab: AppTab = .overview
    var showArmSheet = false
    @ObservationIgnored var pendingArmedAction: (@MainActor () -> Void)?
    var orderPrefill: OrderPrefill?
    var showOrderSheet = false
    var toast: String?

    struct OrderPrefill {
        var symbol: String = ""
        var side: String = "buy"
        var quantity: Double?
        var type: String = "market"
    }

    // MARK: arming
    var armedUntil: Date?
    var isArmed: Bool { (armedUntil ?? .distantPast) > Date() }
    var quickArmAvailable: Bool { PINStore.isEnabled }

    private var pollTask: Task<Void, Never>?

    init() {
        #if os(macOS)
        let fallback = "http://127.0.0.1:8787"
        #else
        let fallback = ""
        #endif
        let saved = UserDefaults.standard.string(forKey: "serverURL")
        serverURLString = (saved?.isEmpty == false) ? saved! : fallback
        api.baseURL = URL(string: serverURLString)
    }

    // MARK: - polling lifecycle

    func start() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshTick()
                let interval = self?.meta?.pollSeconds ?? 30
                try? await Task.sleep(for: .seconds(max(10, interval)))
            }
        }
    }

    func stop() {
        pollTask?.cancel()
        pollTask = nil
    }

    func refreshTick() async {
        guard api.baseURL != nil else {
            connectionError = "No server URL configured — open Settings."
            return
        }
        do {
            meta = try await api.get("/api/meta", as: Meta.self)
            connectionError = nil
        } catch {
            connectionError = (error as? APIError)?.errorDescription ?? error.localizedDescription
            return
        }
        async let ovr: Overview? = try? api.get("/api/overview", as: Overview.self)
        async let alr: AlertsPayload? = try? api.get("/api/alerts", as: AlertsPayload.self)
        if let o = await ovr { overview = o }
        if let a = await alr { alerts = a.alerts ?? [] }
        await load(selectedTab)
        lastRefreshed = Date()
    }

    func load(_ tab: AppTab) async {
        loadingTabs.insert(tab)
        defer { loadingTabs.remove(tab) }
        do {
            switch tab {
            case .overview:
                overview = try await api.get("/api/overview", as: Overview.self)
            case .positions:
                overview = try await api.get("/api/overview", as: Overview.self)
                riskPayload = try? await api.get("/api/risk", as: RiskPayload.self)
            case .orders:
                ordersPayload = try await api.get("/api/orders?days=\(orderHistoryDays)",
                                                  as: OrdersPayload.self)
            case .market:
                marketPayload = try await api.get("/api/market", as: MarketPayload.self)
            case .analyzer:
                symbolPayload = try await api.get(analyzerPath, as: SymbolPayload.self)
            case .thinking:
                thinking = try await api.get("/api/thinking", as: Thinking.self)
            case .signals:
                signalsPayload = try await api.get("/api/signals", as: SignalsPayload.self)
            case .screener:
                screenPayload = try await api.get("/api/screen", as: ScreenPayload.self)
            case .news:
                newsPayload = try await api.get("/api/news", as: NewsPayload.self)
            case .calendar:
                calendarPayload = try await api.get("/api/calendar", as: CalendarPayload.self)
            case .strategy:
                strategyPayload = try await api.get("/api/strategy", as: StrategyPayload.self)
            case .learning:
                learningPayload = try await api.get("/api/learning", as: LearningPayload.self)
            case .library:
                libraryPayload = try await api.get("/api/library", as: LibraryPayload.self)
            case .shadow:
                shadowPayload = try await api.get("/api/shadow", as: ShadowPayload.self)
            case .rx3:
                rx3Payload = try await api.get("/api/rx3", as: RX3Payload.self)
            case .performance:
                performance = try await api.get("/api/performance", as: Performance.self)
            case .trades:
                tradesPayload = try await api.get("/api/trades", as: TradesPayload.self)
            case .risk:
                riskPayload = try await api.get("/api/risk", as: RiskPayload.self)
            case .activity:
                logsPayload = try await api.get("/api/logs", as: LogsPayload.self)
            case .health:
                healthPayload = try await api.get("/api/health", as: HealthPayload.self)
            case .console:
                actions = (try await api.get("/api/actions", as: ActionsPayload.self)).actions ?? []
            }
            tabErrors[tab] = nil
        } catch {
            tabErrors[tab] = (error as? APIError)?.errorDescription ?? error.localizedDescription
        }
    }

    private var analyzerPath: String {
        "/api/symbol?sym=\(analyzerSymbol.urlQueryEncoded)&interval=\(analyzerInterval)"
    }

    /// Point the Analyzer at a symbol and jump to it (used from Signals,
    /// Screener, Market and Positions rows).
    func analyze(_ symbol: String) {
        analyzerSymbol = symbol.uppercased()
        selectedTab = .analyzer
        Task { await load(.analyzer) }
    }

    /// Re-run the momentum screen server-side, bypassing its 10-min cache.
    func rerunScreen() async {
        loadingTabs.insert(.screener)
        defer { loadingTabs.remove(.screener) }
        do {
            screenPayload = try await api.get("/api/screen?fresh=1", as: ScreenPayload.self)
            tabErrors[.screener] = nil
        } catch {
            tabErrors[.screener] = (error as? APIError)?.errorDescription ?? error.localizedDescription
        }
    }

    // MARK: - arming

    /// Run `action` immediately if armed, else surface the arm sheet first.
    func requireArm(_ action: @escaping @MainActor () -> Void) {
        if isArmed { action() }
        else { pendingArmedAction = action; showArmSheet = true }
    }

    @discardableResult
    func arm(pin: String) async -> String? {
        do {
            let r: ArmResponse = try await api.post("/api/arm", body: ["pin": pin])
            guard r.ok == true, let token = r.token else {
                return r.error ?? "arm failed"
            }
            api.armToken = token
            armedUntil = Date().addingTimeInterval(r.ttl ?? 300)
            if let act = pendingArmedAction { pendingArmedAction = nil; act() }
            return nil
        } catch {
            return (error as? APIError)?.errorDescription ?? error.localizedDescription
        }
    }

    func quickArm() async -> String? {
        do {
            let pin = try await PINStore.retrieve(reason: "Arm trading controls")
            return await arm(pin: pin)
        } catch {
            return (error as? APIError)?.errorDescription ?? error.localizedDescription
        }
    }

    func disarm() async {
        _ = try? await api.post("/api/disarm", as: SimpleResponse.self)
        api.armToken = nil
        armedUntil = nil
    }

    // MARK: - control actions (all require arm server-side)

    private func control(_ path: String, body: [String: Any?] = [:],
                         success: String) async -> String? {
        do {
            let r: SimpleResponse = try await api.post(path, body: body)
            if r.ok == true { toast = success; await refreshTick(); return nil }
            return r.error ?? "failed"
        } catch APIError.needsArm {
            armedUntil = nil
            return APIError.needsArm.errorDescription
        } catch {
            return (error as? APIError)?.errorDescription ?? error.localizedDescription
        }
    }

    func pauseBot() async -> String? {
        await control("/api/bot/pause", body: ["reason": "app"],
                      success: "Bot paused — protective exits stay armed")
    }
    func resumeBot() async -> String? {
        await control("/api/bot/resume", success: "Bot resumed")
    }
    func haltBot(reason: String) async -> String? {
        await control("/api/bot/halt", body: ["reason": reason], success: "HALT written")
    }
    func clearHalt(confirm: String) async -> String? {
        await control("/api/bot/clear_halt", body: ["confirm": confirm],
                      success: "HALT cleared")
    }
    func setDNT(symbol: String, blocked: Bool) async -> String? {
        await control("/api/dnt", body: ["symbol": symbol, "blocked": blocked],
                      success: blocked ? "\(symbol) blocked" : "\(symbol) unblocked")
    }
    func setStopOverride(symbol: String, stopPrice: Double?, stopPct: Double?,
                         trailPct: Double?, clear: Bool) async -> String? {
        await control("/api/stop_override",
                      body: ["symbol": symbol, "stop_price": stopPrice,
                             "stop_pct": stopPct, "trail_pct": trailPct,
                             "clear": clear],
                      success: clear ? "Override cleared" : "Override saved")
    }

    func brokerRefresh() async -> String? {
        do {
            struct R: Codable { var ok: Bool?; var error: String? }
            let r: R = try await api.post("/api/broker/refresh")
            if r.ok == true { toast = "Broker refreshed"; await refreshTick(); return nil }
            return r.error ?? "refresh failed"
        } catch {
            return (error as? APIError)?.errorDescription ?? error.localizedDescription
        }
    }

    func retryDirectBroker() async {
        struct R: Codable { var ok: Bool? }
        _ = try? await api.post("/api/broker/retry_direct", as: R.self)
        await refreshTick()
    }

    func saveWatchlist(_ symbols: [String]) async -> String? {
        do {
            let r: SimpleResponse = try await api.post("/api/watchlist",
                                                       body: ["symbols": symbols])
            if r.ok == true { toast = "Watchlist saved"; await load(.market); return nil }
            return r.error ?? "save failed"
        } catch {
            return (error as? APIError)?.errorDescription ?? error.localizedDescription
        }
    }

    // MARK: - orders

    func previewOrder(symbol: String, side: String, type: String,
                      quantity: Double?, dollarAmount: Double?,
                      limitPrice: Double?, stopPrice: Double?,
                      tif: String) async throws -> OrderPreview {
        try await api.post("/api/order/preview", body: [
            "symbol": symbol, "side": side, "type": type,
            "quantity": quantity, "dollar_amount": dollarAmount,
            "limit_price": limitPrice, "stop_price": stopPrice,
            "time_in_force": tif,
        ])
    }

    func placeOrder(previewId: String) async throws -> OrderPlaceResult {
        let r: OrderPlaceResult = try await api.post("/api/order/place",
                                                     body: ["preview_id": previewId])
        toast = "Order placed"
        Task { await refreshTick() }
        return r
    }

    func cancelOrder(id: String) async -> String? {
        do {
            let r: OrderPlaceResult = try await api.post("/api/order/cancel",
                                                         body: ["order_id": id])
            if r.ok == true { toast = "Cancel sent"; await refreshTick(); return nil }
            return r.error ?? "cancel failed"
        } catch APIError.needsArm {
            armedUntil = nil
            return APIError.needsArm.errorDescription
        } catch {
            return (error as? APIError)?.errorDescription ?? error.localizedDescription
        }
    }

    // MARK: - documents

    func postmortemText(_ file: String) async -> String {
        (try? await api.get("/api/postmortem?file=\(file.urlQueryEncoded)",
                            as: FileTextPayload.self))?.text ?? "(unavailable)"
    }

    func researchText(_ file: String) async -> String {
        (try? await api.get("/api/research_file?file=\(file.urlQueryEncoded)",
                            as: FileTextPayload.self))?.text ?? "(unavailable)"
    }
}

extension String {
    var urlQueryEncoded: String {
        addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? self
    }
}
