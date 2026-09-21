# Resuming a first complete episode

`scripts/finish_episode.py` runs a subtitle-led dub using audio stems already
produced by Doblarr. It uses Prompture for translation and Voicebox for speech.
Run it from the repository root with the project virtual environment.

```powershell
.venv/Scripts/python scripts/finish_episode.py "work/input/episode.mkv" `
  --subs "work/subs/episode.en.ass" `
  --profile "YOUR_VOICEBOX_PROFILE_ID" --engine kokoro `
  --from en --to es --work-dir work/episode-es
```

The default translator is the local `ollama/qwen3:8b`. `--from` is the language
of the supplied subtitle script. For Kokoro, create a Spanish preset profile in
Voicebox (for example `em_alex`) and pass its ID. This is a single preset voice
preview, not a multi-character voice clone.

The runner expects `<input stem>.source.wav`, `.vocals.wav` and `.background.wav`
under `--stems-dir` (default `work`). Use stems from the exact episode cut. The
source audio determines the full output duration. The MKV keeps the original
video, audio, subtitles and attachments and appends `Spanish AI (preset preview)`.
Select that track in your player.

Add `--mp4 output/episode-es.mp4` for a portable copy that plays the dub by
default and includes optional translated captions. The original video is copied;
the dubbed audio is encoded to AAC. A Spanish SRT is also written next to the MKV.

For reviewed dialogue, `--script-edits work/episode-es/script-edits.json` accepts
an `exclude` list of subtitle indices and a `lines` object keyed by index, with
optional `text`, `start` and `end` values. This can remove title cards, merge
repeated cues or shorten translations. The original script cache stays intact;
the effective script is saved separately. Edited runs rebuild the mix and mux
but reuse unchanged speech clips.

Re-run the same command after an interruption. Completed translation batches
are saved atomically in the script JSON. Raw voice clips have request fingerprints
and checksums; changed text, language, profile or engine regenerates the affected
line. Clips are isolated by video path and target language. Old unverified clips
and script caches are not reused. Fitted clips are separate from raw generations.

Assembly keeps the original timeline from zero through the closing credits.
Large scripts are mixed in bounded groups to avoid Windows command-length limits.
Finished mixes and videos replace temporary files only after ffmpeg succeeds.
The original input cannot be overwritten by the mux stage.

Inspect `result.json`, the script and the render logs in the selected work directory.
Timing compression is capped at 1.3x; warnings flag lines that remain too long.
Listen to the result before considering it a finished performance. Translation,
speaker casting, music separation and pronunciation may still need refinement.

## Regenerating with a character cast

Use `--script <reviewed.script.json>` to keep existing reviewed translations and
`--cast-file <cast.json>` instead of `--profile` to assign character voices. Each
spoken cue must appear exactly once; missing or duplicate assignments fail before
speech generation. Existing per-line voice overrides are cleared in favor of the cast.

```json
{
  "speakers": {
    "NARRATOR": {"voice": "narrator-profile-id", "engine": "kokoro", "segments": [0, 1]},
    "CHARACTER": {"voice": "character-profile-id", "engine": "kokoro", "segments": [2, 3]}
  }
}
```

Choose a separate `--work-dir` and `--output-dir` to preserve the previous render.
Add `--normalize` to balance dialogue loudness across voices and
`--track-name "{language_name} AI (multi-voice)"` to identify the version in Plex.
The MKV dub is encoded to AAC while original audio streams and video are copied.
The result report records the cast and speech cache metrics. Character assignments
in this workflow are editorial decisions; they are not automatically verified diarization.

## Source-language checks

The normal extraction stage now explicitly selects the requested audio language
using stream tags, including `jpn`, `eng` and `spa`. It refuses ambiguous/missing
matches rather than silently choosing a different-language track. A single untagged
track is accepted with a warning. An extraction receipt records the selected
stream, and changing it invalidates the separated stems.

For voice cloning, the reference transcript must describe the actual reference
audio. An English audio sample paired with Japanese subtitle text is not a valid
reference, even when both came from the same episode.
