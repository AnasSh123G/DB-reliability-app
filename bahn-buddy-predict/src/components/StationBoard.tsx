import { MapPin } from "lucide-react";
import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useStationBoard } from "@/hooks/use-dbrel";
import { delayColor, expectedTime } from "@/lib/delay";
import { copy, type Locale } from "@/lib/i18n";

interface Props {
  stationId: number;
  locale: Locale;
}

export function StationBoard({ stationId, locale }: Props) {
  const t = copy[locale];
  const [widened, setWidened] = useState(false);
  const { data: board, isLoading, isError } = useStationBoard(
    stationId,
    widened ? { backMin: 120, fwdMin: 240 } : undefined,
  );

  if (isLoading) {
    return (
      <section className="rise mt-6 rounded-2xl border border-line bg-panel p-5 sm:p-7">
        <p className="text-sm text-fog">{t.loading}</p>
      </section>
    );
  }

  if (isError || !board) {
    return (
      <section className="rise mt-6 rounded-2xl border border-line bg-panel p-5 sm:p-7">
        <p className="text-sm text-fog">{t.error}</p>
      </section>
    );
  }

  return (
    <section className="rise mt-6 rounded-2xl border border-line bg-panel p-5 sm:p-7" aria-labelledby="board-heading">
      <div className="border-b border-line pb-5">
        <p className="text-xs text-fog">{t.stationBoard}</p>
        <h2 id="board-heading" className="mt-1 font-display text-2xl font-semibold text-paper">
          {board.station.name}
          {board.station.eva && <span className="ml-2 align-middle text-xs font-normal text-fog">EVA {board.station.eva}</span>}
        </h2>
        <p className="mt-1 text-xs text-fog">
          {t.windowLabel}: {board.window.from} – {board.window.to} ({board.window.date})
        </p>
        {!board.station.tracked && (
          <p className="mt-2 rounded-xl border border-line bg-ink p-3 text-xs text-amber">{t.notTrackedStation}</p>
        )}
      </div>

      {board.calls.length > 0 ? (
        <ol className="mt-2">
          {board.calls.map((call) => {
            const expectedArr = expectedTime(call.arr, call.arr_pred);
            const expectedDep = expectedTime(call.dep, call.dep_pred);
            return (
              <li key={call.station_stop_id} className="flex flex-wrap items-center justify-between gap-3 border-t border-line py-4 sm:py-3">
                <div className="flex min-w-0 items-center gap-3">
                  <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-ink text-teal">
                    <MapPin className="size-4" />
                  </span>
                  <div className="min-w-0">
                    <Link
                      to="/train/$trainId"
                      params={{ trainId: String(call.train_id) }}
                      className="font-display font-semibold text-paper hover:underline"
                    >
                      {call.train}
                    </Link>
                    <p className="truncate text-[11px] text-fog">
                      {call.headline}
                      {call.terminates && <span className="ml-2 text-amber">{t.endsHere}</span>}
                      {call.platform && <span className="ml-2">{t.platform} {call.platform}</span>}
                    </p>
                  </div>
                </div>
                <div className="grid shrink-0 grid-cols-2 gap-x-4 text-right">
                  <span>
                    <span className="block text-[10px] uppercase font-normal text-fog">{t.arrival}</span>
                    <span className="mt-1 block font-display text-sm text-mist">{call.arr ?? "—"}</span>
                    {expectedArr && call.arr_pred ? (
                      <span className={`block text-[10px] font-semibold ${delayColor(call.arr_pred.p50_min)}`}>
                        {expectedArr} · +{call.arr_pred.p50_min}
                      </span>
                    ) : (
                      <span className="block text-[10px] text-fog">{call.arr ? t.noAverage : "—"}</span>
                    )}
                  </span>
                  <span>
                    <span className="block text-[10px] uppercase font-normal text-fog">{t.departure}</span>
                    <span className="mt-1 block font-display text-sm text-mist">{call.dep ?? "—"}</span>
                    {expectedDep && call.dep_pred ? (
                      <span className={`block text-[10px] font-semibold ${delayColor(call.dep_pred.p50_min)}`}>
                        {expectedDep} · +{call.dep_pred.p50_min}
                      </span>
                    ) : (
                      <span className="block text-[10px] text-fog">{call.dep ? t.noAverage : "—"}</span>
                    )}
                  </span>
                </div>
              </li>
            );
          })}
        </ol>
      ) : (
        <div className="px-1 py-8 text-center text-sm text-fog">
          <p>{t.nothingScheduled}</p>
          {board.station.tracked && !widened && (
            <button
              type="button"
              onClick={() => setWidened(true)}
              className="mt-2 text-teal hover:underline"
            >
              ±240 min
            </button>
          )}
        </div>
      )}
    </section>
  );
}
