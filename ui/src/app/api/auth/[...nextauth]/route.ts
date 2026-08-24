import NextAuth, { NextAuthOptions } from "next-auth";
import GoogleProvider from "next-auth/providers/google";
import CredentialsProvider from "next-auth/providers/credentials";
import { SignJWT } from "jose";

export const authOptions: NextAuthOptions = {
  providers: [
    GoogleProvider({
      clientId: process.env.GOOGLE_CLIENT_ID ?? "",
      clientSecret: process.env.GOOGLE_CLIENT_SECRET ?? "",
      authorization: {
        params: {
          // Force account selection so state cookie is always fresh
          prompt: "select_account",
        },
      },
    }),
    CredentialsProvider({
      name: "Email",
      credentials: {
        email:     { label: "Email",      type: "email" },
        firstName: { label: "First Name", type: "text" },
        lastName:  { label: "Last Name",  type: "text" },
      },
      async authorize(credentials) {
        if (!credentials?.email || !credentials?.firstName) return null;
        return {
          id:    credentials.email,
          name:  `${credentials.firstName} ${credentials.lastName ?? ""}`.trim(),
          email: credentials.email,
        };
      },
    }),
  ],

  session: {
    strategy: "jwt",
    // Keep sessions alive for 30 days; state cookies expire much sooner
    maxAge: 30 * 24 * 60 * 60,
  },

  // Explicitly set secret so Next.js 16 can find it without env ambiguity
  secret: process.env.NEXTAUTH_SECRET,

  callbacks: {
    async jwt({ token, user, account }) {
      // Persist extra fields from the first sign-in into the JWT
      if (user) {
        token.name    = user.name;
        token.email   = user.email;
        token.picture = (user as any).image ?? "";
      }
      return token;
    },
    async session({ session, token }) {
      if (session.user) {
        session.user.name  = token.name  as string;
        session.user.email = token.email as string;
        (session.user as any).image = token.picture as string;
      }
      // Mint a short-lived HS256 bearer token for the FastAPI backend. This is a
      // PLAIN signed JWS — NOT NextAuth's encrypted session cookie — so the backend
      // only needs the ONE shared symmetric secret to verify it (NEXTAUTH_SECRET
      // here == BACKEND_JWT_SECRET on the backend; see auth.py). This callback runs
      // server-side and re-runs on every getSession()/`/api/auth/session` fetch, so
      // the 30-min token is continually re-minted fresh inside the 30-day session.
      // api.ts sends it as `Authorization: Bearer`. Skip minting if the email or the
      // secret is missing — an unsigned/mis-signed token would only be rejected.
      const secret = process.env.NEXTAUTH_SECRET;
      if (token.email && secret) {
        session.backendToken = await new SignJWT({ email: token.email, name: token.name })
          .setProtectedHeader({ alg: "HS256" })
          .setSubject(token.email as string)
          .setAudience("iris-backend")
          .setIssuedAt()
          .setExpirationTime("30m")
          .sign(new TextEncoder().encode(secret));
      }
      return session;
    },
  },

  pages: {
    signIn:    "/",
    error:     "/",   // redirect auth errors back to home, not a separate error page
  },

  // Fixes state-cookie issues in local dev (HTTP)
  useSecureCookies: process.env.NEXTAUTH_URL?.startsWith("https://") ?? false,

  cookies: {
    // Ensure the state cookie is always readable during the OAuth callback
    pkceCodeVerifier: {
      name: "next-auth.pkce.code_verifier",
      options: {
        httpOnly: true,
        sameSite: "lax",
        path: "/",
        secure: process.env.NEXTAUTH_URL?.startsWith("https://") ?? false,
      },
    },
    state: {
      name: "next-auth.state",
      options: {
        httpOnly: true,
        sameSite: "lax",
        path: "/",
        secure: process.env.NEXTAUTH_URL?.startsWith("https://") ?? false,
        maxAge: 60 * 15, // 15 minutes — plenty of time for OAuth round-trip
      },
    },
  },
};

const handler = NextAuth(authOptions);

export { handler as GET, handler as POST };
