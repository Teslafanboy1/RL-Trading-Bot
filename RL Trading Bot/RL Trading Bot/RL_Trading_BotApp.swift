//
//  RL_Trading_BotApp.swift
//  RL Trading Bot — native command center for the TradeCommand server.
//

import SwiftUI

@main
struct RL_Trading_BotApp: App {
    @State private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(model)
                .tint(DS.accent)
                #if os(macOS)
                .frame(minWidth: 900, minHeight: 560)
                #endif
        }
        #if os(macOS)
        // no header bar above the sidebar — the design's shell starts at the
        // very top of the window, with the traffic lights overlaid
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1320, height: 860)
        .commands {
            CommandGroup(after: .toolbar) {
                Button("Refresh") { Task { await model.refreshTick() } }
                    .keyboardShortcut("r", modifiers: .command)
                Divider()
                ForEach(Array(AppTab.allCases.prefix(9).enumerated()), id: \.element) { i, tab in
                    Button(tab.title) { model.selectedTab = tab }
                        .keyboardShortcut(KeyEquivalent(Character("\(i + 1)")), modifiers: .command)
                }
            }
        }
        #endif
    }
}
