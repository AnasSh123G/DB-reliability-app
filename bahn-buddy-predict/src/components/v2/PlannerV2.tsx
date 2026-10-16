import { useEffect, useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { ApiError } from "@/lib/api";
import { usePlan, usePrefetchPlanTimings } from "@/hooks/use-dbrel";
import { copy, type Locale } from "@/lib/i18n";
import type { PlanSearch } from "@/routes/plan";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { StationPicker, type PlannerStation } from "./StationPicker";
import { FieldLabel } from "./FieldLabel";
import { berlinMoment, formatDay } from "./run-format";
import { icons } from "./icons";
import { JourneyRow } from "./JourneyRow";
import { JOURNEY_ROW_HEIGHT, JourneyColumnHeader } from "./JourneyRow";
import { recordStations } from "@/lib/frequent-stations";
import { v2Paths } from "./paths";
import { JourneyTimings } from "./JourneyTimings";
interface Form {
  from: PlannerStation | null;
  to: PlannerStation | null;
  date: string;
  time: string;
}
// The same fields and quick buttons as the search tab (SearchV2).
const FIELD_CLASS =
  "h-[46px] w-full rounded-md border border-line bg-ink px-3 text-sm font-medium text-paper outline-none focus:border-teal";
const QUICK_CLASS =
  "inline-flex h-[42px] w-full items-center justify-center rounded-md border border-line bg-ink px-4 font-display text-[15px] font-semibold text-teal hover:bg-panel";
const CARD_CLASS = "rounded-lg border border-line bg-panel p-5 text-paper shadow-none sm:p-7";
function fromSearch(search: PlanSearch): Form {
  return {
    from: search.from ? { id: search.from, name: "" } : null,
    to: search.to ? { id: search.to, name: "" } : null,
    date: search.date ?? "",
    time: search.time ?? "",
  };
}
export function PlannerV2({ search, locale }: { search: PlanSearch; locale: Locale }) {
  const t = copy[locale];
  const navigate = useNavigate();
  const [form, setForm] = useState<Form>(() => fromSearch(search));
  // History navigation resets the form; selecting another option preserves typed fields.
  useEffect(() => {
    setForm((previous) => {
      const next = fromSearch({
        from: search.from,
        to: search.to,
        date: search.date,
        time: search.time,
      });
      return {
        ...next,
        from: next.from?.id === previous.from?.id ? previous.from : next.from,
        to: next.to?.id === previous.to?.id ? previous.to : next.to,
      };
    });
  }, [search.from, search.to, search.date, search.time]);
  const submitted =
    search.from !== undefined &&
    search.to !== undefined &&
    search.date !== undefined &&
    search.time !== undefined;
  const query = usePlan(
    submitted
      ? { from: search.from!, to: search.to!, date: search.date!, time: search.time! }
      : undefined,
  );
  useEffect(() => {
    if (!query.data) return;
    const data = query.data;
    setForm((previous) => ({
      ...previous,
      from:
        previous.from?.id === data.from.id && previous.from.name === "" ? data.from : previous.from,
      to: previous.to?.id === data.to.id && previous.to.name === "" ? data.to : previous.to,
    }));
  }, [query.data]);
  const matches =
    submitted &&
    form.from?.id === search.from &&
    form.to?.id === search.to &&
    form.date === search.date &&
    form.time === search.time;
  const journeys = query.data?.journeys ?? [];
  const option = search.option ?? 0;
  const selected = journeys[option] ?? journeys[0];
  // The chosen connection first, so its list is the one ready soonest.
  usePrefetchPlanTimings(
    query.data ? { from: query.data.from.id, to: query.data.to.id } : undefined,
    selected ? [selected, ...journeys.filter((journey) => journey !== selected)] : [],
    berlinMoment,
  );
  const Swap = icons.swap;
  const reason = query.data?.reason;
  const empty =
    reason === "from_unserved"
      ? t.fromUnserved
      : reason === "to_unserved"
        ? t.toUnserved
        : reason === "search_limit"
          ? t.searchLimit
          : t.noConnection;
  const canSubmit = Boolean(
    form.from && form.to && form.from.id !== form.to.id && form.date && form.time,
  );
  const data = query.data;
  return (
    <div>
      <h1 className="sr-only">{t.planner}</h1>
      <Card className="rounded-lg border border-line bg-panel p-4 text-paper shadow-none sm:p-5">
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (!canSubmit) return;
            recordStations([form.from!, form.to!]);
            void navigate({
              to: "/plan",
              search: { from: form.from!.id, to: form.to!.id, date: form.date, time: form.time },
            });
          }}
        >
          <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]">
            <StationPicker
              inputId="plan-from"
              label={t.fromStation}
              icon="from"
              value={form.from}
              onChange={(from) => setForm((previous) => ({ ...previous, from }))}
              t={t}
            />
            <button
              type="button"
              aria-label={t.swapStations}
              title={t.swapStations}
              className="mx-auto grid size-[46px] place-items-center rounded-md border border-line bg-ink text-teal hover:bg-panel"
              onClick={() =>
                setForm((previous) => ({ ...previous, from: previous.to, to: previous.from }))
              }
            >
              <Swap className="size-4" aria-hidden="true" />
            </button>
            <StationPicker
              inputId="plan-to"
              label={t.toStation}
              icon="to"
              value={form.to}
              onChange={(to) => setForm((previous) => ({ ...previous, to }))}
              t={t}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="min-w-0 space-y-2">
              <FieldLabel htmlFor="plan-date" kind="date" className="mb-1.5">
                {t.searchDate}
              </FieldLabel>
              <input
                id="plan-date"
                required
                type="date"
                className={FIELD_CLASS}
                value={form.date}
                onChange={(event) =>
                  setForm((previous) => ({ ...previous, date: event.target.value }))
                }
              />
              <button
                type="button"
                className={QUICK_CLASS}
                onClick={() =>
                  setForm((previous) => ({
                    ...previous,
                    date: berlinMoment(Date.now() / 1000).date,
                  }))
                }
              >
                {t.today}
              </button>
            </div>
            <div className="min-w-0 space-y-2">
              <FieldLabel htmlFor="plan-time" kind="time" className="mb-1.5">
                {t.searchTime}
              </FieldLabel>
              <input
                id="plan-time"
                required
                type="time"
                className={FIELD_CLASS}
                value={form.time}
                onChange={(event) =>
                  setForm((previous) => ({ ...previous, time: event.target.value }))
                }
              />
              <button
                type="button"
                className={QUICK_CLASS}
                onClick={() =>
                  setForm((previous) => ({
                    ...previous,
                    time: berlinMoment(Date.now() / 1000).time,
                  }))
                }
              >
                {t.now}
              </button>
            </div>
          </div>
          <Button
            type="submit"
            disabled={!canSubmit}
            className="h-[42px] w-full rounded-md bg-teal font-display font-semibold text-action-foreground hover:bg-teal disabled:opacity-40"
          >
            {t.findConnections}
          </Button>
        </form>
      </Card>
      {matches && (
        <div aria-live="polite" className="mt-6 space-y-5">
          {query.isPending ? (
            <Card className={`rise ${CARD_CLASS}`} aria-label={t.loading}>
              <p className="text-sm text-fog">{t.searchingSlow}</p>
              <Skeleton className="mt-3 h-7 w-2/3 bg-line" />
              <div className="mt-5 space-y-3">
                {Array.from({ length: 3 }, (_, index) => (
                  <Skeleton key={index} className={`${JOURNEY_ROW_HEIGHT} w-full bg-line`} />
                ))}
              </div>
            </Card>
          ) : query.isError ? (
            <Card className={`rise ${CARD_CLASS}`}>
              <p role="alert" className="text-sm text-danger">
                {query.error instanceof ApiError &&
                query.error.status === 503 &&
                query.error.message === "no delay model is available"
                  ? t.noModel
                  : t.error}
              </p>
            </Card>
          ) : (
            data && (
              <>
                <Card className={`rise ${CARD_CLASS}`} role="region" aria-labelledby="plan-heading">
                  <div className="border-b border-line pb-5">
                    <p className="text-xs text-fog">{t.connections}</p>
                    <h2
                      id="plan-heading"
                      className="mt-1 font-display text-2xl font-semibold text-paper"
                    >
                      <Link
                        {...v2Paths.station(data.from.id, { date: data.date, time: data.time })}
                        className="hover:text-teal hover:underline"
                      >
                        {data.from.name}
                      </Link>
                      <span className="text-fog"> → </span>
                      <Link
                        {...v2Paths.station(data.to.id, { date: data.date, time: data.time })}
                        className="hover:text-teal hover:underline"
                      >
                        {data.to.name}
                      </Link>
                    </h2>
                    <p className="mt-1 text-xs text-fog">
                      <span className="inline-flex items-center gap-1.5">
                        <icons.time className="size-3.5 shrink-0" aria-hidden="true" />
                        {t.departingFrom.replace("{time}", data.time)}
                      </span>
                      <span className="ml-3 inline-flex items-center gap-1.5">
                        <icons.date className="size-3.5 shrink-0" aria-hidden="true" />
                        {formatDay(data.date, locale)}
                      </span>
                    </p>
                    <p className="mt-1 text-xs text-fog">
                      {t.dataUntil.replace("{date}", data.data_until ?? "—")} · {t.forecastNote}
                    </p>
                  </div>
                  {journeys.length ? (
                    <div className="mt-3">
                      <JourneyColumnHeader t={t} />
                      <ol className="divide-y divide-line">
                        {journeys.map((journey, index) => (
                          <li key={journey.route_key} className={JOURNEY_ROW_HEIGHT}>
                            <JourneyRow
                              journey={journey}
                              chosen={journey === selected}
                              marked={journey === selected}
                              locale={locale}
                              t={t}
                              onSelect={() => {
                                void navigate({
                                  to: "/plan",
                                  search: { ...search, option: index },
                                  replace: true,
                                  // Choosing a connection only swaps the cards below it.
                                  resetScroll: false,
                                });
                              }}
                            />
                          </li>
                        ))}
                      </ol>
                    </div>
                  ) : (
                    <p className="px-1 py-8 text-center text-sm text-fog">{empty}</p>
                  )}
                </Card>
                {selected && (
                  <JourneyTimings
                    journey={selected}
                    from={search.from!}
                    to={search.to!}
                    locale={locale}
                    t={t}
                  />
                )}
              </>
            )
          )}
        </div>
      )}
    </div>
  );
}
