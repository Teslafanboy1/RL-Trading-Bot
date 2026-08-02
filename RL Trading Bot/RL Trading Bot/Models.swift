//
//  Models.swift — Codable mirrors of the TradeCommand dashboard JSON API.
//
//  All properties are camelCase; the decoder uses .convertFromSnakeCase.
//  Everything is optional-heavy on purpose: the server returns nulls freely
//  (e.g. before the first broker read) and the app must render whatever
//  subset exists.
//

import Foundation

// MARK: /api/meta

struct Meta: Codable {
    struct Broker: Codable {
        var mode: String?
        var reason: String?
        var account: String?
    }
    var serverTime: String?
    var marketOpen: Bool?
    var broker: Broker?
    var pinConfigured: Bool?
    var pollSeconds: Double?
}

// MARK: /api/overview

struct StopLevels: Codable, Hashable {
    var stopPrice: Double?
    var trailPrice: Double?
    var override_: JSONValue?
    // NOTE: decoder uses .convertFromSnakeCase, so stringValues here must be
    // the POST-conversion camelCase names ("stop_price" arrives as stopPrice).
    enum CodingKeys: String, CodingKey {
        case stopPrice, trailPrice
        case override_ = "override"
    }
}

struct Position: Codable, Identifiable, Hashable {
    var id: String?
    var symbol: String
    var shares: Double?
    var entryPrice: Double?
    var peakPrice: Double?
    var entryDate: String?
    var confidence: Int?
    var thesis: String?
    var adopted: Bool?
    var price: Double?
    var prevClose: Double?
    var spark: [Double]?
    var dayPnl: Double?
    var unrealizedPnl: Double?
    var unrealizedPct: Double?
    var emaState: String?
    var emaTransition: String?
    var stop: StopLevels?
    var dnt: Bool?
    var locked: Bool?

    var stableId: String { id ?? symbol }
}

struct AccountInfo: Codable {
    var total: Double?
    var liveTotal: Double?
    var equity: Double?
    var cash: Double?
    var buyingPower: Double?
    var asof: String?
    var source: String?
    var updated: String?
    var error: String?
    var cashPct: Double?
}

struct Heartbeat: Codable {
    var raw: String?
    var ageSeconds: Double?
}

struct HaltState: Codable {
    var halted: Bool?
    var detail: String?
    var ageSeconds: Double?
}

struct PauseState: Codable {
    var paused: Bool?
    var detail: String?
}

struct CycleStatus: Codable {
    var state: String?
    var task: String?
    var detail: String?
    var ts: String?
    var runningFresh: Bool?
}

struct BotStatus: Codable {
    var state: String?
    var marketOpen: Bool?
    var heartbeat: Heartbeat?
    var halt: HaltState?
    var pause: PauseState?
    var cycle: CycleStatus?
    var riskState: JSONValue?
}

struct Killswitch: Codable {
    var peak: Double?
    var current: Double?
    var drawdown: Double?
    var threshold: Double?
    var haltAtValue: Double?
}

struct BrokerOrder: Codable, Identifiable, Hashable {
    var id: String?
    var symbol: String?
    var side: String?
    var type: String?
    var quantity: Double?
    var notional: Double?
    var state: String?
    var price: Double?
    var createdAt: String?
    var stableId: String { id ?? "\(symbol ?? "?")-\(createdAt ?? UUID().uuidString)" }
}

struct TradeSummary: Codable {
    var totalTrades: Int?
    var wins: Int?
    var losses: Int?
    var winRate: Double?
    var totalPnl: Double?
    var monthStartValue: Double?
    var currentValue: Double?
    var monthlyReturnPct: Double?
    var monthlyGoal: String?
    var onTrack: Bool?
}

struct IndexQuote: Codable, Identifiable, Hashable {
    var symbol: String
    var label: String?
    var price: Double?
    var prevClose: Double?
    var changePct: Double?
    var spark: [Double]?
    var id: String { symbol }
}

struct Overview: Codable {
    var account: AccountInfo?
    var broker: Meta.Broker?
    var dayPnlPositions: Double?
    var realizedToday: Double?
    var positions: [Position]?
    var ordersToday: [BrokerOrder]?
    var bot: BotStatus?
    var killswitch: Killswitch?
    var summary: TradeSummary?
    var alertsCount: Int?
    var indices: [IndexQuote]?
    var dnt: [String]?
    var serverTime: String?
}

// MARK: /api/thinking

struct Pick: Codable, Identifiable, Hashable {
    var rank: Int?
    var symbol: String
    var name: String?
    var confidence: Int?
    var fields: [String: String]?
    var bullets: [String]?
    var emaState: String?
    var price: Double?
    var held: Bool?
    var id: String { "\(rank ?? 0)-\(symbol)" }
}

struct ParsedPicks: Codable {
    var file: String?
    var picks: [Pick]?
    var header: [String]?
    var reassessment: [String]?
    var watchlist: [String]?
    var momentumWatch: [String]?
    var deployment: [String]?
    var raw: String?
}

struct ResearchFileMeta: Codable, Identifiable, Hashable {
    var file: String
    var kind: String?
    var date: String?
    var mtime: Double?
    var id: String { file }
}

struct MomentumWatchEntry: Codable, Hashable {
    var confidence: Int?
    var catalyst: String?
}

struct QueueEntry: Codable, Identifiable, Hashable {
    var raw: String?
    var done: Bool?
    var queuedAt: String?
    var tradeId: String?
    var symbol: String?
    var outcome: String?
    var pnl: String?
    var analysis: String?
    var id: String { raw ?? UUID().uuidString }
}

struct MidweekSection: Codable, Identifiable, Hashable {
    var title: String?
    var body: String?
    var id: String { title ?? UUID().uuidString }
}

struct MidweekParsed: Codable {
    var file: String?
    var sections: [MidweekSection]?
    var raw: String?
}

struct Thinking: Codable {
    var picks: ParsedPicks?
    var picksFile: ResearchFileMeta?
    var midweek: MidweekParsed?
    var midweekFile: ResearchFileMeta?
    var momentumWatch: [String: MomentumWatchEntry]?
    var rewriteQueue: [QueueEntry]?
    var files: [ResearchFileMeta]?
}

// MARK: /api/performance

struct EquityPoint: Codable, Hashable {
    var ts: String?
    var total: Double?
    var cash: Double?
    var source: String?
}

struct DrawdownPoint: Codable, Hashable {
    var ts: String?
    var dd: Double?
}

struct RealizedPoint: Codable, Hashable {
    var ts: String?
    var cumPnl: Double?
    var tradeId: String?
    var symbol: String?
    var pnl: Double?
}

struct DailyClose: Codable, Hashable {
    var ts: String?
    var close: Double?
}

struct RX3Point: Codable, Hashable {
    var ts: String?
    var value: Double?
}

struct PerTrade: Codable, Identifiable, Hashable {
    var id: String?
    var symbol: String?
    var pnl: Double?
    var pnlPct: Double?
    var exitDate: String?
    var stableId: String { id ?? UUID().uuidString }
}

struct Performance: Codable {
    var equityCurve: [EquityPoint]?
    var drawdown: [DrawdownPoint]?
    var realized: [RealizedPoint]?
    var benchmarkSpy: [DailyClose]?
    var rx3Curve: [RX3Point]?
    var rx3Meta: JSONValue?
    var summary: TradeSummary?
    var perTrade: [PerTrade]?
    var progress: JSONValue?
    var exitReasons: [String: Int]?
}

// MARK: /api/trades

struct ClosedTrade: Codable, Identifiable, Hashable {
    var id: String?
    var symbol: String?
    var entryPrice: Double?
    var exitPrice: Double?
    var entryDate: String?
    var exitDate: String?
    var shares: Double?
    var dollarAmount: Double?
    var pnlDollar: Double?
    var pnlPct: Double?
    var outcome: String?
    var stopLoss: Bool?
    var exitReason: String?
    var exitNotes: String?
    var confidenceScore: Int?
    var thesis: String?
    var analysisFile: String?
    var hasAnalysis: Bool?
    var stableId: String { id ?? UUID().uuidString }
}

struct PostmortemMeta: Codable, Identifiable, Hashable {
    var file: String
    var kind: String?
    var title: String?
    var tradeIds: [String]?
    var id: String { file }
}

struct TradesPayload: Codable {
    var trades: [ClosedTrade]?
    var postmortems: [PostmortemMeta]?
    var openPositions: [JSONValue]?
}

// MARK: /api/strategy

struct VersionHistoryEntry: Codable, Hashable {
    var version: JSONValue?
    var date: String?
    var summary: JSONValue?
    var tradeIds: [String]?
    var severity: String?
}

struct ChangeEvent: Codable, Hashable {
    var timestamp: String?
    var kind: String?
    var target: String?
    var version: JSONValue?
    var tradeIds: [String]?
    var severity: String?
    var summary: String?
}

struct SkillVersion: Codable, Hashable {
    var versions: Int?
    var latest: Int?
}

struct StrategyPayload: Codable {
    var config: JSONValue?
    var version: JSONValue?
    var versionHistory: [VersionHistoryEntry]?
    var changeEvents: [ChangeEvent]?
    var skillVersions: [String: SkillVersion]?
}

// MARK: /api/risk

struct RiskRow: Codable, Identifiable, Hashable {
    var symbol: String
    var shares: Double?
    var entry: Double?
    var peak: Double?
    var price: Double?
    var stopPrice: Double?
    var trailPrice: Double?
    var distStopPct: Double?
    var distTrailPct: Double?
    var override_: JSONValue?
    var watchdogStop: JSONValue?
    var id: String { symbol }
    // post-snake-case-conversion names (see StopLevels note)
    enum CodingKeys: String, CodingKey {
        case symbol, shares, entry, peak, price
        case stopPrice, trailPrice, distStopPct, distTrailPct, watchdogStop
        case override_ = "override"
    }
}

struct RiskPayload: Codable {
    var positions: [RiskRow]?
    var killswitch: Killswitch?
    var bot: BotStatus?
    var riskManagement: JSONValue?
    var overrides: [String: JSONValue]?
    var dnt: [String]?
    var stopsFile: JSONValue?
    var watchdogLog: [String]?
    var cashFlows: [CashFlowEntry]?
}

/// A single operator-declared deposit (positive amount) or withdrawal
/// (negative amount) — see controls.record_cash_flow / agent.process_manual_cash_flows.
struct CashFlowEntry: Codable, Hashable, Identifiable {
    var ts: String?
    var amount: Double?
    var note: String?
    var applied: Bool?
    var appliedTs: String?
    var id: String { (ts ?? "") + String(amount ?? 0) }
}

// MARK: /api/news

struct LeaderRow: Codable, Identifiable, Hashable {
    var source: String
    var wins: Int?
    var losses: Int?
    var n: Int?
    var accuracy: Double?
    var weight: Double?
    var id: String { source }
}

struct Citation: Codable, Hashable {
    var when: String?
    var file: String?
    var symbol: String?
    var confidence: Int?
    var sources: String?
    var note: String?
    var invalidates: String?
}

struct VerdictRow: Codable, Hashable {
    var file: String?
    var kind: String?
    var tradeIds: [String]?
    var sources: [String: Bool]?
}

struct Headline: Codable, Hashable {
    var source: String?
    var title: String?
    var link: String?
    var published: String?
}

struct NewsPayload: Codable {
    var citations: [Citation]?
    var leaderboard: [LeaderRow]?
    var verdicts: [VerdictRow]?
    var general: [Headline]?
    var symbolNews: [String: [Headline]]?
}

// MARK: /api/market

struct WatchRow: Codable, Identifiable, Hashable {
    var symbol: String
    var held: Bool?
    var price: Double?
    var changePct: Double?
    var spark: [Double]?
    var emaState: String?
    var emaTransition: String?
    var id: String { symbol }
}

struct MarketPayload: Codable {
    var indices: [IndexQuote]?
    var watchlist: [WatchRow]?
    var watchlistSymbols: [String]?
    var earnings: JSONValue?
    var held: [String]?
    var generalNews: [Headline]?
}

// MARK: /api/shadow

struct ShadowPosition: Codable, Identifiable, Hashable {
    var id: String?
    var underlying: String?
    var type: String?
    var strike: Double?
    var expiry: String?
    var structure: String?
    var entryDate: String?
    var entryUnderlying: Double?
    var entryPremium: Double?
    var contracts: Double?
    var confidence: Int?
    var thesis: String?
    var oversizedForAccount: Bool?
    var maxLossUsd: Double?
    var pctOfAccount: Double?
    var status: String?
    var exitPremium: Double?
    var pnlPerContractUsd: Double?
    var pnlPctOnPremium: Double?
    var exitDate: String?
    var exitReason: String?
    var signalSource: String?
    var catalyst: String?
    var underlyingPriceNow: Double?
    var stableId: String { id ?? UUID().uuidString }
}

struct ShadowSummary: Codable {
    var open: Int?
    var closed: Int?
    var wins: Int?
    var winRate: Double?
    var totalPnlPerContractUsd: Double?
    var avgPctOnPremium: Double?
}

struct ShadowLog: Codable {
    var positions: [ShadowPosition]?
    var summary: ShadowSummary?
}

struct GoLiveGate: Codable, Identifiable, Hashable {
    var gate: String
    var threshold: Double?
    var current: JSONValue?
    var met: Bool?
    var id: String { gate }
}

struct GoLive: Codable {
    var enabled: Bool?
    var shadowMode: Bool?
    var gates: [GoLiveGate]?
}

struct ShadowPayload: Codable {
    var optionsShadow: ShadowLog?
    var momentumScan: JSONValue?
    var momentumWatch: [String: MomentumWatchEntry]?
    var rx3: JSONValue?
    var goLive: GoLive?
    var optionsConfig: JSONValue?
}

// MARK: /api/alerts + /api/actions

struct AlertItem: Codable, Hashable {
    var level: String?
    var kind: String?
    var title: String?
    var detail: String?
    var ageSeconds: Double?
}

struct AlertsPayload: Codable { var alerts: [AlertItem]? }

struct ManualAction: Codable, Hashable {
    var ts: String?
    var action: String?
    var params: JSONValue?
    var result: JSONValue?
    var ip: String?
    var by: String?
}

struct ActionsPayload: Codable { var actions: [ManualAction]? }

// MARK: control/order responses

struct ArmResponse: Codable {
    var ok: Bool?
    var token: String?
    var ttl: Double?
    var expiresAt: String?
    var error: String?
}

struct SimpleResponse: Codable {
    var ok: Bool?
    var error: String?
    var paused: Bool?
    var halted: Bool?
    var symbols: [String]?
}

struct OrderQuote: Codable {
    var symbol: String?
    var price: Double?
    var prevClose: Double?
    var changePct: Double?
}

struct OrderParams: Codable {
    var symbol: String?
    var side: String?
    var type: String?
    var quantity: Double?
    var dollarAmount: Double?
    var limitPrice: Double?
    var stopPrice: Double?
    var timeInForce: String?
}

struct OrderPreview: Codable {
    var ok: Bool?
    var previewId: String?
    var params: OrderParams?
    var quote: OrderQuote?
    var estCost: Double?
    var warnings: [String]?
    var brokerReview: JSONValue?
    var expiresIn: Double?
    var error: String?
}

struct OrderPlaceResult: Codable {
    var ok: Bool?
    var result: JSONValue?
    var error: String?
}

struct FileTextPayload: Codable {
    var file: String?
    var text: String?
}

// MARK: /api/signals

struct EMALines: Codable, Hashable {
    var blue: Double?
    var green: Double?
    var yellow: Double?
    var red: Double?
}

struct SignalRow: Codable, Identifiable, Hashable {
    var symbol: String
    var held: Bool?
    var price: Double?
    var changePct: Double?
    var spark: [Double]?
    var state: String?
    var transition: String?
    var ok: Bool?
    var lines: EMALines?
    var lastClose: Double?
    var bars: Int?
    var source: String?
    var warmupOk: Bool?
    var note: String?
    var dnt: Bool?
    var redMarginPct: Double?
    var id: String { symbol }
}

struct SignalsPayload: Codable {
    var rows: [SignalRow]?
    var interval: String?
    var lengths: [String: Int]?
    var warmupBars: Int?
    var generatedAt: String?
}

// MARK: /api/symbol (Analyzer)

struct BarPoint: Codable, Hashable {
    var ts: String?
    var close: Double?
}

struct RibbonInfo: Codable, Hashable {
    var state: String?
    var transition: String?
    var ok: Bool?
    var lines: EMALines?
    var lastClose: Double?
    var bars: Int?
    var source: String?
    var warmupOk: Bool?
    var note: String?
}

struct SymbolStats: Codable, Hashable {
    var high52w: Double?
    var low52w: Double?
    var offHighPct: Double?
    var returns: [String: JSONValue]?
    var momentumScore: Double?
}

struct PastTrade: Codable, Identifiable, Hashable {
    var id: String?
    var outcome: String?
    var pnlPct: Double?
    var exitReason: String?
    var exitDate: String?
    var stableId: String { id ?? UUID().uuidString }
}

struct SymbolPayload: Codable {
    var symbol: String?
    var interval: String?
    var bars: [BarPoint]?
    var emaSeries: [String: [Double]]?
    var quote: OrderQuote?
    var ribbon: RibbonInfo?
    var position: JSONValue?
    var stops: StopLevels?
    var held: Bool?
    var dnt: Bool?
    var stats: SymbolStats?
    var news: [Headline]?
    var pastTrades: [PastTrade]?
}

// MARK: /api/screen (Screener)

struct ScreenRow: Codable, Identifiable, Hashable {
    var symbol: String
    var lastPrice: Double?
    var returns: [String: JSONValue]?
    var score: Double?
    var qualified: Bool?
    var reason: String?
    var affordable: Bool?
    var affordableReason: String?
    var changePct: Double?
    var held: Bool?
    var pick: Bool?
    var id: String { symbol }
}

struct ScreenPayload: Codable {
    var rows: [ScreenRow]?
    var count: Int?
    var anchor: String?
    var minScore: Double?
    var generatedAt: String?
}

// MARK: /api/orders

struct OrdersPayload: Codable {
    var today: [BrokerOrder]?
    var history: [BrokerOrder]?
    var updated: String?
    var broker: Meta.Broker?
    var days: Int?
    var historyError: String?
    var journal: [ManualAction]?
}

// MARK: /api/rx3

struct RX3Meta: Codable {
    var note: String?
    var startDate: String?
    var startEquity: Double?
    var equity: Double?
    var cash: Double?
    var lastRun: String?
    var returnPct: Double?
}

struct RX3Day: Codable, Identifiable, Hashable {
    var date: String?
    var equity: Double?
    var leaders: [String]?
    var defensive: [String]?
    var riskx: Double?
    var volScale: Double?
    var cashW: Double?
    var id: String { date ?? UUID().uuidString }
}

struct RX3Position: Codable, Identifiable, Hashable {
    var symbol: String
    var shares: Double?
    var lastPx: Double?
    var price: Double?
    var changePct: Double?
    var value: Double?
    var id: String { symbol }
}

struct RX3Payload: Codable {
    var meta: RX3Meta?
    var latest: RX3Day?
    var leaders: [String]?
    var positions: [RX3Position]?
    var curve: [RX3Point]?
    var history: [RX3Day]?
    var paperDays: Int?
    var promotionGateDays: Int?
    var config: JSONValue?
    var spySinceStart: [DailyClose]?
}

// MARK: /api/health

struct StateFile: Codable, Identifiable, Hashable {
    var label: String
    var path: String?
    var ageSeconds: Double?
    var exists: Bool?
    var id: String { label }
}

struct ControlPlane: Codable {
    var pause: PauseState?
    var halt: HaltState?
    var dnt: [String]?
    var overrides: [String: JSONValue]?
}

struct ServerInfo: Codable {
    var started: String?
    var uptimeSeconds: Double?
    var host: String?
    var port: Int?
    var root: String?
    var python: String?
}

struct AgentWarning: Codable, Hashable {
    var file: String?
    var line: String?
}

struct HealthPayload: Codable {
    var bot: BotStatus?
    var files: [StateFile]?
    var control: ControlPlane?
    var watchdogLog: [String]?
    var agentWarnings: [AgentWarning]?
    var latestPicks: ResearchFileMeta?
    var broker: Meta.Broker?
    var pinConfigured: Bool?
    var server: ServerInfo?
    var equityPoints: Int?
    var lastEquity: EquityPoint?
}

// MARK: /api/learning

struct SkillVersionRow: Codable, Identifiable, Hashable {
    var skill: String
    var versions: Int?
    var latest: Int?
    var id: String { skill }
}

struct LearningPayload: Codable {
    var postmortems: [PostmortemMeta]?
    var leaderboard: [LeaderRow]?
    var verdicts: [VerdictRow]?
    var rewriteQueue: [QueueEntry]?
    var skillVersions: [SkillVersionRow]?
    var changeEvents: [ChangeEvent]?
    var learnedRules: JSONValue?
    var observations: JSONValue?
    var confidenceCalibration: JSONValue?
}

// MARK: /api/calendar

struct ScheduleEvent: Codable, Identifiable, Hashable {
    var when: String?
    var label: String?
    var kind: String?
    var id: String { (when ?? "") + (label ?? "") }
}

struct CalendarPayload: Codable {
    var earnings: JSONValue?
    var blackoutWindows: JSONValue?
    var held: [String]?
    var watchlist: [String]?
    var schedule: [ScheduleEvent]?
    var marketOpen: Bool?
    var serverTime: String?
}

// MARK: /api/logs (Activity)

struct LogEvent: Codable, Hashable {
    var ts: String?
    var kind: String?
    var title: String?
    var detail: String?
    var severity: String?
    var ok: Bool?
}

struct LogsPayload: Codable { var events: [LogEvent]? }

// MARK: /api/library

struct LibraryPayload: Codable {
    var research: [ResearchFileMeta]?
    var postmortems: [PostmortemMeta]?
}
