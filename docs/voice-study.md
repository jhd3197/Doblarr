# Voice study

A voice study answers one question: if Doblarr learns each character's voice
from film 1, how well does it recognise those characters in film 2?

It reads the voice analysis Doblarr already made of two films. It never
translates, clones or generates speech, and needs no models or audio of its own.

## How it works

1. **Profiles from film 1.** Each voice group you named in film 1 becomes a
   character profile: up to three duration-weighted prototypes (the same ones
   `voice_tags.prototypes` keeps for series memory), plus the lines and seconds
   used and how tightly the character's lines agree with each other (`spread`).
   Lines too short to embed, or in an unnamed group, are left out.
2. **Probe on film 2, held out.** Every named line of film 2 is compared with the
   profiles. Film 2 never teaches them: the profiles are built from film 1's
   vectors alone. Lines in groups you did not name are not scored.
3. **Score.** A line is answered only when its best match reaches the
   similarity floor (default 0.3) and leads the runner-up by the margin
   (default 0.05); otherwise it is **abstained**. The report gives top-1
   accuracy, precision and coverage, a confusion matrix (rows are the true
   character, columns the answer, plus an *abstained* column), and
   precision, recall, F1 and abstained share per character. Characters that
   only appear in film 2 are listed as `unseen`: no profile can recognise them.

Both films must have been analysed with the same voice model. Names are
per film, because the same group label (`SPEAKER_00`) means different people in
different films.

## Command line

```
doblarr voice-study run FILM1 FILM2 --names names.json [--floor 0.3] [--margin 0.05] [--json]
```

`FILM1` and `FILM2` are an analysed media file or its `.speakers.json`.
`names.json`:

```
{"film1": {"SPEAKER_00": "Kaito", "SPEAKER_01": "Mina"},
 "film2": {"SPEAKER_00": "Mina", "SPEAKER_02": "Kaito", "SPEAKER_03": "Ren"}}
```

## API

All routes need the API key like the rest of `/api`.

| Route | Does |
|---|---|
| `POST /api/voice-studies` | `{name, source: {path, names}, probes: [{path, names}], floor?, margin?}` builds film 1's profiles |
| `GET /api/voice-studies` | the studies with their headline numbers |
| `GET /api/voice-studies/{id}` | the study, its profiles and its results |
| `POST /api/voice-studies/{id}/probe` | `{path}` of one of the study's probes: runs it and stores the result |
| `GET /api/voice-studies/{id}/matrix?probe=PATH` | the confusion matrix and per-character table |
| `DELETE /api/voice-studies/{id}` | removes the study, its profiles and its results |

A `path` is an analysed media file, or a `.speakers.json` inside the work or
output folder. The probe runs inside the request: it is arithmetic over stored
vectors, not a job. The records are kinds `voice_study`, `voice_profile` and
`voice_probe_result`; results hold cue ids and answers, never vectors, and
profiles hold only prototype sums.

## Caveats

- **The result measures this pair of films**, not general accuracy. Different
  mixes, recasting, a child's voice grown up, shouting against calm, and music
  left after separation all lower scores.
- **Read abstention with precision.** A high precision with low coverage means
  most lines were not answered. Raise the floor to trade coverage for precision.
- **Names are your ground truth.** A wrong name in `names.json` shows up as a
  wrong answer. Nothing is merged between films automatically.
- **Embeddings are biometric.** A voice embedding identifies a real person. They
  stay on this machine (in the `.speakers.json` next to the analysis), are not
  put in dub recipes, and are not inferred to a real actor. To remove a study
  call `DELETE /api/voice-studies/{id}`: it deletes the profiles and results.
  The sidecars belong to the analysis and are removed with it.
