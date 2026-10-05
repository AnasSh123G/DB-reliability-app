// A train's operational number ("RB 10478") as small print beside the name a
// passenger sees on the platform ("RB22"). Hidden when the two are the same,
// as for long-distance trains, which are signed by their number.
export function TrainNumber({
  name,
  numberName,
}: {
  name: string;
  numberName?: string | undefined;
}) {
  if (!numberName || numberName === name) return null;
  return <span className="ml-1.5 font-mono text-[12.67px] font-normal text-fog">{numberName}</span>;
}
