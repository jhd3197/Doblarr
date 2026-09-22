// Settings describe presentation only; values/defaults come from /api/config.
const T = (k, l, h = "") => ({ k, l, h, t: "text" });
const N = (k, l, h = "") => ({ k, l, h, t: "number" });
const C = (k, l, o, h = "") => ({ k, l, o, h, t: "choice" });
const B = (k, l, h = "") => C(k, l, ["On", "Off"], h);
const L = (k, l, h = "") => ({ k, l, h, t: "list" });
const J = (k, l, h = "") => ({ k, l, h, t: "json" });
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
    C("dub.preset", "Generation preset", ["custom", "preview", "final"], "Preview uses preset voice IDs and faster separation. Final enables timing correction."),
    { ...C("dub.target_locale", "Target locale", [""], "Regional variety of the dub target (identity, wording and track title); Auto follows the target language. Voice accent is set separately."), emptyLabel: "Auto (from language)" },
    N("dub.audition_lines", "Audition lines", "Short audio samples selected across speakers and scenes."),
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
    B("transcribe.clean_cues", "Remove nonspoken cues"),
    B("transcribe.align_subtitles", "Align source-language subtitles to speech"),
    N("transcribe.batch_size", "Transcription batch size"),
    C("transcribe.device", "Transcription device", ["auto", "cpu", "cuda"]),
    C("transcribe.compute_type", "Transcription precision", ["auto", "float16", "int8"]),
    B("transcribe.keep_models_loaded", "Keep local models loaded", "Uses more memory. Enable only when the GPU can hold the active models."),
  ])]),
  tab("translate", "Translation", [group("Provider", [
    C("translate.provider", "Translation provider", ["claude", "prompture", "voicebox", "passthrough"]),
    T("translate.model", "Translation model"),
    T("translate.endpoint", "Translation endpoint"),
    C("translate.locale", "Spanish region", ["auto", "es-419", "es-MX", "es-ES"], "es-419: neutral Latin America. es-MX: Mexico. es-ES: Spain. Wording only; voice accent is set separately."),
    C("translate.adaptation", "Dialogue style", ["natural", "faithful", "localized"]),
    B("translate.adapt_region", "Adapt wording to this region", "Also adapt subtitles already in the target language. Original dialogue stays available for comparison."),
    B("translate.reuse_memory", "Reuse reviewed translations", "Opt-in pilot: exact lines with matching scene, register, settings and timing only."),
    T("translate.direction", "Dialogue direction", "For example: restrained anime dialogue; keep cultural terms; avoid added catchphrases."),
    J("translate.character_notes", "Character dialogue notes", 'By speaker ID, for example {"GINKO": "Calm, concise; never overly formal"}.'),
    N("translate.batch_size", "Lines per translation batch"),
    N("translate.chars_per_second", "Starting character budget per second"),
    J("translate.glossary", "Names and terminology", 'JSON object, for example {"Ginko": "Ginko"}.'),
  ])]),
  tab("voices", "Voices", [group("Voice casting", [
    C("dub.voice_mode", "Voice approach", ["clone", "preset"]),
    L("dub.preset_voices", "Preset voice IDs", "Comma-separated voicebox profile IDs."),
    C("voicebox.default_engine", "Speech engine", ["qwen", "qwen_custom_voice", "chatterbox", "chatterbox_turbo", "kokoro", "luxtts", "tada"]),
    T("voicebox.model_size", "Speech model size", "Leave blank to use the engine default."),
    T("voicebox.preview_engine", "Preview speech engine"),
    N("voicebox.concurrency", "Simultaneous speech requests", "Start with 1; compare run reports before increasing this."),
    N("voicebox.seed", "Generation seed", "A fixed seed makes takes reproducible where supported."),
    J("dub.pronunciations", "Pronunciation dictionary", 'JSON object, for example {"Ginko": "Gheen-ko"}. Captions keep the original spelling.'),
    T("dub.cast_group", "Recurring cast group", "Use the same group name across episodes."),
    J("dub.character_map", "Speaker to character mapping", 'Map each episode explicitly, for example {"SPEAKER_00": "Ginko"}. Speaker numbers can change between episodes.'),
  ])]),
  tab("audio", "Timing & mix", [group("Audio", [
    B("dub.duration_match", "Fit lines to the original timing"), T("separate.model", "Separation model"),
    T("dub.ducking_ratio", "Ducking ratio"),
    N("dub.max_fit_attempts", "Timing repair attempts"),
    N("dub.background_volume", "Background level", "1 preserves music and effects outside dialogue."),
    N("dub.fallback_volume", "Original-audio fallback level"),
    N("dub.duck_threshold", "Ducking threshold"),
    N("dub.duck_attack_ms", "Ducking attack (ms)"),
    N("dub.duck_release_ms", "Ducking release (ms)"),
    B("quality.enabled", "Check generated clips"), B("quality.normalize", "Normalize dialogue"),
    N("quality.dialogue_lufs", "Dialogue loudness (LUFS)"),
    C("quality.asr", "Verify generated words", ["off", "suspicious", "all"], "Speech recognition adds processing time; mismatches are review suggestions."),
    N("quality.asr_sample", "Also verify this fraction of clean lines", "Between 0 and 1. The same lines are picked on every run, so coverage is comparable."),
    N("quality.max_retries", "Quality retry attempts"),
    N("quality.request_budget", "Extra speech requests per job", "Shared cap across recognition, quality retries, timing repairs and alternative takes. 0 means no cap."),
  ]), group("Performance levels", [
    C("levels.mode", "Dialogue level approach", ["legacy", "off", "consistent", "follow_source", "manual"], "Legacy keeps the loudness pass before timing correction. The others move it after timing, so a quiet performance stays quiet. Only one of the two runs."),
    N("levels.target_db", "Dialogue level target (dBFS)", "Speech-active RMS, not LUFS: short interjections need a measure that works on short audio."),
    N("levels.strength", "Follow the original by", "Between 0 and 1. How much of the original quiet/loud contrast to reproduce."),
    N("levels.max_boost_db", "Most to raise a loud line (dB)"),
    N("levels.max_cut_db", "Most to lower a quiet line (dB)"),
    N("levels.min_seconds", "Least source speech to measure (seconds)", "Shorter source evidence is recorded as insufficient rather than used."),
    N("levels.min_separation_db", "Least speech-to-background separation (dB)", "Below this the original cannot be measured apart from its bed."),
    N("levels.peak_ceiling", "Peak ceiling", "0 to 1. The level pass never crosses this; the contrast is kept and the line sits lower."),
    B("levels.measure_source", "Measure the original even when not following it", "Records how loud each line was without changing any audio."),
  ]), group("Delivery and takes", [
    T("dub.locale_direction", "Accent direction", "Blank derives it from the target locale. A per-line direction replaces only its own part of the instruction."),
    N("dub.candidate_limit", "Most alternative takes per line"),
    B("dub.clone_cleanup", "Clean the voice reference sample", "A restrained high-pass and denoise. The original sample is always kept, and turning this on builds a different voice profile."),
  ]), group("Clip boundaries", [
    B("boundaries.trim", "Trim generator padding", "Removes dead air outside the detected speech before timing correction. Internal pauses are never removed and the raw take is kept."),
    N("boundaries.handle_ms", "Protective margin (ms)", "Kept on each side of the detected speech."),
    N("boundaries.max_trim_seconds", "Most to remove per side (seconds)"),
    N("boundaries.min_trim_ms", "Smallest trim worth doing (ms)"),
    N("boundaries.threshold_db", "Speech threshold above noise (dB)"),
    N("boundaries.min_separation_db", "Least speech-to-noise separation (dB)", "Below this the boundary is not knowable and the clip is left alone."),
    N("boundaries.edge_fade_ms", "Edge fade (ms)", "0 disables it. Applied only where a clip would otherwise start or end on a click."),
    N("boundaries.edge_threshold_db", "Edge already smooth below (dB)"),
  ])]),
  tab("output", "Output", [group("Files", [
    T("dub.track_name_template", "New track name", "Use {language_name} for the language label."),
    B("dub.preserve_versions", "Keep completed dub versions", "Save independent copies so later generations cannot replace an earlier output."),
    T("dub.version_name", "Version name", "For example: LATAM · quiet storytelling. Completed outputs also receive a content ID."),
    T("paths.output_dir", "Output directory"),
    C("dub.output_codec", "Dubbed audio format", ["aac", "flac", "copy"]),
    T("dub.output_bitrate", "AAC bitrate"),
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
  ...["dub.target_locale", "dub.preset", "dub.voice_mode", "voicebox.default_engine", "dub.cast_group", "dub.character_map", "transcribe.whisper_model", "transcribe.diarize",
    "translate.locale", "translate.adaptation", "translate.direction", "translate.character_notes",
    "translate.adapt_region", "translate.reuse_memory",
    "dub.version_name", "dub.preserve_versions",
    "dub.duration_match", "dub.ducking_ratio", "dub.track_name_template", "dub.dry_run",
    // Per-title bypass for boundary preparation; the detector thresholds stay global.
    "boundaries.trim", "boundaries.edge_fade_ms"]
    .map(k => ({ ...FIELD_BY_KEY[k], t: isBoolField(FIELD_BY_KEY[k]) ? "bool" : FIELD_BY_KEY[k].t })),
];


// Fill language-driven choice fields from GET /api/languages. The hardcoded
// fallbacks stay intact when the catalog is unavailable or adds nothing new.
function applyLanguageOptions(langs) {
  if (!langs || !langs.length) return;
  const spanish = langs.filter(l => l.base === "es" && l.region && l.supported).map(l => l.id);
  const localeField = FIELD_BY_KEY["translate.locale"];
  if (spanish.length) localeField.o = ["auto", ...localeField.o.slice(1),
    ...spanish.filter(id => !localeField.o.includes(id))];
  const supported = langs.filter(l => l.supported).map(l => l.id);
  if (supported.length) FIELD_BY_KEY["dub.target_locale"].o = ["", ...supported];
}

export { TABS, FIELD_BY_KEY, PLAN_FIELDS, isBoolField, applyLanguageOptions };
