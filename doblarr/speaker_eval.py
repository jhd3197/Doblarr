"""How good is the voice grouping, measured against what a person decided.

One aggregate clustering score hides the errors that matter in a dub, so these
are reported separately:

- **B-cubed precision/recall/F1** of the groups against the reference characters;
- **incorrect merges**: groups holding a second character for at least 10% of
  their time (the expensive error: a mixed group must be pulled apart by hand);
- **missed brief characters**: characters with few lines that never get a group
  of their own (absorbed into someone else's);
- **naming precision**: if each group were named after its majority character,
  the share of lines that would be right;
- **recognition with abstention**: each line compared with prints built from the
  *other* lines (leave-one-out within the material, or other episodes when
  given); a suggestion is made only above a similarity floor and a margin, and
  precision and coverage are reported together.

A reference built from one episode measures that episode. It is not a general
accuracy claim, and callers say so.
"""

from __future__ import annotations

from collections import Counter


def bcubed(predicted: list[str], truth: list[str]) -> dict:
    n = len(predicted)
    if not n:
        return {"precision": None, "recall": None, "f1": None}
    precision = recall = 0.0
    for i in range(n):
        same_pred = [j for j in range(n) if predicted[j] == predicted[i]]
        same_true = [j for j in range(n) if truth[j] == truth[i]]
        both = sum(1 for j in same_pred if truth[j] == truth[i])
        precision += both / len(same_pred)
        recall += both / len(same_true)
    p, r = precision / n, recall / n
    return {"precision": round(p, 3), "recall": round(r, 3),
            "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0}


def merges(predicted: list[str], truth: list[str], seconds: list[float],
           share: float = 0.10) -> list[dict]:
    groups: dict[str, dict[str, float]] = {}
    for p, t, s in zip(predicted, truth, seconds, strict=True):
        bucket = groups.setdefault(p, {})
        bucket[t] = bucket.get(t, 0.0) + s
    out = []
    for label, counter in groups.items():
        total = sum(counter.values())
        main = max(counter, key=lambda name: counter[name])
        for other, other_s in counter.items():
            if other != main and total and other_s / total >= share:
                out.append({"group": label, "main": main, "also": other,
                            "share": round(other_s / total, 3)})
    return out


def missed_brief(predicted: list[str], truth: list[str], max_lines: int = 5) -> list[dict]:
    lines_of = Counter(truth)
    owner: dict[str, str] = {}
    groups: dict[str, Counter] = {}
    for p, t in zip(predicted, truth, strict=True):
        groups.setdefault(p, Counter())[t] += 1
    for label, counter in groups.items():
        owner[label] = counter.most_common(1)[0][0]
    out = []
    for character, n in lines_of.items():
        if n > max_lines:
            continue
        own = [label for label, main in owner.items() if main == character]
        if not own:
            out.append({"character": character, "lines": n})
    return out


def naming_precision(predicted: list[str], truth: list[str]) -> float | None:
    groups: dict[str, Counter] = {}
    for p, t in zip(predicted, truth, strict=True):
        groups.setdefault(p, Counter())[t] += 1
    named = {label: counter.most_common(1)[0][0] for label, counter in groups.items()}
    if not predicted:
        return None
    return round(sum(1 for p, t in zip(predicted, truth, strict=True) if named[p] == t)
                 / len(predicted), 3)


def recognition(vectors: list, truth: list[str], seconds: list[float], *,
                floor: float = 0.3, margin: float = 0.05, min_seconds: float = 0.8,
                use_prototypes: bool = False, held_vectors: list | None = None,
                held_truth: list[str] | None = None, detail: bool = False) -> dict:
    """Leave-one-out (or held-out) recognition with abstention.

    With `held_vectors`, prints come from `vectors`/`truth` and the held lines
    are recognised; otherwise each line is recognised against prints built from
    every other line. With `detail`, the result also lists each line's answer
    (`per_line`), which `study_summary` turns into a confusion matrix.
    """
    import numpy as np

    from . import voice_tags

    def prints_from(rows):
        by: dict[str, list] = {}
        for vector, name, sec in rows:
            by.setdefault(name, []).append({"vector": list(vector), "start": 0.0, "end": sec})
        out = {}
        for name, lines in by.items():
            protos = voice_tags.prototypes(lines) if use_prototypes else \
                voice_tags.prototypes(lines)[:1]
            mean = np.asarray(protos[0]["sum"], dtype=np.float64)
            norm = float(np.linalg.norm(mean))
            if not norm:
                continue
            extra = []
            for proto in protos[1:]:
                vec = np.asarray(proto["sum"], dtype=np.float64)
                if float(np.linalg.norm(vec)):
                    extra.append(vec / float(np.linalg.norm(vec)))
            out[name] = {"vector": mean / norm, "protos": extra}
        return out

    usable = [i for i, v in enumerate(vectors) if v is not None and seconds[i] >= min_seconds]
    if held_vectors is not None:
        known = prints_from([(vectors[i], truth[i], seconds[i]) for i in usable])
        tests = [(i, np.asarray(v), t, known) for i, (v, t) in
                 enumerate(zip(held_vectors, held_truth or [], strict=True)) if v is not None]
    else:
        tests = []
        for i in usable:
            rest = [(vectors[j], truth[j], seconds[j]) for j in usable if j != i]
            tests.append((i, np.asarray(vectors[i]), truth[i], prints_from(rest)))
    answered = correct = top1 = 0
    per_line = []
    for index, query, actual, known in tests:
        if not known:
            continue
        q = query / (float(np.linalg.norm(query)) or 1.0)
        ranked = sorted(((voice_tags.similarity(q, p, use_prototypes), name)
                         for name, p in known.items()), reverse=True)
        top1 += ranked[0][1] == actual
        gap = ranked[0][0] - ranked[1][0] if len(ranked) > 1 else 1.0
        ok = ranked[0][0] >= floor and gap >= margin
        if ok:
            answered += 1
            correct += ranked[0][1] == actual
        per_line.append({"index": index, "true": actual, "pred": ranked[0][1],
                         "score": round(ranked[0][0], 3), "margin": round(gap, 3),
                         "abstained": not ok})
    total = len(tests)
    out = {"lines": total, "top1": round(top1 / total, 3) if total else None,
           "answered": answered, "coverage": round(answered / total, 3) if total else None,
           "precision": round(correct / answered, 3) if answered else None,
           "floor": floor, "margin": margin, "prototypes": use_prototypes}
    if detail:
        out["per_line"] = per_line
    return out


ABSTAINED = "(abstained)"


def confusion_matrix(true: list[str], pred: list[str], labels: list[str] | None = None,
                     abstain_label: str = ABSTAINED) -> dict:
    """Rows are the true character, columns the predicted one plus a last
    column for abstentions (a `pred` equal to `abstain_label`)."""
    if labels is None:
        labels = sorted({*true, *(p for p in pred if p != abstain_label)})
    where = {name: i for i, name in enumerate(labels)}
    counts = [[0] * (len(labels) + 1) for _ in labels]
    for t, p in zip(true, pred, strict=True):
        if t in where:
            counts[where[t]][where.get(p, len(labels))] += 1
    return {"labels": labels, "abstained": abstain_label, "counts": counts}


def per_character_metrics(matrix: dict) -> dict:
    """Precision, recall, F1, support and abstained share for each row label.
    Precision counts every answer given as that character; a label never
    answered has no precision (None)."""
    labels, counts = matrix["labels"], matrix["counts"]
    out = {}
    for i, name in enumerate(labels):
        support = sum(counts[i])
        hit = counts[i][i]
        said = sum(row[i] for row in counts)
        p = round(hit / said, 3) if said else None
        r = round(hit / support, 3) if support else None
        f1 = None if p is None or r is None else \
            round(2 * p * r / (p + r), 3) if p + r else 0.0
        out[name] = {"precision": p, "recall": r, "f1": f1, "support": support,
                     "abstained": round(counts[i][-1] / support, 3) if support else None}
    return out


def study_summary(per_line: list[dict]) -> dict:
    """Headline numbers and the per-character table from `recognition(detail=True)`."""
    pred = [ABSTAINED if r["abstained"] else r["pred"] for r in per_line]
    matrix = confusion_matrix([r["true"] for r in per_line], pred)
    answered = [r for r in per_line if not r["abstained"]]
    n = len(per_line)
    return {"matrix": matrix, "per_character": per_character_metrics(matrix), "lines": n,
            "top1": round(sum(r["true"] == r["pred"] for r in per_line) / n, 3) if n else None,
            "precision": round(sum(r["true"] == r["pred"] for r in answered) / len(answered), 3)
            if answered else None,
            "coverage": round(len(answered) / n, 3) if n else None}


def evaluate(predicted: list[str], truth: list[str], seconds: list[float]) -> dict:
    return {"bcubed": bcubed(predicted, truth), "merges": merges(predicted, truth, seconds),
            "missed_brief": missed_brief(predicted, truth),
            "naming_precision": naming_precision(predicted, truth),
            "groups": len(set(predicted)), "characters": len(set(truth))}
