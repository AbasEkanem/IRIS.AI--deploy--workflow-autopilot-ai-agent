"use client";
import { useState, useEffect, useRef } from "react";

interface RecalibrationCardProps {
  /** Exact UTC time when Gemini quota resets */
  resumeAt?: Date;
  /** Called when auto-retry fires after cooldown */
  onRetry?: () => void;
}

/** Format a duration into human-readable text: "42s", "3m 12s", "2h 15m" */
function formatDuration(totalSeconds: number): string {
  if (totalSeconds <= 0) return "now";
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = Math.ceil(totalSeconds % 60);
  if (h > 0) return m > 0 ? `${h}h ${m}m` : `${h}h`;
  if (m > 0) return s > 0 ? `${m}m ${s}s` : `${m}m`;
  return `${s}s`;
}

export default function RecalibrationCard({ resumeAt, onRetry }: RecalibrationCardProps) {
  const targetRef = useRef<Date>(resumeAt ?? new Date(Date.now() + 60_000));

  const calcSecondsLeft = () =>
    Math.max(0, Math.ceil((targetRef.current.getTime() - Date.now()) / 1000));

  const [secondsLeft, setSecondsLeft] = useState(calcSecondsLeft);
  const [phase, setPhase] = useState<"waiting" | "ready">("waiting");
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Update anchor when resumeAt changes
  useEffect(() => {
    if (!resumeAt) return;
    targetRef.current = resumeAt;
    setSecondsLeft(calcSecondsLeft());
    setPhase("waiting");
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resumeAt?.getTime()]);

  // Countdown ticker
  useEffect(() => {
    if (phase !== "waiting") return;
    intervalRef.current = setInterval(() => {
      const s = calcSecondsLeft();
      setSecondsLeft(s);
      if (s <= 0) {
        clearInterval(intervalRef.current!);
        setPhase("ready");
        setTimeout(() => onRetry?.(), 1200);
      }
    }, 1000);
    return () => { if (intervalRef.current) clearInterval(intervalRef.current); };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase]);

  const totalDuration = Math.max(
    1,
    Math.ceil((targetRef.current.getTime() - (targetRef.current.getTime() - secondsLeft * 1000)) / 1000)
  );
  const progress = Math.round(((totalDuration - secondsLeft) / totalDuration) * 100);
  const resumeStr = targetRef.current.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const isLongWait = secondsLeft > 300; // > 5 minutes = daily quota

  return (
    <>
      <style>{`
        @keyframes recalPulse  { 0%,100%{opacity:1} 50%{opacity:.6} }
        @keyframes recalSpin   { to{transform:rotate(360deg)} }
        @keyframes recalFadeIn { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:translateY(0)} }
        @keyframes recalCheck  { 0%{stroke-dashoffset:24} 100%{stroke-dashoffset:0} }
        @keyframes shieldPulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.7;transform:scale(1.05)} }
      `}</style>

      <div style={{
        background: "linear-gradient(135deg,rgba(124,111,247,.06),rgba(124,111,247,.02))",
        border: "1px solid rgba(124,111,247,.18)",
        borderRadius: 14, padding: "20px 22px", maxWidth: 460,
        animation: "recalFadeIn .4s ease both",
        position: "relative", overflow: "hidden",
      }}>
        {/* Progress bar */}
        <div style={{ position:"absolute",top:0,left:0,right:0,height:3,background:"rgba(124,111,247,.1)",borderRadius:"14px 14px 0 0" }}>
          <div style={{
            height:"100%", width:`${progress}%`,
            background: phase === "ready"
              ? "linear-gradient(90deg,#4ade80,#22d3ee)"
              : "linear-gradient(90deg,#7c6ff7,#a89cf7)",
            borderRadius:"14px 14px 0 0", transition:"width 1s linear",
          }} />
        </div>

        {/* ── Waiting ── */}
        {phase === "waiting" && (
          <div style={{ display:"flex", alignItems:"flex-start", gap:14 }}>
            {/* Shield icon */}
            <div style={{
              width:40, height:40, borderRadius:"50%", flexShrink:0,
              background:"rgba(124,111,247,.1)", border:"2px solid rgba(124,111,247,.2)",
              display:"flex", alignItems:"center", justifyContent:"center",
              animation:"shieldPulse 2s ease-in-out infinite",
            }}>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#7c6ff7" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
              </svg>
            </div>
            <div style={{ flex:1 }}>
              {/* Headline */}
              <div style={{
                fontSize:14.5, fontWeight:600, color:"var(--text,#EDE8E0)",
                marginBottom:6, lineHeight:1.4,
              }}>
                🛡️ Research quota reached — available again at {resumeStr}
              </div>
              {/* Subline */}
              <div style={{ fontSize:13, color:"var(--muted,#A09890)", lineHeight:1.6, marginBottom:14 }}>
                IRIS can still answer from memory and conversation while research recharges.
              </div>
              {/* Countdown badge */}
              <div style={{
                display:"inline-flex", alignItems:"center", gap:8,
                background:"rgba(124,111,247,.08)",
                border:"1px solid rgba(124,111,247,.18)",
                borderRadius:8, padding:"6px 12px",
              }}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#7c6ff7" strokeWidth="2" strokeLinecap="round">
                  <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
                </svg>
                <span style={{ fontSize:13, fontWeight:600, color:"#a89cf7", fontVariantNumeric:"tabular-nums" }}>
                  {secondsLeft > 0
                    ? isLongWait
                      ? `Resets at ${resumeStr} (${formatDuration(secondsLeft)})`
                      : `Back in ${formatDuration(secondsLeft)}`
                    : "Resuming now…"
                  }
                </span>
              </div>
            </div>
          </div>
        )}

        {/* ── Ready ── */}
        {phase === "ready" && (
          <div style={{ display:"flex",alignItems:"center",gap:14,animation:"recalFadeIn .4s ease both" }}>
            <div style={{
              width:40, height:40, borderRadius:"50%", flexShrink:0,
              background:"rgba(74,222,128,.12)", border:"2px solid rgba(74,222,128,.3)",
              display:"flex", alignItems:"center", justifyContent:"center",
            }}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#4ade80" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="20 6 9 17 4 12" style={{ strokeDasharray:24, animation:"recalCheck .4s ease both .1s" }}/>
              </svg>
            </div>
            <div style={{ flex:1 }}>
              <div style={{ fontSize:14.5, fontWeight:600, color:"#4ade80", marginBottom:2 }}>
                Research is back! ✨
              </div>
              <div style={{ fontSize:12.5, color:"var(--muted,#A09890)" }}>
                Resuming your query automatically…
              </div>
            </div>
          </div>
        )}
      </div>
    </>
  );
}
