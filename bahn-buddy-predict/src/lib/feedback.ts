// The feedback chat posts one message to an n8n webhook, which classifies it with an AI model,
// opens a GitHub issue for bugs, wrong predictions and feature requests, and answers with a
// short reply. The workflow lives in ops/n8n/feedback-classifier.json.
//
// The URL is public by design (it only accepts POSTs from this site's origin). It can be
// overridden at build time with VITE_FEEDBACK_WEBHOOK_URL, for example to point at a test
// workflow.
export const FEEDBACK_WEBHOOK_URL: string =
  import.meta.env["VITE_FEEDBACK_WEBHOOK_URL"] ??
  "https://ai-software-egnineering.app.n8n.cloud/webhook/bahnpredict-feedback";

// The workflow cuts the message at the same length; stopping it here avoids a surprise.
export const FEEDBACK_MAX_LENGTH = 1000;
// The endpoint is public and every call costs an AI request, so the widget paces itself.
export const FEEDBACK_COOLDOWN_MS = 8000;
export const FEEDBACK_MAX_PER_SESSION = 10;
// A classified message normally comes back in a few seconds; a ticket adds a GitHub call.
const FEEDBACK_TIMEOUT_MS = 45000;

export interface FeedbackReply {
  reply: string;
  category: string;
}

const SESSION_KEY = "bahnpredict-feedback-session";

export function feedbackSessionId(): string {
  try {
    const stored = window.sessionStorage.getItem(SESSION_KEY);
    if (stored) return stored;
    const created = crypto.randomUUID();
    window.sessionStorage.setItem(SESSION_KEY, created);
    return created;
  } catch {
    // Storage can be blocked (private mode); an unstored id is still fine for one page view.
    return crypto.randomUUID();
  }
}

export async function sendFeedback(message: string, page: string): Promise<FeedbackReply> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), FEEDBACK_TIMEOUT_MS);
  try {
    const response = await fetch(FEEDBACK_WEBHOOK_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: message.slice(0, FEEDBACK_MAX_LENGTH),
        page,
        sessionId: feedbackSessionId(),
      }),
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`feedback webhook answered ${response.status}`);
    const data: unknown = await response.json();
    if (
      typeof data !== "object" ||
      data === null ||
      typeof (data as FeedbackReply).reply !== "string"
    ) {
      throw new Error("feedback webhook answered without a reply");
    }
    const { reply, category } = data as FeedbackReply;
    return { reply, category: typeof category === "string" ? category : "other" };
  } finally {
    window.clearTimeout(timer);
  }
}
