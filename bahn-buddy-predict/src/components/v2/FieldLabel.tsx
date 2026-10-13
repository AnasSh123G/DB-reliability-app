import type { ReactNode } from "react";
import { icons, type IconKind } from "./icons";

/**
 * A field's or value's label with its icon in front.
 *
 * Args:
 *   kind: which kind of field or value this labels (see icons.ts).
 *   htmlFor: the id of the input it labels; leave out for a label of a displayed value.
 *   className: extra classes for the text size and spacing around it.
 *   children: the label text.
 */
export function FieldLabel({
  kind,
  htmlFor,
  className,
  children,
}: {
  kind: IconKind;
  htmlFor?: string;
  className?: string;
  children: ReactNode;
}) {
  const Icon = icons[kind];
  const classes = `flex items-center gap-1.5 text-[13.67px] uppercase text-fog ${className ?? ""}`;
  const content = (
    <>
      <Icon className="size-3.5 shrink-0" aria-hidden="true" />
      <span>{children}</span>
    </>
  );
  return htmlFor ? (
    <label htmlFor={htmlFor} className={classes}>
      {content}
    </label>
  ) : (
    <span className={classes}>{content}</span>
  );
}
