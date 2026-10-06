"""Unit tests for Stage 16: Evaluation Benchmark Harness."""

from embedforge.benchmark import BENCHMARK_SUITE, BenchmarkScorecard

def test_benchmark_suite_definitions():
    assert len(BENCHMARK_SUITE) >= 4
    scenarios = {s.id for s in BENCHMARK_SUITE}
    assert "BENCH-01" in scenarios
    assert "BENCH-02" in scenarios

def test_benchmark_scorecard_run():
    scorecard = BenchmarkScorecard()
    metrics = scorecard.run_all()

    assert metrics["detection_rate_pct"] == 100.0
    assert metrics["repair_rate_pct"] == 100.0
    assert metrics["false_positive_rate_pct"] == 0.0
    assert len(metrics["scenario_results"]) == len(BENCHMARK_SUITE)
