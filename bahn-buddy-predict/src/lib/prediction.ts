export function addMinutes(hhmm: string, mins: number): string {
  const [h = 0, m = 0] = hhmm.split(":").map(Number);
  const total = (h * 60 + m + mins + 1440) % 1440;
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}
