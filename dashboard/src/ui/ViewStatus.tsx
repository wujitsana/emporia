import AlertBanner from "@components/AlertBanner";
import BlockLoader from "@components/BlockLoader";
import Text from "@components/Text";

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
        <AlertBanner>{error}</AlertBanner>
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