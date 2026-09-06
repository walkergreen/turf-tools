import { auth } from "~/lib/auth";
import { withSendOutcome } from "~/lib/send-outcome";

// Sends the sign-in (magic link) email for an address that already has an
// active membership — Better Auth's before-hook rejects anything else. One
// seam for every server-side invite/resend so tests can replace it. Rejects
// when the mail transport refused the message, which Better Auth's own
// response never reports.
export async function sendSignInEmail(displayEmail: string): Promise<void> {
  await withSendOutcome(() =>
    auth.api.sendVerificationOTP({
      body: { email: displayEmail, type: "sign-in" },
      headers: new Headers(),
    }),
  );
}
