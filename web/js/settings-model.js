// Settings describe presentation only; values/defaults come from /api/config.
const T = (k, l, h = "") => ({ k, l, h, t: "text" });
const N = (k, l, h = "") => ({ k, l, h, t: "number" });
const C = (k, l, o, h = "") => ({ k, l, o, h, t: "choice" });
const B = (k, l, h = "") => C(k, l, ["On", "Off"], h);
const L = (k, l, h = "") => ({ k, l, h, t: "list" });
const group = (title, fields) => ({ title, fields });
const tab = (id, title, groups) => ({ id, title, groups });

const TABS = [
  tab("connections", "Connections", [
    group("Radarr & Sonarr", [T("connect.radarr_url", "Radarr URL"),
      T("connect.radarr_api_key", "Radarr API key"), T("connect.sonarr_url", "Sonarr URL"),
      T("connect.sonarr_api_key", "Sonarr API key")]),
    group("Plex & voicebox", [T("connect.plex_url", "Plex URL"), T("connect.plex_token", "Plex token"),
      B("plex.auto_refresh", "Refresh Plex after dubbing"), T("voicebox.base_url", "voicebox URL"),
      N("voicebox.timeout_seconds", "voicebox timeout (seconds)")]),
  ]),
  tab("general", "General", [group("Dubbing", [
    L("general.target_languages", "Target languages", "Comma-separated language codes, such as en, es."),
    B("discovery.only_original_foreign", "Original language must differ"),
    B("dub.dry_run", "Dry run", "Plan the job without generating audio."),
    N("dub.teaser_minutes", "Teaser length (minutes)"),
  ])]),
  tab("discovery", "Discovery", [group("Scanning", [
    B("discovery.auto_scan", "Auto-scan on the interval"), T("discovery.rescan_interval", "Rescan interval"),
    N("discovery.cache_ttl", "Scan cache (seconds)"), N("discovery.webhook_debounce", "Webhook debounce (seconds)"),
    C("discovery.treat_undefined_as", "Unknown audio language", ["original", "target", "ignore"]),
  ])]),
  tab("plexlabels", "Plex labels", [group("Labels & collections", [
    T("filtering.tag_missing_dub", "Missing-dub label"), T("filtering.hidden_collection_name", "Hidden collection"),
    B("filtering.kometa_handoff", "Hand off to Kometa"), T("filtering.kometa_file", "Kometa file"),
    B("filtering.auto_label", "Auto-apply labels on each scan"),
  ])]),
  tab("speech", "Transcript", [group("Transcription", [
    C("transcribe.source", "Transcript from", ["subtitles", "whisper"]),
    T("transcribe.whisper_model", "Whisper model"), B("transcribe.diarize", "Speaker diarization"),
  ])]),
  tab("translate", "Translation", [group("Provider", [
    C("translate.provider", "Translation provider", ["claude", "voicebox", "passthrough"]),
    T("translate.model", "Translation model"),
  ])]),
  tab("voices", "Voices", [group("Voice casting", [
    C("dub.voice_mode", "Voice approach", ["clone", "preset"]),
    L("dub.preset_voices", "Preset voice IDs", "Comma-separated voicebox profile IDs."),
  ])]),
  tab("audio", "Timing & mix", [group("Audio", [
    B("dub.duration_match", "Fit lines to the original timing"), T("separate.model", "Separation model"),
    T("dub.ducking_ratio", "Ducking ratio"),
  ])]),
  tab("output", "Output", [group("Files", [
    T("dub.track_name_template", "New track name", "Use {language_name} for the language label."),
    T("paths.output_dir", "Output directory"),
  ])]),
  tab("queue", "Queue & storage", [group("Storage", [T("paths.work_dir", "Work directory")])]),
  tab("advanced", "Advanced", [group("Web UI & logging", [
    T("web.host", "Host"), N("web.port", "Port"), T("web.api_key", "API key"),
    T("general.log_file", "Log file"),
  ])]),
];

// Flat lookup of every settings field by its config key.
const FIELD_BY_KEY = {};
TABS.forEach(t => t.groups.forEach(g => g.fields.forEach(f => { FIELD_BY_KEY[f.k] = f; })));

function isBoolField(f) { return f.t === "choice" && Array.isArray(f.o) && f.o[0] === "On"; }

const PLAN_FIELDS = [
  { k: "target_lang", l: "Dub into", t: "choice", dyn: true,
    h: "Applied when a dub is queued for this title." },
  ...["dub.voice_mode", "transcribe.whisper_model", "transcribe.diarize",
    "dub.duration_match", "dub.ducking_ratio", "dub.track_name_template", "dub.dry_run"]
    .map(k => ({ ...FIELD_BY_KEY[k], t: isBoolField(FIELD_BY_KEY[k]) ? "bool" : FIELD_BY_KEY[k].t })),
];


export { TABS, FIELD_BY_KEY, PLAN_FIELDS, isBoolField };
