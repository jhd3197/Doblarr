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
    N("quality.max_retries", "Quality retry attempts"),
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
  ...["dub.preset", "dub.voice_mode", "voicebox.default_engine", "dub.cast_group", "dub.character_map", "transcribe.whisper_model", "transcribe.diarize",
    "translate.locale", "translate.adaptation", "translate.direction", "translate.character_notes",
    "dub.version_name", "dub.preserve_versions",
    "dub.duration_match", "dub.ducking_ratio", "dub.track_name_template", "dub.dry_run"]
    .map(k => ({ ...FIELD_BY_KEY[k], t: isBoolField(FIELD_BY_KEY[k]) ? "bool" : FIELD_BY_KEY[k].t })),
];


export { TABS, FIELD_BY_KEY, PLAN_FIELDS, isBoolField };
