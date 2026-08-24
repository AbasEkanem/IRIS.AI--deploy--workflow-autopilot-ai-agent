// Module augmentation: add the backend bearer token to NextAuth's Session type.
//
// The `session` callback in src/app/api/auth/[...nextauth]/route.ts mints a
// short-lived HS256 JWS and stores it here; src/lib/api.ts reads it to send
// `Authorization: Bearer` to the FastAPI backend (verified in auth.py). Declaring
// it on the Session interface keeps both sites type-safe without `as any`.
import "next-auth";

declare module "next-auth" {
  interface Session {
    /** Short-lived HS256 bearer token the FastAPI backend verifies (auth.py). */
    backendToken?: string;
  }
}
