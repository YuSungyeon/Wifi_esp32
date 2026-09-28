import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from test_lstm import build_dataset, torch

if torch is not None:
    from model_train.cnn1d import CNN1D as cnn


@unittest.skipIf(torch is None, "PyTorch가 설치되지 않음")
class CNN1DTest(unittest.TestCase):
    def test_temporal_channels_output_and_gradients(self):
        torch.manual_seed(0)
        model = cnn.CNN1DClassifier()
        x = torch.randn(2, 300, 192, requires_grad=True)
        observed = []
        handle = model.features[0].register_forward_pre_hook(
            lambda module, inputs: observed.append(tuple(inputs[0].shape))
        )
        output = model(x)
        handle.remove()
        self.assertEqual(observed, [(2, 192, 300)])
        self.assertEqual(tuple(output.shape), (2, 3))
        self.assertEqual(sum(p.numel() for p in model.parameters()), 53763)
        torch.nn.functional.cross_entropy(output, torch.tensor([0, 2])).backward()
        self.assertTrue(torch.isfinite(x.grad).all())
        for parameter in model.parameters():
            self.assertIsNotNone(parameter.grad)
            self.assertTrue(torch.isfinite(parameter.grad).all())
        self.assertGreater(float(model.features[0].weight.grad.abs().sum()), 0)
        model.eval()
        with torch.no_grad():
            self.assertEqual(tuple(model(x[:1]).shape), (1, 3))

    def test_invalid_architecture_and_input(self):
        for kwargs in (
            {"channels": (32, 64)}, {"channels": (32, 0, 64)},
            {"kernel_sizes": (4, 5, 3)}, {"kernel_sizes": (5, -1, 3)},
            {"dropout": 1.0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                cnn.CNN1DClassifier(**kwargs)
        model = cnn.CNN1DClassifier()
        with self.assertRaises(ValueError):
            model(torch.zeros(2, 192, 300))

    def test_train_restore_evaluate_and_model_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            dataset_dir = build_dataset(base)
            run_dir = base / "cnn-run"
            args = cnn.build_parser().parse_args([
                "train", "--dataset-dir", str(dataset_dir),
                "--run-dir", str(run_dir), "--device", "cpu",
                "--channels", "4", "8", "8", "--kernel-sizes", "3", "3", "3",
                "--epochs", "1", "--batch-size", "2", "--class-weight", "balanced",
            ])
            with mock.patch.object(cnn.training, "make_dataloader",
                                   wraps=cnn.training.make_dataloader) as loader:
                cnn.run_train(args)
            self.assertEqual([call.args[1] for call in loader.call_args_list],
                             ["train", "validation"])
            self.assertFalse((run_dir / "test-metrics.json").exists())
            for name in ("config.json", "dataset-manifest.json", "normalization.npz",
                         "best-model.pt", "history.jsonl", "validation-metrics.json",
                         "run-summary.json"):
                self.assertTrue((run_dir / name).is_file(), name)
            config = json.loads((run_dir / "config.json").read_text())
            self.assertEqual(config["model_type"], "cnn1d")
            self.assertEqual(config["model"]["channels"], [4, 8, 8])
            self.assertNotIn("hidden_size", config["training"])
            checkpoint = cnn.training._load_checkpoint(
                run_dir / "best-model.pt", torch.device("cpu"))
            model = cnn.CNN1DClassifier(**checkpoint["model_config"])
            model.load_state_dict(checkpoint["model_state_dict"])
            model.eval()
            self.assertEqual(config["parameter_count"], sum(p.numel() for p in model.parameters()))
            raw = np.load(dataset_dir / "test" / "X.npy")
            normalization = checkpoint["normalization"]
            x = ((raw - np.asarray(normalization["mean"], dtype=np.float32)) /
                 np.asarray(normalization["std_safe"], dtype=np.float32))
            with torch.no_grad():
                expected = model(torch.from_numpy(x)).softmax(dim=1).numpy()

            test_args = cnn.build_parser().parse_args([
                "test", "--dataset-dir", str(dataset_dir), "--run-dir", str(run_dir),
                "--device", "cpu", "--num-workers", "0",
            ])
            with self.assertRaisesRegex(ValueError, "model_type"):
                cnn.training.run_test(test_args)
            result = cnn.run_test(test_args)
            self.assertEqual(result["window_level"]["sample_count"], 6)
            self.assertEqual(result["session_level"]["metrics"]["sample_count"], 6)
            predictions = [json.loads(line) for line in
                           (run_dir / "test-predictions.jsonl").read_text().splitlines()]
            actual = [[row["probabilities"][label] for label in ("empty", "static", "motion")]
                      for row in predictions]
            np.testing.assert_allclose(actual, expected, atol=1e-6)
            self.assertTrue((run_dir / "confusion-matrix.png").is_file())
            with self.assertRaises(FileExistsError):
                cnn.run_test(test_args)


if __name__ == "__main__":
    unittest.main()
