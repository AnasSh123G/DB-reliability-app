import { TrainFront } from "lucide-react";
import { Link } from "@tanstack/react-router";
import { useLineDetail } from "@/hooks/use-dbrel";
import { copy, type Locale } from "@/lib/i18n";

interface Props {
  lineId: number;
  locale: Locale;
}

export function LineDetail({ lineId, locale }: Props) {
  const t = copy[locale];
  const { data: detail, isLoading, isError } = useLineDetail(lineId);

  if (isLoading) {
    return (
      <section className="rise mt-6 rounded-2xl border border-line bg-panel p-5 sm:p-7">
        <p className="text-sm text-fog">{t.loading}</p>
      </section>
    );
  }

  if (isError || !detail) {
    return (
      <section className="rise mt-6 rounded-2xl border border-line bg-panel p-5 sm:p-7">
        <p className="text-sm text-fog">{t.error}</p>
      </section>
    );
  }

  return (
    <section className="rise mt-6 rounded-2xl border border-line bg-panel p-5 sm:p-7" aria-labelledby="line-heading">
      <div className="border-b border-line pb-5">
        <p className="text-xs text-fog">{t.lineDetail}</p>
        <h2 id="line-heading" className="mt-1 font-display text-2xl font-semibold text-paper">
          {detail.line.name}
          {detail.line.replacement_bus && (
            <span className="ml-2 align-middle text-xs font-normal text-amber">{t.replacementBus}</span>
          )}
        </h2>
        <p className="mt-1 text-xs text-fog">
          {detail.n_runs} {t.runs}
          {detail.line.operator && ` · ${detail.line.operator}`}
        </p>
      </div>

      {detail.n_runs > 0 ? (
        <div className="mt-4 grid gap-6 sm:grid-cols-2">
          {detail.directions.map((dir, index) => (
            <section key={index}>
              <h3 className="flex items-center justify-between border-b border-line pb-2 text-xs uppercase text-fog">
                <span>{dir.label}</span>
                <span>{dir.n_runs} {t.runs}</span>
              </h3>
              {dir.runs.length > 0 ? (
                <ol>
                  {dir.runs.map((run) => (
                    <li
                      key={run.id}
                      className={`flex items-center justify-between gap-3 border-b border-line py-3 ${run.current ? "bg-ink/50" : ""}`}
                    >
                      <div className="flex min-w-0 items-center gap-3">
                        <span className="grid size-7 shrink-0 place-items-center rounded-lg bg-ink text-teal">
                          <TrainFront className="size-3.5" />
                        </span>
                        <div className="min-w-0">
                          <Link
                            to="/train/$trainId"
                            params={{ trainId: String(run.id) }}
                            className="font-display font-semibold text-paper hover:underline"
                          >
                            {run.name}
                          </Link>
                          <p className="truncate text-[11px] text-fog">
                            → {run.destination ?? "—"}
                            {run.partial && <span className="ml-2 text-amber">{t.partOfRoute}</span>}
                            {run.current && <span className="ml-2 text-teal">{t.thisRun}</span>}
                          </p>
                        </div>
                      </div>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="py-4 text-sm text-fog">{t.nothingScheduled}</p>
              )}
            </section>
          ))}
        </div>
      ) : (
        <p className="mt-4 text-sm text-fog">{t.noRunsYet}</p>
      )}
    </section>
  );
}
