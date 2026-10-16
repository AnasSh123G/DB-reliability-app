import { useEffect, useRef, useState } from "react";
import { useSuggest } from "@/hooks/use-dbrel";
import { frequentFirst, frequentStations, type FrequentStation } from "@/lib/frequent-stations";
import type { Copy } from "@/lib/i18n";
import { FieldLabel } from "./FieldLabel";
import type { IconKind } from "./icons";
export interface PlannerStation {
  id: number;
  name: string;
}
/** How many of the most used stations an empty field offers. */
const FREQUENT_SHOWN = 6;
const FIELD_CLASS =
  "h-[46px] w-full rounded-md border border-line bg-ink px-3 text-sm font-medium text-paper outline-none focus:border-teal";
export function StationPicker({
  inputId,
  label,
  icon,
  value,
  onChange,
  t,
}: {
  inputId: string;
  label: string;
  icon: IconKind;
  value: PlannerStation | null;
  onChange: (value: PlannerStation | null) => void;
  t: Copy;
}) {
  const [query, setQuery] = useState(value?.name ?? "");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const root = useRef<HTMLDivElement>(null);
  const typedClear = useRef(false);
  const suggestions = useSuggest(query, undefined, "station");
  // Read when the field opens, so a search just made counts the next time.
  const [frequent, setFrequent] = useState<FrequentStation[]>([]);
  useEffect(() => {
    if (open) setFrequent(frequentStations());
  }, [open]);
  const typed = query.trim() !== "";
  const results: PlannerStation[] = typed
    ? frequentFirst(query, suggestions.data?.results ?? [], frequent)
    : frequent.slice(0, FREQUENT_SHOWN);
  useEffect(() => {
    if (value) {
      setQuery(value.name);
      typedClear.current = false;
    } else if (typedClear.current) typedClear.current = false;
    else setQuery("");
  }, [value]);
  useEffect(() => {
    const outside = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, []);
  const choose = (station: PlannerStation) => {
    onChange({ id: station.id, name: station.name });
    setQuery(station.name);
    setOpen(false);
    setActive(-1);
  };
  const shown = open && results.length > 0;
  return (
    <div ref={root} className="relative min-w-0 space-y-2">
      <FieldLabel htmlFor={inputId} kind={icon}>
        {label}
      </FieldLabel>
      <input
        id={inputId}
        className={FIELD_CLASS}
        value={query}
        placeholder={t.stationPlaceholder}
        autoComplete="off"
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={shown}
        aria-controls={`${inputId}-suggestions`}
        aria-activedescendant={shown && active >= 0 ? `${inputId}-option-${active}` : undefined}
        onFocus={() => setOpen(true)}
        onChange={(event) => {
          typedClear.current = value !== null;
          setQuery(event.target.value);
          onChange(null);
          setOpen(true);
          setActive(-1);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") setOpen(false);
          if (shown && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
            event.preventDefault();
            setActive(
              (previous) =>
                (previous + (event.key === "ArrowDown" ? 1 : -1) + results.length) % results.length,
            );
          }
          if (shown && event.key === "Enter" && active >= 0 && results[active]) {
            event.preventDefault();
            choose(results[active]);
          }
        }}
      />
      {shown && (
        <ul
          id={`${inputId}-suggestions`}
          role="listbox"
          aria-label={typed ? label : t.frequentStations}
          className="absolute z-30 max-h-64 w-full overflow-auto rounded-md border border-line bg-panel shadow-lg"
        >
          {!typed && (
            <li role="presentation" className="px-3 pt-2.5 pb-1 text-[11px] uppercase text-fog">
              {t.frequentStations}
            </li>
          )}
          {results.map((station, index) => (
            <li
              key={station.id}
              id={`${inputId}-option-${index}`}
              role="option"
              aria-selected={index === active}
            >
              <button
                type="button"
                tabIndex={-1}
                className={`w-full px-3 py-3 text-left text-sm text-paper hover:bg-ink ${index === active ? "bg-ink" : ""}`}
                onClick={() => choose(station)}
              >
                {station.name}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
