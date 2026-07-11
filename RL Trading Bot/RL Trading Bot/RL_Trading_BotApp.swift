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
                #if os(macOS)
                .frame(minWidth: 720, minHeight: 520)
                #endif
        }
        #if os(macOS)
        .defaultSize(width: 1240, height: 800)
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
