import type { Metadata, Viewport } from "next";
import { JetBrains_Mono, Plus_Jakarta_Sans } from "next/font/google";
import "./iris.css";

import "./greeting-variants.css";
import { ThemeProvider } from "@/context/ThemeContext";
import { AuthProvider } from "@/context/AuthProvider";

const jakarta = Plus_Jakarta_Sans({ subsets: ["latin"], variable: "--font-jakarta" });

/* The code face for the agent workspace — the panel is a code surface, so it is
 * typeset in a code font rather than in the page's prose font.
 *
 * Two things this fixes beyond the workspace itself. `'JetBrains Mono'` is
 * already named in `iris.css:1185` (inline `<code>`) and in ApprovalCard's
 * identifier fields, but the font was never LOADED anywhere — so every one of
 * those stacks silently fell through to Cascadia/Consolas. And next/font
 * rewrites the family to a hashed `__JetBrains_Mono_*`, which means naming it
 * as a string can never match: `var(--font-mono)` is the only handle that
 * resolves. Consumers use the variable.
 *
 * `next/font` self-hosts it from our own origin at build time, so this adds no
 * runtime request to Google and no third-party font fetch on a user's machine —
 * the same posture the existing Jakarta load already takes. `display: "swap"`
 * so a slow font never blanks the execution rail mid-run. */
const jbMono = JetBrains_Mono({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-mono",
});

/* The viewport meta tag itself is set by Next.js and its default
 * (`width=device-width, initial-scale=1`) is what we want — see
 * `node_modules/next/dist/docs/01-app/03-api-reference/04-functions/generate-viewport.md`,
 * which says manual configuration is unnecessary. What is NOT default is
 * `themeColor`: without it a phone paints its browser chrome and the
 * overscroll area in its own colour, so a dark-theme IRIS sat under a white
 * status bar. The two values are the resolved `--bg` for each palette
 * (`iris.css:215` dark, `:272` light). Media-based rather than a single
 * colour because the tag is emitted server-side, before the pre-paint script
 * below has resolved the user's stored preference. */
export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: dark)", color: "#0a0a0e" },
    { media: "(prefers-color-scheme: light)", color: "#e3e6eb" },
  ],
};

export const metadata: Metadata = {
  title: "IRIS 1.0 — Deep Research AI",
  description: "Multi-source investigative AI agent. Powered by Tavily, Exa, SerperDev, SerpAPI & DuckDuckGo.",
  keywords: ["IRIS AI", "deep research", "AI agent", "investigative intelligence"],
};

// Pre-hydration theme bootstrap — runs synchronously in <head> BEFORE first paint
// so the correct day/night palette is applied immediately and the page never
// flashes the wrong theme on load (the previous behaviour: SSR shipped
// data-theme="dark", so a daytime "auto" load painted dark, then ThemeContext's
// post-hydration effect flipped it to light — a visible flash). This mirrors
// ThemeContext EXACTLY: same storage keys ("iris-theme-pref"), same default
// ("auto"), and the same time-of-day rule
// (07:00–18:59 → light, otherwise dark). ThemeContext then takes over on
// hydration and keeps re-resolving on its 60s timer.
const themeInitScript =
  "(function(){try{var p=localStorage.getItem('iris-theme-pref');" +
  "if(p!=='dark'&&p!=='light'&&p!=='auto'){var l=localStorage.getItem('iris-theme');" +
  "p=(l==='dark'||l==='light')?l:'auto';}" +
  "var h=new Date().getHours();var t=p==='auto'?(h>=7&&h<19?'light':'dark'):p;" +
  "var e=document.documentElement;e.setAttribute('data-theme',t);" +
  "e.setAttribute('data-theme-pref',p);}catch(err){}})();";

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="dark" suppressHydrationWarning>
      <head>
        {/* Must be the first thing in <head> so it runs before any paint. */}
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body className={`${jakarta.variable} ${jbMono.variable}`} style={{ fontFamily: "'Plus Jakarta Sans', 'Google Sans', system-ui, -apple-system, sans-serif" }}>
        <AuthProvider>
          <ThemeProvider>{children}</ThemeProvider>
        </AuthProvider>
      </body>
    </html>
  );
}
