import React, { useState, useCallback } from "react";
import { Search, Loader2, AlertCircle } from "lucide-react";

/**
 * SemanticSearchCard.jsx
 *
 * Sends a natural-language query to the backend's vector search endpoint,
 * which is expected to wrap the `intel_core.match_summaries` Postgres RPC
 * (see db_migration.sql) behind a tenant-scoped route, e.g.:
 *
 *   POST /api/v1/webinars/search/semantic
 *   { "query": "what did we decide about the Q3 roadmap" }
 *   -> [{ summary_id, webinar_id, meeting_title, executive_summary, similarity }]
 *
 * The bearer token is the same Supabase-issued JWT used elsewhere; this
 * component never talks to Gemini/OpenAI directly (Pillar 8 — the browser
 * holds no third-party AI keys, only the app's own session token).
 */
export default function SemanticSearchCard({
  authToken = "",
  searchEndpoint = "/api/v1/webinars/search/semantic",
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");
  const [hasSearched, setHasSearched] = useState(false);

  const runSearch = useCallback(
    async (event) => {
      event.preventDefault();
      const trimmed = query.trim();
      if (!trimmed) return;

      setIsLoading(true);
      setError("");
      setHasSearched(true);

      try {
        const response = await fetch(searchEndpoint, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${authToken}`,
          },
          body: JSON.stringify({ query: trimmed }),
        });

        if (!response.ok) {
          throw new Error(
            response.status === 429
              ? "Search rate limit reached. Try again in a moment."
              : "Search failed. Please try again."
          );
        }

        const data = await response.json();
        setResults(Array.isArray(data) ? data : data.results || []);
      } catch (err) {
        setError(err.message || "Something went wrong while searching.");
        setResults([]);
      } finally {
        setIsLoading(false);
      }
    },
    [query, authToken, searchEndpoint]
  );

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <h3 className="text-sm font-semibold text-slate-900">Semantic search</h3>
      <p className="mt-0.5 text-xs text-slate-500">
        Search across every meeting in your organization by meaning, not just keywords.
      </p>

      <form onSubmit={runSearch} className="mt-3 flex gap-2">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
          <input
            type="text"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="e.g. what did we decide about the Q3 roadmap?"
            className="w-full rounded-md border border-slate-300 py-2 pl-9 pr-3 text-sm text-slate-800 placeholder:text-slate-400 focus:border-indigo-400 focus:outline-none focus:ring-2 focus:ring-indigo-100"
          />
        </div>
        <button
          type="submit"
          disabled={isLoading || !query.trim()}
          className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-indigo-300"
        >
          {isLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
          Search
        </button>
      </form>

      {error && (
        <div className="mt-3 flex items-center gap-2 rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          <AlertCircle className="h-4 w-4 shrink-0" />
          {error}
        </div>
      )}

      {!error && hasSearched && !isLoading && results.length === 0 && (
        <p className="mt-4 text-sm text-slate-500">No meetings matched that query closely enough.</p>
      )}

      {results.length > 0 && (
        <ul className="mt-4 space-y-2.5">
          {results.map((result) => {
            const similarityPercent = Math.round((result.similarity || 0) * 100);
            return (
              <li key={result.summary_id} className="rounded-md border border-slate-200 p-3">
                <div className="flex items-start justify-between gap-3">
                  <p className="text-sm font-medium text-slate-900">{result.meeting_title}</p>
                  <span className="shrink-0 rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-semibold text-emerald-700">
                    {similarityPercent}% match
                  </span>
                </div>
                <p className="mt-1 line-clamp-2 text-xs text-slate-600">{result.executive_summary}</p>
                <div className="mt-2 h-1.5 w-full rounded-full bg-slate-100">
                  <div
                    className="h-1.5 rounded-full bg-emerald-500"
                    style={{ width: `${Math.max(4, similarityPercent)}%` }}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
