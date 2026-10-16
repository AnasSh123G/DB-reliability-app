import { createFileRoute } from "@tanstack/react-router";
import { useAppChrome } from "@/components/AppShell";
import { ShellV2 } from "@/components/v2/ShellV2";
import { PlannerV2 } from "@/components/v2/PlannerV2";
export interface PlanSearch {
  from?: number | undefined;
  to?: number | undefined;
  date?: string | undefined;
  time?: string | undefined;
  option?: number | undefined;
}
export const Route = createFileRoute("/plan")({
  validateSearch: (search: Record<string, unknown>): PlanSearch => {
    const result: PlanSearch = {};
    for (const key of ["from", "to"] as const) {
      const raw = search[key];
      const value = typeof raw === "string" || typeof raw === "number" ? Number(raw) : NaN;
      if (Number.isInteger(value) && value > 0) result[key] = value;
    }
    if (typeof search["date"] === "string" && /^\d{4}-\d{2}-\d{2}$/.test(search["date"]))
      result.date = search["date"];
    if (typeof search["time"] === "string" && /^\d{2}:\d{2}$/.test(search["time"]))
      result.time = search["time"];
    const rawOption = search["option"];
    const option =
      typeof rawOption === "string" || typeof rawOption === "number" ? Number(rawOption) : NaN;
    if (Number.isInteger(option) && option >= 0 && option <= 2) result.option = option;
    return result;
  },
  head: () => ({ meta: [{ title: "Reiseplaner – BahnPredict" }] }),
  component: PlanPage,
});
function PlanPage() {
  const search = Route.useSearch();
  const { locale, setLocale, isDark, setIsDark } = useAppChrome();
  return (
    <ShellV2 locale={locale} isDark={isDark} setLocale={setLocale} setIsDark={setIsDark}>
      <PlannerV2 search={search} locale={locale} />
    </ShellV2>
  );
}
