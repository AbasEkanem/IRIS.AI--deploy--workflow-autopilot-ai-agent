"use client";

import { useState } from "react";
import { Loader2, ShieldCheck, ArrowRight, UserCheck, Sun, Moon } from "lucide-react";
import Image from "next/image";
import { signIn } from "next-auth/react";
import { useTheme } from "@/context/ThemeContext";
import IrisLogo from "@/components/IrisLogo";

/* ═══════════════════════════════════════════════════════════
   COMPONENT
   ═══════════════════════════════════════════════════════════ */
export function LoginScreen() {
  const { theme, toggle } = useTheme();
  const isDark = theme === "dark";

  const [step, setStep] = useState<1 | 2>(1);
  const [email, setEmail] = useState("");
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");

  const [isSubmitting, setIsSubmitting] = useState(false);
  const [authError, setAuthError] = useState("");
  const [profileError, setProfileError] = useState("");

  /* ── Profile submit ───────────────────────────────────────── */
  const handleProfileSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!firstName.trim()) {
      setProfileError("First Name is required");
      return;
    }
    setProfileError("");
    setIsSubmitting(true);

    // Attempt credentials sign in
    const res = await signIn("credentials", {
      email: email.trim(),
      firstName: firstName.trim(),
      lastName: lastName.trim(),
      redirect: false,
    });

    if (res?.error) {
      setProfileError(res.error);
      setIsSubmitting(false);
    }
    // On success, NextAuth will update the session and the parent component 
    // (page.tsx) will automatically re-render without the LoginScreen.
  };

  const handleGoogleSignIn = () => {
    setAuthError("");
    signIn("google").catch(() => {
      setAuthError("Google Sign-In failed. Try the email option below.");
    });
  };

  /* ── Shared styles ────────────────────────────────────────── */
  const inputWrapStyle: React.CSSProperties = {
    borderRadius: 14,
    border: "1px solid var(--input-border)",
    background: "var(--input-bg)",
    transition: "border-color 0.2s",
  };
  const inputStyle: React.CSSProperties = {
    width: "100%",
    border: "none",
    background: "transparent",
    padding: "12px 16px",
    fontSize: 14,
    color: "var(--text)",
    outline: "none",
    fontFamily: "inherit",
    boxSizing: "border-box",
  };
  const submitBtnStyle: React.CSSProperties = {
    display: "flex",
    width: "100%",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    borderRadius: "var(--ne-radius-pill, 999px)",
    border: "none",
    background: "var(--ne-gradient)",
    padding: "12px 0",
    fontSize: 14,
    fontWeight: 600,
    color: "#ffffff",
    cursor: isSubmitting ? "wait" : "pointer",
    transition: "filter 180ms cubic-bezier(0.3, 0, 0.1, 1), transform 180ms cubic-bezier(0.3, 0, 0.1, 1)",
    opacity: isSubmitting ? 0.6 : 1,
    fontFamily: "inherit",
  };

  /* ─────────────────────────────────────────────────────────── */
  return (
    <div
      data-theme={theme}
      className={isDark ? "theme-dark gemini-bg" : "theme-light"}
      style={{
        position: "relative",
        display: "flex",
        minHeight: "100dvh",
        width: "100%",
        alignItems: "center",
        justifyContent: "center",
        padding: "12px 16px",
        color: "var(--text)",
        // Vertical scroll, not hidden: the card is centred, so on a short
        // viewport (landscape phone, or step 2 with its taller form) `overflow:
        // hidden` clipped it at BOTH ends with no way to reach the button.
        // Horizontal stays hidden — the glow layer is full-bleed.
        overflowX: "hidden",
        overflowY: "auto",
        backgroundColor: isDark ? "#0a0a0e" : "var(--bg)",
        backgroundImage: isDark
          ? "radial-gradient(ellipse 70% 60% at 50% 45%, #16233d 0%, #0d1520 35%, #0a0a0e 70%)"
          : undefined,
        fontFamily: "var(--ne-font-body, 'Google Sans Text', 'Inter', system-ui, sans-serif)",
        transition: "background 0.3s ease, color 0.3s ease",
      }}
    >
      {/* Top right theme toggle */}
      <button
        type="button"
        onClick={toggle}
        title={isDark ? "Switch to Day theme" : "Switch to Night theme"}
        style={{
          position: "absolute",
          top: 20,
          right: 20,
          zIndex: 10,
          width: 40,
          height: 40,
          borderRadius: "50%",
          background: "var(--surface)",
          border: `1px solid ${isDark ? "rgba(255,255,255,0.08)" : "var(--ne-border, #dcdfe5)"}`,
          cursor: "pointer",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "var(--text)",
          boxShadow: isDark ? "0 4px 12px rgba(0,0,0,0.4)" : "0 2px 8px rgba(0,0,0,0.06)",
          transition: "all 0.2s ease",
        }}
        onMouseEnter={e => {
          e.currentTarget.style.transform = "scale(1.05)";
          e.currentTarget.style.borderColor = "var(--accent)";
        }}
        onMouseLeave={e => {
          e.currentTarget.style.transform = "scale(1)";
          e.currentTarget.style.borderColor = isDark ? "rgba(255,255,255,0.08)" : "var(--ne-border, #dcdfe5)";
        }}
      >
        {isDark ? <Sun size={18} /> : <Moon size={18} />}
      </button>
      {/* Atmospheric Neural Expressive Energy Glow */}
      <div
        style={{
          position: "absolute",
          inset: 0,
          background: isDark
            ? "radial-gradient(circle at 80% 20%, rgba(77,127,255,0.08), transparent 50%), radial-gradient(circle at 20% 80%, rgba(255,111,176,0.05), transparent 50%)"
            : "radial-gradient(circle at 80% 20%, rgba(77,127,255,0.08), transparent 50%), radial-gradient(circle at 20% 80%, rgba(255,111,176,0.06), transparent 50%), radial-gradient(circle at 50% 50%, rgba(143,107,255,0.04), transparent 60%)",
          pointerEvents: "none",
        }}
      />

      {/* Card */}
      <div
        style={{
          position: "relative",
          zIndex: 1,
          width: "100%",
          maxWidth: 420,
          borderRadius: 28,
          border: `1px solid ${isDark ? "rgba(255,255,255,0.08)" : "#E1E3E1"}`,
          background: "var(--surface)",
          // Fluid gutter: 32px each side costs a fifth of a 320px screen.
          padding: "clamp(24px, 6vw, 36px) clamp(18px, 6vw, 32px)",
          boxShadow: isDark
            ? "0 20px 48px -12px rgba(0,0,0,0.6)"
            : "0 16px 36px -12px rgba(0,0,0,0.06)",
          animation: "fade-up 0.3s ease",
        }}
      >
        {/* ── STEP 1: Google Sign-In ─────────────────────────── */}
        {step === 1 && (
          <>
            {/* Brand header */}
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                textAlign: "center",
              }}
            >
              <IrisLogo size={96} style={{ marginBottom: 20 }} />

              <h2
                style={{
                  fontSize: 24,
                  fontWeight: 700,
                  letterSpacing: "-0.02em",
                  color: "var(--text)",
                  margin: 0,
                }}
              >
                Welcome to IRIS 1.0
              </h2>
              <p
                style={{
                  marginTop: 8,
                  fontSize: 13.5,
                  color: "var(--text-muted)",
                }}
              >
                Your workflow autopilot...
              </p>
            </div>

            {/* Auth area */}
            <div
              style={{
                marginTop: 32,
                display: "flex",
                flexDirection: "column",
                gap: 20,
              }}
            >
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  gap: 10,
                }}
              >
                <button
                  type="button"
                  onClick={handleGoogleSignIn}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    gap: 12,
                    width: "100%",
                    borderRadius: 14,
                    border: "1px solid var(--border)",
                    background: isDark ? "var(--surface-2)" : "#fafbfc",
                    padding: "12px 0",
                    fontSize: 15,
                    fontWeight: 600,
                    color: "var(--text)",
                    cursor: "pointer",
                    transition: "all 0.2s",
                    fontFamily: "inherit",
                    boxShadow: isDark ? "none" : "0 1px 3px rgba(0,0,0,0.05)",
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.background = isDark ? "var(--surface-3)" : "var(--surface-2)";
                    e.currentTarget.style.borderColor = "var(--border-strong)";
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.background = isDark ? "var(--surface-2)" : "#fafbfc";
                    e.currentTarget.style.borderColor = "var(--border)";
                  }}
                >
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4" />
                    <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853" />
                    <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05" />
                    <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335" />
                  </svg>
                  Continue with Google
                </button>

                {authError && (
                  <p
                    style={{
                      fontSize: 12,
                      color: "var(--red, #f87171)",
                      fontWeight: 500,
                      margin: 0,
                      textAlign: "center",
                    }}
                  >
                    {authError}
                  </p>
                )}
              </div>

              {/* Divider */}
              <div style={{ display: "flex", alignItems: "center", padding: "0" }}>
                <div style={{ flex: 1, borderTop: "1px solid var(--border)" }} />
                <span
                  style={{
                    padding: "0 16px",
                    fontSize: 11,
                    color: "var(--text-muted)",
                    textTransform: "uppercase",
                    fontWeight: 500,
                    letterSpacing: "0.08em",
                  }}
                >
                  or
                </span>
                <div style={{ flex: 1, borderTop: "1px solid var(--border)" }} />
              </div>

              {/* Manual e-mail fallback */}
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  setAuthError("");
                  if (!email.trim() || !email.includes("@")) {
                    setAuthError("Please enter a valid email address");
                    return;
                  }
                  setStep(2);
                }}
                style={{ display: "flex", flexDirection: "column", gap: 12 }}
              >
                <div style={inputWrapStyle}>
                  <input
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="Enter your email address"
                    style={inputStyle}
                  />
                </div>

                <button
                  type="submit"
                  style={submitBtnStyle}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.filter = "brightness(1.08)";
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.filter = "none";
                  }}
                >
                  Continue with Email <ArrowRight size={16} />
                </button>
              </form>
            </div>
          </>
        )}

        {/* ── STEP 2: Complete Profile ───────────────────────── */}
        {step === 2 && (
          <div style={{ animation: "fade-up 0.3s ease" }}>
            {/* Header */}
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                textAlign: "center",
              }}
            >
              <div
                style={{
                  marginBottom: 20,
                  width: 56,
                  height: 56,
                  borderRadius: 14,
                  background: "var(--accent-subtle)",
                  color: "var(--accent)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                <UserCheck size={28} />
              </div>

              <h2
                style={{
                  fontSize: 20,
                  fontWeight: 700,
                  letterSpacing: "-0.02em",
                  color: "var(--text)",
                  margin: 0,
                }}
              >
                Complete your Profile
              </h2>
              <p
                style={{
                  marginTop: 8,
                  fontSize: 12,
                  color: "var(--text-muted)",
                  maxWidth: 280,
                }}
              >
                Tell us your name so IRIS can personalize your research workspace
              </p>
            </div>

            <form
              onSubmit={handleProfileSubmit}
              style={{
                marginTop: 28,
                display: "flex",
                flexDirection: "column",
                gap: 14,
              }}
            >
              {/* Pre-filled email (read-only) */}
              <div
                style={{
                  borderRadius: 14,
                  border: "1px solid var(--border)",
                  background: "var(--surface-2)",
                  padding: "10px 16px",
                }}
              >
                <p
                  style={{
                    margin: 0,
                    fontSize: 10,
                    textTransform: "uppercase",
                    fontWeight: 700,
                    letterSpacing: "0.08em",
                    color: "var(--text-muted)",
                  }}
                >
                  Email Account
                </p>
                <p
                  style={{
                    margin: "4px 0 0",
                    fontSize: 14,
                    fontWeight: 500,
                    color: "var(--text)",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {email}
                </p>
              </div>

              {/* First name */}
              <div>
                <label
                  style={{
                    display: "block",
                    fontSize: 12,
                    fontWeight: 600,
                    color: "var(--text-muted)",
                    marginBottom: 6,
                  }}
                >
                  First Name
                </label>
                <div style={inputWrapStyle}>
                  <input
                    type="text"
                    value={firstName}
                    onChange={(e) => setFirstName(e.target.value)}
                    placeholder="Enter your first name"
                    required
                    style={inputStyle}
                  />
                </div>
              </div>

              {/* Last name */}
              <div>
                <label
                  style={{
                    display: "block",
                    fontSize: 12,
                    fontWeight: 600,
                    color: "var(--text-muted)",
                    marginBottom: 6,
                  }}
                >
                  Last Name{" "}
                  <span style={{ fontWeight: 400, opacity: 0.5 }}>(Optional)</span>
                </label>
                <div style={inputWrapStyle}>
                  <input
                    type="text"
                    value={lastName}
                    onChange={(e) => setLastName(e.target.value)}
                    placeholder="Enter your last name"
                    style={inputStyle}
                  />
                </div>
              </div>

              {profileError && (
                <p
                  style={{
                    fontSize: 12,
                    color: "var(--red, #f87171)",
                    fontWeight: 500,
                    margin: 0,
                  }}
                >
                  {profileError}
                </p>
              )}

              <button
                type="submit"
                disabled={isSubmitting}
                style={submitBtnStyle}
                onMouseEnter={(e) => {
                  if (!isSubmitting)
                    e.currentTarget.style.filter = "brightness(1.08)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.filter = "none";
                }}
              >
                {isSubmitting ? (
                  <>
                    <Loader2
                      size={16}
                      style={{ animation: "iris-orbit 1s linear infinite" }}
                    />
                    Initializing workspace…
                  </>
                ) : (
                  <>
                    Enter Workspace <ArrowRight size={16} />
                  </>
                )}
              </button>

              {/* Back link */}
              <button
                type="button"
                onClick={() => {
                  setStep(1);
                }}
                style={{
                  background: "none",
                  border: "none",
                  fontSize: 12,
                  color: "var(--text-muted)",
                  cursor: "pointer",
                  fontFamily: "inherit",
                  textAlign: "center",
                  textDecoration: "underline",
                  padding: 0,
                  marginTop: -4,
                }}
              >
                ← Use a different account
              </button>
            </form>
          </div>
        )}

        {/* Security footer */}
        <div
          style={{
            marginTop: 28,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 6,
            fontSize: 11,
            color: "var(--text-muted)",
          }}
        >
          <ShieldCheck size={14} style={{ color: "var(--green, #10b981)" }} />
          <span>Secured with standard end-to-end cryptographic layers.</span>
        </div>
      </div>
    </div>
  );
}