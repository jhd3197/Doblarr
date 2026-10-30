"""The confusion matrix and per-character table built on recognition()'s per-line answers."""

import numpy as np

from doblarr import speaker_eval


def _two_voices(rng, n=8):
    centres = {"Kaito": np.array([1.0, 0, 0]), "Mina": np.array([0, 1.0, 0])}
    vectors, truth = [], []
    for name, centre in centres.items():
        for _ in range(n):
            v = centre + rng.normal(0, 0.1, 3)
            vectors.append(v / np.linalg.norm(v))
            truth.append(name)
    return vectors, truth


def test_recognition_detail_is_extra_and_feeds_the_matrix():
    rng = np.random.default_rng(1)
    vectors, truth = _two_voices(rng)
    held, held_truth = _two_voices(rng, 4)
    args = dict(held_vectors=held, held_truth=held_truth)
    assert "per_line" not in speaker_eval.recognition(vectors, truth, [2.0] * 16, **args)
    found = speaker_eval.recognition(vectors, truth, [2.0] * 16, detail=True, **args)
    assert len(found["per_line"]) == 8 and found["per_line"][0]["true"] == "Kaito"
    study = speaker_eval.study_summary(found["per_line"])
    assert study["matrix"]["counts"] == [[4, 0, 0], [0, 4, 0]]
    assert study["top1"] == 1.0 and study["coverage"] == 1.0


def test_confusion_matrix_has_an_abstained_column_and_per_character_metrics():
    gone = speaker_eval.ABSTAINED
    true = ["Kaito", "Kaito", "Kaito", "Mina", "Mina", "Ren"]
    pred = ["Kaito", "Mina", gone, "Mina", "Mina", gone]
    m = speaker_eval.confusion_matrix(true, pred)
    assert m["labels"] == ["Kaito", "Mina", "Ren"]
    assert m["counts"] == [[1, 1, 0, 1], [0, 2, 0, 0], [0, 0, 0, 1]]
    metrics = speaker_eval.per_character_metrics(m)
    assert metrics["Kaito"] == {"precision": 1.0, "recall": 0.333, "f1": 0.5, "support": 3,
                                "abstained": 0.333}
    assert metrics["Mina"]["precision"] == 0.667 and metrics["Mina"]["recall"] == 1.0
    assert metrics["Ren"]["precision"] is None and metrics["Ren"]["abstained"] == 1.0
