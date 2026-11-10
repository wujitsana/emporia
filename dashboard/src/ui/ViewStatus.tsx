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
        <br />
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