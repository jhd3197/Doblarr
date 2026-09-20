# Translation direction and saved dub versions

Settings → Translation controls the writing. The same settings can be overridden
for a title or episode and travel with exported recipes:

- **Spanish region:** `es-419` for neutral Latin America, `es-MX` for Mexico,
  `es-ES` for Spain, or `auto` to leave the regional choice unspecified.
- **Dialogue style:** `natural` for idiomatic dialogue, `faithful` for closer
  source wording and cultural references, or `localized` for adapted idioms.
- **Dialogue direction:** a short editorial brief, such as “restrained anime
  dialogue; preserve the mystery; avoid added catchphrases.”
- **Character dialogue notes:** register and personality keyed by speaker ID.
  For example, `{"GINKO": "Calm, concise, quietly curious"}`. These affect writing;
  character voice delivery is configured separately in the cast.
- **Names and terminology:** keep names and recurring concepts consistent.

Direction applies to initial translations, failed-batch retries, and timing
shortening. Changing translation settings invalidates the translation cache.
The region setting affects wording, not speech pronunciation. Keep the auditioned
voice profiles and their accent instructions to reproduce the desired voice style.

## Version identity

Settings → Output → **Keep completed dub versions** is enabled by default.
Give an experiment a **Version name**, for example “LATAM · quiet storytelling”.
Completed jobs show the name, short dub ID, and short script ID on the Dubs page.
Watch opens that job's saved copy, even after another run generates the episode.

- The **script ID** is SHA-256 over the translated lines, timing, speakers, and
  languages. Changing only the voice does not change this ID.
- The **dub ID** covers that script, source and output file hashes, generation
  settings, and voice assignments. Different rendered takes get different IDs.
- Full hashes and a UTF-8 script snapshot live in `version.json` alongside the
  output under `output/<media>/<language>/versions/<full-dub-id>/`.

Copies are independent files, not hardlinks to mutable working outputs. Each
copy costs roughly the size of the video; the working output is also retained.
Existing versions are never overwritten. An identical saved version is reused;
if it has been modified externally, saving fails rather than silently replacing it.
Renaming an otherwise identical run does not rename the saved version.

These IDs identify actual local results. They are not signatures, ownership
claims, or a promise that AI generation is deterministic. A fixed seed can help
where an engine supports it, but model/runtime changes may produce different audio.
Older jobs are not migrated automatically, and the standalone recovery script
does not use the main pipeline's version stage. Plex publishing remains separate.
