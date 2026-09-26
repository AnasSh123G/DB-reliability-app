// The v2 line detail view, generated from v1's LineDetail.tsx so it renders and behaves like it,
// with its own visual identity (see openspec/changes/redesign-v2-visual-identity/design.md).
import { TrainFront } from "lucide-react";
import { Link } from "@tanstack/react-router";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useLineDetail } from "@/hooks/use-dbrel";
import { copy, type Locale } from "@/lib/i18n";
import { v2Paths } from "./paths";

interface Props {
  lineId: number;
  locale: Locale;
}

export function LineV2({ lineId, locale }: Props) {
  const t = copy[locale];
  const { data: detail, isLoading, isError } = useLineDetail(lineId);

  if (isLoading) {
    return (
      <Card
        className="rise mt-6 rounded-lg border border-line bg-panel p-5 text-paper shadow-none sm:p-7"
        aria-label={t.loading}
      >
        <Skeleton className="h-7 w-1/2 bg-line" />
        <Skeleton className="mt-2 h-4 w-1/4 bg-line" />
        <div className="mt-4 grid gap-6 sm:grid-cols-2">
          {Array.from({ length: 2 }).map((_, col) => (
            <div key={col} className="space-y-3">
              {Array.from({ length: 4 }).map((_, row) => (
                <Skeleton key={row} className="h-10 w-full bg-line" />
              ))}
            </div>
          ))}
        </div>
      </Card>
    );
  }

  if (isError || !detail) {
    return (
      <Card className="rise mt-6 rounded-lg border border-line bg-panel p-5 text-paper shadow-none sm:p-7">
        <p className="text-sm text-fog">{t.error}</p>
      </Card>
    );
  }

  return (
    <Card
      className="rise mt-6 rounded-lg border border-line bg-panel p-5 text-paper shadow-none sm:p-7"
      role="region"
      aria-labelledby="line-heading"
    >
      <div className="border-b border-line pb-5">
        <p className="text-xs text-fog">{t.lineDetail}</p>
        <h2 id="line-heading" className="mt-1 font-display text-2xl font-semibold text-paper">
          {detail.line.name}
          {detail.line.replacement_bus && (
            <Badge
              variant="outline"
              className="ml-2 align-middle border-amber font-mono text-[10px] font-normal text-amber"
            >
              {t.replacementBus}
            </Badge>
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
                <span>
                  {dir.n_runs} {t.runs}
                </span>
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
                            {...v2Paths.train(run.id)}
                            className="font-display font-semibold text-paper hover:underline"
                          >
                            {run.name}
                          </Link>
                          <p className="mt-0.5 flex flex-wrap items-center gap-1.5 truncate text-[11px] text-fog">
                            <span className="truncate">→ {run.destination ?? "—"}</span>
                            {run.partial && (
                              <Badge
                                variant="outline"
                                className="border-amber text-[10px] font-normal text-amber"
                              >
                                {t.partOfRoute}
                              </Badge>
                            )}
                            {run.current && (
                              <Badge
                                variant="secondary"
                                className="border-teal text-[10px] font-normal text-teal"
                              >
                                {t.thisRun}
                              </Badge>
                            )}
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
    </Card>
  );
}
