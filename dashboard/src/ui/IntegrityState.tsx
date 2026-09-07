import Text from "@components/Text";

/**
 * The third state every integrity badge has and none of them rendered (P68).
 *
 * `RoomChainBadge`, `DmChainBadge` and `AuditBadge` each verify something and
 * each opened with `if (!result) return null`. `useInterval` returns
 * `[data, loading, error]`; all three took only the data. So a verification
 * that *failed to run* — relay down, fetch rejected, Web Crypto unavailable —
 * rendered as **nothing at all**, in a list beside rooms showing
 * "✓ chain verified". The natural reading of a blank cell next to a row of
 * ticks is "that one has not been checked yet", not "the check could not be
 * performed", and those are very different claims to make about a hash chain.
 *
 * This is the same rule the relay already enforces on itself, moved to the one
 * place that was still collapsing it. `payments.x402_authorization_used()`
 * returns True/False/**None**, and P48 records that folding None into False
 * reopens the double charge. `payments.find_settlement_transfer()` returns
 * `(known, transfer_id)`, and P51 records that `known=False` never means "no
 * transfer". Verified, unverified and *could not check* are three answers, and
 * a badge that renders two of them is lying about the third.
 *
 * Absence now means exactly one thing: there is nothing here to verify.
 */

export function IntegrityUnknown({ what, detail }: { what: string; detail?: string | null }) {
  return (
    <Text
      className="e-faint"
      style={{ fontSize: 11, color: "var(--theme-text, inherit)", opacity: 0.75 }}
      title={
        `Could not verify this ${what}. This is not a failed check — it is a check that did ` +
        `not run, so nothing is claimed either way.` + (detail ? `\n\n${detail}` : "")
      }
    >
      ? {what} unverifiable
    </Text>
  );
}

export function IntegrityChecking({ what }: { what: string }) {
  return (
    <Text
      className="e-faint"
      style={{ fontSize: 11, opacity: 0.6 }}
      title={`Verifying this ${what} now.`}
    >
      … checking {what}
    </Text>
  );
}
