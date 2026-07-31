import React, { useEffect, useRef, useState, useCallback } from "react";
import { createClient } from "@supabase/supabase-js";
import { CheckCircle2, Loader2, XCircle, X } from "lucide-react";

/**
 * RealtimeBanner.jsx (Vercel-adapted)
 *
 * The original design subscribed to a raw `wss://.../ws/alerts/{companyId}`
 * FastAPI WebSocket. Vercel serverless functions can't hold that connection
 * open, so this now subscribes directly to the Supabase Realtime broadcast
 * channel that `alert_system.py` publishes onto from api/process_job.py —
 * no bespoke socket server required.
 *
 * Props:
 *   supabaseUrl, supabaseAnonKey - public, safe to ship to the browser
 *   companyId                    - current tenant's organization id
 */
const STATE_META = {
  QUEUED: { label: "Queued", tone: "slate" },
  TRANSCRIBING: { label: "Transcribing audio", tone: "amber" },
  SUMMARIZING: { label: "Generating summary", tone: "amber" },
  COMPLETED: { label: "Summary ready", tone: "emerald" },
  FAILED: { label: "Processing failed", tone: "rose" },
};

const TONE_CLASSES = {
  slate: "bg-slate-50 border-slate-200 text-slate-700",
  amber: "bg-amber-50 border-amber-200 text-amber-800",
  emerald: "bg-emerald-50 border-emerald-200 text-emerald-800",
  rose: "bg-rose-50 border-rose-200 text-rose-800",
};

function ToastIcon({ state }) {
  if (state === "COMPLETED") return <CheckCircle2 className="h-4 w-4 text-emerald-600" />;
  if (state === "FAILED") return <XCircle className="h-4 w-4 text-rose-600" />;
  return <Loader2 className="h-4 w-4 animate-spin text-amber-600" />;
}

export default function RealtimeBanner({
  supabaseUrl = "https://your-project.supabase.co",
  supabaseAnonKey = "public-anon-key",
  companyId = "demo-company-id",
}) {
  const [toasts, setToasts] = useState([]);
  const clientRef = useRef(null);

  const dismissToast = useCallback((id) => {
    setToasts((prev) => prev.filter((toast) => toast.id !== id));
  }, []);

  useEffect(() => {
    const client = createClient(supabaseUrl, supabaseAnonKey);
    clientRef.current = client;

    const channel = client.channel(`intel_core:webinars:${companyId}`, {
      config: { broadcast: { self: false } },
    });

    channel.on("broadcast", { event: "processing_update" }, (payload) => {
      const { webinar_id: webinarId, state, title } = payload.payload || {};
      const meta = STATE_META[state] || { label: state, tone: "slate" };
      const toastId = `${webinarId}-${state}-${Date.now()}`;

      setToasts((prev) => [
        ...prev.slice(-3),
        { id: toastId, webinarId, state, title: title || "Untitled meeting", label: meta.label, tone: meta.tone },
      ]);

      const lifespanMs = state === "COMPLETED" || state === "FAILED" ? 7000 : 4500;
      setTimeout(() => dismissToast(toastId), lifespanMs);
    });

    channel.subscribe();

    return () => {
      client.removeChannel(channel);
    };
  }, [supabaseUrl, supabaseAnonKey, companyId, dismissToast]);

  return (
    <div className="pointer-events-none fixed inset-x-0 top-3 z-50 flex flex-col items-center gap-2 px-4">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`pointer-events-auto flex w-full max-w-sm items-center gap-2.5 rounded-lg border px-3.5 py-2.5 shadow-md transition-all duration-300 ease-out ${TONE_CLASSES[toast.tone]}`}
        >
          <ToastIcon state={toast.state} />
          <div className="flex-1 min-w-0">
            <p className="truncate text-sm font-medium leading-tight">{toast.title}</p>
            <p className="text-xs opacity-80 leading-tight">{toast.label}</p>
          </div>
          <button
            type="button"
            onClick={() => dismissToast(toast.id)}
            className="rounded p-0.5 opacity-60 hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-offset-1 focus:ring-current"
            aria-label="Dismiss notification"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      ))}
    </div>
  );
}
