//
//  Theme.swift — the "Organic" design system: exact color ramps, the 4.4px
//  spacing scale, radii, and the Caprasimo/Figtree type stack.
//
//  Light is the reference palette from the design handoff. Dark mirrors the
//  same token *roles* against the warm neutral ramp run in reverse, so the
//  app keeps its character at night instead of falling back to system grey.
//

import SwiftUI
import CoreText
#if os(macOS)
import AppKit
#else
import UIKit
#endif

// MARK: - Dynamic color helper

extension Color {
    /// A color that resolves differently in light and dark appearance.
    init(light: Color, dark: Color) {
        #if os(macOS)
        self = Color(nsColor: NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            return NSColor(isDark ? dark : light)
        })
        #else
        self = Color(uiColor: UIColor { traits in
            UIColor(traits.userInterfaceStyle == .dark ? dark : light)
        })
        #endif
    }

    /// `#rrggbb` / `#rrggbbaa` hex.
    init(hex: String) {
        var s = hex.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.hasPrefix("#") { s.removeFirst() }
        var v: UInt64 = 0
        Scanner(string: s).scanHexInt64(&v)
        let r, g, b, a: Double
        if s.count == 8 {
            r = Double((v >> 24) & 0xff) / 255
            g = Double((v >> 16) & 0xff) / 255
            b = Double((v >> 8) & 0xff) / 255
            a = Double(v & 0xff) / 255
        } else {
            r = Double((v >> 16) & 0xff) / 255
            g = Double((v >> 8) & 0xff) / 255
            b = Double(v & 0xff) / 255
            a = 1
        }
        self = Color(.sRGB, red: r, green: g, blue: b, opacity: a)
    }
}

// MARK: - Design tokens

enum DS {

    // MARK: ramps (raw, appearance-independent)

    private enum Raw {
        // warm sand neutral, 100 → 900 (light → dark)
        static let n = ["#f9f4ed", "#eee7db", "#dcd3c4", "#c0b6a5", "#a19786",
                        "#82796a", "#645c50", "#474238", "#2e2b25"].map(Color.init(hex:))
        // terracotta
        static let a = ["#fff2eb", "#ffe1d0", "#ffc6a5", "#f6a06b", "#d67f48",
                        "#b2622d", "#8c491a", "#643312", "#402310"].map(Color.init(hex:))
        // sage
        static let g = ["#f0fae1", "#e1eecc", "#ccdbb2", "#aebf92", "#8fa073",
                        "#728157", "#56633f", "#3d472b", "#272e1b"].map(Color.init(hex:))
    }

    /// Neutral step 1…9 (= tokens neutral-100 … neutral-900). In dark mode the
    /// ramp is mirrored so "neutral-100" is still the *near-background* step
    /// and "neutral-900" is still the *near-text* step.
    static func neutral(_ step: Int) -> Color {
        let i = min(max(step, 1), 9) - 1
        return Color(light: Raw.n[i], dark: Raw.n[8 - i])
    }

    /// Terracotta accent step 1…9. Dark shifts one step lighter for contrast.
    static func accent(_ step: Int) -> Color {
        let i = min(max(step, 1), 9) - 1
        return Color(light: Raw.a[i], dark: Raw.a[max(0, 8 - i)])
    }

    /// Sage accent-2 step 1…9.
    static func accent2(_ step: Int) -> Color {
        let i = min(max(step, 1), 9) - 1
        return Color(light: Raw.g[i], dark: Raw.g[max(0, 8 - i)])
    }

    // MARK: semantic tokens

    /// App / window background.
    static let bg = Color(light: Color(hex: "#f5ead8"), dark: Color(hex: "#1c1a16"))
    /// Card / container fill.
    static let surface = Color(light: Color(hex: "#ebddc5"), dark: Color(hex: "#2e2b25"))
    /// A slightly recessed fill used inside cards (mini-tiles, code panels).
    static let sunken = Color(light: Color(hex: "#f9f4ed"), dark: Color(hex: "#241f1b"))
    /// Primary text.
    static let text = Color(light: Color(hex: "#201e1d"), dark: Color(hex: "#f9f4ed"))
    /// Muted body text (neutral-600).
    static let textMuted = Color(light: Color(hex: "#82796a"), dark: Color(hex: "#c0b6a5"))
    /// Faint text (neutral-500).
    static let textFaint = Color(light: Color(hex: "#a19786"), dark: Color(hex: "#a19786"))
    /// Hairline separators — text at 16%.
    static let divider = Color(light: Color(hex: "#201e1d").opacity(0.16),
                               dark: Color(hex: "#f9f4ed").opacity(0.16))

    /// Primary action / active nav.
    static let accent = Color(light: Color(hex: "#c67139"), dark: Color(hex: "#d67f48"))
    /// Secondary accent — sage.
    static let accent2 = Color(light: Color(hex: "#7a8a5e"), dark: Color(hex: "#8fa073"))

    /// Gains, "on" states.
    static let up = Color(light: Color(hex: "#56633f"), dark: Color(hex: "#aebf92"))
    /// Losses. Deliberately a deep red-terracotta, distinct from the sage.
    static let down = Color(light: Color(hex: "#b5432f"), dark: Color(hex: "#e08a76"))
    /// Warning / pending.
    static let caution = Color(light: Color(hex: "#b2622d"), dark: Color(hex: "#f6a06b"))
    /// Neutral / unknown values.
    static let flat = Color(light: Color(hex: "#82796a"), dark: Color(hex: "#a19786"))

    /// Console panel: near-black warm ink + off-white text (same in both modes).
    static let consoleBG = Color(hex: "#211f1a")
    static let consoleText = Color(hex: "#f2ece1")

    // MARK: spacing — steps of 4.4, no step-5

    static let s1: CGFloat = 4.4
    static let s2: CGFloat = 8.8
    static let s3: CGFloat = 13.2
    static let s4: CGFloat = 17.6
    static let s6: CGFloat = 26.4
    static let s8: CGFloat = 35.2

    // MARK: radii

    static let rSm: CGFloat = 8
    static let rMd: CGFloat = 16
    static let rLg: CGFloat = 28
    /// Cards use ~1.15 × lg.
    static let rCard: CGFloat = 32

    // MARK: layout

    static let sidebarWidth: CGFloat = 272
    static let cardPadding: CGFloat = 22

    // MARK: elevation — soft, warm, ink-tinted (never cool grey)

    static let shadowColor = Color(light: Color(hex: "#4a3a26").opacity(0.10),
                                   dark: Color.black.opacity(0.34))
    static let shadowRadius: CGFloat = 10
    static let shadowY: CGFloat = 3
}

// MARK: - Type stack

enum DSFont {
    /// Registered lazily on first use; falls back to system fonts if the
    /// bundled files are missing (the app must never fail to draw text).
    private static let registered: Bool = {
        var ok = false
        for name in ["Caprasimo", "Figtree"] {
            guard let url = Bundle.main.url(forResource: name, withExtension: "ttf")
                    ?? Bundle.main.url(forResource: name, withExtension: "ttf",
                                       subdirectory: "Fonts") else { continue }
            var err: Unmanaged<CFError>?
            if CTFontManagerRegisterFontsForURL(url as CFURL, .process, &err) {
                ok = true
            } else if let code = err?.takeRetainedValue(),
                      CFErrorGetCode(code)
                        == CTFontManagerError.alreadyRegistered.rawValue {
                // Info.plist already registered it — that's a success for us
                ok = true
            }
        }
        return ok
    }()

    private static var hasCaprasimo: Bool {
        _ = registered
        #if os(macOS)
        return NSFont(name: "Caprasimo-Regular", size: 12) != nil
        #else
        return UIFont(name: "Caprasimo-Regular", size: 12) != nil
        #endif
    }

    private static var hasFigtree: Bool {
        _ = registered
        #if os(macOS)
        return NSFont(name: "Figtree-Light", size: 12) != nil
            || NSFont(name: "Figtree-Regular", size: 12) != nil
        #else
        return UIFont(name: "Figtree-Light", size: 12) != nil
            || UIFont(name: "Figtree-Regular", size: 12) != nil
        #endif
    }

    /// Figtree ships as a variable font whose default instance is Light, so
    /// the weight is dialled in explicitly on the `wght` axis rather than
    /// left to trait synthesis.
    private static func figtree(_ size: CGFloat, _ wght: CGFloat) -> Font {
        guard hasFigtree else { return .system(size: size, weight: weightFor(wght)) }
        let variation = [2_003_265_652 as CFNumber: wght as CFNumber]  // 'wght'
        let desc = CTFontDescriptorCreateWithAttributes([
            kCTFontNameAttribute: "Figtree" as CFString,
            kCTFontVariationAttribute: variation as CFDictionary,
        ] as CFDictionary)
        let ct = CTFontCreateWithFontDescriptor(desc, size, nil)
        return Font(ct)
    }

    private static func weightFor(_ wght: CGFloat) -> Font.Weight {
        switch wght {
        case ..<450: .regular
        case ..<550: .medium
        case ..<650: .semibold
        case ..<750: .bold
        default: .heavy
        }
    }

    // MARK: public API

    /// Caprasimo — screen titles, card titles, stat values.
    static func heading(_ size: CGFloat) -> Font {
        hasCaprasimo
            ? .custom("Caprasimo-Regular", fixedSize: size)
            : .system(size: size, weight: .heavy, design: .serif)
    }

    static func body(_ size: CGFloat = 14) -> Font { figtree(size, 400) }
    static func medium(_ size: CGFloat = 14) -> Font { figtree(size, 500) }
    static func semibold(_ size: CGFloat = 14) -> Font { figtree(size, 600) }
    static func bold(_ size: CGFloat = 14) -> Font { figtree(size, 700) }

    /// Numeric text that must column-align (prices, P&L, quantities).
    static func num(_ size: CGFloat = 14, _ weight: Font.Weight = .regular) -> Font {
        let w: CGFloat = switch weight {
        case .semibold: 600
        case .bold, .heavy: 700
        case .medium: 500
        default: 400
        }
        return figtree(size, w).monospacedDigit()
    }

    /// True monospace — console, raw JSON, log dumps, order ids.
    static func mono(_ size: CGFloat = 12, _ weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }

    /// Uppercase micro-label (nav section headings, stat-card labels).
    static func label(_ size: CGFloat = 11) -> Font { figtree(size, 700) }
}

// MARK: - Value coloring

extension Double {
    /// Gains sage, losses deep red, flat neutral — the design's semantic rule.
    var pnlColor: Color { self > 0 ? DS.up : self < 0 ? DS.down : DS.flat }
}

extension Optional where Wrapped == Double {
    var pnlColor: Color { (self ?? 0).pnlColor }
}
