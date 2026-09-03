import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LSTM_DIR = PROJECT_ROOT / "model_train" / "lstm"
sys.path.insert(0, str(LSTM_DIR))

try:
    import torch
except ModuleNotFoundError:  # dependency가 없는 환경에서도 다른 테스트는 실행한다.
    torch = None

if torch is not None:
    import LSTM as lm
else:
    lm = None


def build_dataset(base: Path) -> Path:
    dataset_dir = base / "dataset"
    dataset_dir.mkdir()
    label_map = {"empty": 0, "static": 1, "motion": 2}
    rx_order = [101, 102, 103]
    split_sessions = {
        "train": [1, 2, 11, 12, 21, 23],
        "validation": [7, 8, 17, 18, 27, 28],
        "test": [9, 10, 19, 20, 29, 30],
    }
    split_labels = np.asarray([0, 0, 1, 1, 2, 2], dtype=np.int64)
    rng = np.random.default_rng(1234)
    arrays = {}
    split_summary = {}

    for split, sessions in split_sessions.items():
        split_dir = dataset_dir / split
        split_dir.mkdir()
        x = np.empty((6, 300, 192), dtype=np.float32)
        for index, label in enumerate(split_labels):
            # class마다 중심이 다르고 모든 feature의 표준편차는 0보다 크게 만든다.
            x[index] = rng.normal(
                loc=float(label) * 2.0,
                scale=0.5,
                size=(300, 192),
            ).astype(np.float32)
        np.save(split_dir / "X.npy", x)
        np.save(split_dir / "y.npy", split_labels)
        with (split_dir / "windows.jsonl").open("w", encoding="utf-8") as fp:
            for index, (session_id, label_id) in enumerate(
                zip(sessions, split_labels)
            ):
                label = ["empty", "static", "motion"][int(label_id)]
                fp.write(
                    json.dumps(
                        {
                            "index": index,
                            "session_id": session_id,
                            "label": label,
                            "label_id": int(label_id),
                            "window_start_tx_seq": 1000 + index * 30,
                            "rx_order": rx_order,
                            "observed_ratio": {
                                "101": 1.0,
                                "102": 1.0,
                                "103": 1.0,
                            },
                        }
                    )
                    + "\n"
                )
        arrays[split] = x
        split_summary[split] = {
            "sessions": sessions,
            "used_sessions": sessions,
            "missing_sessions": [],
            "window_count": 6,
            "class_window_counts": {"empty": 2, "static": 2, "motion": 2},
        }

    mean = arrays["train"].mean(axis=(0, 1), dtype=np.float64)
    std = arrays["train"].std(axis=(0, 1), dtype=np.float64)
    std_safe = np.where(std < 1e-6, 1.0, std)
    np.savez(
        dataset_dir / "normalization.npz",
        mean=mean,
        std=std,
        std_safe=std_safe,
        zero_std_epsilon=1e-6,
        zero_std_replacement=1.0,
        train_frame_count=1800,
    )

    manifest = {
        "generated_by": "test fixture",
        "design_doc": "model_train/docs/[전처리]-설계.md",
        "raw_dir": "fixture",
        "dry_run": False,
        "config": {
            "rx_order": rx_order,
            "features_per_rx": 64,
            "window": 300,
            "stride": 30,
            "min_common_length": 300,
            "min_observed_ratio": 0.85,
            "max_interp_gap": 5,
            "reboot_small_seq": 10,
            "reboot_min_drop": 100,
            "zero_std_epsilon": 1e-6,
        },
        "label_map": label_map,
        "rx_order": rx_order,
        "splits": split_sessions,
        "unassigned_sessions": [],
        "split_summary": split_summary,
        "normalization": {
            "file": "normalization.npz",
            "computed_from": "train",
            "train_frame_count": 1800,
            "zero_std_epsilon": 1e-6,
            "zero_std_replacement": 1.0,
            "zero_std_feature_count": 0,
            "mean": mean.tolist(),
            "std": std.tolist(),
        },
        "note_normalization": "fixture X는 raw amplitude",
        "sessions": [],
    }
    with (dataset_dir / "manifest.json").open("w", encoding="utf-8") as fp:
        json.dump(manifest, fp)
    return dataset_dir


@unittest.skipIf(torch is None, "PyTorch가 설치되지 않음")
class DatasetAndModelTest(unittest.TestCase):
    def test_contract_and_memmap_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            dataset_dir = build_dataset(Path(directory))
            contract = lm.validate_dataset_contract(dataset_dir)
            self.assertEqual(contract.split_counts["train"], 6)
            dataset = lm.CSIMemmapDataset(
                dataset_dir,
                "train",
                contract.mean,
                contract.std_safe,
                6,
            )
            sample, label, index = dataset[0]
            raw = np.load(dataset_dir / "train" / "X.npy", mmap_mode="r")[0]
            expected = (raw - contract.mean) / contract.std_safe
            self.assertEqual(tuple(sample.shape), (300, 192))
            self.assertEqual(sample.dtype, torch.float32)
            self.assertEqual(label.dtype, torch.long)
            self.assertEqual(int(label), 0)
            self.assertEqual(index, 0)
            np.testing.assert_allclose(sample.numpy(), expected, rtol=1e-5, atol=1e-5)

    def test_model_output_shape(self):
        model = lm.LSTMClassifier(hidden_size=8, num_layers=1, dropout=0.0)
        output = model(torch.zeros(2, 300, 192))
        self.assertEqual(tuple(output.shape), (2, 3))

    def test_metrics_are_macro_averaged(self):
        matrix = np.asarray([[2, 0, 0], [0, 1, 1], [0, 0, 2]])
        metrics = lm.metrics_from_confusion(matrix, ["empty", "static", "motion"])
        self.assertAlmostEqual(metrics["accuracy"], 5 / 6)
        self.assertAlmostEqual(metrics["per_class"]["static"]["recall"], 0.5)
        self.assertEqual(metrics["sample_count"], 6)

    def test_train_validation_checkpoint_and_final_test(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            dataset_dir = build_dataset(base)
            run_dir = base / "run"
            train_args = argparse.Namespace(
                dataset_dir=dataset_dir,
                skip_full_scan=False,
                run_dir=run_dir,
                output_root=base / "runs",
                seed=0,
                class_weight="none",
                batch_size=2,
                hidden_size=4,
                num_layers=1,
                dropout=0.0,
                learning_rate=1e-3,
                epochs=1,
                patience=1,
                min_delta=0.0,
                num_workers=0,
                device="cpu",
            )
            self.assertEqual(lm.run_train(train_args), run_dir.resolve())
            for name in (
                "config.json",
                "dataset-manifest.json",
                "normalization.npz",
                "best-model.pt",
                "history.jsonl",
                "validation-metrics.json",
                "run-summary.json",
            ):
                self.assertTrue((run_dir / name).is_file(), name)

            test_args = argparse.Namespace(
                dataset_dir=dataset_dir,
                skip_full_scan=False,
                run_dir=run_dir,
                num_workers=0,
                device="cpu",
            )
            result = lm.run_test(test_args)
            self.assertEqual(result["window_level"]["sample_count"], 6)
            self.assertEqual(result["session_level"]["metrics"]["sample_count"], 6)
            for name in (
                "test-metrics.json",
                "test-predictions.jsonl",
                "confusion-matrix.png",
            ):
                self.assertTrue((run_dir / name).is_file(), name)
            with self.assertRaises(FileExistsError):
                lm.run_test(test_args)


if __name__ == "__main__":
    unittest.main()
