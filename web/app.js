"use strict";
const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const num = (value) => (value == null ? "—" : Number(value).toLocaleString());
const stamp = (value) =>
  value ? value.replace("T", " ").replace("Z", " UTC") : "Not collected";
const weekStart = new Date(Date.now() - 7 * 86400000)
  .toISOString()
  .slice(0, 10);
let state = {
  view: "overview",
  data: null,
  selected: null,
  request: 0,
  searchRequest: 0,
  filterMode: "observation",
  filterValues: {
    observation: { since: weekStart, until: "" },
    publication: { since: "", until: "" },
  },
};
function switchFilters(mode) {
  state.filterValues[state.filterMode] = {
    since: $("since").value,
    until: $("until").value,
  };
  if (state.filterMode === mode) return;
  state.filterMode = mode;
  $("since").value = state.filterValues[mode].since;
  $("until").value = state.filterValues[mode].until;
}
const params = () => {
  const p = new URLSearchParams();
  if ($("channel").value) p.set("channel", $("channel").value);
  if ($("since").value) p.set("since", $("since").value);
  if ($("until").value) p.set("until", $("until").value + "T23:59:59Z");
  return p;
};
async function api(path, data) {
  const response = await fetch(
    path,
    data
      ? {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Ytclaw-Token": state.data?.token || "",
          },
          body: JSON.stringify(data),
        }
      : {},
  );
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Request failed");
  return result;
}
function notice(message = "") {
  $("notice").textContent = message;
}
function empty(message) {
  return `<p class="empty">${esc(message)}</p>`;
}
function videoButton(row) {
  return `<button class="text-button" data-video="${esc(row.video_id)}">${esc(row.title || row.video_id)}</button>`;
}
function image(asset, label) {
  return `<div><div class="image-slot">${asset?.sha256 ? `<img src="/assets/${encodeURIComponent(asset.sha256)}" alt="${esc(label)}" loading="lazy">` : "Image not archived"}</div><p class="image-caption">${esc(label)}</p></div>`;
}
function difference(field, values) {
  const format = (x) =>
    x == null
      ? "(not recorded)"
      : typeof x === "object"
        ? JSON.stringify(x, null, 2)
        : String(x);
  field =
    {
      channel_id: "Channel",
      published_at: "Published",
      category_id: "Category",
      default_language: "Language",
      default_audio_language: "Audio language",
    }[field] || field;
  return `<div class="diff"><div><small>${esc(field)} · BEFORE</small>${esc(format(values.before))}</div><div><small>${esc(field)} · AFTER</small>${esc(format(values.after))}</div></div>`;
}
function changeCard(row, detailed = false) {
  const fields = Object.entries(row.changes).filter(
    ([k]) => !["thumbnail_asset", "thumbnails"].includes(k),
  );
  return `<article class="change">${videoButton(row)}<div class="meta">${esc(stamp(row.observed_at))} · ${esc(row.source)} · ${row.initial ? "First observation" : "Version " + row.version_id}</div>
  ${row.before_thumbnail || row.after_thumbnail ? `<div class="compare-images">${image(row.before_thumbnail, "PREVIOUS IMAGE")}${image(row.after_thumbnail, "OBSERVED IMAGE")}</div>` : ""}
  ${fields
    .slice(0, detailed ? fields.length : 2)
    .map(([k, v]) => difference(k, v))
    .join(
      "",
    )}${!detailed && fields.length > 2 ? `<p class="note">${fields.length - 2} more fields in the video record</p>` : ""}</article>`;
}
function growthRows(rows) {
  return rows.length
    ? rows
        .map(
          (r) =>
            `<div class="growth-row"><div>${videoButton(r)}<p class="meta">${esc(r.from_observed_at.slice(0, 10))} → ${esc(r.to_observed_at.slice(0, 10))}${r.partial_window ? " · Partial window" : ""}</p></div><strong>${r.views_gained >= 0 ? "+" : ""}${num(r.views_gained)}</strong></div>`,
        )
        .join("")
    : empty("Two dated observations are needed to measure view growth.");
}
function questionRows(rows) {
  return rows.length
    ? rows
        .map(
          (r) =>
            `<article class="question"><p>“${esc(r.text)}”</p>${videoButton(r)}<p class="meta">${esc(r.author)} · ${num(r.likes)} likes · Scanned ${esc(stamp(r.observed_at))}</p><a class="note" href="${esc(r.url)}" target="_blank" rel="noreferrer">Open comment ↗</a></article>`,
        )
        .join("")
    : empty(
        "No matching questions without replies in completed comment scans.",
      );
}
function metrics() {
  const cs = state.data.coverage,
    total = (k) => cs.reduce((sum, c) => sum + (c[k] || 0), 0);
  return `<div class="metrics"><div class="metric"><strong>${num(total("videos"))}</strong><span>Videos in your memory</span><small>${cs.length} channel${cs.length === 1 ? "" : "s"}</small></div><div class="metric"><strong>${num(state.data.changes.filter((r) => !r.initial).length)}</strong><span>Changes in this window</span><small>Up to 100 recent versions</small></div><div class="metric"><strong>${num(total("with_transcripts"))}</strong><span>Searchable transcripts</span><small>of ${num(total("videos"))} local videos</small></div><div class="metric"><strong>${num(total("comments_complete"))}</strong><span>Complete comment scans</span><small>of ${num(total("videos"))} local videos</small></div></div>`;
}
function render() {
  if (!state.data) return;
  const d = state.data,
    content = $("content");
  document.querySelectorAll(".nav").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === state.view);
    button.setAttribute(
      "aria-current",
      button.dataset.view === state.view ? "page" : "false",
    );
  });
  const headings = {
    overview: [
      "THE WEEKLY READ",
      "A record of what changed.",
      "Your channel’s recent changes, audience questions, and observed growth.",
    ],
    library: [
      "THE LOCAL CATALOG",
      "Keep the good details close.",
      "Open a video to explore its transcript, versions, and owner reports.",
    ],
    changes: [
      "THE CHANGE LOG",
      "Every observed version.",
      "Titles and thumbnails, side by side. Exact edit times may fall between syncs.",
    ],
    questions: [
      "THE AUDIENCE",
      "A useful place to listen.",
      "Questions with no replies found in completed scans. This is a simple question-mark filter.",
    ],
    collection: [
      "THE COLLECTION",
      "Know what you have.",
      "Coverage, recent runs, and scheduled jobs. A partial scan is visible here.",
    ],
    search: [
      "THE SEARCH",
      "Find it in your history.",
      "Search results include source links. Dates filter video publication time.",
    ],
  };
  const h = headings[state.view] || headings.overview;
  $("eyebrow").textContent = h[0];
  $("title").textContent = h[1];
  $("subtitle").textContent = h[2];
  if (!d.channels.length) {
    content.innerHTML = `<section class="panel"><h2>Start your channel memory.</h2><p>Configure a channel in the terminal, then collect your first records.</p><pre>ytclaw init --channel @yourchannel\nytclaw sync --comments --transcripts --thumbnails</pre><p class="note">To explore without a key: <code>ytclaw --db demo.sqlite demo</code>, then serve that database.</p></section>`;
    return;
  }
  if (state.view === "overview")
    content.innerHTML =
      metrics() +
      `<div class="grid"><section class="panel"><div class="section-head"><div><h2>What changed</h2><p>Latest title and image observations</p></div><span class="badge">LOCAL HISTORY</span></div>${
        d.changes
          .filter((r) => !r.initial)
          .slice(0, 3)
          .map((r) => changeCard(r))
          .join("") || empty("No changes observed in this window.")
      }</section><div><section class="panel"><div class="section-head"><div><h2>Gaining ground</h2><p>View differences between actual observations</p></div></div>${growthRows(d.growth.slice(0, 5))}<p class="note">A change followed by growth does not establish cause.</p></section><section class="panel"><h2>From the audience</h2>${questionRows(d.questions.slice(0, 3))}</section></div></div>`;
  else if (state.view === "changes")
    content.innerHTML = `<section class="panel">${d.changes.map((r) => changeCard(r, true)).join("") || empty("No versions observed in this window.")}</section>`;
  else if (state.view === "questions")
    content.innerHTML = `<section class="panel">${questionRows(d.questions)}</section>`;
  else if (state.view === "library") {
    const videos = d.videos.filter(
      (v) =>
        (!$("since").value ||
          v.published_at?.slice(0, 10) >= $("since").value) &&
        (!$("until").value || v.published_at?.slice(0, 10) <= $("until").value),
    );
    content.innerHTML =
      `<p class="note">Publication dates filter this catalog. Clear “Since” to see older videos. Up to 200 videos are shown.</p><div class="library">${videos.map((v) => `<article class="video-card">${image(v.thumbnail_asset, "LAST ARCHIVED IMAGE")}<div class="card-body">${videoButton(v)}<p class="meta">${num(v.views)} views · ${(v.published_at || "").slice(0, 10)}</p></div></article>`).join("")}</div>` +
      (videos.length
        ? ""
        : empty(
            "No videos published in this window. Clear the date filter to see your catalog.",
          ));
  } else if (state.view === "collection")
    content.innerHTML =
      metrics() +
      d.coverage
        .map(
          (c) =>
            `<section class="panel"><h2>${esc(c.title)}</h2><p class="meta">${esc(c.handle)} · Last successful run ${esc(stamp(c.last_success_at))}</p><div class="health"><span>${c.metadata_observed}/${c.videos} API metadata</span><span>${c.thumbnails_checked}/${c.videos} thumbnail checks</span><span>${c.pending_videos} pending videos</span><span>${c.unavailable_videos} unavailable videos</span></div><p class="note">Oldest metadata: ${esc(stamp(c.oldest_metadata_at))}. Reported public videos: ${num(c.reported_public_videos)}.</p></section>`,
        )
        .join("") +
      `<section class="panel"><h2>Recent runs</h2><div class="table-wrap"><table><thead><tr><th>CHANNEL</th><th>STARTED</th><th>STATUS</th><th>DETAIL</th></tr></thead><tbody>${d.runs.map((r) => `<tr><td>${esc(r.handle)}</td><td>${esc(stamp(r.started_at))}</td><td><span class="badge ${r.status === "success" ? "" : "warn"}">${esc(r.status)}</span></td><td>${esc(r.error || "—")}</td></tr>`).join("")}</tbody></table></div></section><section class="panel"><h2>Scheduled collection</h2>${d.watch_jobs.length ? d.watch_jobs.map((j) => `<p>${esc(j.handle)} · every ${j.interval_seconds / 3600} hours · ${esc(j.last_status || "not run yet")}</p>`).join("") : "<p>No jobs configured.</p><pre>ytclaw watch add @handle --every-hours 24 --comments --thumbnails\nytclaw watch install</pre>"}<p class="note">Jobs run while this computer and the OS scheduler are available. Installing a job requires the CLI.</p></section>`;
}
async function load() {
  const request = ++state.request;
  try {
    const d = await api("/api/overview?" + params());
    if (request !== state.request) return;
    state.data = d;
    const current = $("channel").value;
    $("channel").innerHTML =
      '<option value="">All channels</option>' +
      d.channels
        .map(
          (c) =>
            `<option value="${esc(c.channel_id)}">${esc(c.title)}</option>`,
        )
        .join("");
    $("channel").value = current;
    $("demo").classList.toggle("hidden", !d.demo);
    $("sync").disabled = d.demo || !current || d.task?.status === "running";
    $("download").href = "/api/report.md?" + params();
    if (d.task?.status === "running")
      notice(
        "Sync is running. Completed checkpoints are saved as it progresses.",
      );
    render();
    if (state.view === "search") await runSearch();
  } catch (error) {
    notice(error.message);
    $("content").innerHTML = empty(
      "Could not load records. Refresh to try again.",
    );
  }
}
async function openVideo(vid) {
  state.selected = vid;
  $("video-content").innerHTML = empty("Loading video…");
  if (!$("video-dialog").open) $("video-dialog").showModal();
  try {
    const v = await api("/api/video?id=" + encodeURIComponent(vid));
    if (state.selected !== vid) return;
    const history = v.metadata_history,
      latest = history[history.length - 1];
    const versions = history
      .map((r, i) => ({
        ...r,
        title: r.metadata.title,
        initial: i === 0,
        before_thumbnail: i ? history[i - 1].metadata.thumbnail_asset : null,
        after_thumbnail: r.metadata.thumbnail_asset,
      }))
      .reverse();
    const stats = v.stats_history.filter((r) => r.views != null);
    let chart = "";
    if (stats.length > 1) {
      const values = stats.map((r) => r.views),
        lo = Math.min(...values),
        hi = Math.max(...values);
      const points = values
        .map(
          (value, i) =>
            `${10 + (i / (values.length - 1)) * 680},${75 - ((value - lo) / (hi - lo || 1)) * 60}`,
        )
        .join(" ");
      chart = `<svg class="sparkline" viewBox="0 0 700 95" role="img" aria-label="Observed views from ${esc(values[0])} to ${esc(values.at(-1))}"><polyline points="${points}"></polyline></svg><p class="meta">${esc(stats[0].observed_at)} → ${esc(stats.at(-1).observed_at)} · ${num(values[0])} → ${num(values.at(-1))} views · Observations spaced evenly</p>`;
    }
    $("video-content").innerHTML =
      `<h2>${esc(v.title)}</h2><p class="meta">${esc(v.video_id)} · ${num(v.views)} views · ${v.transcript_segments} transcript segments</p><div class="toolbar-actions"><a class="quiet" href="${esc(v.url)}" target="_blank" rel="noreferrer">Open on YouTube ↗</a><button class="dark" id="pin-baseline">${v.baseline ? "Replace baseline with latest" : "Pin current version"}</button></div>${chart}<details><summary>Description</summary><pre>${esc(v.description)}</pre></details><details><summary>Transcript · ${v.transcript.length} segments</summary>${v.transcript.length ? v.transcript.map((s) => `<p class="context"><a href="${esc(v.url)}&amp;t=${Math.floor(s.start || 0)}s" target="_blank" rel="noreferrer">${Math.floor(s.start || 0)}s</a> ${esc(s.text)}</p>`).join("") : '<p class="note">No transcript has been collected.</p>'}</details><section class="panel"><h3>Baseline & project files</h3>${
        v.baseline
          ? `<p>Baseline version ${v.baseline.baseline_version}: <span class="badge ${v.baseline.drifted ? "warn" : ""}">${v.baseline.drifted ? "Drift detected" : "Matches latest"}</span></p>${Object.entries(
              v.baseline.changes,
            )
              .map(([k, val]) => difference(k, val))
              .join("")}`
          : '<p class="note">Pin a version before editing to compare future observations against it.</p>'
      }${v.project ? `<details open><summary>Linked project comparison</summary><pre>${esc(JSON.stringify(v.project, null, 2))}</pre></details>` : '<p class="note">Link local title, description, tags, or thumbnail files with <code>ytclaw project link VIDEO_ID manifest.json</code>.</p>'}</section><section class="panel"><h3>Experiment notes</h3><p class="note">Record your reasoning or a result from YouTube Studio. These notes are not an automated A/B test.</p><form id="experiment-form"><label class="sr-only" for="experiment-note">Experiment note</label><textarea id="experiment-note" required placeholder="What did you change, and why?"></textarea><button class="quiet" type="submit">Save note</button></form>${v.experiments.map((e) => `<p>${esc(e.note)}${e.result ? "<br>" + esc(e.result) : ""}<br><span class="meta">${esc(stamp(e.recorded_at))}</span></p>`).join("")}</section><section class="panel"><h3>Owner analytics</h3>${
        v.owner_reports.length
          ? v.owner_reports
              .map(
                (r) =>
                  `<details><summary>${esc(r.kind)} · ${esc(r.start_date)} to ${esc(r.end_date)} · ${esc(r.source)} · ${r.rows.length} rows</summary><p class="meta">Fetched ${esc(stamp(r.fetched_at))}</p><div class="table-wrap"><table><thead><tr>${r.columns.map((k) => "<th>" + esc(k) + "</th>").join("")}</tr></thead><tbody>${r.rows
                    .slice(0, 100)
                    .map(
                      (row) =>
                        "<tr>" +
                        row.map((val) => "<td>" + esc(val) + "</td>").join("") +
                        "</tr>",
                    )
                    .join("")}</tbody></table></div></details>`,
              )
              .join("")
          : '<p class="note">No owner reports saved. Connect with <code>ytclaw auth login</code> or import a Studio CSV. Public views alone do not explain performance.</p>'
      }</section><div class="timeline"><h2>Version timeline</h2><p class="note">${history.length} observed versions. Image references show the last successful archive.</p>${versions.map((r) => changeCard(r, true)).join("")}</div>`;
    $("pin-baseline").onclick = async () => {
      try {
        await api("/api/baseline", { video_id: vid });
        await openVideo(vid);
      } catch (e) {
        notice(e.message);
      }
    };
    $("experiment-form").onsubmit = async (event) => {
      event.preventDefault();
      try {
        await api("/api/experiment", {
          video_id: vid,
          note: $("experiment-note").value,
        });
        await openVideo(vid);
      } catch (e) {
        notice(e.message);
      }
    };
  } catch (error) {
    $("video-content").innerHTML = empty(error.message);
  }
}
$("since").value = weekStart;
document.querySelectorAll(".nav").forEach(
  (button) =>
    (button.onclick = () => {
      state.view = button.dataset.view;
      state.searchRequest++;
      switchFilters(state.view === "library" ? "publication" : "observation");
      notice();
      load();
    }),
);
$("channel").onchange = () => {
  notice();
  load();
};
$("since").onchange = () => load();
$("until").onchange = () => load();
$("refresh").onclick = () => {
  notice();
  load();
};
$("close-dialog").onclick = () => {
  $("video-dialog").close();
  state.selected = null;
};
document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-video]");
  if (target) openVideo(target.dataset.video);
});
async function runSearch() {
  if (!$("search").value.trim()) return;
  switchFilters("publication");
  const searchRequest = ++state.searchRequest;
  const p = params();
  p.set("q", $("search").value.trim());
  p.set("scope", $("scope").value);
  state.view = "search";
  render();
  $("content").innerHTML = empty("Searching your local records…");
  try {
    const rows = await api("/api/search?" + p);
    if (searchRequest !== state.searchRequest || state.view !== "search")
      return;
    $("content").innerHTML =
      `<section class="panel">${rows.map((r) => `<article class="search-result"><span class="badge">${esc(r.kind.toUpperCase())}</span><p>${videoButton(r)}</p><p>${esc(r.snip)}</p>${r.context ? '<div class="context">' + r.context.map((s) => esc(s.text)).join(" ") + "</div>" : ""}<p class="meta">${esc(r.author || "")} · ${esc(stamp(r.observed_at))}</p><a class="note" href="${esc(r.url)}" target="_blank" rel="noreferrer">Open source${r.kind === "transcript" ? " at " + Math.floor(r.start || 0) + "s" : ""} ↗</a></article>`).join("") || empty("No matches in the selected channel and publication dates.")}</section>`;
  } catch (error) {
    notice(error.message);
  }
}
$("search-form").onsubmit = (event) => {
  event.preventDefault();
  runSearch();
};
$("sync").onclick = async () => {
  try {
    await api("/api/sync", { channel: $("channel").value });
    notice("Sync started. Refresh local data to inspect progress.");
    $("sync").disabled = true;
    const timer = setInterval(async () => {
      try {
        const d = await api("/api/overview?" + params());
        if (d.task?.status !== "running") {
          clearInterval(timer);
          await load();
          notice(
            "Sync finished: " +
              (d.task?.status || "unknown") +
              (d.task?.stopped ? " · " + d.task.stopped : ""),
          );
        }
      } catch (e) {
        clearInterval(timer);
        notice(e.message);
      }
    }, 2500);
  } catch (error) {
    notice(error.message);
  }
};
load();
