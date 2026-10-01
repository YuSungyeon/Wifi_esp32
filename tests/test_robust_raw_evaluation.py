import unittest

from model_train.robust.evaluate_raw import summarize, session_date, summarize_dates


class FrozenEvaluationAggregationTests(unittest.TestCase):
    def test_session_probability_mean_is_not_window_majority_or_window_weighted_accuracy(self):
        rows = [
            {"label": "static", "ensemble": {
                "prediction": "static", "windows": [
                    {"scores": [.51, .48, .01]}, {"scores": [.51, .48, .01]},
                    {"scores": [.01, .98, .01]}]}},
            {"label": "motion", "ensemble": {
                "prediction": "empty", "windows": [{"scores": [.8, .1, .1]}]}},
        ]
        result = summarize(rows, ["empty", "static", "motion"])
        self.assertEqual(result["window"]["accuracy"], .25)
        self.assertEqual(result["session"]["accuracy"], .5)
        self.assertEqual(result["first_valid_window"]["accuracy"], 0.)
        self.assertEqual(result["session"]["confusion"], [[0, 0, 0], [0, 1, 0], [1, 0, 0]])
        self.assertEqual(result["window"]["n"], 4)
        self.assertEqual(result["session"]["n"], 2)

    def test_inconsistent_saved_session_prediction_is_rejected(self):
        rows = [{"label": "empty", "ensemble": {
            "prediction": "motion", "windows": [{"scores": [.8, .1, .1]}]}}]
        with self.assertRaisesRegex(ValueError, "session prediction"):
            summarize(rows, ["empty", "static", "motion"])

    def test_row_dates_keep_same_session_number_on_different_days_distinct(self):
        audit = {"date": "20260917"}
        legacy = {"session_id": 31, "path": "raw/20260917/120000_empty_s31"}
        other = {"date": "20260916", "session_id": 31, "path": "raw/20260916/120000_empty_s31"}
        self.assertEqual(session_date(audit, legacy), "20260917")
        self.assertEqual(session_date(audit, other), "20260916")
        with self.assertRaisesRegex(ValueError, "directory"):
            session_date(audit, dict(other, date="20260920"))

    def test_date_metrics_keep_their_own_session_and_window_denominators(self):
        rows = [
            {"date": "20260916", "label": "empty", "ensemble": {
                "prediction": "empty", "windows": [{"scores": [.8, .1, .1]}]}},
            {"date": "20260917", "label": "static", "ensemble": {
                "prediction": "empty", "windows": [
                    {"scores": [.8, .1, .1]}, {"scores": [.8, .1, .1]}]}},
        ]
        per_date = summarize_dates(rows, ["empty", "static", "motion"])
        self.assertEqual(per_date["20260916"]["window"]["n"], 1)
        self.assertEqual(per_date["20260916"]["session"]["accuracy"], 1.)
        self.assertEqual(per_date["20260917"]["window"]["n"], 2)
        self.assertEqual(per_date["20260917"]["session"]["n"], 1)
        self.assertEqual(per_date["20260917"]["session"]["accuracy"], 0.)


if __name__ == "__main__":
    unittest.main()
