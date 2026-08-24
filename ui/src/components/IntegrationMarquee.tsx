"use client";
import React from "react";
import { useTheme } from "@/context/ThemeContext";

/*
  IntegrationMarquee
  ──────────────────
  All icons live inside a fixed ICON_SIZE × ICON_SIZE container for
  perfect visual uniformity.

  Slack & Gmail  → inline SVG, fills container with preserveAspectRatio
  Jira           → transparent PNG, blue mark visible on both themes
  Tavily         → transparent PNG with white marks;
                   filter:invert(1) applied in light mode so the
                   marks become dark and stay visible on light bg.
*/

const ICON_SIZE = 32; // px — single source of truth for all icon sizes

/* ── Helper: uniform icon wrapper ─────────────────────────────────── */

function IconBox({ children, title }: { children: React.ReactNode; title: string }) {
  return (
    <div
      title={title}
      style={{
        width: ICON_SIZE,
        height: ICON_SIZE,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        flexShrink: 0,
      }}
    >
      {children}
    </div>
  );
}

/* ── SVG icons ─────────────────────────────────────────────────────── */

const SlackIcon = () => (
  <svg
    viewBox="0 0 124 124"
    xmlns="http://www.w3.org/2000/svg"
    width={ICON_SIZE}
    height={ICON_SIZE}
  >
    <path d="M26.3 78.4c0 7.2-5.9 13.1-13.1 13.1S0 85.6 0 78.4c0-7.2 5.9-13.1 13.1-13.1H26.3v13.1z" fill="#E01E5A"/>
    <path d="M32.9 78.4c0-7.2 5.9-13.1 13.1-13.1s13.1 5.9 13.1 13.1v32.8c0 7.2-5.9 13.1-13.1 13.1s-13.1-5.9-13.1-13.1V78.4z" fill="#E01E5A"/>
    <path d="M46 26.3c-7.2 0-13.1-5.9-13.1-13.1S38.8 0 46 0s13.1 5.9 13.1 13.1V26.3H46z" fill="#36C5F0"/>
    <path d="M46 32.9c7.2 0 13.1 5.9 13.1 13.1S53.2 59.1 46 59.1H13.1C5.9 59.1 0 53.2 0 46s5.9-13.1 13.1-13.1H46z" fill="#36C5F0"/>
    <path d="M98 46c0-7.2 5.9-13.1 13.1-13.1S124 38.8 124 46s-5.9 13.1-13.1 13.1H98V46z" fill="#2EB67D"/>
    <path d="M91.4 46c0 7.2-5.9 13.1-13.1 13.1S65.2 53.2 65.2 46V13.1C65.2 5.9 71.1 0 78.3 0s13.1 5.9 13.1 13.1V46z" fill="#2EB67D"/>
    <path d="M78.3 97.8c7.2 0 13.1 5.9 13.1 13.1S85.5 124 78.3 124s-13.1-5.9-13.1-13.1V97.8h13.1z" fill="#ECB22E"/>
    <path d="M78.3 91.2c-7.2 0-13.1-5.9-13.1-13.1s5.9-13.1 13.1-13.1H111c7.2 0 13.1 5.9 13.1 13.1s-5.9 13.1-13.1 13.1H78.3z" fill="#ECB22E"/>
  </svg>
);

/* Gmail: original viewBox is landscape (88×66). We pad it to a square
   so the envelope renders at the same visual size as the other icons. */
const GmailIcon = () => (
  <svg
    viewBox="41 31 110 110"
    xmlns="http://www.w3.org/2000/svg"
    width={ICON_SIZE}
    height={ICON_SIZE}
  >
    <path fill="#4285f4" d="M58 108h14V74L52 59v43c0 3.32 2.69 6 6 6"/>
    <path fill="#34a853" d="M120 108h14c3.32 0 6-2.69 6-6V59l-20 15"/>
    <path fill="#fbbc04" d="M120 48v26l20-15v-8c0-7.42-8.47-11.65-14.4-7.2"/>
    <path fill="#ea4335" d="M72 74V48l24 18 24-18v26L96 92"/>
    <path fill="#c5221f" d="M52 51v8l20 15V48l-5.6-4.2C60.47 39.35 52 43.58 52 51"/>
  </svg>
);

/* ── PNG icon ──────────────────────────────────────────────────────── */

function Logo({
  src, alt, invertOnLight = false, isDark,
}: {
  src: string;
  alt: string;
  invertOnLight?: boolean;
  isDark: boolean;
}) {
  return (
    <img
      src={src}
      alt={alt}
      style={{
        width: "100%",
        height: "100%",
        objectFit: "contain",
        display: "block",
        filter: invertOnLight && !isDark ? "invert(1)" : "none",
        transition: "filter 0.3s ease",
      }}
    />
  );
}

/* ── Marquee ────────────────────────────────────────────────────────── */

export default function IntegrationMarquee() {
  const { theme } = useTheme();
  const isDark = theme === "dark";

  const icons: { name: string; node: React.ReactNode }[] = [
    {
      name: "Slack",
      node: <IconBox title="Slack"><SlackIcon /></IconBox>,
    },
    {
      name: "Gmail",
      node: <IconBox title="Gmail"><GmailIcon /></IconBox>,
    },
    {
      name: "Jira",
      node: (
        <IconBox title="Jira">
          <Logo src="/integrations/jira_clean.png" alt="Jira" isDark={isDark} />
        </IconBox>
      ),
    },
    {
      name: "Tavily",
      node: (
        <IconBox title="Tavily">
          <Logo src="/integrations/tavily_clean.png" alt="Tavily" isDark={isDark} invertOnLight />
        </IconBox>
      ),
    },
    {
      name: "Google Drive",
      node: (
        <IconBox title="Google Drive">
          <Logo src="/integrations/gdrive_clean.png" alt="Google Drive" isDark={isDark} />
        </IconBox>
      ),
    },
    {
      name: "Google Docs",
      node: (
        <IconBox title="Google Docs">
          <Logo src="/integrations/gdocs_clean.png" alt="Google Docs" isDark={isDark} />
        </IconBox>
      ),
    },
    {
      name: "Google Forms",
      node: (
        <IconBox title="Google Forms">
          <Logo src="/integrations/gforms_clean.png" alt="Google Forms" isDark={isDark} />
        </IconBox>
      ),
    },
    {
      name: "Google Calendar",
      node: (
        <IconBox title="Google Calendar">
          <Logo src="/integrations/gcal_clean.png" alt="Google Calendar" isDark={isDark} />
        </IconBox>
      ),
    },
  ];

  return (
    <>
      <style>{`
        .mqwrap {
          width: 420px;
          max-width: 100%;
          overflow: hidden;
          -webkit-mask-image: linear-gradient(
            to right, transparent 0%, black 12%, black 88%, transparent 100%
          );
          mask-image: linear-gradient(
            to right, transparent 0%, black 12%, black 88%, transparent 100%
          );
        }
        .mqtrack {
          display: flex;
          width: max-content;
          align-items: center;
          gap: 36px;
          padding: 6px 0;
          animation: mq-scroll 22s linear infinite;
          will-change: transform;
        }
        @keyframes mq-scroll {
          from { transform: translateX(0); }
          to   { transform: translateX(-50%); }
        }
        .mqitem {
          flex: 0 0 auto;
          display: flex;
          align-items: center;
          justify-content: center;
          opacity: 0.85;
          transition: opacity 0.18s ease, transform 0.18s ease;
          cursor: default;
        }
        .mqitem:hover {
          opacity: 1;
          transform: scale(1.12) translateY(-2px);
        }
      `}</style>

      <div className="mqwrap">
        <div className="mqtrack">
          {[0, 1].map((set) => (
            <React.Fragment key={set}>
              {icons.map(({ name, node }) => (
                <div key={name} className="mqitem">
                  {node}
                </div>
              ))}
            </React.Fragment>
          ))}
        </div>
      </div>
    </>
  );
}