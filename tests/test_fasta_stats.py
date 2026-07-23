"""Phase 4 测试用例：验证 fasta_stats 工具端到端可用。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools" / "genomics"))

from fasta_stats import fasta_stats  # noqa: E402


def test_fasta_stats(tmp_path):
    fa = tmp_path / "demo.fasta"
    fa.write_text(">seq1\nACGTACGT\n>seq2\nGCGC\nACGT\n", encoding="utf-8")
    out = fasta_stats("统计这个文件", params={"path": str(fa)})
    assert out["status"] == "success", out["log"]
    r = out["result"]
    assert r["n_sequences"] == 2
    assert r["total_length"] == 16
    assert r["n50"] == 8
    assert 0.0 <= r["gc_content"] <= 1.0
    assert "result=" in out["log"]


def test_fasta_stats_missing():
    out = fasta_stats("统计不存在的文件", params={"path": "/no/such.fasta"})
    assert out["status"] == "error"
    assert "not found" in out["log"]
