/**
 * markdownExporter.js
 * Converts one or more meeting summary records into a Markdown document and
 * triggers an instant client-side download — no server round trip needed.
 *
 * Expected summary shape (matches the `summaries` table / MeetingSummary
 * schema in ai_summarizer.py):
 * {
 *   meetingTitle, executiveSummary, keyTopics: string[],
 *   actionItems: [{ assignee, taskDescription, deadline }],
 *   projectDeadlines: string[], created_at, source_channel, duration_seconds
 * }
 */

function escapeMarkdown(text) {
  if (!text) return "";
  return String(text).replace(/([*_`|])/g, "\\$1");
}

function formatDurationLabel(totalSeconds) {
  if (totalSeconds === null || totalSeconds === undefined) return "unknown duration";
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}

function formatDateLabel(isoString) {
  if (!isoString) return "unknown date";
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return "unknown date";
  return date.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
}

function renderActionItemsTable(actionItems) {
  if (!actionItems || actionItems.length === 0) {
    return "_No action items were captured for this meeting._\n";
  }

  const header = "| Assignee | Task | Deadline |\n| --- | --- | --- |\n";
  const rows = actionItems
    .map((item) => {
      const assignee = escapeMarkdown(item.assignee || "Unassigned");
      const task = escapeMarkdown(item.taskDescription || "");
      const deadline = item.deadline && item.deadline !== "unspecified" ? item.deadline : "No date set";
      return `| ${assignee} | ${task} | ${deadline} |`;
    })
    .join("\n");

  return header + rows + "\n";
}

/**
 * Builds the full Markdown string for a single summary record.
 */
export function summaryToMarkdown(summary) {
  const lines = [];

  lines.push(`# ${summary.meetingTitle || "Untitled meeting"}`);
  lines.push("");
  lines.push(
    `*${formatDateLabel(summary.created_at)} · ${formatDurationLabel(summary.duration_seconds)} · ${
      summary.source_channel === "webhook_stream" ? "Webhook stream" : "Dashboard upload"
    }*`
  );
  lines.push("");

  lines.push("## Executive summary");
  lines.push("");
  lines.push(summary.executiveSummary || "_No summary available._");
  lines.push("");

  if (summary.keyTopics && summary.keyTopics.length > 0) {
    lines.push("## Key topics");
    lines.push("");
    lines.push(summary.keyTopics.map((topic) => `- ${escapeMarkdown(topic)}`).join("\n"));
    lines.push("");
  }

  lines.push("## Action items");
  lines.push("");
  lines.push(renderActionItemsTable(summary.actionItems));

  if (summary.projectDeadlines && summary.projectDeadlines.length > 0) {
    lines.push("## Project deadlines");
    lines.push("");
    lines.push(summary.projectDeadlines.map((deadline) => `- ${escapeMarkdown(deadline)}`).join("\n"));
    lines.push("");
  }

  return lines.join("\n");
}

/**
 * Builds a combined Markdown document for multiple summaries, separated by
 * horizontal rules, with a small table of contents at the top.
 */
export function summariesToMarkdown(summaries) {
  if (!summaries || summaries.length === 0) {
    return "# Meeting summaries\n\n_No summaries selected for export._\n";
  }

  const toc = summaries
    .map((summary, index) => `${index + 1}. ${escapeMarkdown(summary.meetingTitle || "Untitled meeting")}`)
    .join("\n");

  const sections = summaries.map((summary) => summaryToMarkdown(summary)).join("\n\n---\n\n");

  return `# Meeting summaries export\n\n${toc}\n\n---\n\n${sections}`;
}

function slugifyFilename(text) {
  return (text || "meeting-summaries")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

/**
 * Triggers a browser download of the given Markdown content.
 * Pure DOM API — no server round trip, no external dependency.
 */
export function downloadMarkdownFile(markdownContent, filename) {
  const blob = new Blob([markdownContent], { type: "text/markdown;charset=utf-8" });
  const objectUrl = URL.createObjectURL(blob);

  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename.endsWith(".md") ? filename : `${filename}.md`;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);

  URL.revokeObjectURL(objectUrl);
}

/**
 * Convenience entry point: given an array of summary records, builds the
 * combined Markdown and immediately triggers the download.
 */
export function exportSummariesToMarkdown(summaries, filenameHint) {
  const markdownContent = summariesToMarkdown(summaries);
  const defaultName =
    summaries && summaries.length === 1
      ? slugifyFilename(summaries[0].meetingTitle)
      : `meeting-summaries-${new Date().toISOString().slice(0, 10)}`;

  downloadMarkdownFile(markdownContent, filenameHint || defaultName);
}
