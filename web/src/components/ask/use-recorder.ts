"use client";

import { useCallback, useRef, useState } from "react";

export type Recording = { blob: Blob; durationMs: number; levels: number[]; mimeType: string };

const MIME_CANDIDATES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
const MAX_MS = 30_000; // Sarvam's real-time limit

/** MediaRecorder push-to-talk with a live level meter for the waveform. */
export function useRecorder() {
  const [state, setState] = useState<"idle" | "recording" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const [levels, setLevels] = useState<number[]>([]);
  const [elapsed, setElapsed] = useState(0);
  const rec = useRef<{
    recorder: MediaRecorder;
    stream: MediaStream;
    chunks: Blob[];
    started: number;
    levels: number[];
    raf: number;
    ctx: AudioContext;
    timeout: ReturnType<typeof setTimeout>;
  } | null>(null);

  const start = useCallback(async () => {
    setError(null);
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setState("error");
      setError("This browser can't record audio. Type your question instead.");
      return false;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch {
      setState("error");
      setError("Microphone access is blocked. Allow it in the browser, or type your question.");
      return false;
    }
    const mimeType = MIME_CANDIDATES.find((m) => MediaRecorder.isTypeSupported(m)) ?? "";
    const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    const chunks: Blob[] = [];
    recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    const ctx = new AudioContext();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 256;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const buf = new Uint8Array(analyser.frequencyBinCount);
    const started = performance.now();
    const state = { recorder, stream, chunks, started, levels: [] as number[], raf: 0, ctx, timeout: 0 as unknown as ReturnType<typeof setTimeout> };
    let lastSample = 0;
    const tick = (t: number) => {
      analyser.getByteTimeDomainData(buf);
      let peak = 0;
      for (const v of buf) peak = Math.max(peak, Math.abs(v - 128));
      if (t - lastSample > 90) {
        lastSample = t;
        state.levels.push(Math.min(1, peak / 64));
        setLevels(state.levels.slice(-40));
        setElapsed(t - started);
      }
      state.raf = requestAnimationFrame(tick);
    };
    state.raf = requestAnimationFrame(tick);
    recorder.start(250);
    rec.current = state;
    setLevels([]);
    setElapsed(0);
    setState("recording");
    return true;
  }, []);

  const stop = useCallback(async (): Promise<Recording | null> => {
    const r = rec.current;
    if (!r) return null;
    rec.current = null;
    clearTimeout(r.timeout);
    cancelAnimationFrame(r.raf);
    const done = new Promise<void>((resolve) => (r.recorder.onstop = () => resolve()));
    r.recorder.stop();
    await done;
    r.stream.getTracks().forEach((t) => t.stop());
    r.ctx.close();
    setState("idle");
    const durationMs = performance.now() - r.started;
    const blob = new Blob(r.chunks, { type: r.recorder.mimeType || "audio/webm" });
    return { blob, durationMs, levels: r.levels, mimeType: blob.type };
  }, []);

  const armTimeout = useCallback((onLimit: () => void) => {
    if (rec.current) rec.current.timeout = setTimeout(onLimit, MAX_MS);
  }, []);

  return { state, error, levels, elapsed, start, stop, armTimeout, setError };
}
