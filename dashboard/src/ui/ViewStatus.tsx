import AlertBanner from "@kit/AlertBanner";
import BlockLoader from "@kit/BlockLoader";
import Text from "@kit/Text";
import { friendlyError } from "../friendlyError";

export function ViewStatus({
  loading,
  error,
  empty,
  emptyHint,
}: {
  loading?: boolean;
  error?: string | null;
  empty?: boolean;
  emptyHint?: string;
}) {
  if (error) {
    return (
      <>
        <AlertBanner>{friendlyError(error)}</AlertBanner>
        {/* the banner's shadow hangs below it: leave room so the row under it is not covered */}
        <div style={{ height: 14 }} aria-hidden />
      </>
    );
  }
  if (loading && empty) {
    return (
      <>
        <BlockLoader />
        <br />
      </>
    );
  }
  if (empty && emptyHint) {
    return <Text className="e-faint">{emptyHint}</Text>;
  }
  return null;
}