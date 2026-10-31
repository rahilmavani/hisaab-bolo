const OFFLINE =
  "Can't reach Hisaab right now. Check your internet connection and try again.";

/** fetch() rejects with a TypeError when the network is down; everything else carries our own message. */
export const errorText = (e: unknown, fallback: string) =>
  e instanceof TypeError ? OFFLINE : e instanceof Error ? e.message : fallback;
