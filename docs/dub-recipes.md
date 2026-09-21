# Doblarr recipes, version 1

A `.dobdub` file is UTF-8 JSON, not an archive. The first implementation accepts
only `format: "doblarr-recipe"`, `schema_version: 1`, and `mode: "recipe-only"`.
Unknown versions and fields are rejected. The browser caps files at 128 KiB;
settings are capped at 32 KB, casts at 100 characters, notes at 2,000 characters.

## Workflow

1. Open a movie or a specific episode and select Recipes.
2. Save any changes to its plan and cast before exporting. Optionally enter a
   creator, revision, release notes and expected runtime in seconds.
3. Export the `.dobdub` file. Review names, notes and dictionaries before sharing.
4. The recipient opens the same movie or episode and imports the file. Movies
   match by TMDB ID when available, otherwise title. Episodes match TVDB series ID,
   season and episode number; Sonarr instance-specific episode IDs are not shared.
5. Preview the recipe and choose a local voice and speech engine for each role.
   A unique identical voice name is suggested, not treated as proof of identity.
   Missing names fall back to local source audio. Source casting requires a cloning
   engine; missing Kokoro/custom-Qwen presets default to Qwen in the UI. Delivery
   directions require Qwen. Voice and engine availability must be checked locally.
6. Apply. This atomically updates the title plan and cast, clears old dialogue
   edits and shared-cast mappings, preserves local dry-run/output settings, and
   queues nothing. Audition and review character mapping before generating.

## Contents and limits

- `media`: movie/episode identity and optional declared runtime. The current UI
  does not probe the recipient runtime; it shows an unverified-runtime notice.
- `source_language`, `target_language`: language metadata and generation target.
- `settings`: an explicit allowlist of timing, mix, quality, transcription behavior,
  glossary, pronunciations and seed. Export expands presets into concrete settings
  and uses `dub.preset: custom` to prevent local presets overriding the recipe.
- `narrator`, `characters`: friendly voice names, requested engines and delivery
  directions, plus character labels, categories and source speaker IDs.
- `creator`, `revision`, `notes`: user-entered attribution and release information.

No media, transcript, line edits, per-line timing, profile IDs, reference samples,
file paths, service URLs, API keys or model paths are exported. This version does
not provide a transcript-inclusive mode, hosted sharing, automatic voice downloads,
or a version library. Models and translation-provider configuration stay local.
Regenerating from a recipe does not guarantee identical translations, speaker
numbering or audio. Character roles describe the saved cast; they do not identify
characters across releases automatically.

## API

All endpoints use the application's existing API authentication.

- `GET /api/recipes/schema`: Pydantic JSON schema for the versioned envelope.
  Settings also undergo the allowlist/type/range validation in `doblarr/recipes.py`.
- `POST /api/recipes/export`: `{identity, parent?, media, source_language?,
  target_language?, creator?, revision?, notes?}` → `{recipe, warnings}`.
  `identity` is a local title lookup; optional `parent` provides inherited show settings.
- `POST /api/recipes/preview`: `{identity, media, recipe}` → normalized recipe,
  local voice choices and warnings. Read-only; no generation, profile import or writes.
- `POST /api/recipes/import`: same request plus `voices` and optional `engines`.
  Keys are `narrator` and `character:<speaker_id>`. Each voice selection must be an
  available local profile ID or an explicit empty string for source casting.
  Wrong media, missing selections and unavailable profiles are rejected before writes.
  The result contains the saved plan and character count.

The local `identity`, `parent`, selected profile IDs and engine overrides belong
to API requests only. They are not serialized into the shared recipe file.
