import React, { useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Clock,
  Radio,
  UploadCloud,
  CheckCircle2,
  Loader2,
  XCircle,
  Circle,
} from "lucide-react";

/**
 * WebinarSummaryRow.jsx
 * One data-dense row per ingested meeting. Collapsed, it shows just enough to
 * scan a long list at a glance; expanded, it reveals the full executive
 * summary, key topics, and action-item table returned by the AI pipeline.
 *
 * Expected `webinar` shape (matches the summaries + webinars tables):
 * {
 *   id, title, duration_seconds, source_channel, processing_state, created_at,
 *   meetingTitle, executiveSummary, keyTopics: string[],
 *   actionItems: [{ assignee, taskDescription, deadline }],
 *   projectDeadlines: string[]
 * }
 */
const CHANNEL_META = {
  dashboard_upload: { label: "Dashboard upload", icon: UploadCloud, classes: "bg-blue-50 text-blue-700 border-blue-200" },
  webhook_stream: { label: "Webhook stream", icon: Radio, classes: "bg-purple-50 text-purple-700 border-purple-200" },
};

const STATE_META = {
  QUEUED: { label: "Queued", icon: Circle, classes: "bg-slate-50 text-slate-600 border-slate-200" },
  TRANSCRIBING: { label: "Transcribing", icon: Loader2, classes: "bg-amber-50 text-amber-700 border-amber-200", spin: true },
  SUMMARIZING: { label: "Summarizing", icon: Loader2, classes: "bg-amber-50 text-amber-700 border-amber-200", spin: true },
  COMPLETED: { label: "Completed", icon: CheckCircle2, classes: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  FAILED: { label: "Failed", icon: XCircle, classes: "bg-rose-50 text-rose-700 border-rose-200" },
};

function formatDuration(totalSeconds) {
  if (!totalSeconds && totalSeconds !== 0) return "—";
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}

function formatDate(isoString) {
  if (!isoString) return "—";
  const date = new Date(isoString);
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function Badge({ children, className = "" }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-medium ${className}`}>
      {children}
    </span>
  );
}

export default function WebinarSummaryRow({ webinar }) {
  const [expanded, setExpanded] = useState(false);

  const channelMeta = CHANNEL_META[webinar.source_channel] || CHANNEL_META.dashboard_upload;
  const stateMeta = STATE_META[webinar.processing_state] || STATE_META.QUEUED;
  const ChannelIcon = channelMeta.icon;
  const StateIcon = stateMeta.icon;

  const keyTopics = webinar.keyTopics || [];
  const actionItems = webinar.actionItems || [];
  const isReady = webinar.processing_state === "COMPLETED";

  return (
    <div className="rounded-lg border border-slate-200 bg-white shadow-sm">
      <button
        type="button"
        onClick={() => setExpanded((prev) => !prev)}
        className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-slate-50 focus:outline-none focus:ring-2 focus:ring-indigo-300 rounded-lg"
      >
        <span className="text-slate-400">
          {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        </span>

        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-slate-900">
            {webinar.meetingTitle || webinar.title || "Untitled meeting"}
          </p>
          <p className="text-xs text-slate-500">{formatDate(webinar.created_at)}</p>
        </div>

        <span className="hidden items-center gap-1 text-xs text-slate-500 sm:flex">
          <Clock className="h-3.5 w-3.5" />
          {formatDuration(webinar.duration_seconds)}
        </span>

        <Badge className={channelMeta.classes}>
          <ChannelIcon className="h-3 w-3" />
          {channelMeta.label}
        </Badge>

        <Badge className={stateMeta.classes}>
          <StateIcon className={`h-3 w-3 ${stateMeta.spin ? "animate-spin" : ""}`} />
          {stateMeta.label}
        </Badge>
      </button>

      {expanded && (
        <div className="border-t border-slate-100 px-4 py-4">
          {!isReady && (
            <p className="text-sm text-slate-500">
              Summary will appear here once processing reaches the COMPLETED state.
            </p>
          )}

          {isReady && (
            <div className="space-y-4">
              <div>
                <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400">Executive summary</h4>
                <p className="mt-1 text-sm leading-relaxed text-slate-700">{webinar.executiveSummary}</p>
              </div>

              {keyTopics.length > 0 && (
                <div>
                  <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400">Key topics</h4>
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    {keyTopics.map((topic) => (
                      <span
                        key={topic}
                        className="rounded-full bg-indigo-50 px-2.5 py-0.5 text-xs font-medium text-indigo-700"
                      >
                        {topic}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {actionItems.length > 0 && (
                <div>
                  <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400">Action items</h4>
                  <div className="mt-1.5 overflow-hidden rounded-md border border-slate-200">
                    <table className="w-full text-left text-sm">
                      <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                        <tr>
                          <th className="px-3 py-2 font-medium">Assignee</th>
                          <th className="px-3 py-2 font-medium">Task</th>
                          <th className="px-3 py-2 font-medium">Deadline</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-100">
                        {actionItems.map((item, index) => (
                          <tr key={`${item.assignee}-${index}`}>
                            <td className="px-3 py-2 font-medium text-slate-800">{item.assignee}</td>
                            <td className="px-3 py-2 text-slate-600">{item.taskDescription}</td>
                            <td className="px-3 py-2 text-slate-500">
                              {item.deadline === "unspecified" ? (
                                <span className="text-slate-400">No date set</span>
                              ) : (
                                item.deadline
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
