import React, { useMemo } from "react";
import { AlertTriangle, Gauge } from "lucide-react";

/**
 * UsageAnalyticsCard.jsx
 * Shows an organization's monthly audio-processing consumption against its
 * contracted quota (`companies.processing_usage_this_month` /
 * `monthly_audio_minutes_limit` from db_migration.sql), with an escalating
 * warning once usage crosses 80%.
 */
const WARNING_THRESHOLD_PERCENT = 80;

function getToneClasses(percentUsed) {
  if (percentUsed >= 100) {
    return { bar: "bg-rose-500", text: "text-rose-700", track: "bg-rose-50" };
  }
  if (percentUsed >= WARNING_THRESHOLD_PERCENT) {
    return { bar: "bg-amber-500", text: "text-amber-700", track: "bg-amber-50" };
  }
  return { bar: "bg-emerald-500", text: "text-emerald-700", track: "bg-slate-100" };
}

export default function UsageAnalyticsCard({
  usedMinutes = 0,
  limitMinutes = 300,
  planTier = "starter",
}) {
  const percentUsed = useMemo(() => {
    if (!limitMinutes || limitMinutes <= 0) return 0;
    return Math.min(150, Math.round((usedMinutes / limitMinutes) * 100));
  }, [usedMinutes, limitMinutes]);

  const tone = getToneClasses(percentUsed);
  const isOverThreshold = percentUsed >= WARNING_THRESHOLD_PERCENT;
  const remainingMinutes = Math.max(0, limitMinutes - usedMinutes);

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Gauge className="h-4 w-4 text-slate-400" />
          <h3 className="text-sm font-semibold text-slate-900">Monthly processing usage</h3>
        </div>
        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium capitalize text-slate-600">
          {planTier} plan
        </span>
      </div>

      <div className="mt-4">
        <div className="flex items-baseline justify-between">
          <p className="text-2xl font-semibold text-slate-900">
            {usedMinutes.toLocaleString()}
            <span className="ml-1 text-sm font-normal text-slate-400">/ {limitMinutes.toLocaleString()} min</span>
          </p>
          <p className={`text-sm font-medium ${tone.text}`}>{percentUsed}%</p>
        </div>

        <div className={`mt-2 h-2.5 w-full overflow-hidden rounded-full ${tone.track}`}>
          <div
            className={`h-2.5 rounded-full transition-all duration-500 ${tone.bar}`}
            style={{ width: `${Math.min(100, percentUsed)}%` }}
          />
        </div>

        <p className="mt-2 text-xs text-slate-500">
          {remainingMinutes.toLocaleString()} minutes remaining this billing cycle
        </p>
      </div>

      {isOverThreshold && (
        <div
          className={`mt-3 flex items-start gap-2 rounded-md border px-3 py-2 text-sm ${
            percentUsed >= 100
              ? "border-rose-200 bg-rose-50 text-rose-700"
              : "border-amber-200 bg-amber-50 text-amber-700"
          }`}
        >
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            {percentUsed >= 100
              ? "Quota exceeded. New uploads will be rejected until your cycle resets or you upgrade your plan."
              : "You've used over 80% of your monthly quota. Consider upgrading to avoid interruptions."}
          </span>
        </div>
      )}
    </div>
  );
}
