"""Resume one subtitle-led dub with existing stems and an explicit voice profile.

Run from the repository root with the project's Python environment.
All media and receipts stay in the ignored work/output directories.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from doblarr.clients.translator import PromptureTranslator  # noqa: E402
from doblarr.clients.voicebox import VoiceboxClient  # noqa: E402
from doblarr.models import DubJob, Speaker  # noqa: E402
from doblarr.stages import fit_timing, mix, mux, synthesize, transcribe  # noqa: E402
from doblarr.stages.common import save_script  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--subs", type=Path, required=True)
    parser.add_argument("--stems-dir", type=Path, default=Path("work"))
    parser.add_argument("--work-dir", type=Path, default=Path("work/episode-es"))
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--engine", default="kokoro")
    parser.add_argument("--from", dest="source", default="en",
                        help="Language of the supplied subtitle script")
    parser.add_argument("--to", dest="target", default="es")
    parser.add_argument("--model", default="ollama/qwen3:8b")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    work = args.work_dir
    work.mkdir(parents=True, exist_ok=True)
    job = DubJob(args.input.resolve(), args.source, args.target, args.subs.resolve())
    for attr, suffix in [("source_audio", "source"), ("vocals", "vocals"),
                         ("background", "background")]:
        path = (args.stems_dir / f"{args.input.stem}.{suffix}.wav").resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        setattr(job, attr, path)
    transcribe.run(job, work)
    job.speakers = {"NARRATOR": Speaker("NARRATOR", voicebox_profile_id=args.profile)}
    for seg in job.segments:
        seg.speaker = "NARRATOR"
    save_script(job, work)
    translator = PromptureTranslator(args.model)
    pending = [s for s in job.segments if not s.text_translated]
    for offset in range(0, len(pending), 12):
        batch = pending[offset:offset + 12]
        text = "\n".join(s.text_src for s in batch)
        result = translator.translate(text, job.source_lang, job.target_lang)
        lines = result.splitlines()
        if len(lines) != len(batch):
            raise ValueError("translation changed the number of subtitle lines")
        for seg, line in zip(batch, lines, strict=True):
            seg.text_translated = line
        save_script(job, work)
        logging.info("Translation checkpoint: %d/%d", sum(bool(s.text_translated)
                     for s in job.segments), len(job.segments))
    vb = VoiceboxClient("http://127.0.0.1:17493", timeout=240)
    synthesize.run(job, vb, work, voice_mode="preset", engine=args.engine)
    fit_timing.run(job, work)
    mix.run(job, work)
    mux.run(job, args.output_dir, track_name_template="{language_name} AI (preset preview)")
    report = {"input": str(job.input_file), "output": str(job.output_file.resolve()),
              "segments": len(job.segments), "engine": args.engine,
              "profile": args.profile, "script": str(save_script(job, work)),
              "limitations": ["Single preset voice; not an actor voice clone",
                              "Translation and performance require listening review"]}
    (work / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    logging.info("Finished: %s", job.output_file)


if __name__ == "__main__":
    main()
