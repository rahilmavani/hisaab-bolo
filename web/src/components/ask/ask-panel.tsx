"use client";

import clsx from "clsx";
import {
  Loader2,
  MessageCircle,
  Mic,
  Play,
  Send,
  Square,
  X,
} from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
} from "react";
import { api, API_BASE, ApiError } from "@/lib/api";
import { supabaseBrowser } from "@/lib/supabase/client";
import type { AskResponse } from "@/lib/types";
import { AnswerComponents } from "./answer-components";
import { errorText } from "./errors";
import { useRecorder } from "./use-recorder";

type Turn = {
  id: string;
  question: string;
  voice?: { durationMs: number; levels: number[] };
  status: "thinking" | "done" | "error";
  answer?: AskResponse;
  error?: string;
  audio?: "loading" | "ready" | "playing" | "unavailable";
};

const EXAMPLES = [
  "Ram ne pichhle 4 mahine mein kitna diya?",
  "Aaj kitna collection hua?",
  "Kiske payment pending hain?",
];

function Waveform({
  levels,
  live = false,
}: {
  levels: number[];
  live?: boolean;
}) {
  const bars = levels.length
    ? levels.slice(-28)
    : Array.from({ length: 16 }, () => 0.15);
  return (
    <span aria-hidden className="flex h-6 items-center gap-[3px]">
      {bars.map((l, i) => (
        <span
          key={i}
          className={clsx(
            "w-[3px] rounded-full",
            live ? "bg-white" : "bg-white/80",
          )}
          style={{ height: `${Math.max(3, Math.round(l * 22))}px` }}
        />
      ))}
    </span>
  );
}

function clock(ms: number) {
  const s = Math.round(ms / 1000);
  return `0:${String(s).padStart(2, "0")}`;
}

// From xl the panel is docked beside the page; below that it opens over the page as a dialog.
const DOCKED = "(min-width: 1280px)";

/** The agent answers in the language it was asked in (Sarvam hears and speaks all of these). */
const LANGUAGES: Record<string, string> = {
  "hi-IN": "Hinglish",
  "en-IN": "English",
  "gu-IN": "Gujarati",
  "mr-IN": "Marathi",
  "bn-IN": "Bengali",
  "pa-IN": "Punjabi",
  "od-IN": "Odia",
  "ta-IN": "Tamil",
  "te-IN": "Telugu",
  "kn-IN": "Kannada",
  "ml-IN": "Malayalam",
};
const subscribeDocked = (cb: () => void) => {
  const mq = window.matchMedia(DOCKED);
  mq.addEventListener("change", cb);
  return () => mq.removeEventListener("change", cb);
};
const useDocked = () =>
  useSyncExternalStore(
    subscribeDocked,
    () => window.matchMedia(DOCKED).matches,
    () => true,
  );

const FOCUSABLE =
  'button:not([disabled]), input:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';

async function authHeader(): Promise<Record<string, string>> {
  const { data } = await supabaseBrowser().auth.getSession();
  return data.session
    ? { Authorization: `Bearer ${data.session.access_token}` }
    : {};
}

export function AskPanel({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [text, setText] = useState("");
  const [toggleMode, setToggleMode] = useState(false);
  const recorder = useRecorder();
  const pressStart = useRef(0);
  const audioEl = useRef<HTMLAudioElement | null>(null);
  const audioUrls = useRef(new Map<string, string>());
  const scroller = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const panel = useRef<HTMLElement>(null);
  const stopRecorder = recorder.stop;
  const closeRef = useRef(onClose);
  const docked = useDocked();
  // A voice answer can arrive after the merchant closed the dialog: then it waits, unspoken, for a tap.
  const visible = useRef(true);
  useEffect(() => {
    visible.current = docked || open;
  }, [docked, open]);

  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    scroller.current?.scrollTo({
      top: scroller.current.scrollHeight,
      behavior: "smooth",
    });
  }, [turns]);

  // As a dialog: focus moves in and stays in (Tab wraps), Escape closes, and focus goes back to whatever
  // opened it. Closing also drops a recording in progress and stops speech, so nothing carries on unseen.
  useEffect(() => {
    if (docked || !open) return;
    const opener = document.activeElement as HTMLElement | null;
    input.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        closeRef.current();
        return;
      }
      if (e.key !== "Tab" || !panel.current) return;
      const items = [
        ...panel.current.querySelectorAll<HTMLElement>(FOCUSABLE),
      ].filter((el) => el.offsetParent !== null);
      if (!items.length) return;
      const [first, last] = [items[0], items[items.length - 1]];
      const inside = panel.current.contains(document.activeElement);
      if (
        e.shiftKey
          ? !inside || document.activeElement === first
          : !inside || document.activeElement === last
      ) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      void stopRecorder();
      setToggleMode(false);
      audioEl.current?.pause();
      opener?.focus();
    };
  }, [open, docked, stopRecorder]);

  const update = (id: string, patch: Partial<Turn>) =>
    setTurns((ts) => ts.map((t) => (t.id === id ? { ...t, ...patch } : t)));

  const history = () =>
    turns
      .filter((t) => t.status === "done" && t.answer)
      .slice(-3)
      .map((t) => ({
        question: t.question.slice(0, 500),
        answer: t.answer!.display.slice(0, 2000),
      }));

  async function speak(turnId: string, answer: AskResponse) {
    if (!answer.speech) return;
    update(turnId, { audio: "loading" });
    try {
      let url = audioUrls.current.get(answer.query_id);
      if (!url) {
        const res = await fetch(`${API_BASE}/voice/tts`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(await authHeader()),
          },
          body: JSON.stringify({ query_id: answer.query_id }),
        });
        if (!res.ok) throw new Error(String(res.status));
        url = URL.createObjectURL(await res.blob());
        audioUrls.current.set(answer.query_id, url);
      }
      if (!visible.current) {
        update(turnId, { audio: "ready" });
        return;
      }
      audioEl.current?.pause();
      const el = new Audio(url);
      audioEl.current = el;
      // Ending, closing the dialog or starting another reply all pause it: the button is free again.
      el.onended = el.onpause = () => update(turnId, { audio: "ready" });
      update(turnId, { audio: "playing" });
      await el.play();
    } catch (e) {
      // Interrupted before it started (the dialog closed): still replayable, not "unavailable".
      const interrupted =
        (e instanceof DOMException && e.name === "AbortError") ||
        !visible.current;
      update(turnId, { audio: interrupted ? "ready" : "unavailable" });
    }
  }

  function finish(id: string, answer: AskResponse, speakNow: boolean) {
    update(id, {
      status: "done",
      answer,
      ...(answer.transcript ? { question: answer.transcript } : {}),
      ...(answer.speech && !(speakNow && visible.current)
        ? { audio: "ready" as const }
        : {}),
    });
    if (speakNow && visible.current) speak(id, answer);
  }

  async function askText(question: string, payerHint?: string) {
    const q = question.trim();
    if (!q) return;
    const id = crypto.randomUUID();
    setTurns((ts) => [...ts, { id, question: q, status: "thinking" }]);
    setText("");
    try {
      const answer = await api<AskResponse>("/ask", {
        method: "POST",
        body: JSON.stringify({
          text: q,
          payer_hint: payerHint ?? null,
          history: history(),
        }),
      });
      finish(id, answer, false);
    } catch (e) {
      update(id, {
        status: "error",
        error: errorText(e, "Something went wrong"),
      });
    }
  }

  async function sendRecording() {
    const rec = await recorder.stop();
    setToggleMode(false);
    if (!rec) return;
    if (rec.durationMs < 600) {
      recorder.setError("Hold the mic while you speak, then let go.");
      return;
    }
    const id = crypto.randomUUID();
    setTurns((ts) => [
      ...ts,
      {
        id,
        question: "",
        voice: { durationMs: rec.durationMs, levels: rec.levels },
        status: "thinking",
      },
    ]);
    const form = new FormData();
    form.append(
      "audio",
      rec.blob,
      `question.${rec.mimeType.includes("mp4") ? "mp4" : "webm"}`,
    );
    form.append("history", JSON.stringify(history()));
    try {
      const res = await fetch(`${API_BASE}/voice/ask`, {
        method: "POST",
        headers: await authHeader(),
        body: form,
      });
      let body: { detail?: unknown } | null = null;
      try {
        body = await res.json();
      } catch {
        body = null; // a proxy or crash page, not our JSON
      }
      if (!res.ok || !body) {
        const detail =
          typeof body?.detail === "string"
            ? body.detail
            : "Couldn't get an answer. Try again, or type your question.";
        throw new ApiError(res.status, detail);
      }
      finish(id, body as AskResponse, true);
    } catch (e) {
      update(id, {
        status: "error",
        error: errorText(e, "Voice request failed"),
      });
    }
  }

  async function onMicDown(e: React.PointerEvent) {
    if (recorder.state === "recording") return;
    pressStart.current = e.timeStamp;
    if (await recorder.start()) recorder.armTimeout(() => sendRecording());
  }

  function onMicUp(e: React.PointerEvent) {
    if (recorder.state !== "recording") return;
    // A quick tap switches to tap-to-stop; a hold sends on release.
    if (!toggleMode && e.timeStamp - pressStart.current < 350) {
      setToggleMode(true);
      return;
    }
    sendRecording();
  }

  // Keyboard: hold Space like the mouse (a quick tap switches to tap-to-stop); Enter starts, Enter again sends.
  // Held keys repeat keydown, so only the first press counts.
  async function onMicKeyDown(e: React.KeyboardEvent) {
    if (e.key !== " " && e.key !== "Enter") return;
    e.preventDefault();
    if (e.repeat) return;
    if (recorder.state === "recording") {
      sendRecording();
      return;
    }
    pressStart.current = e.timeStamp;
    if (await recorder.start()) {
      setToggleMode(e.key === "Enter");
      recorder.armTimeout(() => sendRecording());
    }
  }

  function onMicKeyUp(e: React.KeyboardEvent) {
    if (e.key !== " " || recorder.state !== "recording" || toggleMode) return;
    e.preventDefault();
    if (e.timeStamp - pressStart.current < 350) {
      setToggleMode(true);
      return;
    }
    sendRecording();
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    askText(text);
  }

  const recording = recorder.state === "recording";

  return (
    <aside
      ref={panel}
      aria-label="Ask Hisaab"
      role={docked ? undefined : "dialog"}
      aria-modal={!docked && open ? true : undefined}
      className={clsx(
        "flex-col border-line bg-page xl:sticky xl:top-0 xl:flex xl:h-dvh xl:border-l xl:bg-side",
        open ? "fixed inset-0 z-40 flex" : "hidden",
      )}
    >
      <header className="flex items-center justify-between gap-3 px-5 pt-5 pb-3">
        <h2 className="flex items-center gap-2 text-lg font-medium">
          <MessageCircle size={16} className="text-muted" aria-hidden /> Ask
          Hisaab
        </h2>
        <span className="text-sm text-muted">Hindi · English · Gujarati · more</span>
        <button
          onClick={onClose}
          aria-label="Close Ask Hisaab"
          className="rounded-full p-1 text-muted hover:text-text xl:hidden"
        >
          <X size={18} />
        </button>
      </header>

      <div
        ref={scroller}
        className="flex-1 space-y-5 overflow-y-auto px-5 pb-4"
        aria-live="polite"
      >
        {turns.length === 0 ? (
          <div className="space-y-3 pt-4">
            <p className="text-muted">
              Ask about any customer, day or invoice. Hold the mic and speak, or
              type below.
            </p>
            <div className="flex flex-col items-start gap-2">
              {EXAMPLES.map((q) => (
                <button
                  key={q}
                  onClick={() => askText(q)}
                  className="rounded-lg border border-line-strong bg-card px-3 py-1.5 text-left text-sm hover:border-blue"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {turns.map((t) => (
          <div key={t.id} className="space-y-2.5">
            <div className="ml-auto w-fit max-w-[90%] rounded-[20px] rounded-br-md bg-blue px-4 py-3 text-white">
              <p className="text-[1rem] leading-snug">
                {t.question ? (
                  `“${t.question}”`
                ) : (
                  <span className="opacity-80">Listening back…</span>
                )}
              </p>
              {t.voice ? (
                <p className="mt-1.5 flex items-center gap-2 font-mono text-xs opacity-90">
                  <Waveform levels={t.voice.levels} />{" "}
                  {clock(t.voice.durationMs)}
                </p>
              ) : null}
            </div>

            {t.status === "thinking" ? (
              <p
                className="flex items-center gap-2 text-sm text-muted"
                role="status"
              >
                <Loader2 size={15} className="animate-spin" aria-hidden />{" "}
                Checking your hisaab…
              </p>
            ) : t.status === "error" ? (
              <p
                role="alert"
                className="rounded-2xl border border-rose/30 px-4 py-3 text-sm text-rose"
              >
                {t.error}
              </p>
            ) : t.answer ? (
              <div className="space-y-2.5">
                <div className="max-w-[95%] rounded-[20px] rounded-bl-md border border-line bg-card px-4 py-3">
                  <p className="text-[1rem] leading-relaxed">
                    {t.answer.display}
                  </p>
                  {t.answer.speech ? (
                    <button
                      onClick={() => speak(t.id, t.answer!)}
                      disabled={t.audio === "loading" || t.audio === "playing"}
                      className="mt-2 inline-flex items-center gap-1.5 text-sm text-blue disabled:opacity-70"
                    >
                      {t.audio === "loading" ? (
                        <Loader2 size={13} className="animate-spin" />
                      ) : (
                        <Play size={13} />
                      )}
                      {t.audio === "playing"
                        ? "Speaking…"
                        : t.audio === "unavailable"
                          ? "Voice unavailable, tap to retry"
                          : `Spoken back in ${LANGUAGES[t.answer.lang] ?? "English"}`}
                    </button>
                  ) : null}
                </div>
                <AnswerComponents
                  components={t.answer.components}
                  onPickPayer={(id, label) =>
                    askText(
                      `${t.question.slice(0, 490 - label.length)} (${label})`,
                      id,
                    )
                  }
                />
                {t.answer.follow_ups.length ? (
                  <div className="flex flex-wrap gap-2">
                    {t.answer.follow_ups.map((f) => (
                      <button
                        key={f}
                        onClick={() => askText(f)}
                        className="rounded-lg border border-line-strong bg-card px-3 py-1.5 text-sm hover:border-blue"
                      >
                        {f}
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        ))}
      </div>

      <div className="border-t border-line px-4 pt-3 pb-[max(1rem,env(safe-area-inset-bottom))]">
        {recorder.error ? (
          <p role="alert" className="mb-2 text-sm text-amber">
            {recorder.error}
          </p>
        ) : null}
        {recording ? (
          <div
            className="mb-3 flex items-center justify-between rounded-2xl bg-blue px-4 py-3 text-white"
            role="status"
          >
            <span className="flex items-center gap-3">
              <Waveform levels={recorder.levels} live />{" "}
              <span className="font-mono text-sm">
                {clock(recorder.elapsed)}
              </span>
            </span>
            <span className="text-sm">
              {toggleMode ? "Tap the mic to send" : "Release to send"}
            </span>
          </div>
        ) : null}
        <form onSubmit={submit} className="flex items-center gap-2">
          <button
            type="button"
            aria-label={recording ? "Stop and send recording" : "Hold to speak"}
            aria-pressed={recording}
            onPointerDown={(e) => {
              e.preventDefault();
              if (recording && toggleMode) sendRecording();
              else onMicDown(e);
            }}
            onPointerUp={onMicUp}
            onPointerLeave={() => recording && !toggleMode && sendRecording()}
            onKeyDown={onMicKeyDown}
            onKeyUp={onMicKeyUp}
            className={clsx(
              "grid h-12 w-12 shrink-0 touch-none place-items-center rounded-full text-white transition-colors select-none",
              recording ? "bg-rose" : "bg-blue hover:bg-blue-strong",
            )}
          >
            {recording ? <Square size={18} fill="white" /> : <Mic size={20} />}
          </button>
          <label className="sr-only" htmlFor="ask-text">
            Type a question
          </label>
          <input
            ref={input}
            id="ask-text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Or type: Ram ne kitna diya?"
            maxLength={500}
            className="field h-12 min-w-0 flex-1"
          />
          <button
            type="submit"
            aria-label="Send question"
            disabled={!text.trim()}
            className="grid h-12 w-12 shrink-0 place-items-center rounded-[10px] border border-line-strong bg-card text-text disabled:opacity-40"
          >
            <Send size={18} />
          </button>
        </form>
      </div>
    </aside>
  );
}
