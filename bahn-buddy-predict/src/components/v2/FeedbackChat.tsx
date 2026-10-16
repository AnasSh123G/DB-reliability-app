// The floating feedback chat of the v2 shell: an icon in the corner opens a small panel, the
// visitor types a message, and it goes to the n8n workflow in ops/n8n/feedback-classifier.json
// (see src/lib/feedback.ts). Nothing is stored in the browser except a random session id.
import { MessageCircle, Send, X } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  FEEDBACK_COOLDOWN_MS,
  FEEDBACK_MAX_LENGTH,
  FEEDBACK_MAX_PER_SESSION,
  sendFeedback,
} from "@/lib/feedback";
import { copy, type Locale } from "@/lib/i18n";

interface Entry {
  from: "you" | "team" | "notice";
  text: string;
}

export function FeedbackChat({ locale }: { locale: Locale }) {
  const t = copy[locale];
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [entries, setEntries] = useState<Entry[]>([]);
  const [sending, setSending] = useState(false);
  const sentCount = useRef(0);
  const lastSentAt = useRef(0);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [entries, sending]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const notice = (message: string) =>
    setEntries((current) => [...current, { from: "notice", text: message }]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const message = text.trim();
    if (!message || sending) return;
    if (sentCount.current >= FEEDBACK_MAX_PER_SESSION) {
      notice(t.feedbackLimit);
      return;
    }
    if (Date.now() - lastSentAt.current < FEEDBACK_COOLDOWN_MS) {
      notice(t.feedbackWait);
      return;
    }
    setEntries((current) => [...current, { from: "you", text: message }]);
    setText("");
    setSending(true);
    lastSentAt.current = Date.now();
    sentCount.current += 1;
    try {
      const answer = await sendFeedback(message, window.location.pathname);
      setEntries((current) => [...current, { from: "team", text: answer.reply }]);
    } catch (error) {
      console.error(error);
      notice(t.feedbackError);
    } finally {
      setSending(false);
      inputRef.current?.focus();
    }
  };

  return (
    <div className="fixed bottom-4 right-4 z-40 flex flex-col items-end gap-3 sm:bottom-6 sm:right-6">
      {open && (
        <section
          role="dialog"
          aria-label={t.feedbackTitle}
          className="flex max-h-[min(32rem,calc(100vh-7rem))] w-[min(22rem,calc(100vw-2rem))] flex-col overflow-hidden rounded-lg border border-line bg-panel shadow-xl"
        >
          <header className="flex items-center justify-between border-b border-line px-4 py-3">
            <h2 className="font-display text-base font-semibold tracking-tight">
              {t.feedbackTitle}
            </h2>
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setOpen(false)}
              aria-label={t.feedbackClose}
              className="size-8 text-fog hover:bg-ink hover:text-paper"
            >
              <X className="size-4" />
            </Button>
          </header>

          <div
            ref={listRef}
            className="flex-1 space-y-3 overflow-y-auto px-4 py-3 text-sm"
            aria-live="polite"
          >
            <p className="text-fog">{t.feedbackIntro}</p>
            {entries.map((entry, index) =>
              entry.from === "notice" ? (
                <p key={index} className="text-amber">
                  {entry.text}
                </p>
              ) : (
                <div
                  key={index}
                  className={entry.from === "you" ? "flex flex-col items-end" : "flex flex-col"}
                >
                  <span className="mb-0.5 text-xs text-fog">
                    {entry.from === "you" ? t.feedbackYou : t.feedbackTeam}
                  </span>
                  <p
                    className={
                      entry.from === "you"
                        ? "max-w-[85%] whitespace-pre-wrap break-words rounded-lg bg-teal px-3 py-2 text-action-foreground"
                        : "max-w-[85%] whitespace-pre-wrap break-words rounded-lg border border-line bg-ink px-3 py-2 text-paper"
                    }
                  >
                    {entry.text}
                  </p>
                </div>
              ),
            )}
            {sending && <p className="text-fog">{t.feedbackSending}</p>}
          </div>

          <form onSubmit={submit} className="flex items-end gap-2 border-t border-line p-3">
            <Textarea
              ref={inputRef}
              value={text}
              onChange={(event) => setText(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
              maxLength={FEEDBACK_MAX_LENGTH}
              rows={2}
              placeholder={t.feedbackPlaceholder}
              aria-label={t.feedbackPlaceholder}
              className="min-h-0 resize-none border-line bg-ink text-paper"
            />
            <Button
              type="submit"
              disabled={sending || !text.trim()}
              aria-label={t.feedbackSend}
              className="shrink-0 bg-teal text-action-foreground hover:bg-teal/90"
            >
              <Send className="size-4" />
              <span className="hidden sm:inline">{t.feedbackSend}</span>
            </Button>
          </form>
        </section>
      )}

      <Button
        onClick={() => setOpen((current) => !current)}
        aria-label={t.feedbackOpen}
        aria-expanded={open}
        title={t.feedbackOpen}
        className="size-12 rounded-full bg-teal text-action-foreground shadow-lg hover:bg-teal/90"
      >
        <MessageCircle className="size-5" />
      </Button>
    </div>
  );
}
