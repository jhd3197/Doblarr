# Command line

Everything the web UI does to a dub can be done from a terminal. Commands that
change a dub talk to the running server (`doblarr serve`), so a job queued here
is the same job the UI shows. The commands on this page take `--json` for
scripts, except `research`, which prints the cited answer.

## From nothing to a finished dub

```
doblarr make "Harbor Lights - 01.mkv" --to es-419
doblarr make "Harbor Lights/Season 01" --to es-419 --keep-going
```

`make` runs these steps in order, skipping any that has nothing to do:

| Step | What it does | On its own |
|---|---|---|
| doctor | checks the server, the speech service, the translator, ffmpeg, GPU memory and disk | `doblarr doctor` |
| analyze | separates the dialogue, transcribes, groups voices, finds and verifies dub tracks in the file | `doblarr analyze FILE` |
| lookup | catalogue ids from Wikidata, the published cast from the anime catalogues Wikidata names, characters | `doblarr lookup SERIES` |
| voices | lists voice groups and which are unnamed | `doblarr voices FILE` |
| dub | translates (along the file's own dub when it has one), voices, checks, fits, mixes | `doblarr queue FILE --to es-419 --wait` |
| fix | re-voices lines whose words were misheard, whose voice drifted, or that run far past their slot | `doblarr fix JOB` |
| report | PASS/WARN/FAIL on timing, casting and speech | `doblarr report JOB` |

Leave steps out with `--skip lookup,fix`, run some with `--only report,fix`, and
set a value for these dubs only with `--set quality.max_retries=3`. A doctor
FAIL stops the run unless you pass `--anyway`. Running `make` again does only
the missing work.

## Working on one dub

```
doblarr voices FILE                         # groups, names, sample lines
doblarr voices FILE --name SPEAKER_03=Kaito --move 12,14=Mina
doblarr line JOB 151                        # source, text, what was heard, voice, findings
doblarr line JOB 151 --text "Hola, Mina."   # new words, re-voiced
doblarr line JOB 151 --retake --wait        # same words, a new take
doblarr line JOB 151 --clip line151.wav     # save the take
doblarr dubref FILE --compare JOB --lines   # our lines next to the file's own dub
doblarr fix JOB --dry-run                   # which lines fix would re-voice
```

Each edit queues a re-render that regenerates only the changed lines.

## Research and terms

```
doblarr lookup show:tvdb:123                # ids, cast links, characters
doblarr terms show:tvdb:123                 # the show's names and terms
doblarr terms show:tvdb:123 add Dora=Tora --locale es-419
doblarr terms show:tvdb:123 approve 2b8e2427
doblarr research show:tvdb:123 "How does the Latin American dub say the guild's name?"
```

`analyze` prints the series id of a file.

## Voice study

```
doblarr voice-study run "Harbor Lights.speakers.json" "Harbor Lights 2.speakers.json" --names names.json
doblarr voice-study run FILM1.mkv FILM2.mkv --names names.json --floor 0.35 --json
```

Builds a voice profile per character from film 1 and recognises film 2's lines
against them, held out: film 2 never teaches the profiles. Each film is an
analysed media file or its `.speakers.json`, and both must have been analysed
with the same voice model. `names.json` says which character each voice group is:

```
{"film1": {"SPEAKER_00": "Kaito", "SPEAKER_01": "Mina"},
 "film2": {"SPEAKER_00": "Mina", "SPEAKER_02": "Kaito"}}
```

Film 2 lines in a group without a name are not scored. The output is top-1,
precision and coverage at the floor and margin, a confusion matrix with a
column for abstentions, and precision, recall and abstained share per character;
`--json` adds every line's answer. It works offline and never translates,
clones or speaks. See [voice-study.md](voice-study.md).

## Settings

```
doblarr settings                            # the sections
doblarr settings get translate              # a section
doblarr settings get translate.published_dub   # a value, its default and choices
doblarr settings set translate.provider=prompture translate.model=ollama/gemma3:12b
```

A key or value the settings schema does not know is refused, with suggestions,
before anything is saved.
