import { AsyncLocalStorage } from "node:async_hooks";

// Better Auth awaits the email-OTP send callback inside a catch that only
// logs, so a mail-transport rejection never reaches whoever called
// `auth.api.sendVerificationOTP`. The callback records its failure here, and
// a server-side sender that opened a scope with `withSendOutcome` gets it
// rethrown once the API call returns. Recording no-ops outside a scope, so
// client-initiated sends from the login page are unaffected.

type SendOutcome = { failure: { error: unknown } | null };

const sendOutcome = new AsyncLocalStorage<SendOutcome>();

export function recordSendFailure(error: unknown) {
  const store = sendOutcome.getStore();
  if (store) store.failure = { error };
}

export async function withSendOutcome<T>(fn: () => Promise<T>): Promise<T> {
  const store: SendOutcome = { failure: null };
  const result = await sendOutcome.run(store, fn);
  if (store.failure) throw store.failure.error;
  return result;
}
